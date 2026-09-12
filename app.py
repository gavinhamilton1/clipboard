#!/usr/bin/env python3
"""Cross-system clipboard: a tiny paste/upload board backed by local disk.

Standard library only -- run with `python3 app.py`.

Configuration (environment variables):
    CLIPBOARD_HOST          bind address            (default 0.0.0.0)
    CLIPBOARD_PORT          bind port               (default 8000)
    CLIPBOARD_DATA_DIR      storage directory       (default ./storage)
    CLIPBOARD_MAX_BYTES     max upload size         (default 104857600 = 100 MiB)
    CLIPBOARD_MAX_AGE_HOURS auto-delete items older (default 24, 0 disables)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import uuid
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

HOST = os.environ.get("CLIPBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("CLIPBOARD_PORT", "8000"))
DATA_DIR = Path(os.environ.get("CLIPBOARD_DATA_DIR", BASE_DIR / "storage")).resolve()
ITEMS_DIR = DATA_DIR / "items"
MAX_BYTES = int(os.environ.get("CLIPBOARD_MAX_BYTES", str(100 * 1024 * 1024)))
MAX_AGE_SECONDS = float(os.environ.get("CLIPBOARD_MAX_AGE_HOURS", "24")) * 3600

# Text this size or smaller is inlined in the listing so the browser can copy it
# to the clipboard synchronously (Safari blocks clipboard writes after an await).
INLINE_TEXT_LIMIT = 1024 * 1024

ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Sent on every response, so nothing here gets indexed even if it is exposed.
ROBOTS_TAG = "noindex, nofollow, noarchive, nosnippet, noimageindex, notranslate"

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/robots.txt": ("robots.txt", "text/plain; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

def ensure_storage() -> None:
    ITEMS_DIR.mkdir(parents=True, exist_ok=True)


def meta_path(item_id: str) -> Path:
    return ITEMS_DIR / f"{item_id}.json"


def blob_path(item_id: str) -> Path:
    return ITEMS_DIR / f"{item_id}.blob"


def read_meta(item_id: str) -> dict | None:
    try:
        with meta_path(item_id).open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_meta(item_id: str, meta: dict) -> None:
    tmp = meta_path(item_id).with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    tmp.replace(meta_path(item_id))


def delete_item(item_id: str) -> bool:
    """Remove an item's blob and metadata. True if anything was removed."""
    removed = False
    for path in (blob_path(item_id), meta_path(item_id)):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return removed


def all_item_ids() -> list[str]:
    try:
        names = os.listdir(ITEMS_DIR)
    except OSError:
        return []
    return [n[:-5] for n in names if n.endswith(".json") and ID_RE.match(n[:-5])]


def reap_expired() -> None:
    """Drop items older than CLIPBOARD_MAX_AGE_HOURS so the disk cannot fill up."""
    if MAX_AGE_SECONDS <= 0:
        return
    cutoff = time.time() - MAX_AGE_SECONDS
    for item_id in all_item_ids():
        meta = read_meta(item_id)
        if meta is None or meta.get("created", 0) < cutoff:
            delete_item(item_id)


def list_items() -> list[dict]:
    reap_expired()
    items = []
    for item_id in all_item_ids():
        meta = read_meta(item_id)
        if meta is None:
            continue
        entry = {
            "id": meta["id"],
            "kind": meta["kind"],
            "name": meta.get("name", ""),
            "size": meta.get("size", 0),
            "content_type": meta.get("content_type", "application/octet-stream"),
            "created": meta.get("created", 0),
        }
        if meta["kind"] == "text":
            entry["preview"] = meta.get("preview", "")
            entry["truncated"] = meta.get("truncated", False)
            if meta.get("size", 0) <= INLINE_TEXT_LIMIT:
                try:
                    entry["text"] = blob_path(item_id).read_text("utf-8")
                except OSError:
                    continue
        items.append(entry)
    items.sort(key=lambda i: i["created"], reverse=True)
    return items


def new_id() -> str:
    return uuid.uuid4().hex


def safe_name(name: str) -> str:
    """Keep a display-safe basename; storage never uses this value as a path."""
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    return name[:200] or "upload.bin"


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "clipboard"
    sys_version = ""

    # ---- helpers -------------------------------------------------------- #

    def _base_headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Robots-Tag", ROBOTS_TAG)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store, max-age=0")

    def send_bytes(self, status: int, body: bytes, content_type: str,
                   extra: dict | None = None) -> None:
        self.send_response(status)
        self._base_headers(content_type, len(body))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, payload) -> None:
        self.send_bytes(status, json.dumps(payload).encode("utf-8"),
                        "application/json; charset=utf-8")

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def read_body(self) -> bytes | None:
        """Read the request body, enforcing the size limit. None => already answered."""
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self.send_error_json(HTTPStatus.LENGTH_REQUIRED,
                                 "Chunked uploads are not supported.")
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Bad Content-Length.")
            return None
        if length < 0:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Bad Content-Length.")
            return None
        if length > MAX_BYTES:
            # The body is never read, so the keep-alive stream would be left
            # mid-request; close the connection instead of desyncing it.
            self.close_connection = True
            self.send_bytes(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            json.dumps({"error": f"Too large. Limit is {MAX_BYTES} bytes."}).encode(),
                            "application/json; charset=utf-8",
                            {"Connection": "close"})
            return None
        return self.rfile.read(length) if length else b""

    # ---- routing -------------------------------------------------------- #

    def do_GET(self) -> None:
        self.route("GET")

    def do_HEAD(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")

    def do_DELETE(self) -> None:
        self.route("DELETE")

    def route(self, method: str) -> None:
        path = urlparse(self.path).path
        try:
            if method == "GET" and path in STATIC_FILES:
                return self.serve_static(path)
            if method == "GET" and path == "/api/items":
                return self.send_json(HTTPStatus.OK, {"items": list_items()})
            if method == "POST" and path == "/api/text":
                return self.create_text()
            if method == "POST" and path == "/api/files":
                return self.create_file()
            if method == "DELETE" and path == "/api/items":
                return self.clear_all()

            match = re.fullmatch(r"/api/items/([0-9a-f]{32})(?:/(raw|download))?", path)
            if match:
                item_id, action = match.group(1), match.group(2)
                if method == "DELETE" and action is None:
                    return self.delete_one(item_id)
                if method == "GET" and action == "raw":
                    return self.serve_blob(item_id, as_attachment=False)
                if method == "GET" and action == "download":
                    return self.serve_blob(item_id, as_attachment=True)
                return self.send_error_json(HTTPStatus.METHOD_NOT_ALLOWED,
                                            "Method not allowed.")

            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        except BrokenPipeError:
            pass
        except Exception as exc:  # keep one bad request from killing the server
            self.log_error("unhandled error: %s", exc)
            try:
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                     "Internal server error.")
            except Exception:
                pass

    # ---- endpoints ------------------------------------------------------ #

    def serve_static(self, path: str) -> None:
        filename, content_type = STATIC_FILES[path]
        try:
            body = (STATIC_DIR / filename).read_bytes()
        except OSError:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        self.send_bytes(HTTPStatus.OK, body, content_type)

    def create_text(self) -> None:
        body = self.read_body()
        if body is None:
            return
        try:
            payload = json.loads(body.decode("utf-8"))
            text = payload["text"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            return self.send_error_json(HTTPStatus.BAD_REQUEST,
                                        "Expected JSON body {\"text\": \"...\"}.")
        if not isinstance(text, str) or not text.strip():
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "Text is empty.")

        encoded = text.encode("utf-8")
        item_id = new_id()
        ensure_storage()
        blob_path(item_id).write_bytes(encoded)
        meta = {
            "id": item_id,
            "kind": "text",
            "name": "",
            "size": len(encoded),
            "content_type": "text/plain; charset=utf-8",
            "created": time.time(),
            "preview": text[:400],
            "truncated": len(text) > 400,
        }
        write_meta(item_id, meta)
        self.send_json(HTTPStatus.CREATED, {"id": item_id})

    def create_file(self) -> None:
        """Raw-body upload: the browser sends the bytes with the name in a header."""
        raw_name = self.headers.get("X-Filename", "")
        name = safe_name(unquote(raw_name))
        content_type = self.headers.get("Content-Type") or "application/octet-stream"

        body = self.read_body()
        if body is None:
            return
        if not body:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, "Empty upload.")

        item_id = new_id()
        ensure_storage()
        blob_path(item_id).write_bytes(body)
        write_meta(item_id, {
            "id": item_id,
            "kind": "file",
            "name": name,
            "size": len(body),
            "content_type": content_type,
            "created": time.time(),
        })
        self.send_json(HTTPStatus.CREATED, {"id": item_id})

    def serve_blob(self, item_id: str, as_attachment: bool) -> None:
        meta = read_meta(item_id)
        path = blob_path(item_id)
        if meta is None or not path.is_file():
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

        size = path.stat().st_size
        if as_attachment:
            name = meta.get("name") or (f"clip-{item_id[:8]}.txt"
                                        if meta["kind"] == "text" else f"clip-{item_id[:8]}.bin")
            ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "download"
            disposition = (f'attachment; filename="{ascii_name}"; '
                           f"filename*=UTF-8''{quote(name)}")
            content_type = meta.get("content_type", "application/octet-stream")
        else:
            disposition = "inline"
            content_type = ("text/plain; charset=utf-8" if meta["kind"] == "text"
                            else meta.get("content_type", "application/octet-stream"))

        self.send_response(HTTPStatus.OK)
        self._base_headers(content_type, size)
        self.send_header("Content-Disposition", disposition)
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as fh:
            shutil.copyfileobj(fh, self.wfile)

    def delete_one(self, item_id: str) -> None:
        if not delete_item(item_id):
            return self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        self.send_json(HTTPStatus.OK, {"deleted": 1})

    def clear_all(self) -> None:
        count = sum(1 for item_id in all_item_ids() if delete_item(item_id))
        self.send_json(HTTPStatus.OK, {"deleted": count})

    # ---- logging -------------------------------------------------------- #

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s [%s] %s\n" % (self.address_string(),
                                           formatdate(usegmt=True), fmt % args))


def main() -> None:
    ensure_storage()
    reap_expired()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"clipboard serving on http://{HOST}:{PORT}")
    print(f"  storage:   {DATA_DIR}")
    print(f"  max size:  {MAX_BYTES} bytes")
    print(f"  max age:   {'disabled' if MAX_AGE_SECONDS <= 0 else f'{MAX_AGE_SECONDS / 3600:g}h'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
