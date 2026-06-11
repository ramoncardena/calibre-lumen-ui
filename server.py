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

Usage:
    python3 server.py --calibre http://127.0.0.1:8081
    python3 server.py --calibre http://127.0.0.1:8081 --port 8090
    python3 server.py --calibre http://127.0.0.1:8081 --user X --password Y
"""

import argparse
import json
import sys
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

    # -------------------- GET --------------------

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.serve_index()
        elif self.path.startswith(PROXY_PREFIXES):
            self.proxy("GET")
        elif self.path.startswith("/meta/"):
            self.meta()
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
        # Write operations (edit metadata) go through /cdb/.
        if self.path.startswith("/cdb/"):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            self.proxy("POST", body)
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


def main():
    global CALIBRE_URL, OPENER, GOOGLE_KEY
    p = argparse.ArgumentParser(description="Lumen — modern UI for the Calibre Content Server")
    p.add_argument("--calibre", required=True,
                   help="Base URL of the Calibre Content Server, e.g. http://127.0.0.1:8081")
    p.add_argument("--port", type=int, default=8090, help="Port to listen on (default 8090)")
    p.add_argument("--bind", default="0.0.0.0", help="Address to bind (default 0.0.0.0)")
    p.add_argument("--user", help="Calibre content-server username (if auth is enabled)")
    p.add_argument("--password", help="Calibre content-server password")
    p.add_argument("--google-key", default="",
                   help="Optional Google Books API key (avoids 429 rate limits)")
    args = p.parse_args()

    CALIBRE_URL = args.calibre.rstrip("/")
    GOOGLE_KEY = args.google_key
    if args.user and args.password:
        # Calibre's content server uses digest auth by default over HTTP
        # and basic auth over HTTPS — register handlers for both.
        pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, CALIBRE_URL, args.user, args.password)
        OPENER = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(pm),
            urllib.request.HTTPBasicAuthHandler(pm),
        )

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"Lumen running on http://{args.bind}:{args.port} -> proxying {CALIBRE_URL}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
