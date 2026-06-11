# Lumen

A modern, dark, single-page web UI for the [Calibre Content Server](https://manual.calibre-ebook.com/server.html).
Lumen talks to your existing Calibre library through its built-in API - nothing is
imported, converted, or duplicated, and the desktop GUI, OPDS feeds, and your
e-reader keep working against the same database.

<img width="1690" height="798" alt="lumen1" src="https://github.com/user-attachments/assets/a9a49fcb-88b4-4d4c-bb04-62051f20d66a" />


| Details | Metadata |
|------|------|
| <img width="1683" height="784" alt="lumen2" src="https://github.com/user-attachments/assets/0bfc8d81-5b18-4ec8-ba9d-b974d83f44f0" /> |<img width="1691" height="791" alt="lumen3" src="https://github.com/user-attachments/assets/ed73add5-ebef-4860-ad0f-b1fe618fa512" />|

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
  Languages, Rating, custom columns - with item counts, stacking filters, and a
  pinned Status selector (Read / In progress / Not started)
- Book drawer with downloads per format and one-tap read / in-progress toggles
- Metadata editing in a centered dialog with the full Calibre field set:
  title + sort, authors + sort, series + number, rating, tags, identifiers,
  date, published, publisher, languages, comments, cover replacement
- Download metadata from internet sources (Google Books + Open Library):
  search by title/author/ISBN, inspect each candidate's full metadata, then
  apply it for review before saving
- Counts and filtered views refresh live after every change

## Quick start

1. Start the Calibre Content Server (GUI: Connect/share → Start Content
   Server, or headless `calibre-server --port 8081 /path/to/library`).
2. Run Lumen:

       python3 server.py --calibre http://127.0.0.1:8081

3. Open `http://<host>:8090`.

Options: `--port`, `--bind`, `--user`/`--password` (digest and basic auth
supported), `--google-key` (optional Google Books API key, avoids 429
rate-limiting on metadata search).

## Enabling writes (editing, status toggles)

The content server is read-only for anonymous users. Either:

- run Lumen with `--user`/`--password` for a content-server account that has
  write permission, **or**
- with auth disabled, add the proxy's source IP in Calibre under
  *Preferences → Sharing over the net → Advanced → "Allow un-authenticated
  connections from specific IP addresses to make changes"*.

This option is read **once at server start** - restart the content server
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

## Reading status columns

Status tracking expects two yes/no custom columns in Calibre, lookup names
`#read` and `#started` (*Preferences → Add your own columns*). Different
names? Change the `READ_COL` / `STARTED_COL` constants at the top of the
script in `index.html`.

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

- No book upload or deletion (by design - use the Calibre GUI)
- Metadata sources are Google Books and Open Library, not the desktop's
  full plugin set; Open Library results have no descriptions
- Metadata search needs outbound internet from the host running `server.py`
