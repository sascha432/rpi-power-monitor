"""Thread-safe in-memory store for the live power-monitor stream.

The TCP reader thread pushes decoded readings in here; the web-server threads
read the latest snapshot per channel. Every value is stored in canonical SI
units (V, A, W, Wh).

Only the **latest** reading per channel is retained: the browser keeps its own
rolling chart buffers, so a server-side sample history would be memory nobody
reads - it existed only to seed a freshly opened chart, which the live stream
repopulates anyway. (The per-day *energy* totals are a separate one-shot block
from the Pi and are still stored here - see ``apply_daily``.)
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

from shared.catalog import ChannelInfo

from .config import ClientConfig

# Canonical value order used on the wire to the browser (sample messages):
# (t, voltage_v, current_a, power_w, total_wh)


@dataclass
class Reading:
    """One decoded reading for one channel (canonical units, local wall time)."""

    t: float  # epoch seconds (UTC) at receipt - used as the chart x-axis
    voltage_v: float = 0.0
    current_a: float = 0.0
    power_w: float = 0.0
    total_wh: float = 0.0


class DataStore:
    """Registry + latest snapshot per channel, guarded by one lock."""

    def __init__(self, config: ClientConfig) -> None:
        self.channels: Dict[int, ChannelInfo] = config.channel_map()
        self._lock = threading.Lock()
        self._latest: Dict[int, Reading] = {}
        self._connected = False
        # One-shot per-day energy block pushed by the server on connect:
        # str(cid) -> {"vals": [..n Wh oldest->today], "anchor": all-time Wh}.
        self._daily = None
        self._daily_today: Optional[str] = None  # server (Pi-local) newest date
        self._daily_rev = 0

    # -- status --------------------------------------------------------------

    def set_connected(self, connected: bool) -> None:
        """Called by the TCP reader thread on (dis)connect."""
        with self._lock:
            if connected == self._connected:
                return
            self._connected = connected
            # A fresh connection means a fresh server run: drop the previous
            # run's snapshots so nothing stale is shown until new frames arrive.
            if connected:
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
        total_wh: float = 0.0,
    ) -> None:
        """Store one decoded frame (unknown channel ids are ignored)."""
        if channel_id not in self.channels:
            return  # not in the configured channel table
        reading = Reading(
            t=ts,
            voltage_v=voltage_v,
            current_a=current_a,
            power_w=power_w,
            total_wh=total_wh,
        )
        with self._lock:
            self._latest[channel_id] = reading

    # -- reads ---------------------------------------------------------------

    def snapshot(self) -> Dict[int, Reading]:
        """Copy of the latest reading per configured channel."""
        with self._lock:
            return dict(self._latest)

    def state(self) -> Dict[str, object]:
        """Pi connection flag carried in the ``hello``/``sample`` messages."""
        with self._lock:
            return {"connected": self._connected}

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
                if cid not in self.channels:
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

    def snapshot_rows(self) -> Dict[str, List[float]]:
        """Newest reading per channel as canonical rows (for ``sample`` msgs)."""
        return {str(cid): _round_row(r) for cid, r in self.snapshot().items()}


def _round_row(reading: Reading) -> List[float]:
    return [
        round(reading.t, 3),
        round(reading.voltage_v, 3),
        round(reading.current_a, 3),
        round(reading.power_w, 3),
        round(reading.total_wh, 3),
    ]
