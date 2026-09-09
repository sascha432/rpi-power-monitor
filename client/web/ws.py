"""Minimal RFC 6455 WebSocket server implementation (stdlib only).

This is deliberately small and focused on what the dashboard needs:

* handshake: 101 Switching Protocols with ``Sec-WebSocket-Accept``,
* server -> client text frames (unmasked),
* client -> server masked frames (text), including ping / pong / close and
  fragmented text messages,
* thread-safe ``send_text`` so the broadcaster thread and the per-connection
  reader thread can both write to one socket.

It is not a general-purpose WebSocket library (no extensions, no permessage-
deflate), which is fine for a localhost/LAN dashboard.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import socket
import struct
import threading
from typing import Callable, Optional, Tuple

LOGGER = logging.getLogger("client.web.ws")

WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    """Raised on protocol / transport errors (connection should be dropped)."""


def compute_accept(key: str) -> str:
    """RFC 6455 section 4.2.2: SHA-1 of (key + GUID), base64-encoded."""
    digest = hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(payload: bytes, opcode: int = OP_TEXT, fin: bool = True) -> bytes:
    """Build a server -> client frame (never masked)."""
    first = (0x80 if fin else 0x00) | (opcode & 0x0F)
    header = bytearray([first])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    return bytes(header) + payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly ``n`` bytes, or raise ``ConnectionError`` on EOF."""
    chunks = bytearray()
    while len(chunks) < n:
        chunk = sock.recv(n - len(chunks))
        if not chunk:
            raise ConnectionError("connection closed by peer")
        chunks += chunk
    return bytes(chunks)


def read_frame(sock: socket.socket) -> Tuple[bool, int, bytes]:
    """Read and decode one client frame -> ``(fin, opcode, payload)``.

    Client frames may be masked (RFC 6455 requires it); the mask is stripped.
    Raises :class:`ConnectionError` on EOF and :class:`WebSocketError` on a
    malformed frame.
    """
    header = _recv_exact(sock, 2)
    b0, b1 = header[0], header[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    if length > 16 * 1024 * 1024:
        raise WebSocketError(f"frame too large: {length} bytes")
    mask = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length)
    if mask is not None:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


def send_close(sock: socket.socket, code: int = 1000, reason: str = "") -> None:
    """Send a close frame (server -> client)."""
    payload = struct.pack(">H", code) + reason.encode("utf-8")
    sock.sendall(encode_frame(payload, OP_CLOSE))


class WebSocketConnection:
    """One upgraded socket; ``send_text`` is safe from any thread."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._send_lock = threading.Lock()
        self.closed = False

    def send_text(self, text: str) -> None:
        self.send_bytes(text.encode("utf-8"), OP_TEXT)

    def send_bytes(self, payload: bytes, opcode: int = OP_TEXT) -> None:
        with self._send_lock:
            if self.closed:
                return
            try:
                self._sock.sendall(encode_frame(payload, opcode))
            except OSError:
                self.closed = True

    def close(self) -> None:
        with self._send_lock:
            if self.closed:
                return
            self.closed = True
            try:
                self._sock.close()
            except OSError:
                pass


def run_read_loop(
    conn: WebSocketConnection,
    on_message: Optional[Callable[[str], None]] = None,
) -> None:
    """Read frames until the connection closes.

    Handles ping (replies pong), close (replies close) and fragmented text
    messages; calls ``on_message`` with each complete text payload. Returns
    normally on a clean close; raises on a transport error.
    """
    sock = conn._sock  # noqa: SLF001 - same module, intentional
    fragmented = bytearray()
    fragment_opcode: Optional[int] = None
    while True:
        fin, opcode, payload = read_frame(sock)
        if opcode == OP_PING:
            conn.send_bytes(payload, OP_PONG)
            continue
        if opcode == OP_PONG:
            continue
        if opcode == OP_CLOSE:
            try:
                send_close(sock)
            except OSError:
                pass
            return
        if opcode in (OP_TEXT, OP_BINARY):
            if fin:
                _dispatch_message(conn, opcode, payload, on_message)
                continue
            fragment_opcode = opcode
            fragmented = bytearray(payload)
            continue
        if opcode == OP_CONTINUATION:
            fragmented += payload
            if fin and fragment_opcode is not None:
                _dispatch_message(conn, fragment_opcode, bytes(fragmented), on_message)
                fragmented = bytearray()
                fragment_opcode = None
            continue
        raise WebSocketError(f"unsupported opcode {opcode:#x}")


def _dispatch_message(
    conn: WebSocketConnection,
    opcode: int,
    payload: bytes,
    on_message: Optional[Callable[[str], None]],
) -> None:
    if opcode == OP_TEXT and on_message is not None:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            LOGGER.warning("dropping non-UTF-8 text frame")
            return
        try:
            on_message(text)
        except Exception:  # noqa: BLE001 - a bad handler must not kill the loop
            LOGGER.exception("ws on_message handler failed")
