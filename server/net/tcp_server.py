"""Threaded TCP broadcast server for the binary power-monitor stream.

Clients receive a raw stream of packed fixed-size frames (see ``shared.binary``
and ``docs/protocol.md``): one frame per channel per sample, no framing. The
layout is C-compatible so a C++ client can memcpy whole frames.

Optional client allowlist: pass ``allowed_clients`` as a list of IPs or CIDR
networks (e.g. ``["127.0.0.0/8", "192.168.0.0/24"]``). Connections whose
source address is not inside one of those networks are rejected on accept.
If the list is empty/omitted, only localhost (127.0.0.0/8, ::1) is allowed.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from typing import List, Optional, Sequence, Set, Union

LOGGER = logging.getLogger("server.net.tcp_server")

_Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# Default allowlist when no allowed_clients are configured: loopback only.
_LOCALHOST_NETWORKS: List[_Network] = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]


def _compile_allowlist(
    entries: Optional[Sequence[str]],
) -> List[_Network]:
    """Turn ``allowed_clients`` CIDR strings into networks.

    An empty/None list yields a default allowlist of localhost only. A bare
    IP such as ``"192.168.0.5"`` becomes a /32 host network. Raises
    ``ValueError`` if an entry cannot be parsed - a broken allowlist should
    fail fast instead of silently mis-blocking (or mis-allowing) traffic.
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
                f"invalid allowed_clients entry {text!r}: {exc}"
            ) from exc
    return networks or list(_LOCALHOST_NETWORKS)


class TcpServer:
    """Accept TCP clients and broadcast raw frames to all of them."""

    def __init__(
        self,
        host: str,
        port: int,
        max_clients: int = 8,
        allowed_clients: Optional[Sequence[str]] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._max_clients = max_clients
        self._networks: List[_Network] = _compile_allowlist(allowed_clients)
        self._server_socket: Optional[socket.socket] = None
        self._clients: Set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        self._connect_payload_builder = None
        self._running = False
        self._accept_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Bind/listen and start the accept loop in a daemon thread."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self._host, self._port))
        server.listen(self._max_clients)
        self._server_socket = server
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="tcp-accept", daemon=True
        )
        self._accept_thread.start()
        LOGGER.info("TCP broadcast listening on %s:%d", self._host, self._port)
        if set(self._networks) == set(_LOCALHOST_NETWORKS):
            LOGGER.info("TCP client allowlist: localhost only (default)")
        else:
            LOGGER.info(
                "TCP client allowlist: %s",
                ", ".join(str(net) for net in self._networks),
            )

    def broadcast(self, payload: bytes) -> None:
        """Send raw bytes to every connected client (drops dead sockets)."""
        if not payload:
            return
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.sendall(payload)
            except OSError:
                self._drop(client)

    def set_connect_payload(self, builder) -> None:
        """Register a callable whose bytes are sent to each new client first.

        ``builder`` is called (no args) in the accept thread right after a
        client connects and passes the allowlist. Its returned bytes are sent
        to that client *before* it is registered for broadcasts, so the client
        always receives the payload (e.g. the daily-energy history block)
        ahead of the first sample frame and the two never interleave. Return
        ``None`` or empty bytes to send nothing.
        """
        self._connect_payload_builder = builder

    def close(self) -> None:
        """Stop accepting and close every client connection."""
        self._running = False
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        LOGGER.info("TCP broadcast server stopped")

    # -- internals ----------------------------------------------------------

    def _is_allowed(self, address: str) -> bool:
        """True when ``address`` is inside one of the allowlisted networks."""
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in network for network in self._networks)

    def _accept_loop(self) -> None:
        assert self._server_socket is not None
        while self._running:
            try:
                client, addr = self._server_socket.accept()
            except OSError:
                break  # listener closed
            if not self._is_allowed(addr[0]):
                LOGGER.warning(
                    "TCP client %s rejected (source not in allowed_clients)",
                    addr[0],
                )
                try:
                    client.close()
                except OSError:
                    pass
                continue
            with self._clients_lock:
                if len(self._clients) >= self._max_clients:
                    try:
                        client.close()
                    except OSError:
                        pass
                    continue
            client.settimeout(5.0)
            # Deliver the one-shot connect payload (daily-energy history) BEFORE
            # registering for broadcasts, so it always precedes the first sample
            # frame for this client and the two can never interleave.
            if self._connect_payload_builder is not None:
                try:
                    payload = self._connect_payload_builder()
                    if payload:
                        client.sendall(payload)
                except OSError:
                    LOGGER.debug("TCP connect payload failed for %s", addr)
                    self._drop(client)
                    continue
            with self._clients_lock:
                self._clients.add(client)
            LOGGER.debug("TCP client connected: %s (total %d)", addr, len(self._clients))
            threading.Thread(
                target=self._reader_loop, args=(client, addr), name="tcp-client", daemon=True
            ).start()

    def _reader_loop(self, client: socket.socket, addr: object) -> None:
        """Watch the socket; drop the client when the peer closes it."""
        try:
            while self._running:
                try:
                    data = client.recv(1024)
                except socket.timeout:
                    continue  # idle but alive
                if not data:
                    break
        except OSError:
            pass
        finally:
            self._drop(client)
            LOGGER.debug("TCP client gone: %s", addr)

    def _drop(self, client: socket.socket) -> None:
        with self._clients_lock:
            self._clients.discard(client)
        try:
            client.close()
        except OSError:
            pass
