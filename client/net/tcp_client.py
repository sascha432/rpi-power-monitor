"""Reconnecting TCP reader for the web dashboard.

Consumes the Raspberry Pi's **raw binary TCP stream** (``shared.binary`` 40-byte
frames, no framing/length prefix) and pushes decoded readings into a
:class:`~client.store.DataStore`.

The socket runs on a **daemon worker thread** so the HTTP/WebSocket server
threads never block. Frames can be split or coalesced by the network, so the
reader keeps a byte buffer and slices off whole frames as they arrive.

This client is intentionally just a consumer: the raw stream is left untouched
for other clients (C++, scripts, …).
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Optional

from shared.binary import FRAME_SIZE, unpack_channel

from ..config import ConnectionConfig
from ..store import DataStore

LOGGER = logging.getLogger("client.net.tcp_client")


class TcpClient:
    """Connect / reconnect to the server and decode binary frames into a store."""

    def __init__(self, config: ConnectionConfig, store: DataStore) -> None:
        self._config = config
        self._store = store
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._socket: Optional[socket.socket] = None

    def start(self) -> None:
        """Launch the background reader thread (daemon)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="power-monitor-tcp", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Ask the reader thread to stop and close the socket."""
        self._stop.set()
        self._close_socket()

    # -- internals -----------------------------------------------------------

    def _close_socket(self) -> None:
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        cfg = self._config
        while not self._stop.is_set():
            try:
                self._connect_once(cfg)
            except OSError as exc:
                LOGGER.debug("connection error: %s", exc)
            # any exit from _connect_once = disconnected
            self._store.set_connected(False)
            if not cfg.reconnect or self._stop.is_set():
                break
            LOGGER.info(
                "reconnecting in %.1fs...", cfg.reconnect_delay_s
            )
            if self._stop.wait(cfg.reconnect_delay_s):
                break

    def _connect_once(self, cfg: ConnectionConfig) -> None:
        sock = socket.create_connection(
            (cfg.host, cfg.port), timeout=cfg.connect_timeout_s
        )
        sock.settimeout(cfg.read_timeout_s)
        self._socket = sock
        self._store.set_connected(True)
        LOGGER.info("connected to %s:%d", cfg.host, cfg.port)
        buffer = bytearray()
        try:
            while not self._stop.is_set():
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    LOGGER.warning(
                        "read timeout: no frame within %.0fs",
                        cfg.read_timeout_s,
                    )
                    break
                if not data:
                    break  # server closed the connection
                buffer += data
                self._drain(buffer)
        finally:
            self._close_socket()
            LOGGER.info("disconnected from %s:%d", cfg.host, cfg.port)

    def _drain(self, buffer: bytearray) -> None:
        """Slice and decode every complete 40-byte frame currently buffered."""
        while len(buffer) >= FRAME_SIZE:
            frame = bytes(buffer[:FRAME_SIZE])
            del buffer[:FRAME_SIZE]
            # channel_id, ts_ms, volt_mv, cur_ma, pow_mw, session_mwh, total_mwh
            (
                channel_id,
                _ts_ms,
                voltage_mv,
                current_ma,
                power_mw,
                session_mwh,
                total_mwh,
            ) = unpack_channel(frame)
            self._store.apply_frame(
                ts=time.time(),
                channel_id=channel_id,
                voltage_v=voltage_mv / 1000.0,
                current_a=current_ma / 1000.0,
                power_w=power_mw / 1000.0,
                session_wh=session_mwh / 1000.0,
                total_wh=total_mwh / 1000.0,
            )

