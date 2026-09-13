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
from typing import Dict, Optional

from shared.binary import (
    DAILY_HEADER_ID,
    DAILY_VALUE_ID,
    FRAME_SIZE,
    unpack_channel,
)

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
        # State for the one-shot daily-energy block the server sends on connect
        # (header frame + one value frame per day x channel).
        self._daily_header: Optional[Dict[str, object]] = None
        self._daily_values: list = []

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
        # A fresh connection = a fresh one-shot daily block from the server.
        self._daily_header = None
        self._daily_values = []
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
                ts_ms,
                voltage_mv,
                current_ma,
                power_mw,
                session_mwh,
                total_mwh,
            ) = unpack_channel(frame)
            if channel_id == DAILY_HEADER_ID:
                self._daily_header = {
                    "n_days": int(voltage_mv),
                    "n_channels": int(current_ma),
                    "today": self._yyyymmdd_to_iso(ts_ms),
                }
                self._daily_values = []
                continue
            if channel_id == DAILY_VALUE_ID:
                # Only collect while a header has been seen; a stray value
                # frame is never a channel sample.
                if self._daily_header is not None:
                    self._daily_values.append(
                        (int(ts_ms), int(voltage_mv), int(session_mwh), int(total_mwh))
                    )
                    if self._finish_daily_if_complete():
                        continue
                continue
            # Regular channel sample.
            self._store.apply_frame(
                ts=time.time(),
                channel_id=channel_id,
                voltage_v=voltage_mv / 1000.0,
                current_a=current_ma / 1000.0,
                power_w=power_mw / 1000.0,
                total_wh=total_mwh / 1000.0,
            )

    @staticmethod
    def _yyyymmdd_to_iso(value: int) -> str:
        """Turn a uint32 YYYYMMDD (e.g. 20260909) into ``YYYY-MM-DD``."""
        y, rem = divmod(int(value), 10000)
        m, d = divmod(rem, 100)
        return f"{y:04d}-{m:02d}-{d:02d}"

    def _finish_daily_if_complete(self) -> bool:
        """Reassemble the daily block once every day x channel frame is in.

        Returns True when the block was consumed (header cleared) so the caller
        stops treating subsequent frames as part of it.
        """
        header = self._daily_header
        if header is None:
            return False
        n_days = int(header["n_days"])
        n_channels = int(header["n_channels"])
        expected = n_days * n_channels
        if len(self._daily_values) < expected:
            return False
        if expected and len(self._daily_values) != expected:
            # Malformed block: discard and wait for the next one.
            self._daily_header = None
            self._daily_values = []
            return False
        per_channel: Dict[int, Dict[str, float]] = {}
        for day_index, cid, anchor_mwh, daily_mwh in self._daily_values:
            if not (0 <= day_index < n_days):
                continue
            entry = per_channel.setdefault(
                cid, {"vals": [0.0] * n_days, "anchor": 0.0}
            )
            entry["vals"][day_index] = daily_mwh / 1000.0
            if day_index == n_days - 1:
                entry["anchor"] = anchor_mwh / 1000.0
        today = str(header["today"])
        self._daily_header = None
        self._daily_values = []
        if per_channel:
            self._store.apply_daily(today, per_channel)
            LOGGER.info(
                "received daily energy block: %d channel(s), %d day(s), today %s",
                len(per_channel), n_days, today,
            )
        return True

