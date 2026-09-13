"""Typed loader for ``config/client.yaml``.

The YAML file is the operator-facing source of truth; the dataclasses below
mirror its schema so the web dashboard never touches raw dicts.

The client is a **stdlib web dashboard**:

* it connects to the Pi's raw TCP binary stream (``shared.binary`` frames),
* keeps the latest reading per channel, and
* serves an HTML/JS page over HTTP + WebSocket. The WebSocket delivers the UI
  "catalog" (channels / metric metadata / tab title / cadence), the Pi's
  daily-energy totals and live samples; the rolling chart buffers live in the
  browser. The purely visual defaults live in the browser
  (``client/web/static/app.js``) and each visitor's UI choices are stored in a
  cookie.

The **channel set is not configured here**: the binary wire carries ids only,
so the ids/names are derived from the server's own ``server.yaml``
(``shared.catalog``) - see ``server_config`` below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from shared.catalog import (
    DEFAULT_SERVER_CONFIG_PATH,
    ChannelInfo,
    load_catalog,
)

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
class WebConfig:
    host: str = "0.0.0.0"   # bind address for the dashboard HTTP + WebSocket server
    port: int = 8080        # point a browser at http://<host>:<port>/
    allowed_clients: List[str] = field(default_factory=list)  # empty => localhost only

@dataclass
class DisplayConfig:
    title: str = "Power Monitor"     # browser tab title
    update_ms: int = 500             # WebSocket sample-push cadence
    # NOTE: the visual UI defaults (metric, theme, energy unit, ...) are NOT
    # configured here - they are browser-local constants in
    # client/web/static/app.js, persisted per visitor in the pwm_settings
    # cookie.


@dataclass
class ClientConfig:
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    web: WebConfig = field(default_factory=WebConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    #: Channel catalog derived from the server config (rails + aggregates).
    channels: List[ChannelInfo] = field(default_factory=list)
    #: Resolved path of the ``server.yaml`` the catalog came from.
    server_config: Path = DEFAULT_SERVER_CONFIG_PATH

    def channel_map(self) -> Dict[int, ChannelInfo]:
        """Channel lookup by wire id (raises on duplicate ids)."""
        mapping: Dict[int, ChannelInfo] = {}
        for channel in self.channels:
            if channel.id in mapping:
                raise ValueError(f"duplicate channel id {channel.id}")
            mapping[channel.id] = channel
        return mapping


def load_config(
    path: Path = DEFAULT_CONFIG_PATH,
    server_config_path: Optional[Path] = None,
) -> ClientConfig:
    """Load ``path`` (YAML) and return a validated :class:`ClientConfig`.

    The channel catalog is read from the *server* config: ``server_config_path``
    when given (CLI override), else the client YAML's ``server_config`` key
    (resolved relative to ``path``), else the repo default ``config/server.yaml``.
    """
    path = Path(path)
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

    # Channel ids/names come from the server config (server.yaml): the binary
    # wire carries ids only. A CLI override wins; otherwise client.yaml's
    # ``server_config`` is used (relative paths resolve next to client.yaml).
    if server_config_path is not None:
        server_path = Path(server_config_path)
    elif data.get("server_config"):
        candidate = Path(str(data["server_config"]))
        server_path = candidate if candidate.is_absolute() else path.parent / candidate
    else:
        server_path = DEFAULT_SERVER_CONFIG_PATH
    if not server_path.is_file():
        raise FileNotFoundError(
            f"server config not found: {server_path} (set 'server_config' in "
            f"{path} or pass --server-config)"
        )
    channels = load_catalog(server_path)

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
            update_ms=_to_int(display.get("update_ms"), 500),
        ),
        channels=channels,
        server_config=server_path,
    )
