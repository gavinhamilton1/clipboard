# Clipboard

A single-page cross-system clip/paste board. Paste text or upload a file from one
machine, then copy or download it from another. Content lives on the server's local
disk until you clear it.

No dependencies — Python 3.9+ standard library only.

## Run it

```bash
python3 app.py
```

Then open `http://<server>:8000/` on any machine that can reach it.

## Using it

- **Text** — paste into the box and press *Send text* (or Ctrl/⌘+Enter). On the other
  machine the snippet shows up in the list with a *Copy* button that puts it straight
  on the system clipboard.
- **Files** — drag onto the drop zone, click *browse*, or paste a screenshot anywhere
  on the page. Pick them up with *Download*.
- **Clearing** — *Delete* removes one item; *Clear all* wipes everything. Both delete
  the blob and its metadata from disk immediately.

The list polls every 4 seconds, so a clip sent on one machine appears on the other
without a reload.

## Configuration

All optional, via environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLIPBOARD_HOST` | `0.0.0.0` | Bind address. Use `127.0.0.1` behind a reverse proxy. |
| `CLIPBOARD_PORT` | `$PORT`, else `8000` | Bind port. Hosts like Render inject `$PORT`; leave both unset there. |
| `CLIPBOARD_BASE_PATH` | `""` (root) | Mount the whole app under a prefix, e.g. `/clip`. |
| `CLIPBOARD_DATA_DIR` | `./storage` | Where blobs and metadata are written. |
| `CLIPBOARD_MAX_BYTES` | `104857600` (100 MiB) | Largest accepted upload. |
| `CLIPBOARD_MAX_AGE_HOURS` | `24` | Items older than this are deleted on the next request. `0` disables expiry. |

Example:

```bash
CLIPBOARD_HOST=127.0.0.1 CLIPBOARD_PORT=8000 \
CLIPBOARD_DATA_DIR=/var/lib/clipboard \
CLIPBOARD_MAX_AGE_HOURS=6 python3 app.py
```

`deploy/` has example nginx, Caddy, and systemd configs, plus
[`deploy/RENDER.md`](deploy/RENDER.md) for deploying to Render under a subpath such
as `https://dpop.fun/clip`.

## Keeping it out of search engines and crawlers

Three layers, all included:

1. `static/robots.txt` (served at `/robots.txt`) disallows everything, with the major
   search and AI crawlers named explicitly as well as the `*` wildcard.
2. Every HTTP response carries
   `X-Robots-Tag: noindex, nofollow, noarchive, nosnippet, noimageindex, notranslate`,
   which also covers uploaded files — a `robots.txt` rule alone does not deindex a
   file that has already been fetched, but this header does.
3. The page itself carries matching `<meta name="robots">` tags, plus
   `Referrer-Policy: no-referrer` so the URL is not leaked to sites you visit from it.

A crawler has to be pointed at a URL to find it, so also avoid linking to the
instance from anywhere public.

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/items` | List items, newest first. Text items ≤ 1 MiB include their content inline. |
| `POST` | `/api/text` | Create a text item. JSON body `{"text": "..."}`. |
| `POST` | `/api/files` | Create a file item. Raw body is the bytes; `X-Filename` header carries the (percent-encoded) name. |
| `GET` | `/api/items/<id>/raw` | Fetch the content inline. |
| `GET` | `/api/items/<id>/download` | Fetch the content as an attachment. |
| `DELETE` | `/api/items/<id>` | Delete one item. |
| `DELETE` | `/api/items` | Delete everything. |
| `GET` | `/healthz` | Liveness check, for platform health checks. |

When `CLIPBOARD_BASE_PATH` is set, every path above sits under it
(`/clip/api/items`, and so on). `/robots.txt` and `/healthz` answer at the true root
as well, since crawlers only read robots.txt from the domain root and health checks
are often configured without the prefix.

So you can drive it from a shell too:

```bash
# send
curl -X POST -H 'Content-Type: application/json' \
     -d '{"text":"hello"}' http://server:8000/api/text
curl -X POST -H 'X-Filename: notes.pdf' \
     --data-binary @notes.pdf http://server:8000/api/files

# clear
curl -X DELETE http://server:8000/api/items
```

## Scope

This first iteration has **no authentication, no TLS, and no per-user separation** —
anyone who can reach the port can read, write, and delete everything. On a LAN, VPN,
or Tailscale that is fine. On a public host it means anyone who finds the URL has the
same access you do; the `noindex` rules keep it out of search results, but that is
obscurity, not access control.

Storage is a flat directory of `<uuid>.blob` + `<uuid>.json` pairs. Item ids are
random UUID4 hex and are validated against `^[0-9a-f]{32}$` before touching the
filesystem, so a request cannot escape the storage directory. Uploaded filenames are
only used for display and the download header, never as paths.
