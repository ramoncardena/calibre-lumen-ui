# Lumen

**[Lumen Website](https://ramoncardena.github.io/calibre-lumen-ui/)**

A modern, themeable, single-page web UI for the [Calibre Content Server](https://manual.calibre-ebook.com/server.html).
Lumen talks to your existing Calibre library through its built-in API. Nothing is
imported, converted, or duplicated, and the desktop GUI, OPDS feeds, and your
e-reader keep working against the same database.

Two files, zero dependencies (Python 3.8+ standard library only):

| File | Role |
|------|------|
| `server.py` | Serves the UI and proxies the Calibre API (reads `/ajax/*` `/get/*`, writes `/cdb/*`). Also provides internet metadata search (`/meta/*`). |
| `index.html` | The entire UI. |

## Features

- Cover grid with lazy thumbnails, infinite scroll, and status badges
  (green ✓ = read, amber 🕒 = in progress) driven by two Calibre yes/no
  custom columns
- Live search with full Calibre query syntax (`author:clausen`, `tag:scifi`)
- Filter sidebar like Calibre's tag browser: Authors, Series, Tags, Publisher,
  Languages, Rating, custom columns, with item counts, stacking filters, and a
  pinned Status selector (Read / In progress / Not started)
- Book drawer with downloads per format and one-tap read / in-progress
  toggles; series, genre, and tag chips filter the library when clicked
- Metadata editing in a centered dialog with the full Calibre field set:
  title + sort, authors + sort, series + number, rating, tags, identifiers,
  date, published, publisher, languages, comments, cover replacement
- Download metadata from internet sources (Google Books + Open Library):
  search by title/author/ISBN, inspect each candidate's full metadata, then
  apply it for review before saving
- Counts and filtered views refresh live after every change
- Click-to-rate stars in the book panel
- Optional AI assistant per book: similar titles, summary, questions, and
  one-click genre/tag generation (OpenAI / Anthropic / xAI, bring your own
  key), optionally grounded in the actual book by sending its EPUB/TXT text
  to the model
- Eight selectable themes: four dark (Lamplight, Midnight, Forest, Plum) and
  four light (Paper, Linen, Sakura, Dune), picked from the header and saved
  per browser
- Analytics dashboard: reading status, formats, books added per month,
  publication decades, top tags/authors, and rating distribution. Every
  chart is clickable and filters the library to what you tapped
- Cover-size slider in the header, remembered per browser
- Optional genre support through a `#genre` custom column: a sidebar section,
  chips in the book panel, an edit field, and a Top genres chart in analytics

## Quick start

1. Start the Calibre Content Server (GUI: Connect/share → Start Content
   Server, or headless `calibre-server --port 8081 /path/to/library`).
2. Run Lumen:

       python3 server.py --calibre http://127.0.0.1:8081

3. Open `http://<host>:8090`.

Options: `--port`, `--bind`, `--env-file`, `--user`/`--password` (digest and
basic auth supported). Secrets (AI keys, the Google Books key, calibre
credentials) belong in a `.env` file (copy `.env.example`), not on the
command line, where they would be visible in `ps` output and shell history.

## Enabling writes (editing, status toggles)

The content server is read-only for anonymous users. Either:

- run Lumen with `--user`/`--password` for a content-server account that has
  write permission, **or**
- with auth disabled, add the proxy's source IP in Calibre under
  *Preferences → Sharing over the net → Advanced → "Allow un-authenticated
  connections from specific IP addresses to make changes"*.

This option is read **once at server start**, so restart the content server
after changing it. If Calibre runs in Docker, requests from the host arrive
from the Docker network gateway; `172.16.0.0/12` covers the default subnets,
or find the exact gateway with:

    docker network inspect <network> -f '{{(index .IPAM.Config 0).Gateway}}'

Verify from the machine running `server.py` (any response other than a 403
"Anonymous users are not allowed to make changes" means writes work):

    curl -i -X POST http://127.0.0.1:8081/cdb/set-fields/1/library \
      -H 'Content-Type: application/json' -H 'Accept: application/json' \
      -d '{"changes":{},"loaded_book_ids":[]}'

Trust model: anyone who can reach Lumen can edit the library through it,
while direct connections to the content-server port (e.g. OPDS readers)
stay read-only. Run it on a trusted network.

## AI assistant (optional)

The book details panel has an AI button offering three book-scoped actions:
similar-book recommendations (each linked to a Goodreads search), a
spoiler-light summary, and free-form questions about the book. Supported
providers: OpenAI, Anthropic, and xAI. Bring your own API key.

By default the model only sees the book's metadata. Ticking **"Send the book
text"** makes the server extract the text from the book's EPUB (or TXT) and
include it in the prompt, so summaries and plot questions are answered from
the actual book. The text is capped at 250,000 characters by default
(`LUMEN_AI_BOOK_CHARS` in `.env` to change it); longer books are truncated
from the start and the model is told the ending is missing. Mind the cost:
a full-text question is cheap on small models (~$0.01 on gpt-4o-mini) but
can reach tens of cents on premium ones. Books with neither EPUB nor TXT
can't use this option (convert in Calibre first).

Configure it server-side (recommended; one key for every device) via a
`.env` file next to `server.py`. Copy `.env.example` to `.env` and fill in:

    LUMEN_AI_PROVIDER=anthropic
    LUMEN_AI_KEY=sk-ant-...

`.env` is gitignored; never commit real keys. Precedence is CLI flags >
environment variables > `.env`, so systemd `EnvironmentFile=` works too.
`LUMEN_AI_MODEL` overrides the per-provider default (gpt-4o-mini /
claude-sonnet-4-6 / grok-4). Alternatively, each browser can set its own
provider + key under "AI settings" in the dialog; that key is stored in that
browser's localStorage only and takes precedence over the server key.

Generate genre and tags: the metadata edit dialog has a **Generate** button on
the Tags field. It asks the model to classify the book from its metadata and
fills both the Genre and Tags fields at once (a single request). Genres are
kept broad, tags specific, every term is Title Cased, and a term never appears
in both lists. The fields are filled for review; nothing is saved until you
click Save. If you have not added a genre column, only Tags are filled.

Privacy note: the selected book's metadata (title, authors, tags, publisher
description), your question, and (only when the checkbox is ticked) the
book's extracted text are sent to the chosen AI provider. Nothing
else from the library leaves the server, and nothing is sent until an action
is clicked.

## Custom columns: reading status and genre

Status tracking expects two yes/no custom columns in Calibre, lookup names
`#read` and `#started` (*Preferences → Add your own columns*). Different
names? Change the `READ_COL` / `STARTED_COL` constants at the top of the
script in `index.html`.

Genre support is optional. Create a custom column of type **"Comma separated
text, like tags"** with lookup name `genre`, then reload Lumen. The sidebar
section, book-panel chips, edit field, and analytics chart all appear
automatically once the column exists; until then Lumen behaves as before.
A different lookup name goes in the `GENRE_COL` constant.

## Themes

The header's theme button opens a swatch picker with eight themes. The choice
is stored in that browser's localStorage, so each device keeps its own, and
it's applied before first paint (no flash on load).

Every color in the UI is a CSS variable, so adding your own theme is a small
block in `index.html`: copy any `html[data-theme="..."]` block, change the
values, and add an entry to the `THEMES` array in the script.

## Run as a service

`/etc/systemd/system/lumen.service`:

    [Unit]
    Description=Lumen Calibre UI
    After=network-online.target

    [Service]
    ExecStart=/usr/bin/python3 /opt/lumen/server.py --calibre http://127.0.0.1:8081
    Restart=on-failure

    [Install]
    WantedBy=multi-user.target

Then `systemctl enable --now lumen`.

## Limitations

- No book upload or deletion (by design, use the Calibre GUI)
- Metadata sources are Google Books and Open Library, not the desktop's
  full plugin set; Open Library results have no descriptions
- Metadata search needs outbound internet from the host running `server.py`
