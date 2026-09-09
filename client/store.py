"""Thread-safe in-memory store for the live power-monitor stream.

The TCP reader thread pushes decoded readings in here; the web-server threads
read latest snapshots and per-channel history from it. Every value is stored
in canonical SI units (V, A, W, Wh).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional

from .config import ChannelConfig, ClientConfig

# Canonical value order used on the wire to the browser (history + sample):
# (t, voltage_v, current_a, power_w, session_wh, total_wh)


@dataclass
class Reading:
    """One decoded reading for one channel (canonical units, local wall time)."""

    t: float  # epoch seconds (UTC) at receipt - used as the chart x-axis
    voltage_v: float = 0.0
    current_a: float = 0.0
    power_w: float = 0.0
    session_wh: float = 0.0
    total_wh: float = 0.0

    def as_list(self) -> List[float]:
        """Flatten into the canonical ``[t, v, a, w, session_wh, total_wh]`` order."""
        return [self.t, self.voltage_v, self.current_a, self.power_w,
                self.session_wh, self.total_wh]


class DataStore:
    """Registry + rolling history + latest snapshot, guarded by one lock."""

    def __init__(self, config: ClientConfig) -> None:
        self._max_points: int = max(2, config.display.history_points)
        self.channels: Dict[int, ChannelConfig] = config.channel_map()
        self._lock = threading.Lock()
        self._series: Dict[int, Deque[Reading]] = {
            cid: deque(maxlen=self._max_points) for cid in self.channels
        }
        self._latest: Dict[int, Reading] = {}
        self._connected = False
        self._connected_at: Optional[float] = None
        self._last_receive: Optional[float] = None
        self._last_frame_at: Optional[float] = None
        # One-shot per-day energy block pushed by the server on connect:
        # str(cid) -> {"vals": [..n Wh oldest->today], "anchor": all-time Wh}.
        self._daily = None
        self._daily_today: Optional[str] = None  # server (Pi-local) newest date
        self._daily_rev = 0

    # -- status --------------------------------------------------------------

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def set_connected(self, connected: bool) -> None:
        """Called by the TCP reader thread on (dis)connect."""
        now = time.time()
        with self._lock:
            if connected == self._connected:
                return
            self._connected = connected
            self._connected_at = now if connected else None
            # A fresh connection means a fresh server run: drop old history so
            # charts don't bridge across a server restart with a time gap.
            if connected:
                for cid in self._series:
                    self._series[cid].clear()
                self._latest.clear()
                # The daily block belongs to the previous server run too: clear
                # it until the new connection's one-shot block arrives.
                self._daily = None
                self._daily_today = None
                self._daily_rev += 1

    # -- ingest --------------------------------------------------------------

    def apply_frame(
        self,
        ts: float,
        channel_id: int,
        voltage_v: float = 0.0,
        current_a: float = 0.0,
        power_w: float = 0.0,
        session_wh: float = 0.0,
        total_wh: float = 0.0,
    ) -> None:
        """Store one decoded frame (unknown channel ids are ignored)."""
        if channel_id not in self._series:
            return  # not in the configured channel table
        reading = Reading(
            t=ts,
            voltage_v=voltage_v,
            current_a=current_a,
            power_w=power_w,
            session_wh=session_wh,
            total_wh=total_wh,
        )
        with self._lock:
            self._series[channel_id].append(reading)
            self._latest[channel_id] = reading
            self._last_receive = ts
            self._last_frame_at = ts

    # -- reads ---------------------------------------------------------------

    def channel_ids(self) -> List[int]:
        return sorted(self.channels)

    def history(self, channel_id: int) -> List[Reading]:
        """Newest-last list of readings for one channel (copy)."""
        with self._lock:
            return list(self._series.get(channel_id, ()))

    def latest(self, channel_id: int) -> Optional[Reading]:
        with self._lock:
            return self._latest.get(channel_id)

    def snapshot(self) -> Dict[int, Reading]:
        """Copy of the latest reading per configured channel."""
        with self._lock:
            return dict(self._latest)

    def state(self) -> Dict[str, object]:
        """Small status blob used in messages and the /api/state endpoint."""
        with self._lock:
            return {
                "connected": self._connected,
                "connected_at": self._connected_at,
                "last_receive": self._last_receive,
                "age_s": (time.time() - self._last_receive) if self._last_receive else None,
                "channels": len(self.channels),
            }

    # -- daily energy (one-shot block from the server) -----------------------

    def apply_daily(
        self, today: str, per_channel: Dict[int, Dict[str, float]]
    ) -> None:
        """Store the server's one-shot per-channel daily-energy block.

        ``per_channel`` maps a wire channel id to
        ``{"vals": [..n Wh, oldest->today], "anchor": <all-time total Wh at
        block build>}``. Only configured channels are kept; unknown ids are
        ignored. Replaces any previous block (a fresh block arrives on every
        server connection).
        """
        with self._lock:
            kept: Dict[str, Dict[str, float]] = {}
            for cid, entry in per_channel.items():
                if cid not in self._series:
                    continue
                vals = [round(float(v), 4) for v in (entry.get("vals") or [])]
                if not vals:
                    continue
                kept[str(cid)] = {
                    "vals": vals,
                    "anchor": round(float(entry.get("anchor") or 0.0), 4),
                }
            if not kept:
                return
            self._daily = kept
            self._daily_today = today
            self._daily_rev += 1

    def daily(self) -> Optional[Dict[str, object]]:
        """Latest daily block as ``{"today": iso, "days": {cid: ..}}``."""
        with self._lock:
            if not self._daily:
                return None
            return {"today": self._daily_today, "days": self._daily}

    def daily_revision(self) -> int:
        """Monotonic counter bumped whenever the daily block is (re)applied."""
        with self._lock:
            return self._daily_rev

    def seed_history(self, max_points: int = 1500) -> Dict[str, List[List[float]]]:
        """Decimated per-channel history to seed a freshly opened browser tab.

        Rows are the canonical ``[t, v, a, w, session_wh, total_wh]`` order,
        rounded to save bandwidth. At most ``max_points`` rows per channel and
        never denser than one row per configured update tick.
        """
        step_target = max(1, self._max_points // max_points)
        out: Dict[str, List[List[float]]] = {}
        for cid in self.channel_ids():
            rows = self.history(cid)
            step = max(step_target, 1)
            picked = rows[::step][-max_points:]
            out[str(cid)] = [_round_row(r) for r in picked]
        return out

    def snapshot_rows(self) -> Dict[str, List[float]]:
        """Newest reading per channel as canonical rows (for ``sample`` msgs)."""
        return {str(cid): _round_row(r) for cid, r in self.snapshot().items()}


def _round_row(reading: Reading) -> List[float]:
    return [
        round(reading.t, 3),
        round(reading.voltage_v, 3),
        round(reading.current_a, 3),
        round(reading.power_w, 3),
        round(reading.session_wh, 3),
        round(reading.total_wh, 3),
    ]
