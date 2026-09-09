"""Typed loader for ``config/server.yaml``.

The YAML file is the operator-facing source of truth; the dataclasses below
mirror its schema so the rest of the server never touches raw dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "server.yaml"


@dataclass
class I2cConfig:
    bus: int = 1
    address: int = 0x40  # YAML may spell this as a string "0x40"


@dataclass
class ShuntChannel:
    channel: int  # INA3221 input number, 1..3
    name: str
    voltage: float  # nominal rail voltage (V), e.g. 12.0 or 5.0
    shunt_milliohm: float
    aggregate: str = "output"  # rail grouping: "input" or "output"


@dataclass
class SensorConfig:
    type: str = "ina3221"
    i2c: I2cConfig = field(default_factory=I2cConfig)
    shunt: List[ShuntChannel] = field(default_factory=list)


@dataclass
class SamplingConfig:
    averaging: int = 16  # 1|4|16|64|128|256|1024
    bus_conversion_us: int = 1100
    shunt_conversion_us: int = 1100
    # NOTE: no interval_s - the poll period is derived from the chip timing:
    #   interval = averaging * (bus_conversion_us + shunt_conversion_us)
    # (see sensor.ina3221.Ina3221.expected_interval_s)


@dataclass
class LoggingConfig:
    level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR
    format: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"


@dataclass
class MqttChannelConfig:
    name: str  # matches a rail name or an aggregate tag
    topic: str  # topic slug, e.g. "12v_output"
    units: List[str] = field(default_factory=list)  # e.g. ["V","A","W","kWh"]


@dataclass
class MqttConfig:
    # Empty host (YAML `host: ~`) or `port: 0` disables MQTT entirely.
    host: str = "localhost"
    port: int = 1883
    update_interval: int = 30  # seconds between MQTT publishes
    topic_prefix: str = "home"
    base_topic: str = ""  # e.g. "acidpi4_power_monitor"
    auto_discovery: bool = True  # publish Home Assistant discovery configs
    auto_discovery_prefix: str = "homeassistant"
    channels: List[MqttChannelConfig] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        """True only when a broker host is set on a non-zero port."""
        return bool(self.host) and self.port > 0


@dataclass
class ServerConfig:
    # ``server:`` section
    name: str = "rpi-power-monitor"
    bind_host: str = "0.0.0.0"
    bind_port: int = 7000
    max_clients: int = 8
    allowed_clients: List[str] = field(default_factory=list)  # empty = localhost only
    # sub-sections
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    sensor: SensorConfig = field(default_factory=SensorConfig)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> ServerConfig:
    """Load ``path`` (YAML) and return a validated :class:`ServerConfig`."""
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

    def _to_address(value: Any, default: int) -> int:
        # accepts "0x40", "0X40", "64" or an already-parsed int
        if isinstance(value, int):
            return value
        text = str(value).strip()
        try:
            return int(text, 0)  # auto-detect 0x/0o/0b prefixes
        except ValueError:
            return int(text, 16)

    server = data.get("server") or {}
    logging_cfg = data.get("logging") or {}
    sensor = data.get("sensor") or {}
    sampling = data.get("sampling") or {}
    mqtt = data.get("mqtt") or {}
    i2c = sensor.get("i2c") or {}

    # A YAML null (`host: ~`) must map to an empty host (disables MQTT), not
    # the string "None" - which would be truthy and accidentally enable it.
    mqtt_host = mqtt.get("host", "localhost")  # absent => default "localhost"
    if mqtt_host is None:
        mqtt_host = ""  # explicit null => disabled

    return ServerConfig(
        name=str(server.get("name", "rpi-power-monitor")),
        bind_host=str(server.get("bind_host", "0.0.0.0")),
        bind_port=_to_int(server.get("bind_port"), 7000),
        max_clients=_to_int(server.get("max_clients"), 8),
        allowed_clients=[
            str(item).strip()
            for item in (server.get("allowed_clients") or [])
            if str(item).strip()
        ],
        logging=LoggingConfig(
            level=str(logging_cfg.get("level", "INFO")),
            format=str(
                logging_cfg.get(
                    "format", "%(asctime)s %(levelname)s %(name)s: %(message)s"
                )
            ),
        ),
        sensor=SensorConfig(
            type=str(sensor.get("type", "ina3221")),
            i2c=I2cConfig(
                bus=_to_int(i2c.get("bus"), 1),
                address=_to_address(i2c.get("address"), 0x40),
            ),
            shunt=[
                ShuntChannel(
                    channel=_to_int(item.get("channel"), idx + 1),
                    name=str(item.get("name", f"ch{idx + 1}")),
                    voltage=_to_float(item.get("voltage"), 0.0),
                    shunt_milliohm=_to_float(item.get("shunt_milliohm"), 0.0),
                    aggregate=str(item.get("aggregate", "output")),
                )
                for idx, item in enumerate(sensor.get("shunt") or [])
            ],
        ),
        sampling=SamplingConfig(
            averaging=_to_int(sampling.get("averaging"), 16),
            bus_conversion_us=_to_int(sampling.get("bus_conversion_us"), 1100),
            shunt_conversion_us=_to_int(sampling.get("shunt_conversion_us"), 1100),
        ),
        mqtt=MqttConfig(
            host=str(mqtt_host),  # "" when host is null (YAML `host: ~`) => disabled
            port=_to_int(mqtt.get("port"), 1883),
            update_interval=_to_int(mqtt.get("update_interval"), 30),
            topic_prefix=str(mqtt.get("topic_prefix", "home")),
            base_topic=str(mqtt.get("base_topic", "")),
            auto_discovery=_to_bool(mqtt.get("auto_discovery"), True),
            auto_discovery_prefix=str(
                mqtt.get("auto_discovery_prefix", "homeassistant")
            ),
            channels=[
                MqttChannelConfig(
                    name=str(item.get("name", "")),
                    topic=str(item.get("topic", "")),
                    units=[str(u) for u in (item.get("units") or [])],
                )
                for item in (mqtt.get("channels") or [])
            ],
        ),
    )
