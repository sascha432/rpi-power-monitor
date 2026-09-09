"""Per-channel energy counters in milliwatt-hours with JSON persistence.

Energy/power are tracked per channel (physical rails AND aggregate tags).

Three accumulators per channel:
  - session (mWh since this server process started)
  - total   (mWh across all runs - loaded from / saved to the state file)
  - daily   (mWh per calendar day, in the Pi's local time)

``state/energy.json`` is a small versioned document:

.. code-block:: json

    {
      "version": 2,
      "total_mwh": { "<channel>": <all-time mWh>, ... },
      "days": { "YYYY-MM-DD": { "<channel>": <day mWh>, ... }, ... }
    }

The ``days`` map is a rolling per-day log: on every save (and at each
calendar-day rollover) it is pruned to the newest ``storage_days`` days from
``config/server.yaml`` (``energy.storage_days``). Days the server was down are
filled with zero buckets so the log stays contiguous (a powered-off Pi really
used ~0). Legacy v1 files (a flat ``{channel: mWh}`` total map) migrate to v2
automatically on the first save.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, Tuple

LOGGER = logging.getLogger("server.energy")

DEFAULT_STATE_FILE = Path(__file__).resolve().parents[1] / "state" / "energy.json"

#: Schema version written by :meth:`EnergyStore.save` to ``state/energy.json``.
STATE_VERSION = 2


def _iso_today() -> str:
    """Today's date (Pi local time) as ``YYYY-MM-DD`` - a daily bucket key."""
    return date.today().isoformat()


class EnergyStore:
    """Thread-safe per-channel energy counters with JSON persistence."""

    def __init__(
        self,
        channels: Iterable[str],
        state_file: Path = DEFAULT_STATE_FILE,
        storage_days: int = 90,
    ) -> None:
        self._state_file = Path(state_file)
        # Rolling per-day log depth: today + (storage_days - 1) previous days.
        self._storage_days = max(1, int(storage_days))
        self._session: Dict[str, float] = {}
        self._total: Dict[str, float] = {}
        self._days: Dict[str, Dict[str, float]] = {}
        self._today: str = ""  # set below from the last stored day
        self._lock = threading.Lock()

        stored = self._load()
        totals, days = self._split(stored)

        # Only keep counters for the channels that currently exist so stale
        # entries from an older config are dropped on the next save.
        configured = [str(channel) for channel in channels]
        for name in configured:
            self._session[name] = 0.0
            self._total[name] = float(totals.get(name, 0.0))

        # Keep only well-formed daily buckets for channels that still exist.
        for day in sorted(days):
            bucket = days[day]
            if not isinstance(bucket, dict):
                continue
            try:
                date.fromisoformat(str(day))
            except ValueError:
                continue  # ignore a malformed date key rather than crash
            self._days[str(day)] = {
                str(key): float(value)
                for key, value in bucket.items()
                if str(key) in self._total
            }

        # Roll forward from the last stored day to today so any downtime gap
        # becomes zero-filled days, today's bucket is opened (keeps prior
        # same-day data) and the log is pruned to the retention window.
        self._today = max(self._days) if self._days else _iso_today()
        today = _iso_today()
        if today != self._today:
            self._rollover_locked(today)
        else:
            self._days[today] = self._normalise_bucket(self._days.get(today, {}))
            self._prune(today)

    @property
    def storage_days(self) -> int:
        """Retention window for the rolling per-day log (today included)."""
        return self._storage_days

        """Integrate ``power_mw`` over ``dt_s``; return (session_mwh, total_mwh).

        The daily bucket for today (Pi local time) is updated as well. On a
        calendar-day change the store rolls over and prunes old days.
        """
        if channel not in self._total:
            return 0.0, 0.0
        delta_mwh = power_mw * dt_s / 3600.0
        with self._lock:
            today = _iso_today()
            if today != self._today:
                self._rollover_locked(today)
            session = self._session[channel] + delta_mwh
            total = self._total[channel] + delta_mwh
            self._session[channel] = session
            self._total[channel] = total
            self._days[today][channel] = self._days[today].get(channel, 0.0) + delta_mwh
            return session, total

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        """Return {channel: {"session_mwh": .., "total_mwh": ..}}."""
        with self._lock:
            return {
                name: {"session_mwh": self._session[name], "total_mwh": self._total[name]}
                for name in sorted(self._total)
            }

    def last_days(self, n: int = 7) -> Tuple[str, list, Dict[str, float]]:
        """Return ``(today, per_day, totals)`` for the trailing ``n`` days.

        * ``today`` - today's iso date (Pi local time), the newest bucket.
        * ``per_day`` - a list of exactly ``n`` buckets, oldest -> today. Each
          bucket is ``{channel: mWh}`` normalised across the current channels
          (missing channels are 0.0). When the rolling log is younger than
          ``n`` days (fresh install or small ``storage_days``) the leading
          buckets are zero-filled so callers can assume index ``n - 1`` is
          today and the dates are contiguous.
        * ``totals`` - the all-time ``{channel: mWh}`` at the same instant, so
          a caller can pair today's bucket with a consistent live-update
          anchor (they advance together in :meth:`add`).

        Rolls the store forward / prunes first, exactly like :meth:`save`.
        """
        with self._lock:
            today = _iso_today()
            if today != self._today:
                self._rollover_locked(today)
            self._prune(today)
            try:
                cursor = date.fromisoformat(today)
            except (TypeError, ValueError):
                cursor = date.today()
            cursor = cursor - timedelta(days=n - 1)
            per_day: list = []
            for _ in range(n):
                per_day.append(self._normalise_bucket(self._days.get(cursor.isoformat(), {})))
                cursor += timedelta(days=1)
            totals = {name: self._total[name] for name in sorted(self._total)}
            return today, per_day, totals


    def save(self) -> None:
        """Persist the all-time totals + the rolling per-day log."""
        with self._lock:
            today = _iso_today()
            if today != self._today:
                self._rollover_locked(today)
            payload = {
                "version": STATE_VERSION,
                "total_mwh": {
                    name: round(self._total[name], 6) for name in sorted(self._total)
                },
                "days": {
                    day: self._normalise_bucket(bucket)
                    for day, bucket in sorted(self._days.items())
                },
            }
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(self._state_file)
        except OSError as exc:
            LOGGER.warning("Could not save energy state %s: %s", self._state_file, exc)

    # -- persistence --------------------------------------------------------

    def _load(self):
        if not self._state_file.is_file():
            return {}
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOGGER.warning("Could not read energy state %s: %s", self._state_file, exc)
            return {}

    @staticmethod
    def _split(stored) -> Tuple[Dict[str, float], Dict[str, dict]]:
        """Separate (totals, per-day log); migrate the legacy flat format."""
        if not isinstance(stored, dict):
            return {}, {}
        if (
            "total_mwh" in stored
            or "days" in stored
            or stored.get("version") is not None
        ):
            totals = stored.get("total_mwh") or {}
            days = stored.get("days") or {}
            return (
                totals if isinstance(totals, dict) else {},
                days if isinstance(days, dict) else {},
            )
        # Legacy v1 layout: flat {channel: all-time mWh}.
        return stored, {}

    # -- day rolling --------------------------------------------------------

    def _normalise_bucket(self, bucket: Dict[str, float]) -> Dict[str, float]:
        """Return ``bucket`` restricted to current channels (missing => 0)."""
        return {
            name: round(float(bucket.get(name, 0.0)), 6) for name in sorted(self._total)
        }

    def _rollover_locked(self, today: str) -> None:
        """Close the previous day and open ``today`` (caller holds the lock)."""
        try:
            cursor = date.fromisoformat(self._today) + timedelta(days=1)
        except (TypeError, ValueError):
            cursor = date.fromisoformat(today)
        target = date.fromisoformat(today)
        # Fill whole days the server was down with zero buckets so the per-day
        # log stays contiguous.
        while cursor < target:
            self._days.setdefault(cursor.isoformat(), self._normalise_bucket({}))
            cursor += timedelta(days=1)
        self._today = today
        self._days[today] = self._normalise_bucket(self._days.get(today, {}))
        self._prune(today)

    def _prune(self, today: str) -> None:
        """Drop daily buckets older than the ``storage_days`` retention window."""
        if self._storage_days <= 0:
            return
        try:
            cutoff = (
                date.fromisoformat(today) - timedelta(days=self._storage_days - 1)
            ).isoformat()
        except (TypeError, ValueError):
            return
        self._days = {
            day: bucket for day, bucket in self._days.items() if day >= cutoff
        }
