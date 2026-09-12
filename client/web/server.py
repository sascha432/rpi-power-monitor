"""Stdlib HTTP + WebSocket dashboard server.

Serves the offline HTML/JS UI (``client/web/static``) and exposes one
WebSocket endpoint (``/ws``) that is the single pipe to the browser:

* on connect the server sends ``hello`` (the UI catalog: channels, metrics,
  units, theme options, energy unit, cadence - the channels come from the
  server's ``server.yaml`` via ``shared.catalog``, the rest from
  ``client.yaml``),
* then ``history`` (per-channel point arrays to seed the charts),
* then a ``sample`` every ``display.update_ms`` with the latest readings and
  the Pi connection state.

The user's UI choices (metric, theme, energy unit, time window, hidden
channels) are applied and stored by the browser in a cookie, so this server
does not track per-client settings.

Concurrency: every connection runs in its own thread (``ThreadingTCPServer``);
a single broadcaster thread writes ``sample`` messages to all connected
WebSockets. The Pi's raw TCP stream is left untouched for other clients.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import mimetypes
import socketserver
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from ..config import ClientConfig, metrics_for
from ..store import DataStore
from .ws import (
    WebSocketConnection,
    WebSocketError,
    compute_accept,
    run_read_loop,
)

LOGGER = logging.getLogger("client.web.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Metadata for the metrics the UI can chart (mirrors canonical wire fields).
METRIC_META: Dict[str, Dict[str, Any]] = {
    "power_w": {"label": "Power", "unit": "W", "kinds": ["rail", "aggregate"]},
    "voltage_v": {"label": "Voltage", "unit": "V", "kinds": ["rail"]},
    "current_a": {"label": "Current", "unit": "A", "kinds": ["rail"]},
}

_INDEX_NAME = "index.html"

# Dashboard client allowlist (mirrors server.allowed_clients semantics).
_Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# Default allowlist when web.allowed_clients is empty/omitted: loopback only.
_LOCALHOST_NETWORKS: List[_Network] = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]


def _compile_allowlist(entries: Optional[List[str]]) -> List[_Network]:
    """Turn ``web.allowed_clients`` CIDR strings into networks.

    Empty/None yields a default allowlist of localhost only. A bare IP such
    as ``"192.168.0.5"`` becomes a /32 host network. Raises ``ValueError``
    on an unparseable entry so a broken allowlist fails fast instead of
    silently mis-blocking (or mis-allowing) traffic.
    """
    if not entries:
        return list(_LOCALHOST_NETWORKS)
    networks: List[_Network] = []
    for entry in entries:
        text = str(entry).strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            raise ValueError(
                f"invalid web.allowed_clients entry {text!r}: {exc}"
            ) from exc
    return networks or list(_LOCALHOST_NETWORKS)


def build_catalog(cfg: ClientConfig) -> Dict[str, Any]:
    """UI catalog delivered in the ``hello`` message (and /api/catalog)."""
    display = cfg.display
    return {
        "title": display.title,
        "default_metric": display.default_metric,
        "metrics": METRIC_META,
        "energy_unit": display.energy_unit,
        "energy_units": display.energy_units,
        "theme": display.theme,
        "themes": display.theme_options,
        "update_ms": display.update_ms,
        "history_points": display.history_points,
        "channels": [
            {
                "id": channel.id,
                "name": channel.name,
                "label": channel.name,
                "kind": channel.kind,
                "aggregate": channel.aggregate,
                "metrics": metrics_for(channel.kind),
            }
            for channel in cfg.channels
        ],
    }


class _Handler(socketserver.BaseRequestHandler):
    """Routes one connection: static HTTP or a /ws WebSocket upgrade."""

    server: "WebDashboardServer"

    def handle(self) -> None:
        server = self.server
        request = self.request
        # Enforce the dashboard client allowlist before reading the request.
        if not server.is_client_allowed(self.client_address[0]):
            LOGGER.warning(
                "dashboard client %s rejected (not in web.allowed_clients)",
                self.client_address[0],
            )
            server.respond(request, 403, b"Forbidden", "text/plain")
            return
        # Guard against connections that never send anything.
        try:
            request.settimeout(10.0)
            head, _rest = _read_http_head(request)
        except (ConnectionError, OSError, ValueError):
            return

        try:
            method, path, headers = _parse_head(head)
        except ValueError:
            server.respond(request, 400, b"Bad Request", "text/plain")
            return

        if path == "/ws":
            if method != "GET":
                server.respond(request, 405, b"Method Not Allowed", "text/plain")
                return
            server.handle_websocket(request, headers)
            return

        if method != "GET":
            server.respond(request, 405, b"Method Not Allowed", "text/plain")
            return
        server.handle_http(request, path)


class WebDashboardServer(socketserver.ThreadingTCPServer):
    """HTTP + WebSocket dashboard. Instantiate with ``run()`` / ``close()``."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        config: ClientConfig,
        store: DataStore,
    ) -> None:
        self.config = config
        self.store = store
        self._catalog = build_catalog(config)
        self._clients: Set[WebSocketConnection] = set()
        self._clients_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._broadcaster: Optional[threading.Thread] = None
        self._last_daily_rev = store.daily_revision()
        self._started = False
        self._networks: List[_Network] = _compile_allowlist(config.web.allowed_clients)
        super().__init__(server_address, _Handler)

    # -- lifecycle -----------------------------------------------------------

    def run(self) -> None:
        """Start the broadcaster and serve until interrupted."""
        self._start_broadcaster()
        LOGGER.info(
            "dashboard listening on http://%s:%d  (WebSocket /ws)",
            self.config.web.host or "0.0.0.0",
            self.server_address[1],
        )
        if set(self._networks) == set(_LOCALHOST_NETWORKS):
            LOGGER.info("dashboard client allowlist: localhost only (default)")
        else:
            LOGGER.info(
                "dashboard client allowlist: %s",
                ", ".join(str(net) for net in self._networks),
            )
        try:
            self.serve_forever(poll_interval=0.5)
        finally:
            self.close()

    def close(self) -> None:
        """Stop the broadcaster, drop clients, and close the listening socket."""
        self._stop_event.set()
        if self._broadcaster is not None and self._broadcaster.is_alive():
            self._broadcaster.join(timeout=2.0)
        self._broadcaster = None
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for conn in clients:
            conn.close()
        try:
            self.server_close()
        except OSError:
            pass

    def _start_broadcaster(self) -> None:
        if self._broadcaster is not None:
            return
        self._stop_event.clear()
        self._broadcaster = threading.Thread(
            target=self._broadcast_loop, name="dashboard-broadcast", daemon=True
        )
        self._broadcaster.start()

    # -- client allowlist ----------------------------------------------------

    def is_client_allowed(self, address: str) -> bool:
        """True when ``address`` is inside one of the allowlisted networks."""
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in network for network in self._networks)

    # -- routing -------------------------------------------------------------

    def handle_http(self, request: Any, path: str) -> None:
        if path.startswith("/api/"):
            self._handle_api(request, path)
            return
        target = self._static_target(path)
        if target is None:
            self.respond(request, 404, b"Not Found", "text/plain")
            return
        try:
            body = target.read_bytes()
        except OSError:
            self.respond(request, 404, b"Not Found", "text/plain")
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.respond(request, 200, body, ctype)

    def _handle_api(self, request: Any, path: str) -> None:
        if path == "/api/state":
            body = json.dumps({"state": self.store.state()}).encode("utf-8")
        elif path == "/api/catalog":
            body = json.dumps({"catalog": self._catalog}).encode("utf-8")
        else:
            self.respond(request, 404, b"Not Found", "text/plain")
            return
        self.respond(request, 200, body, "application/json")

    def _static_target(self, path: str) -> Optional[Path]:
        """Resolve a request path to a file under STATIC_DIR (path-safe)."""
        if path in ("/", "/index.html"):
            rel = Path(_INDEX_NAME)
        elif path.startswith("/static/"):
            rel = Path(path[len("/static/"):])
        else:
            return None
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return None
        return target if target.is_file() else None

    # -- websocket -----------------------------------------------------------

    def handle_websocket(self, request: Any, headers: Dict[str, str]) -> None:
        key = headers.get("sec-websocket-key")
        if not key:
            self.respond(request, 400, b"missing Sec-WebSocket-Key", "text/plain")
            return
        accept = compute_accept(key)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("latin-1")
        try:
            request.sendall(response)
        except OSError:
            return
        request.settimeout(None)  # long-lived

        conn = WebSocketConnection(request)
        self._register(conn)
        try:
            # Push catalog + seed history right after the upgrade, then the
            # per-day energy block (may be empty until the Pi sends one).
            conn.send_text(self._hello_message())
            conn.send_text(self._history_message())
            conn.send_text(self._daily_message())
            run_read_loop(conn, on_message=self._on_client_message)
        except (ConnectionError, OSError, WebSocketError) as exc:
            LOGGER.debug("WebSocket client left: %s", exc)
        finally:
            self._unregister(conn)
            conn.close()

    def _on_client_message(self, text: str) -> None:
        # UI settings are kept in a browser cookie; there is nothing the server
        # needs to persist. Accept (and ignore) well-formed JSON for forward
        # compatibility, e.g. {"type":"ping"}.
        try:
            message = json.loads(text)
        except ValueError:
            return
        if isinstance(message, dict) and message.get("type") == "ping":
            return

    # -- messages ------------------------------------------------------------

    def _hello_message(self) -> str:
        return json.dumps(
            {"type": "hello", "catalog": self._catalog, "state": self.store.state()},
            separators=(",", ":"),
        )

    def _history_message(self) -> str:
        return json.dumps(
            {"type": "history", "history": self.store.seed_history(max_points=1500)},
            separators=(",", ":"),
        )

    def _daily_message(self) -> str:
        """The latest per-day energy block, or an explicit empty payload."""
        daily = self.store.daily()
        if daily is None:
            return json.dumps(
                {"type": "daily", "today": None, "days": None},
                separators=(",", ":"),
            )
        return json.dumps({"type": "daily", **daily}, separators=(",", ":"))

    def _sample_message(self) -> str:
        return json.dumps(
            {
                "type": "sample",
                "ts": time.time(),
                "state": self.store.state(),
                "channels": self.store.snapshot_rows(),
            },
            separators=(",", ":"),
        )

    def _broadcast_loop(self) -> None:
        update_s = max(0.05, self.config.display.update_ms / 1000.0)
        while not self._stop_event.is_set():
            started = time.monotonic()
            # The daily block only changes on a (re)connect or a day rollover,
            # so broadcast it just when the store revision moves.
            if self.store.daily_revision() != self._last_daily_rev:
                self._last_daily_rev = self.store.daily_revision()
                self._broadcast(self._daily_message())
            text = self._sample_message()
            self._broadcast(text)
            wait = update_s - (time.monotonic() - started)
            if wait > 0.0:
                self._stop_event.wait(wait)

    def _broadcast(self, text: str) -> None:
        with self._clients_lock:
            clients = list(self._clients)
        for conn in clients:
            conn.send_text(text)
        # Prune connections the writer gave up on.
        with self._clients_lock:
            for conn in list(self._clients):
                if conn.closed:
                    self._clients.discard(conn)

    def _register(self, conn: WebSocketConnection) -> None:
        with self._clients_lock:
            self._clients.add(conn)

    def _unregister(self, conn: WebSocketConnection) -> None:
        with self._clients_lock:
            self._clients.discard(conn)

    # -- low-level HTTP helpers ----------------------------------------------

    @staticmethod
    def respond(request: Any, status: int, body: bytes, ctype: str) -> None:
        """Write a minimal HTTP/1.1 response and signal connection close."""
        reason = {
            200: "OK",
            400: "Bad Request",
            403: "Forbidden",
            404: "Not Found",
            405: "Method Not Allowed",
            500: "Internal Server Error",
        }.get(status, "OK")
        header = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        try:
            request.sendall(header + body)
        except OSError:
            pass


def _read_http_head(request: Any, limit: int = 65536) -> Tuple[str, bytes]:
    """Read until the end of the HTTP headers; return (head, leftover)."""
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = request.recv(4096)
        if not chunk:
            raise ConnectionError("connection closed before headers")
        data += chunk
        if len(data) > limit:
            raise ValueError("headers too large")
    head, _, rest = bytes(data).partition(b"\r\n\r\n")
    return head.decode("latin-1"), rest


def _parse_head(head: str) -> Tuple[str, str, Dict[str, str]]:
    """Parse ``GET /path HTTP/1.1`` + headers into (method, path, headers)."""
    lines = head.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        raise ValueError("malformed request line")
    method, path = parts[0].upper(), parts[1]
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    return method, path, headers
