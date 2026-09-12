# Deploying to Render at `https://dpop.fun/clip`

## First, the routing question

A Render custom domain maps a whole **hostname** to one service. Render has no
path-based routing, so you cannot point `dpop.fun/clip` at service B while
`dpop.fun/` stays on service A *from inside Render*. There are two ways to get the
URL you want, and the app supports both — the only difference is one env var.

### Case A — this service owns `dpop.fun`

Attach `dpop.fun` as a custom domain on this service and let the app serve the
`/clip` prefix itself.

    CLIPBOARD_BASE_PATH=/clip

`https://dpop.fun/clip` serves the page; `https://dpop.fun/` redirects to it.
Nothing else on the domain is reachable.

### Case B — something else already owns `dpop.fun`

Give this service its own hostname (the free `clipboard-xxxx.onrender.com` is
fine) and have whatever serves `dpop.fun` proxy `/clip` to it, keeping
`CLIPBOARD_BASE_PATH=/clip`:

```nginx
location /clip {
    # No URI after the host, so nginx forwards the path unchanged, prefix included.
    proxy_pass https://clipboard-xxxx.onrender.com;
    proxy_set_header Host clipboard-xxxx.onrender.com;
    proxy_ssl_server_name on;
    proxy_request_buffering off;
    client_max_body_size 50m;
}
```

**The proxy must forward the `/clip` prefix, not strip it.** The page emits absolute
URLs (`/clip/app.js`, `/clip/api/items`), so if the prefix is stripped on the way in,
the browser's follow-up requests land outside `/clip` on `dpop.fun` and never reach
this service. The `proxy_pass` above preserves it; adding a trailing `/` to that
directive (`proxy_pass https://host/;`) is what strips it, so do not.

Health check path is `/clip/healthz`. `/healthz` also answers at the true root, so a
check configured without the prefix works too.

## Manual dashboard setup

New → Web Service → connect `gavinhamilton1/clipboard`, then:

| Setting | Value |
| --- | --- |
| Language / Runtime | Python 3 |
| Branch | `main` |
| Build command | `pip install -r requirements.txt` |
| Start command | `python3 app.py` |
| Health check path | `/clip/healthz` |
| Auto-deploy | On commit (default) |

Environment variables:

| Key | Value | Why |
| --- | --- | --- |
| `CLIPBOARD_BASE_PATH` | `/clip` | Serve under the subpath. Leave empty to serve at the root. |
| `CLIPBOARD_DATA_DIR` | `/tmp/clipboard` | Render's disk is ephemeral anyway; `/tmp` makes that explicit. |
| `CLIPBOARD_MAX_BYTES` | `52428800` | 50 MiB. |
| `CLIPBOARD_MAX_AGE_HOURS` | `24` | Reap anything you forgot to clear. |

Do **not** set `PORT` — Render injects it and the app reads it automatically. Do not
set `CLIPBOARD_PORT` either, or it will override Render's port and the deploy will
fail its health check.

No Python version is pinned, so Render uses its default. The app is standard-library
only and runs on any Python 3.9+; pin `PYTHON_VERSION` if you want to be explicit.

`render.yaml` in the repo root carries the same configuration as a Blueprint if you
would rather not click through the dashboard.

## Things worth knowing before you ship it

**It is public and unauthenticated.** On a LAN that was fine; on `dpop.fun` anyone
who guesses or stumbles onto the URL can read every clip, upload files, and delete
them. The `noindex` headers keep it out of search results but are not access
control. If you want a shared password or token gate, that is a small change.

**Storage is ephemeral.** Every deploy, restart, and (on free instances) idle
spin-down wipes the clips. That is usually the right behaviour for a scratch
clipboard — it just means you cannot treat it as a file store. Mount a disk (see the
commented block in `render.yaml`) if you want clips to survive a redeploy.

**One instance only.** Clips live on the instance's own disk, so if you ever scale
to more than one instance, a clip written on one will not be visible from the other.

**Large uploads.** Platform request timeouts, not the size limit, are what usually
bite first on a hosted instance. 50 MiB is a safe default; raise
`CLIPBOARD_MAX_BYTES` if you need more and find it works for your files.

**robots.txt at the domain root.** Crawlers only read `https://dpop.fun/robots.txt`.
In Case A the app serves that itself. In Case B it is served by whatever owns
`dpop.fun`, so add the disallow rule there — copy `static/robots.txt`, or at minimum:

```
User-agent: *
Disallow: /clip
```

The `X-Robots-Tag` header the app sets on every response (including file downloads)
does not depend on this and keeps working either way.
