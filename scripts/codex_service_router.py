#!/usr/bin/env python3
from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import urllib.error
import urllib.request


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_GATEWAY_UPSTREAM = "http://127.0.0.1:8788"
DEFAULT_AUDIT_UPSTREAM = "http://127.0.0.1:8797"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def json_response(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, object]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status.value)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def resolve_upstream(path: str) -> str:
    if path == "/v1/codex-audit":
        return os.environ.get("CODEX_SERVICE_ROUTER_AUDIT_UPSTREAM", DEFAULT_AUDIT_UPSTREAM).rstrip("/")
    return os.environ.get("CODEX_SERVICE_ROUTER_GATEWAY_UPSTREAM", DEFAULT_GATEWAY_UPSTREAM).rstrip("/")


def forwarded_headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in handler.headers.items():
        if key.lower() in HOP_BY_HOP_HEADERS or key.lower() == "host":
            continue
        headers[key] = value
    headers["X-Forwarded-Host"] = handler.headers.get("Host", "")
    headers["X-Forwarded-Proto"] = "https"
    return headers


class CodexServiceRouterHandler(BaseHTTPRequestHandler):
    server_version = "CodexServiceRouter/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - BaseHTTPRequestHandler API.
        print("[codex-service-router] " + format % args, file=sys.stderr)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            json_response(self, HTTPStatus.OK, {"status": "ok"})
            return
        self.forward()

    def do_POST(self) -> None:
        self.forward()

    def forward(self) -> None:
        upstream = resolve_upstream(self.path)
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length > 0 else None
        request = urllib.request.Request(
            upstream + self.path,
            data=body,
            method=self.command,
            headers=forwarded_headers(self),
        )
        timeout = int(os.environ.get("CODEX_SERVICE_ROUTER_TIMEOUT_SECONDS", "3600"))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read()
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() in HOP_BY_HOP_HEADERS:
                        continue
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(response_body)
        except urllib.error.HTTPError as exc:
            response_body = exc.read()
            self.send_response(exc.code)
            for key, value in exc.headers.items():
                if key.lower() in HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response_body)
        except Exception as exc:  # noqa: BLE001 - router boundary should fail closed.
            print(f"[codex-service-router] {type(exc).__name__}: {exc}", file=sys.stderr)
            json_response(self, HTTPStatus.BAD_GATEWAY, {"status": "error", "error": "upstream unavailable"})


def main() -> int:
    host = os.environ.get("CODEX_SERVICE_ROUTER_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    port = int(os.environ.get("CODEX_SERVICE_ROUTER_PORT", str(DEFAULT_PORT)))
    server = ThreadingHTTPServer((host, port), CodexServiceRouterHandler)
    print(f"[codex-service-router] listening on http://{host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
