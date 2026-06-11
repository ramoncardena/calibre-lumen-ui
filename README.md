# Lumen — a modern UI for Calibre

A dark, fast, single-page front-end for your existing Calibre library, talking to the
Calibre Content Server's API. Your library, metadata, covers and formats stay exactly
where they are.

Two files, zero dependencies (Python 3.8+ stdlib only):

- `server.py` — proxy that serves the UI, forwards reads (`/ajax/*`, `/get/*`) and
  writes (`/cdb/*`) to Calibre, and provides the internet metadata service (`/meta/*`)
- `index.html` — the UI

## Features

- Cover grid with lazy thumbnails, infinite scroll, read check marks (#read column)
- Live search with full Calibre query syntax (`author:clausen`, `tag:scifi`)
- Filter sidebar (Authors, Series, Tags, Publisher, Languages, Rating, custom columns)
- Detail drawer: series, rating, tags, comments, identifiers, per-format downloads,
  one-tap read/unread toggle
- Edit metadata in a centered dialog with the full Calibre field set: title, title
  sort, authors, author sort, series + number, rating, tags, ids, date, published,
  publisher, languages, comments, cover replacement
- Download metadata from internet sources (Google Books + Open Library): search by
  title/author/ISBN, pick a result, fields and cover fill in for review before saving

## Run

    python3 server.py --calibre http://127.0.0.1:8081

Open http://<host>:8090. Options: `--port`, `--bind`, and `--user`/`--password`
if the content server has authentication (digest and basic both supported).

Note: metadata download needs outbound internet access from wherever server.py runs
(googleapis.com and openlibrary.org).

## Enabling writes (edit metadata / read toggle)

The Calibre content server is read-only for anonymous users. With auth disabled,
the proxy's source address must be listed in:

Preferences → Sharing over the net → Advanced →
"Allow un-authenticated connections from specific IP addresses to make changes"

IMPORTANT: this option is read once, when the content server starts. After changing
it you must restart the content server (easiest: restart the whole container,
`docker restart calibre`). This is the most common reason the setting "doesn't work".

To verify, from the machine running server.py:

    curl -i -X POST http://127.0.0.1:8081/cdb/set-fields/1/library \
      -H 'Content-Type: application/json' -H 'Accept: application/json' \
      -d '{"changes":{},"loaded_book_ids":[]}'

- 403 "Anonymous users are not allowed to make changes" → IP not trusted yet
- anything else (200, or an error about the book id) → write access is working

For Docker setups, requests from the host arrive from the Docker network gateway.
Find it with:

    docker network inspect calibre_default -f '{{(index .IPAM.Config 0).Gateway}}'

`172.16.0.0/12` covers all default Docker subnets. Trust model: anyone who can reach
Lumen can edit the library; direct connections to the content-server port from other
LAN devices (e.g. OPDS readers) stay read-only.

## Run as a service

`/etc/systemd/system/lumen.service`:

    [Unit]
    Description=Lumen Calibre UI
    After=network-online.target docker.service

    [Service]
    ExecStart=/usr/bin/python3 /srv/calibre/lumen/server.py --calibre http://127.0.0.1:8081
    Restart=on-failure

    [Install]
    WantedBy=multi-user.target

Then `systemctl enable --now lumen`.
