"""Shared test helpers: load the scripts as modules, and a small fake HTTP server."""

import importlib.util
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeServer:
    """HTTP server answering with `handle(method, path, query, headers, body)`.

    `handle` returns (status, body), where body is a dict (sent as JSON), bytes or None."""

    def __init__(self, handle):
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # quiet: tests read self.requests instead
                pass

            def _serve(self):
                parsed = urllib.parse.urlparse(self.path)
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                query = dict(urllib.parse.parse_qsl(parsed.query))
                rec = {"method": self.command, "path": parsed.path, "query": query,
                       "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
                server.requests.append(rec)
                status, out = handle(self.command, parsed.path, query, rec["headers"], body)
                data = json.dumps(out).encode() if isinstance(out, (dict, list)) else (out or b"")
                self.send_response(status)
                self.send_header("content-type", "application/json" if isinstance(out, (dict, list)) else "text/plain")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()

    def find(self, path: str) -> list[dict]:
        return [r for r in self.requests if r["path"] == path]
