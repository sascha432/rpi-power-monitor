"""MQTT publishing of averaged per-channel readings + Home Assistant discovery.

The sensor is sampled far faster than the publish cadence (``update_interval``
may be up to an hour), so instantaneous samples (voltage / current / power) are
averaged with running **sum + count** accumulators - nothing is stored per
sample. ``energy_total`` is a cumulative meter, so its *latest* value is
published (converted to the channel's configured unit, default kWh).

One JSON object per channel is published (retained) to
``<topic_prefix>/<base_topic>/<channel topic>``, for example::

    home/acidpi4_power_monitor/12v_output
    {"voltage": 12.05, "current": 1.203, "power": 14.46,
     "energy_total": 0.123456,
     "units": {"voltage": "V", "current": "A", "power": "W",
               "energy_total": "kWh"}}

Home Assistant auto-discovery is published (retained) at startup / on every
(Re)connect to ``<discovery_prefix>/sensor/<node_id>/<object_id>/config``, one
sensor per metric, each reading ``{{ value_json.<metric> }}`` from the JSON
value topic. An LWT availability topic (``<root>/status``) is used so HA marks
the device unavailable when the server is gone.
"""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Dict, List, Optional

LOGGER = logging.getLogger("server.mqtt")

_ALLOWED_ENERGY_UNITS = {"mwh", "wh", "kwh"}
_ENERGY_UNIT_DISPLAY = {"mwh": "mWh", "wh": "Wh", "kwh": "kWh"}

# metric -> Home Assistant sensor metadata for the averaged instantaneous fields
_METRIC_SENSOR = {
    "voltage": {
        "title": "Voltage",
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "precision": 2,
        "icon": "mdi:sine-wave",
    },
    "current": {
        "title": "Current",
        "unit": "A",
        "device_class": "current",
        "state_class": "measurement",
        "precision": 2,
        "icon": "mdi:current-dc",
    },
    "power": {
        "title": "Power",
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
        "precision": 2,
        "icon": "mdi:flash-outline",
    },
}
_ENERGY_SENSOR = {
    "title": "Energy",
    "device_class": "energy",
    "state_class": "total",  # net energy may go backwards (signed), so not total_increasing
    "precision": 2,
    "icon": "mdi:counter",
}


def energy_mwh_to(value_mwh: float, unit: str) -> float:
    """Convert milliwatt-hours into the (already normalised) ``unit``."""
    if unit == "kwh":
        return value_mwh / 1_000_000.0
    if unit == "wh":
        return value_mwh / 1000.0
    return value_mwh  # mwh


def normalise_energy_unit(unit: Optional[str]) -> str:
    u = (unit or "kWh").strip().lower()
    return u if u in _ALLOWED_ENERGY_UNITS else "kwh"


def slugify(name: str) -> str:
    """Turn a channel name into a topic slug, e.g. '12V Output' -> '12v_output'."""
    out = [c if c.isalnum() else "_" for c in name.strip().lower()]
    return "".join(out).strip("_")


_SI_PREFIX = {"": 1.0, "m": 1e-3, "k": 1e3, "M": 1e6, "u": 1e-6, "µ": 1e-6}


def unit_dimension(unit: str) -> Optional[str]:
    """Classify an MQTT unit token: voltage | current | power | energy | None."""
    token = (unit or "").strip().lower()
    if token.endswith("wh"):
        return "energy"
    if token.endswith("a"):
        return "current"
    if token.endswith("v"):
        return "voltage"
    if token.endswith("w"):
        return "power"
    return None


def scale_si_to_unit(value: float, unit: str) -> float:
    """Scale a RAW SI value (V / A / W) into the configured unit token.

    E.g. ``scale_si_to_unit(5.104, "mV") == 5104.0``, ``scale_si_to_unit(5.104, "kV")``
    is ~0.0051. Unknown tokens return the raw value unchanged.
    """
    token = (unit or "").strip()
    if not token or token[-1].upper() not in "VAW":
        return value
    factor = _SI_PREFIX.get(token[:-1].lower())
    return value / factor if factor is not None else value


class MqttTarget:
    """One channel: running averages + latest energy, published to one topic."""

    def __init__(
        self,
        key: str,
        topic: str,
        averaged_metrics: Dict[str, str],
        energy_unit: Optional[str] = "kWh",
        label: Optional[str] = None,
    ) -> None:
        self.key = key
        self.topic = topic
        self.label = label or key
        self.metric_units = dict(averaged_metrics)  # metric -> unit token (V/mV/A/...)
        self.energy_unit = normalise_energy_unit(energy_unit)  # internal (kwh)
        self.energy_unit_display = _ENERGY_UNIT_DISPLAY[self.energy_unit]  # e.g. "kWh"
        self._sums: Dict[str, float] = {}
        self._counts: Dict[str, int] = {}
        self._latest_energy_mwh: float = 0.0
        self._has_sample: bool = False

    def add(
        self,
        metrics: Dict[str, float],
        energy_total_mwh: Optional[float] = None,
    ) -> None:
        """Accumulate one sample; energy_total is kept as the latest value."""
        for metric, value in metrics.items():
            if metric not in self.metric_units:
                continue
            self._sums[metric] = self._sums.get(metric, 0.0) + float(value)
            self._counts[metric] = self._counts.get(metric, 0) + 1
            self._has_sample = True
        if energy_total_mwh is not None:
            self._latest_energy_mwh = float(energy_total_mwh)

    def reset_averages(self) -> None:
        self._sums.clear()
        self._counts.clear()

    @property
    def has_sample(self) -> bool:
        """True once at least one real measurement sample was accumulated."""
        return self._has_sample

    def build_payload(self) -> Dict:
        payload: Dict = {}
        for metric in self.metric_units:
            count = self._counts.get(metric, 0)
            if count:
                # Average is kept in RAW SI (V/A/W); scale to the unit selected
                # for this channel before publishing.
                average = self._sums[metric] / count
                payload[metric] = round(
                    scale_si_to_unit(average, self.metric_units[metric]), 4
                )
        payload["energy_total"] = round(
            energy_mwh_to(self._latest_energy_mwh, self.energy_unit), 6
        )
        # Only list units whose values are actually present in this payload.
        units = {
            metric: self.metric_units[metric]
            for metric in payload
            if metric != "energy_total"
        }
        units["energy_total"] = self.energy_unit_display
        payload["units"] = units
        return payload


class MqttPublisher:
    """Connects to a broker, publishes averaged JSON per channel, and announces
    the channels to Home Assistant via MQTT discovery on (re)connect."""

    def __init__(
        self,
        host: str,
        port: int,
        topic_root: str,
        targets: List[MqttTarget],
        update_interval_s: float = 30.0,
        retain: bool = True,
        discovery: bool = False,
        discovery_prefix: str = "homeassistant",
        node_id: str = "power_monitor",
        device_name: str = "Power Monitor",
        sensor_model: str = "ina3221",
    ) -> None:
        self._host = host
        self._port = port
        self._root = topic_root.strip("/")
        self._targets = targets
        self._by_key = {target.key: target for target in targets}
        self._interval = float(update_interval_s)
        self._retain = retain
        self._client = None
        self._last_publish = time.monotonic()

        self._discovery = discovery
        self._discovery_prefix = discovery_prefix.strip("/")
        self._node_id = node_id or slugify(device_name)
        self._device_name = device_name
        self._sensor_model = sensor_model
        self._status_topic = f"{self._root}/status".strip("/")

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        """Start the paho client (daemon network loop). Returns False on error."""
        try:
            import paho.mqtt.client as mqtt  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dev machines
            LOGGER.warning("paho-mqtt not installed; MQTT publishing disabled (%s)", exc)
            return False

        client_id = f"power-monitor-{random.randrange(0x10000):04x}"
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
        except (AttributeError, TypeError):  # older paho-mqtt
            client = mqtt.Client(client_id=client_id)

        if self._root:
            client.will_set(
                self._status_topic, "offline", qos=1, retain=True
            )
        client.on_connect = self._on_connect
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        try:
            client.connect_async(self._host, self._port, keepalive=60)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("MQTT connect to %s:%d failed: %s", self._host, self._port, exc)
            return False
        client.loop_start()
        self._client = client
        LOGGER.info(
            "MQTT publisher %s:%d root='%s' targets=%d interval=%ss discovery=%s node=%s",
            self._host, self._port, self._root, len(self._targets),
            self._interval, self._discovery, self._node_id,
        )
        return True

    def _on_connect(self, client, userdata, flags, rc) -> None:  # paho callback
        if rc != 0:
            LOGGER.warning("MQTT connection failed, rc=%s (will retry)", rc)
            return
        LOGGER.info("MQTT connected to %s:%d", self._host, self._port)
        if self._root:
            client.publish(self._status_topic, "online", qos=1, retain=True)
        if self._discovery:
            self._publish_discovery()
        # Seed retained values so Home Assistant has a state right away.
        self._publish_snapshot()

    def stop(self) -> None:
        if self._client is not None:
            if self._root:
                try:
                    self._client.publish(self._status_topic, "offline", qos=1, retain=True)
                except Exception:  # noqa: BLE001
                    pass
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # -- sample feed ---------------------------------------------------------

    def feed(
        self,
        key: str,
        metrics: Optional[Dict[str, float]] = None,
        energy_total_mwh: Optional[float] = None,
    ) -> None:
        target = self._by_key.get(key)
        if target is not None:
            target.add(metrics or {}, energy_total_mwh)

    def publish_due(self, now_mono: Optional[float] = None) -> None:
        """Publish (and reset) once at least ``update_interval`` has passed."""
        now = now_mono if now_mono is not None else time.monotonic()
        if now - self._last_publish < self._interval:
            return
        self._last_publish = now
        self.publish_now()

    def publish_now(self) -> None:
        if self._client is None:
            return
        for target in self._targets:
            if not target.has_sample:
                continue  # nothing real measured yet - do not publish a zero stub
            topic = self._value_topic(target)
            payload = json.dumps(target.build_payload())
            try:
                self._client.publish(topic, payload, qos=0, retain=self._retain)
            except Exception as exc:  # noqa: BLE001 - broker hiccup
                LOGGER.warning("MQTT publish %s failed: %s", topic, exc)
            finally:
                target.reset_averages()

    def _publish_snapshot(self) -> None:
        """Publish the current state without resetting the averages (used on connect)."""
        if self._client is None:
            return
        for target in self._targets:
            if not target.has_sample:
                continue  # nothing real measured yet - do not publish a zero stub
            topic = self._value_topic(target)
            try:
                self._client.publish(
                    topic, json.dumps(target.build_payload()), qos=0, retain=self._retain
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("MQTT snapshot %s failed: %s", topic, exc)

    def _value_topic(self, target: MqttTarget) -> str:
        return f"{self._root}/{target.topic}".strip("/")

    # -- Home Assistant discovery -------------------------------------------

    def _publish_discovery(self) -> None:
        """Publish retained discovery configs, one sensor per metric per channel."""
        if self._client is None:
            return
        for target in self._targets:
            channel_slug = slugify(target.topic) or slugify(target.key)
            for metric in list(target.metric_units) + ["energy_total"]:
                topic = (
                    f"{self._discovery_prefix}/sensor/{self._node_id}/"
                    f"{channel_slug}_{metric}/config"
                )
                payload = self._sensor_config(target, metric)
                try:
                    self._client.publish(topic, json.dumps(payload), qos=1, retain=True)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("MQTT discovery %s failed: %s", topic, exc)
        LOGGER.info(
            "Published HA discovery for %d channel(s) under '%s'",
            len(self._targets), self._discovery_prefix,
        )

    def _sensor_config(self, target: MqttTarget, metric: str) -> Dict:
        if metric == "energy_total":
            meta = dict(_ENERGY_SENSOR)
            meta["unit"] = _ENERGY_UNIT_DISPLAY[target.energy_unit]
        else:
            meta = dict(_METRIC_SENSOR[metric])
            unit = target.metric_units.get(metric)
            meta["unit"] = unit or meta["unit"]

        config: Dict = {
            "name": f"{target.label} {meta['title']}",
            "state_topic": self._value_topic(target),
            "value_template": "{{ value_json.%s }}" % metric,
            "unit_of_measurement": meta["unit"],
            "device_class": meta["device_class"],
            "state_class": meta["state_class"],
            "suggested_display_precision": meta["precision"],
            "icon": meta["icon"],
            "unique_id": f"{self._node_id}_{target.topic}_{metric}",
            "object_id": f"{self._node_id}_{target.topic}_{metric}",
            "qos": 0,
            "availability_topic": self._status_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {
                "identifiers": [self._node_id],
                "name": self._device_name,
                "model": self._sensor_model,
                "manufacturer": "rpi-power-monitor",
            },
        }
        return config
