"""Wire channel catalog derived from the server's ``sensor.shunt`` table.

The binary TCP stream carries **numeric channel ids only, never names** (see
``shared.binary``), so the Pi server and every consumer must agree on how
those ids map to channel names. Both sides derive that mapping here, from
``config/server.yaml``, so they can never drift:

* **Rails** (INA3221 inputs) keep their chip number as the wire id
  (``sensor.shunt[].channel``, normally 1..3); the name is
  ``sensor.shunt[].name``.
* **Aggregates** group rails by their ``sensor.shunt[].aggregate`` tag. Tags
  are de-duplicated and *sorted*; the tag at index ``i`` gets wire id
  ``AGGREGATE_ID_BASE + i`` and keeps the tag itself as its name.

Every sample emits rails first (ascending chip channel), then aggregates
(ascending tag) - that order, and the ids, are part of the TCP protocol
(``docs/protocol.md``).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

#: First wire id handed to an aggregate channel: ``AGGREGATE_ID_BASE + index``
#: over the *sorted* aggregate tags. Rail ids are the INA3221 channel numbers
#: (1..3), so 100+ can never collide with them.
AGGREGATE_ID_BASE = 100

DEFAULT_SERVER_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "server.yaml"
)


@dataclass(frozen=True)
class ChannelInfo:
    """One published wire channel: its id, display name and kind."""

    id: int
    name: str
    kind: str            # "rail" | "aggregate"
    aggregate: str = ""  # rails: the aggregate tag this rail feeds


def _value(item: Any, key: str) -> Optional[Any]:
    """Read ``key`` from a mapping or a plain object (both are supported)."""
    if isinstance(item, Mapping):
        return item.get(key)
    return getattr(item, key, None)


def _normalise(shunts: Iterable[Any]) -> List[Dict[str, Any]]:
    """Shunt entries as plain dicts with the same defaults as the server.

    ``channel`` defaults to the 1-based YAML position, ``name`` to ``chN``
    and ``aggregate`` to ``"output"`` (mirrors ``server.config``).
    """
    items: List[Dict[str, Any]] = []
    for index, item in enumerate(shunts):
        channel = _value(item, "channel")
        name = _value(item, "name")
        aggregate = _value(item, "aggregate")
        items.append(
            {
                "channel": index + 1 if channel is None else int(channel),
                "name": f"ch{index + 1}" if name is None else str(name),
                "aggregate": "output" if aggregate is None else str(aggregate),
            }
        )
    return items


def aggregate_tags(shunts: Iterable[Any]) -> List[str]:
    """Sorted, de-duplicated aggregate tags (drives the aggregate wire ids)."""
    return sorted({entry["aggregate"] for entry in _normalise(shunts)})


def build_catalog(shunts: Iterable[Any]) -> List[ChannelInfo]:
    """Rails (ascending chip channel) followed by aggregates (ascending tag)."""
    rails = sorted(_normalise(shunts), key=lambda entry: entry["channel"])
    catalog: List[ChannelInfo] = [
        ChannelInfo(
            id=entry["channel"],
            name=entry["name"],
            kind="rail",
            aggregate=entry["aggregate"],
        )
        for entry in rails
    ]
    for index, tag in enumerate(aggregate_tags(rails)):
        catalog.append(
            ChannelInfo(id=AGGREGATE_ID_BASE + index, name=tag, kind="aggregate")
        )
    return catalog


def load_catalog(path: Path = DEFAULT_SERVER_CONFIG_PATH) -> List[ChannelInfo]:
    """Load a ``server.yaml`` and return its derived wire channel catalog.

    Only ``sensor.shunt`` is read, so this works on any machine that has the
    server config (the dashboard uses it to name the ids it receives).
    """
    with Path(path).open("r", encoding="utf-8") as fh:
        data: Dict[str, Any] = yaml.safe_load(fh) or {}
    sensor = data.get("sensor") or {}
    return build_catalog(sensor.get("shunt") or [])
