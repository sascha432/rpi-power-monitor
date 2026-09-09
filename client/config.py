"""Typed loader for ``config/client.yaml``.

The YAML file is the operator-facing source of truth; the dataclasses below
mirror its schema so the web dashboard never touches raw dicts.

The client is a **stdlib web dashboard**:

* it connects to the Pi's raw TCP binary stream (``shared.binary`` frames),
* keeps a rolling in-memory history per channel, and
* serves an HTML/JS page over HTTP + WebSocket. The WebSocket delivers the UI
  "catalog" (available channels / metrics / units / theme / cadence, built from
  this file) plus history and live samples; the browser stores the user's UI
  choices in a cookie.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "client.yaml"

# Metrics a channel of a given kind publishes on the wire (server semantics).
# Rails report voltage + current + power; aggregates report power only
# (voltage/current are 0 on aggregate frames). Every channel also carries
# session/total energy (decoded to Wh).
RAIL_METRICS: Tuple[str, ...] = ("voltage_v", "current_a", "power_w")
AGGREGATE_METRICS: Tuple[str, ...] = ("power_w",)


def metrics_for(kind: str) -> List[str]:
    """Metric keys that a channel of ``kind`` publishes (for the UI catalog)."""
    if kind == "aggregate":
        return list(AGGREGATE_METRICS)
    return list(RAIL_METRICS)


@dataclass
class ConnectionConfig:
    host: str = "192.168.0.4"   # TCP server = the Raspberry Pi
    port: int = 7000            # must match server.bind_port
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 10.0  # reconnect if no frame arrives within this time
    reconnect: bool = True        # keep retrying while the server is away
    reconnect_delay_s: float = 2.0


@dataclass
class ChannelConfig:
    id: int              # wire channel_id: rails 1..3, aggregates 100+ (see server.yaml)
    name: str            # matches the server's rail name / aggregate tag
    label: str = ""      # friendly name for the UI; defaults to ``name``
    kind: str = "rail"   # "rail" | "aggregate"
    aggregate: str = ""  # for rails: the aggregate tag this rail feeds (informational)

    @property
    def display_name(self) -> str:
        return self.label or self.name

    @property
    def metrics(self) -> List[str]:
        return metrics_for(self.kind)


@dataclass
class WebConfig:
    host: str = "0.0.0.0"   # bind address for the dashboard HTTP + WebSocket server
    port: int = 8080        # point a browser at http://<host>:<port>/
    allowed_clients: List[str] = field(default_factory=list)  # empty => localhost only

@dataclass
class DisplayConfig:
    title: str = "Power Monitor"
    default_metric: str = "power_w"   # metric selected on first visit
    update_ms: int = 500              # WebSocket sample-push cadence
    history_points: int = 3600        # per-channel rolling sample count kept
    energy_unit: str = "kWh"          # Wh | kWh (display only; server sends Wh)
    theme: str = "dark"               # default UI theme ("dark" | "light")
    theme_options: List[str] = field(default_factory=lambda: ["dark", "light"])
    energy_units: List[str] = field(default_factory=lambda: ["Wh", "kWh"])


@dataclass
class ClientConfig:
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    web: WebConfig = field(default_factory=WebConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    channels: List[ChannelConfig] = field(default_factory=list)

    def channel_map(self) -> Dict[int, ChannelConfig]:
        """Channel lookup by wire id (raises on duplicate ids)."""
        mapping: Dict[int, ChannelConfig] = {}
        for channel in self.channels:
            if channel.id in mapping:
                raise ValueError(f"duplicate client channel id {channel.id}")
            mapping[channel.id] = channel
        return mapping


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> ClientConfig:
    """Load ``path`` (YAML) and return a validated :class:`ClientConfig`."""
    with path.open("r", encoding="utf-8") as fh:
        data: Dict[str, Any] = yaml.safe_load(fh) or {}

    def _to_int(value: Any, default: int) -> int:
        return default if value is None else int(value)

    def _to_float(value: Any, default: float) -> float:
        return default if value is None else float(value)

    def _to_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return default

    connection = data.get("connection") or {}
    web = data.get("web") or {}
    display = data.get("display") or {}
    channels = data.get("channels") or []

    return ClientConfig(
        connection=ConnectionConfig(
            host=str(connection.get("host", "192.168.0.4")),
            port=_to_int(connection.get("port"), 7000),
            connect_timeout_s=_to_float(connection.get("connect_timeout_s"), 5.0),
            read_timeout_s=_to_float(connection.get("read_timeout_s"), 10.0),
            reconnect=_to_bool(connection.get("reconnect"), True),
            reconnect_delay_s=_to_float(connection.get("reconnect_delay_s"), 2.0),
        ),
        web=WebConfig(
            host=str(web.get("host", "0.0.0.0")),
            port=_to_int(web.get("port"), 8080),
            allowed_clients=[
                str(item).strip()
                for item in (web.get("allowed_clients") or [])
                if str(item).strip()
            ],
        ),
        display=DisplayConfig(
            title=str(display.get("title", "Power Monitor")),
            default_metric=str(display.get("default_metric", "power_w")),
            update_ms=_to_int(display.get("update_ms"), 500),
            history_points=_to_int(display.get("history_points"), 3600),
            energy_unit=str(display.get("energy_unit", "kWh")),
            theme=str(display.get("theme", "dark")),
            theme_options=[
                str(item) for item in (display.get("theme_options") or ["dark", "light"])
            ]
            or ["dark", "light"],
            energy_units=[
                str(item) for item in (display.get("energy_units") or ["Wh", "kWh"])
            ]
            or ["Wh", "kWh"],
        ),
        channels=[
            ChannelConfig(
                id=_to_int(item.get("id"), idx + 1),
                name=str(item.get("name", f"channel_{idx + 1}")),
                label=str(item.get("label", "")),
                kind=str(item.get("kind", "rail")),
                aggregate=str(item.get("aggregate", "")),
            )
            for idx, item in enumerate(channels)
        ],
    )
