"""Per-channel energy counters in milliwatt-hours with JSON persistence.

Energy/power are tracked ONLY on aggregate channels (rails grouped by their
``aggregate`` tag); physical rails report voltage/current only.

Two counters per aggregate channel:
  - session (mWh since this server process started)
  - total   (mWh across all runs - loaded from / saved to the state file)
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Dict, Iterable, Tuple

LOGGER = logging.getLogger("server.energy")

DEFAULT_STATE_FILE = Path(__file__).resolve().parents[1] / "state" / "energy.json"


class EnergyStore:
    """Thread-safe per-channel energy counters with JSON persistence."""

    def __init__(self, channels: Iterable[str], state_file: Path = DEFAULT_STATE_FILE) -> None:
        self._state_file = Path(state_file)
        self._session: Dict[str, float] = {}
        self._total: Dict[str, float] = {}
        self._lock = threading.Lock()

        stored = self._load()
        # Only keep counters for the channels that currently exist so stale
        # entries from an older config are dropped on the next save.
        for name in {str(channel) for channel in channels}:
            self._session[name] = 0.0
            self._total[name] = stored.get(name, 0.0)

    # -- public API ---------------------------------------------------------

    def add(self, channel: str, power_mw: float, dt_s: float) -> Tuple[float, float]:
        """Integrate ``power_mw`` over ``dt_s``; return (session_mwh, total_mwh)."""
        if channel not in self._total:
            return 0.0, 0.0
        delta_mwh = power_mw * dt_s / 3600.0
        with self._lock:
            session = self._session[channel] + delta_mwh
            total = self._total[channel] + delta_mwh
            self._session[channel] = session
            self._total[channel] = total
            return session, total

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        """Return {channel: {"session_mwh": .., "total_mwh": ..}}."""
        with self._lock:
            return {
                name: {"session_mwh": self._session[name], "total_mwh": self._total[name]}
                for name in sorted(self._total)
            }

    def save(self) -> None:
        """Persist the `total` counters (session is per-run only)."""
        with self._lock:
            payload = {name: round(self._total[name], 6) for name in sorted(self._total)}
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(self._state_file)
        except OSError as exc:
            LOGGER.warning("Could not save energy state %s: %s", self._state_file, exc)

    # -- persistence --------------------------------------------------------

    def _load(self) -> Dict[str, float]:
        if not self._state_file.is_file():
            return {}
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return {str(key): float(value) for key, value in data.items()}
        except (OSError, ValueError) as exc:
            LOGGER.warning("Could not read energy state %s: %s", self._state_file, exc)
            return {}
