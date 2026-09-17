"""HTTP server for the browser.

Binds loopback by default, sets no CORS headers, answers only to a loopback
Host header, and makes no outbound requests. Routes under /api return JSON;
everything else is one of the files in `static`, matched by exact name.
"""

from __future__ import annotations

import gzip
import json
import re
import sqlite3
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, query
from .config import LOOPBACK, Config
from .index import connect

STATIC = Path(__file__).parent / "static"
STATIC_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'")
GZIP_MIN = 1400

ROUTES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^/api/stats$"), "stats"),
    (re.compile(r"^/api/doctor$"), "doctor"),
    (re.compile(r"^/api/sessions$"), "sessions"),
    (re.compile(r"^/api/sessions/(?P<session_id>[^/]+)$"), "session"),
    (re.compile(r"^/api/agents$"), "agents"),
    (re.compile(r"^/api/entries$"), "entries"),
    (re.compile(r"^/api/entries/(?P<entry_id>\d+)$"), "entry"),
    (re.compile(r"^/api/tool/(?P<tool_use_id>[^/]+)$"), "tool"),
    (re.compile(r"^/api/hooks$"), "hooks"),
    (re.compile(r"^/api/search$"), "search"),
]


class Api:
    """Thread local read only connections over one index."""

    def __init__(self, config: Config):
        self.config = config
        self._local = threading.local()

    @property
    def db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = connect(self.config.db_path, read_only=True)
            self._local.db = db
        return db

    def stats(self, _params: dict) -> dict:
        return query.stats(self.db)

    def doctor(self, _params: dict) -> dict:
        return {"version": __version__, "lines": query.doctor(self.db, self.config.enabled_kinds),
                "db": str(self.config.db_path),
                "roots": [{"name": r.name, "path": str(r.path)} for r in self.config.roots]}

    def sessions(self, params: dict) -> dict:
        return query.sessions(self.db, params)

    def session(self, params: dict, session_id: str = "") -> dict | None:
        return query.session(self.db, session_id)

    def agents(self, params: dict) -> dict:
        return query.agents(self.db, params)

    def entries(self, params: dict) -> dict:
        return query.entries(self.db, params)

    def entry(self, params: dict, entry_id: str = "") -> dict | None:
        found = query.body(self.db, int(entry_id))
        if found is None:
            return None
        found["related"] = query.related(self.db, int(entry_id))
        return found

    def tool(self, params: dict, tool_use_id: str = "") -> dict:
        return query.tool_call(self.db, tool_use_id)

    def hooks(self, params: dict) -> dict:
        return query.hooks(self.db, params)

    def search(self, params: dict) -> dict:
        return query.search(self.db, params)


def hostname_of(header: str) -> str:
    """The host part of a Host header, without its port or brackets."""
    value = (header or "").strip().lower()
    if value.startswith("["):
        return value[1:].partition("]")[0]
    return value.partition(":")[0] if value.count(":") <= 1 else value


def host_allowed(header: str, config: Config) -> bool:
    """Whether to answer a request carrying this Host header.

    A loopback bind is reachable by any page that points a name it owns at
    127.0.0.1, which makes the request same origin as far as the browser is
    concerned and leaves CORS with nothing to refuse. Requiring the name to be
    loopback is what closes that. The port is not checked: the name is what the
    attack turns on, and the server may be bound to a port other than the
    configured one.

    A bind the operator chose to expose is answered whatever the name, since
    reaching the port already implies reaching the logs.
    """
    return True if not config.loopback_only else hostname_of(header) in LOOPBACK


def make_handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"cclens/{__version__}"
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            if not host_allowed(self.headers.get("Host", ""), api.config):
                self._json({"error": "this server answers only to localhost"},
                           HTTPStatus.FORBIDDEN)
                return
            split = urlsplit(self.path)
            path = unquote(split.path)
            if path.startswith("/api/"):
                self._api(path, split.query)
            else:
                self._static(path)

        def _api(self, path: str, raw_query: str) -> None:
            params = {k: v[-1] for k, v in parse_qs(raw_query, keep_blank_values=True).items()}
            for pattern, name in ROUTES:
                match = pattern.match(path)
                if not match:
                    continue
                try:
                    result = getattr(api, name)(params, **match.groupdict())
                except (ValueError, sqlite3.Error) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                if result is None:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._json(result)
                return
            self._json({"error": "no such route"}, HTTPStatus.NOT_FOUND)

        def _static(self, path: str) -> None:
            name = "index.html" if path == "/" else path.lstrip("/")
            if STATIC_NAME.match(name):
                target = STATIC / name
                if target.is_file():
                    self._send(target.read_bytes(),
                               CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
                    return
            if ".." in PurePosixPath(name).parts:
                self._json({"error": "no such file"}, HTTPStatus.NOT_FOUND)
                return
            # Any other path belongs to the hash router, which lives in the page.
            self._send((STATIC / "index.html").read_bytes(), CONTENT_TYPES[".html"])

        def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, default=str, separators=(",", ":")).encode()
            self._send(body, "application/json; charset=utf-8", status)

        def _send(self, body: bytes, content_type: str,
                  status: HTTPStatus = HTTPStatus.OK) -> None:
            headers = {}
            if len(body) >= GZIP_MIN and "gzip" in self.headers.get("Accept-Encoding", ""):
                body = gzip.compress(body, 5)
                headers["Content-Encoding"] = "gzip"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            """Quiet by default; the interesting failures come back as JSON."""

    return Handler


def serve(config: Config, *, open_browser: bool = False) -> int:
    api = Api(config)
    httpd = ThreadingHTTPServer((config.host, config.port), make_handler(api))
    httpd.daemon_threads = True
    where = f"http://{config.host}:{config.port}/"
    print(f"cclens {__version__} serving {where}")
    print(f"index {config.db_path}")
    if not config.loopback_only:
        print(f"warning: {config.host} is not loopback, this exposes your logs "
              "to anything that can reach this port")
    if open_browser:
        webbrowser.open(where)
    print("press control-c to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0
