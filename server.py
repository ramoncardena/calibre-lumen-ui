#!/usr/bin/env python3
"""
Lumen — a modern web UI for the Calibre Content Server.

Zero dependencies (Python 3.8+ stdlib only). Serves the single-page UI
(index.html) and proxies the Calibre Content Server API:

  GET  /ajax/*   read API (search, metadata, categories)
  GET  /get/*    covers, thumbnails, format downloads
  POST /cdb/*    write API (edit metadata)

It also provides a small metadata-fetching service so the UI can fill
metadata from internet sources (Google Books and Open Library):

  GET /meta/search?title=&author=&isbn=   normalized candidate list
  GET /meta/cover?url=                    cover image proxy (allowlisted hosts)

Optionally, with --boox-dav, it exposes a tiny WebDAV receiver for Boox
NeoReader sync:

  /dav/                                   save/read uploaded annotation files
  GET /dav-inbox                          list received .txt/.html files

Usage:
    python3 server.py --calibre http://127.0.0.1:8081
    python3 server.py --calibre http://127.0.0.1:8081 --port 8090
    python3 server.py --calibre http://127.0.0.1:8081 --user X --password Y
"""

import argparse
import base64
import email.utils
import hmac
import io
import json
import mimetypes
import os
import posixpath
import sys
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"

CALIBRE_URL = ""
OPENER = urllib.request.build_opener()
DAV_ROOT = None
DAV_USER = ""
DAV_PASSWORD = ""

PROXY_PREFIXES = ("/ajax/", "/get/")  # GET; /cdb/ is POST-only

# Hosts the cover proxy is allowed to fetch from (avoids being an open proxy).
COVER_HOSTS = {
    "books.google.com",
    "books.googleusercontent.com",
    "covers.openlibrary.org",
}


GOOGLE_KEY = ""
_META_CACHE = {}            # url -> (expires_at_monotonic, data)
_META_CACHE_TTL = 6 * 3600
_META_CACHE_MAX = 200


def http_get_json(url, timeout=12, retries=1):
    """GET JSON with a small cache and one retry on 429/5xx (Google Books
    rate-limits unauthenticated clients aggressively)."""
    now = time.monotonic()
    hit = _META_CACHE.get(url)
    if hit and hit[0] > now:
        return hit[1]
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "lumen/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            if len(_META_CACHE) >= _META_CACHE_MAX:
                _META_CACHE.clear()
            _META_CACHE[url] = (now + _META_CACHE_TTL, data)
            return data
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise last


# --------------------------------------------------------------------------
# Metadata sources
# --------------------------------------------------------------------------

def search_google_books(title, author, isbn):
    if isbn:
        q = "isbn:" + isbn
    else:
        parts = []
        if title:
            parts.append('intitle:"%s"' % title)
        if author:
            parts.append('inauthor:"%s"' % author)
        q = " ".join(parts)
    if not q:
        return []
    url = ("https://www.googleapis.com/books/v1/volumes?maxResults=10&q=" +
           urllib.parse.quote(q))
    if GOOGLE_KEY:
        url += "&key=" + urllib.parse.quote(GOOGLE_KEY)
    try:
        data = http_get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError(
                "Google Books rate-limited this IP (429). Results below are from "
                "Open Library only; retry in a minute or run server.py with --google-key.")
        raise
    out = []
    for item in data.get("items", []) or []:
        v = item.get("volumeInfo", {}) or {}
        idents = {}
        for ii in v.get("industryIdentifiers", []) or []:
            t = (ii.get("type") or "").lower()
            if t in ("isbn_13", "isbn_10") and "isbn" not in idents:
                idents["isbn"] = ii.get("identifier", "")
        if item.get("id"):
            idents["google"] = item["id"]
        cover = (v.get("imageLinks", {}) or {}).get("thumbnail", "")
        if cover.startswith("http://"):
            cover = "https://" + cover[len("http://"):]
        out.append({
            "source": "Google Books",
            "title": v.get("title", ""),
            "authors": v.get("authors", []) or [],
            "publisher": v.get("publisher", ""),
            "pubdate": v.get("publishedDate", ""),
            "tags": (v.get("categories", []) or [])[:6],
            "comments": v.get("description", ""),
            "identifiers": idents,
            "cover_url": cover,
        })
    return out


def search_open_library(title, author, isbn):
    params = {"limit": "10",
              "fields": "title,author_name,first_publish_year,publisher,isbn,subject,cover_i"}
    if isbn:
        params["q"] = "isbn:" + isbn
    else:
        if title:
            params["title"] = title
        if author:
            params["author"] = author
        if not title and not author:
            return []
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
    data = http_get_json(url)
    if not (data.get("docs") or []) and not isbn and (title or author):
        loose = {"limit": "10", "fields": params["fields"],
                 "q": " ".join(x for x in (title, author) if x)}
        data = http_get_json("https://openlibrary.org/search.json?" +
                             urllib.parse.urlencode(loose))
    out = []
    for d in data.get("docs", []) or []:
        idents = {}
        isbns = d.get("isbn") or []
        if isbns:
            isbns13 = [x for x in isbns if len(x) == 13]
            idents["isbn"] = (isbns13 or isbns)[0]
        cover = ""
        if d.get("cover_i"):
            cover = f"https://covers.openlibrary.org/b/id/{d['cover_i']}-L.jpg"
        out.append({
            "source": "Open Library",
            "title": d.get("title", ""),
            "authors": d.get("author_name", []) or [],
            "publisher": (d.get("publisher") or [""])[0],
            "pubdate": str(d.get("first_publish_year") or ""),
            "tags": (d.get("subject") or [])[:6],
            "comments": "",
            "identifiers": idents,
            "cover_url": cover,
        })
    return out


def meta_search(query):
    title = (query.get("title") or [""])[0].strip()
    author = (query.get("author") or [""])[0].strip()
    isbn = (query.get("isbn") or [""])[0].strip().replace("-", "")
    results, errors = [], []
    for fn in (search_google_books, search_open_library):
        try:
            results.extend(fn(title, author, isbn))
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
    return {"results": results, "errors": errors}


# --------------------------------------------------------------------------
# AI service (optional): similar books, summaries, book questions
# --------------------------------------------------------------------------

AI_PROVIDER = ""
AI_KEY = ""
AI_MODEL = ""

AI_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-6",
    "xai": "grok-4",
}
AI_ENDPOINTS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "xai": "https://api.x.ai/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
}

AI_BOOK_CHARS = 250_000          # max characters of book text sent to the model
_BOOK_TEXT_CACHE = {}            # (book_id, lib, fmt) -> (text, truncated)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "div", "br", "h1", "h2", "h3", "h4", "li", "tr"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def extract_epub_text(raw, max_chars):
    """Extract readable text from an EPUB, following spine order when possible."""
    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = []
    try:  # locate the OPF and read its spine for proper chapter ordering
        c = ET.fromstring(zf.read("META-INF/container.xml"))
        opf_path = c.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile").get("full-path")
        opf = ET.fromstring(zf.read(opf_path))
        ns = {"o": "http://www.idpf.org/2007/opf"}
        manifest = {i.get("id"): i.get("href") for i in opf.findall(".//o:manifest/o:item", ns)}
        base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
        names = [base + manifest[r.get("idref")] for r in opf.findall(".//o:spine/o:itemref", ns)
                 if r.get("idref") in manifest]
    except Exception:
        pass
    if not names:  # fallback: any html-ish member in archive order
        names = [n for n in zf.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
    existing = set(zf.namelist())
    parts, total, truncated = [], 0, False
    for n in names:
        if n not in existing:
            continue
        p = _TextExtractor()
        try:
            p.feed(zf.read(n).decode("utf-8", "replace"))
        except Exception:
            continue
        t = "\n".join(s for s in ("".join(p.out)).split("\n") if s.strip())
        if not t:
            continue
        parts.append(t)
        total += len(t)
        if total >= max_chars:
            truncated = True
            break
    text = "\n\n".join(parts)
    return text[:max_chars], truncated or len(text) > max_chars


def fetch_book_text(book_id, library_id, formats, max_chars):
    fmts = [f.upper() for f in (formats or [])]
    fmt = "EPUB" if "EPUB" in fmts else ("TXT" if "TXT" in fmts else None)
    if not fmt:
        raise RuntimeError("Sending book text needs an EPUB or TXT format for this book.")
    key = (str(book_id), library_id, fmt, max_chars)
    if key in _BOOK_TEXT_CACHE:
        return _BOOK_TEXT_CACHE[key]
    url = f"{CALIBRE_URL}/get/{fmt}/{book_id}/{urllib.parse.quote(library_id)}"
    with OPENER.open(url, timeout=60) as r:
        raw = r.read()
    if fmt == "TXT":
        full = raw.decode("utf-8", "replace")
        ans = (full[:max_chars], len(full) > max_chars)
    else:
        ans = extract_epub_text(raw, max_chars)
    if len(_BOOK_TEXT_CACHE) >= 3:   # keep memory bounded: a few books at a time
        _BOOK_TEXT_CACHE.clear()
    _BOOK_TEXT_CACHE[key] = ans
    return ans


AI_SYSTEM = ("You are a knowledgeable, well-read librarian assisting a reader of a "
             "personal ebook library. Be concise, concrete, and honest: if you do not "
             "know the specific book, say so instead of inventing details.")


def ai_build_user_prompt(action, book, question):
    lines = ["Book:"]
    for label, key in (("Title", "title"), ("Authors", "authors"), ("Series", "series"),
                       ("Tags", "tags"), ("Publisher", "publisher"), ("Published", "pubdate")):
        v = book.get(key)
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        if v:
            lines.append(f"{label}: {v}")
    desc = (book.get("description") or "").strip()
    if desc:
        lines.append("Publisher description: " + desc[:1500])
    info = "\n".join(lines)
    body_text = book.get("_text")
    if body_text:
        note = (" (TRUNCATED — the ending is missing, say so if the question concerns it)"
                if book.get("_truncated") else "")
        info += ("\n\nFULL TEXT OF THE BOOK" + note + ":\n<book>\n" + body_text +
                 "\n</book>\nBase your answer primarily on this text.")

    if action == "similar":
        task = ("Recommend 6 to 8 books similar to this one. For each, give exactly one "
                "line in the exact format 'Title — Author: short reason it fits' (em dash "
                "between title and author, colon after the author, no numbering, no bold). "
                "Prefer variety over multiple books by the same author, and do not "
                "recommend this book itself or other entries of the same series.")
    elif action == "summary":
        task = ("Give a spoiler-light overview of this book in two short paragraphs "
                "(premise and what makes it notable), then list its three main themes. "
                "If you do not know this specific book, say so and base the overview "
                "only on the description above.")
    elif action == "classify":
        task = ("Classify this book for a personal library. Return ONLY a JSON object "
                "with two keys: \"genre\" and \"tags\", each a comma-separated string.\n"
                "- genre: 1 to 3 BROAD categories (e.g. Fiction, Science Fiction, "
                "History, Biography, Fantasy, Mystery).\n"
                "- tags: 4 to 8 SPECIFIC descriptors (themes, settings, subgenres, "
                "e.g. Space Opera, Time Travel, Ancient Rome, Coming Of Age).\n"
                "Rules: Title Case every word. No term may appear in both lists. "
                "Tags must be more specific than genres, not synonyms of them. "
                "No commentary, no markdown, no code fences. Example: "
                "{\"genre\": \"Science Fiction, Fiction\", "
                "\"tags\": \"First Contact, Space Exploration, Artificial Intelligence\"}")
    else:  # question
        task = ("Answer the reader's question about this specific book. If the answer "
                "requires plot spoilers, start the line with 'Spoilers:' so the reader "
                "can stop. Question: " + (question or "").strip()[:500])
    return info + "\n\n" + task


def ai_call(provider, key, model, user_prompt):
    body_headers = {"Content-Type": "application/json"}
    if provider == "anthropic":
        body_headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        payload = {"model": model, "max_tokens": 1024, "system": AI_SYSTEM,
                   "messages": [{"role": "user", "content": user_prompt}]}
    else:  # openai / xai share the chat-completions shape
        body_headers["Authorization"] = "Bearer " + key
        payload = {"model": model, "max_tokens": 1024,
                   "messages": [{"role": "system", "content": AI_SYSTEM},
                                {"role": "user", "content": user_prompt}]}
    req = urllib.request.Request(AI_ENDPOINTS[provider],
                                 data=json.dumps(payload).encode(),
                                 headers=body_headers, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    if provider == "anthropic":
        return "".join(b.get("text", "") for b in data.get("content", []))
    return data["choices"][0]["message"]["content"]


def _titlecase_csv(s):
    items, seen, out = [x.strip() for x in str(s).split(",")], set(), []
    for it in items:
        if not it:
            continue
        words = []
        for w in it.split(" "):
            if not w:
                continue
            words.append(w if (w.isupper() and len(w) <= 3) else w[:1].upper() + w[1:].lower())
        tc = " ".join(words)
        k = tc.lower()
        if k not in seen:
            seen.add(k); out.append(tc)
    return out


def parse_classification(text):
    """Pull {genre, tags} out of the model reply, tolerating stray prose/fences."""
    raw = text.strip()
    if "{" in raw and "}" in raw:
        raw = raw[raw.index("{"): raw.rindex("}") + 1]
    genre = tags = ""
    try:
        obj = json.loads(raw)
        genre, tags = obj.get("genre", ""), obj.get("tags", "")
        if isinstance(genre, list): genre = ", ".join(genre)
        if isinstance(tags, list): tags = ", ".join(tags)
    except ValueError:
        pass
    g, t = _titlecase_csv(genre), _titlecase_csv(tags)
    gset = {x.lower() for x in g}
    t = [x for x in t if x.lower() not in gset]   # enforce no overlap
    return {"genre": ", ".join(g), "tags": ", ".join(t)}


def ai_handle(body):
    try:
        req = json.loads(body or b"{}")
    except ValueError:
        return 400, {"error": "Invalid JSON"}
    action = req.get("action")
    if action not in ("similar", "summary", "question", "classify"):
        return 400, {"error": "Unknown action"}
    provider = (req.get("provider") or AI_PROVIDER or "").lower()
    key = req.get("api_key") or AI_KEY
    if not provider or not key:
        return 400, {"error": "AI is not configured. Start server.py with "
                              "--ai-provider and --ai-key, or set a key in the dialog."}
    if provider not in AI_ENDPOINTS:
        return 400, {"error": f"Unknown provider '{provider}'"}
    model = req.get("model") or AI_MODEL or AI_DEFAULT_MODELS[provider]
    book = req.get("book") or {}
    if req.get("include_text") and action in ("summary", "question"):
        try:
            max_chars = int(os.environ.get("LUMEN_AI_BOOK_CHARS") or AI_BOOK_CHARS)
            text, truncated = fetch_book_text(req.get("book_id"), req.get("library_id"),
                                              req.get("formats"), max_chars)
            book["_text"], book["_truncated"] = text, truncated
        except Exception as e:
            return 400, {"error": f"Couldn't read the book text: {e}"}
    prompt = ai_build_user_prompt(action, book, req.get("question"))
    try:
        text = ai_call(provider, key, model, prompt)
        out = {"text": text, "provider": provider, "model": model}
        if action == "classify":
            out.update(parse_classification(text))
        return 200, out
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        return 502, {"error": f"{provider} returned {e.code}: {detail}"}
    except (urllib.error.URLError, OSError) as e:
        return 502, {"error": f"Could not reach {provider}: {e}"}


# --------------------------------------------------------------------------
# Minimal WebDAV receiver for Boox NeoReader sync (optional)
# --------------------------------------------------------------------------

DAV_METHODS = "OPTIONS, PROPFIND, GET, HEAD, PUT, MKCOL, DELETE"
DAV_TEXT_SUFFIXES = {".txt", ".html", ".htm"}


def dav_enabled():
    return DAV_ROOT is not None


def dav_auth_required():
    return dav_enabled() and bool(DAV_USER and DAV_PASSWORD)


def xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def http_date(ts):
    return email.utils.formatdate(ts, usegmt=True)


def dav_content_type(path):
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def dav_local_path(request_path):
    if not dav_enabled():
        raise FileNotFoundError("Boox WebDAV is disabled")
    path = urllib.parse.urlsplit(request_path).path
    if path == "/dav":
        path = "/dav/"
    if not path.startswith("/dav/"):
        raise ValueError("Not a DAV path")
    rel_url = path[len("/dav/"):]
    rel = urllib.parse.unquote(rel_url)
    if "\x00" in rel:
        raise ValueError("Invalid DAV path")
    norm = posixpath.normpath("/" + rel).lstrip("/")
    parts = [] if norm in ("", ".") else norm.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("Invalid DAV path")
    root = DAV_ROOT.resolve()
    local = (root.joinpath(*parts)).resolve()
    if local != root and root not in local.parents:
        raise ValueError("Invalid DAV path")
    return local


def dav_href(path):
    root = DAV_ROOT.resolve()
    rel = path.resolve().relative_to(root)
    if not rel.parts:
        return "/dav/"
    href = "/dav/" + "/".join(urllib.parse.quote(p) for p in rel.parts)
    if path.is_dir() and not href.endswith("/"):
        href += "/"
    return href


def dav_prop_response(path):
    st = path.stat()
    is_dir = path.is_dir()
    href = xml_escape(dav_href(path))
    ctype = "httpd/unix-directory" if is_dir else dav_content_type(path)
    length = 0 if is_dir else st.st_size
    resourcetype = "<d:collection/>" if is_dir else ""
    etag = f'"{int(st.st_mtime)}-{st.st_size}"'
    return f"""  <d:response>
    <d:href>{href}</d:href>
    <d:propstat>
      <d:prop>
        <d:resourcetype>{resourcetype}</d:resourcetype>
        <d:getcontentlength>{length}</d:getcontentlength>
        <d:getlastmodified>{xml_escape(http_date(st.st_mtime))}</d:getlastmodified>
        <d:getcontenttype>{xml_escape(ctype)}</d:getcontenttype>
        <d:getetag>{xml_escape(etag)}</d:getetag>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>"""


def dav_multistatus(paths):
    body = ['<?xml version="1.0" encoding="utf-8"?>',
            '<d:multistatus xmlns:d="DAV:">']
    body.extend(dav_prop_response(p) for p in paths)
    body.append("</d:multistatus>")
    return ("\n".join(body) + "\n").encode("utf-8")


def dav_inbox_items():
    if not dav_enabled():
        raise FileNotFoundError("Boox WebDAV is disabled")
    root = DAV_ROOT.resolve()
    if not root.exists():
        return []
    items = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in DAV_TEXT_SUFFIXES:
            continue
        try:
            st = path.stat()
            rel = path.relative_to(root)
        except OSError:
            continue
        items.append({
            "name": path.name,
            "path": rel.as_posix(),
            "size": st.st_size,
            "modified": http_date(st.st_mtime),
            "mtime": st.st_mtime,
            "url": dav_href(path),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_body(self, body, ctype, code=200, cache=False, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_empty(self, code=204, extra=None):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def is_dav_path(self):
        path = urllib.parse.urlsplit(self.path).path
        return path == "/dav" or path.startswith("/dav/")

    def dav_not_enabled(self):
        self.send_error(404, "Boox WebDAV is not enabled")

    def dav_unauthorized(self):
        self.close_connection = True
        self.send_empty(401, {
            "WWW-Authenticate": 'Basic realm="Lumen Boox DAV"',
            "Connection": "close",
        })

    def drain_request_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def check_dav_auth(self):
        if not dav_auth_required():
            return True
        header = self.headers.get("Authorization") or ""
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "basic" or not token:
            return False
        try:
            raw = base64.b64decode(token.strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        user, sep, password = raw.partition(":")
        if not sep:
            return False
        return (hmac.compare_digest(user, DAV_USER) and
                hmac.compare_digest(password, DAV_PASSWORD))

    def require_dav_auth(self):
        if self.check_dav_auth():
            return True
        self.dav_unauthorized()
        return False

    def dav_options(self):
        if not dav_enabled():
            self.dav_not_enabled()
            return
        self.drain_request_body()
        self.send_empty(204, {"DAV": "1", "Allow": DAV_METHODS})

    def dav_propfind(self):
        if not dav_enabled():
            self.dav_not_enabled()
            return
        self.drain_request_body()
        try:
            path = dav_local_path(self.path)
        except ValueError:
            self.send_error(403, "Invalid DAV path")
            return
        if not path.exists():
            self.send_error(404, "Not found")
            return
        depth = (self.headers.get("Depth") or "infinity").lower()
        paths = [path]
        if path.is_dir() and depth != "0":
            try:
                paths.extend(sorted(path.iterdir(), key=lambda p: p.name.lower()))
            except OSError as e:
                self.send_error(500, str(e))
                return
        self.send_body(dav_multistatus(paths), "application/xml; charset=utf-8", 207,
                       extra={"DAV": "1"})

    def dav_get_head(self, include_body=True):
        if not dav_enabled():
            self.dav_not_enabled()
            return
        try:
            path = dav_local_path(self.path)
        except ValueError:
            self.send_error(403, "Invalid DAV path")
            return
        if not path.exists():
            self.send_error(404, "Not found")
            return
        if path.is_dir():
            listing = "\n".join(p.name + ("/" if p.is_dir() else "")
                                for p in sorted(path.iterdir(), key=lambda p: p.name.lower()))
            data = (listing + ("\n" if listing else "")).encode("utf-8")
            ctype = "text/plain; charset=utf-8"
            cache = False
        else:
            try:
                data = path.read_bytes()
            except OSError as e:
                self.send_error(500, str(e))
                return
            ctype = dav_content_type(path)
            cache = False
        if include_body:
            self.send_body(data, ctype, cache=cache)
        else:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

    def dav_put(self):
        if not dav_enabled():
            self.dav_not_enabled()
            return
        try:
            path = dav_local_path(self.path)
        except ValueError:
            self.send_error(403, "Invalid DAV path")
            return
        if path == DAV_ROOT.resolve():
            self.send_error(405, "Cannot PUT to the DAV root")
            return
        length = int(self.headers.get("Content-Length") or 0)
        existed = path.exists()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except OSError as e:
            self.send_error(500, str(e))
            return
        self.send_empty(204 if existed else 201, {"DAV": "1"})

    def dav_mkcol(self):
        if not dav_enabled():
            self.dav_not_enabled()
            return
        try:
            path = dav_local_path(self.path)
        except ValueError:
            self.send_error(403, "Invalid DAV path")
            return
        if path.exists():
            self.send_error(405, "Collection already exists")
            return
        if not path.parent.exists():
            self.send_error(409, "Parent collection does not exist")
            return
        try:
            path.mkdir()
        except OSError as e:
            self.send_error(500, str(e))
            return
        self.send_empty(201, {"DAV": "1"})

    def dav_delete(self):
        if not dav_enabled():
            self.dav_not_enabled()
            return
        try:
            path = dav_local_path(self.path)
        except ValueError:
            self.send_error(403, "Invalid DAV path")
            return
        if path == DAV_ROOT.resolve():
            self.send_error(405, "Cannot delete the DAV root")
            return
        if not path.exists():
            self.send_error(404, "Not found")
            return
        try:
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
        except OSError as e:
            self.send_error(409, str(e))
            return
        self.send_empty(204, {"DAV": "1"})

    # -------------------- GET --------------------

    def do_GET(self):
        if self.is_dav_path():
            if not self.require_dav_auth():
                return
            self.dav_get_head(True)
        elif self.path == "/dav-inbox":
            if not dav_enabled():
                self.dav_not_enabled()
                return
            if not self.require_dav_auth():
                return
            ans = {"enabled": True, "root": str(DAV_ROOT), "files": dav_inbox_items()}
            self.send_body(json.dumps(ans).encode(), "application/json")
        elif self.path == "/dav-status":
            ans = {"enabled": dav_enabled(), "auth": dav_auth_required(),
                   "count": len(dav_inbox_items()) if dav_enabled() else 0}
            self.send_body(json.dumps(ans).encode(), "application/json")
        elif self.path == "/" or self.path == "/index.html":
            self.serve_index()
        elif self.path.startswith(PROXY_PREFIXES):
            self.proxy("GET")
        elif self.path.startswith("/meta/"):
            self.meta()
        elif self.path == "/ai/config":
            ans = {"configured": bool(AI_KEY and AI_PROVIDER),
                   "provider": AI_PROVIDER or None,
                   "model": AI_MODEL or (AI_DEFAULT_MODELS.get(AI_PROVIDER) if AI_PROVIDER else None)}
            self.send_body(json.dumps(ans).encode(), "application/json")
        else:
            self.send_error(404, "Not found")

    def do_HEAD(self):
        if self.is_dav_path():
            if not self.require_dav_auth():
                return
            self.dav_get_head(False)
        elif self.path == "/" or self.path == "/index.html":
            try:
                body = INDEX.read_bytes()
            except OSError:
                self.send_error(500, "index.html not found next to server.py")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
        else:
            self.send_error(404, "Not found")

    def do_OPTIONS(self):
        if self.is_dav_path():
            if not self.require_dav_auth():
                return
            self.dav_options()
        else:
            self.send_empty(204, {"Allow": "GET, HEAD, POST, OPTIONS"})

    def do_PROPFIND(self):
        if self.is_dav_path():
            if not self.require_dav_auth():
                return
            self.dav_propfind()
        else:
            self.send_error(404, "Not found")

    def do_PUT(self):
        if self.is_dav_path():
            if not self.require_dav_auth():
                return
            self.dav_put()
        else:
            self.send_error(404, "Not found")

    def do_MKCOL(self):
        if self.is_dav_path():
            if not self.require_dav_auth():
                return
            self.dav_mkcol()
        else:
            self.send_error(404, "Not found")

    def do_DELETE(self):
        if self.is_dav_path():
            if not self.require_dav_auth():
                return
            self.dav_delete()
        else:
            self.send_error(404, "Not found")

    def serve_index(self):
        try:
            body = INDEX.read_bytes()
        except OSError:
            self.send_error(500, "index.html not found next to server.py")
            return
        self.send_body(body, "text/html; charset=utf-8")

    # -------------------- POST -------------------

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        # Write operations (edit metadata) go through /cdb/.
        if self.path.startswith("/cdb/"):
            self.proxy("POST", body)
        elif self.path == "/ai/ask":
            code, ans = ai_handle(body)
            self.send_body(json.dumps(ans).encode(), "application/json", code)
        else:
            self.send_error(404, "Not found")

    # ----------------- Calibre proxy -------------

    def proxy(self, method, body=None):
        url = CALIBRE_URL + self.path
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("User-Agent", "lumen-proxy/1.0")
        for h in ("Content-Type", "Accept"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        try:
            with OPENER.open(req, timeout=30) as upstream:
                data = upstream.read()
                ctype = upstream.headers.get("Content-Type", "application/octet-stream")
                extra = {}
                cd = upstream.headers.get("Content-Disposition")
                if cd:
                    extra["Content-Disposition"] = cd
                self.send_body(data, ctype, upstream.status,
                               cache=self.path.startswith("/get/"), extra=extra)
        except urllib.error.HTTPError as e:
            detail = e.read()[:500]
            self.send_body(detail or e.reason.encode(), "text/plain; charset=utf-8", e.code)
        except (urllib.error.URLError, OSError) as e:
            msg = (f"Cannot reach Calibre at {CALIBRE_URL} — {e}").encode()
            self.send_body(msg, "text/plain; charset=utf-8", 502)

    # --------------- metadata service ------------

    def meta(self):
        parsed = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/meta/search":
            try:
                ans = meta_search(q)
                self.send_body(json.dumps(ans).encode(), "application/json")
            except Exception as e:
                self.send_body(json.dumps({"results": [], "errors": [str(e)]}).encode(),
                               "application/json", 500)
        elif parsed.path == "/meta/cover":
            url = (q.get("url") or [""])[0]
            u = urllib.parse.urlsplit(url)
            if u.scheme not in ("http", "https") or u.hostname not in COVER_HOSTS:
                self.send_error(403, "Cover host not allowed")
                return
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "lumen/1.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    self.send_body(r.read(), r.headers.get("Content-Type", "image/jpeg"),
                                   cache=True)
            except Exception as e:
                self.send_error(502, f"Cover fetch failed: {e}")
        else:
            self.send_error(404, "Not found")


def load_env_file(path):
    """Minimal .env parser (KEY=VALUE, # comments). Does not override
    variables already present in the process environment."""
    try:
        text = Path(path).read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


def cfg(cli_value, env_name, default=""):
    """Config precedence: CLI flag > environment / .env > default."""
    return cli_value or os.environ.get(env_name, "") or default


def main():
    global CALIBRE_URL, OPENER, GOOGLE_KEY, AI_PROVIDER, AI_KEY, AI_MODEL, DAV_ROOT, DAV_USER, DAV_PASSWORD
    p = argparse.ArgumentParser(description="Lumen — modern UI for the Calibre Content Server")
    p.add_argument("--calibre", required=True,
                   help="Base URL of the Calibre Content Server, e.g. http://127.0.0.1:8081")
    p.add_argument("--port", type=int, default=8090, help="Port to listen on (default 8090)")
    p.add_argument("--bind", default="0.0.0.0", help="Address to bind (default 0.0.0.0)")
    p.add_argument("--user", help="Calibre content-server username (if auth is enabled)")
    p.add_argument("--password", help="Calibre content-server password")
    p.add_argument("--google-key", default="",
                   help="Optional Google Books API key (avoids 429 rate limits)")
    p.add_argument("--ai-provider", default="", choices=["", "openai", "anthropic", "xai"],
                   help="AI provider for the book assistant (optional)")
    p.add_argument("--ai-key", default="",
                   help="API key for the AI provider (prefer LUMEN_AI_KEY in .env — "
                        "CLI args are visible in `ps` output)")
    p.add_argument("--ai-model", default="",
                   help="Override the default model for the chosen provider")
    p.add_argument("--boox-dav", nargs="?", const="dav", default=None,
                   help="Enable the Boox WebDAV receiver at /dav/; optional value sets "
                        "the inbox folder (default: dav next to server.py)")
    p.add_argument("--boox-dav-user", default="",
                   help="Optional username for Boox WebDAV Basic auth")
    p.add_argument("--boox-dav-password", default="",
                   help="Optional password for Boox WebDAV Basic auth")
    p.add_argument("--env-file", default=str(HERE / ".env"),
                   help="Path to a .env file with secrets (default: .env next to server.py)")
    args = p.parse_args()
    load_env_file(args.env_file)

    CALIBRE_URL = args.calibre.rstrip("/")
    GOOGLE_KEY = cfg(args.google_key, "LUMEN_GOOGLE_KEY")
    AI_PROVIDER = cfg(args.ai_provider, "LUMEN_AI_PROVIDER").lower()
    AI_KEY = cfg(args.ai_key, "LUMEN_AI_KEY")
    AI_MODEL = cfg(args.ai_model, "LUMEN_AI_MODEL")
    if args.boox_dav is not None:
        dav = Path(args.boox_dav)
        DAV_ROOT = (dav if dav.is_absolute() else HERE / dav).resolve()
        DAV_ROOT.mkdir(parents=True, exist_ok=True)
        DAV_USER = args.boox_dav_user or ""
        DAV_PASSWORD = args.boox_dav_password or ""
    user = cfg(args.user or "", "LUMEN_CALIBRE_USER")
    password = cfg(args.password or "", "LUMEN_CALIBRE_PASSWORD")
    if user and password:
        # Calibre's content server uses digest auth by default over HTTP
        # and basic auth over HTTPS — register handlers for both.
        pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, CALIBRE_URL, user, password)
        OPENER = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(pm),
            urllib.request.HTTPBasicAuthHandler(pm),
        )

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"Lumen running on http://{args.bind}:{args.port} -> proxying {CALIBRE_URL}")
    if DAV_ROOT:
        print(f"Boox WebDAV enabled at /dav/ -> {DAV_ROOT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
