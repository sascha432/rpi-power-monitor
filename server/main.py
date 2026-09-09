"""Entry point: read the INA3221 and publish binary TCP frames.

Runs on the Raspberry Pi. The poll cadence is derived from the chip timing
(averaging * (bus + shunt conversion time)), so every channel is read as fast
as the chip can deliver a fresh averaged value.

Data model
----------
* Physical rails (INA3221 inputs) report voltage + current + power and their
  own energy counters.
* Aggregated channels (rails grouped by their ``aggregate`` tag) report
  **power + energy only** (voltage/current are 0): their power is the sum of
  the member rails' instantaneous power and energy is integrated from it.
* Energy is tracked per channel (rails AND aggregates) in milliwatt-hours
  with two counters: *session* (since this run) and *total* (persisted across
  restarts in ``state/energy.json``).
* Every sample is broadcast to TCP clients as one fixed-size binary frame per
  channel (see ``shared.binary``) - little-endian and layout-identical to a C
  struct on x86-64, so a C++ client can memcpy without conversion.
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Dict, List, Optional

from shared.binary import FRAME_SIZE, pack_channel

from .config import DEFAULT_CONFIG_PATH, ServerConfig, load_config
from .energy import DEFAULT_STATE_FILE, EnergyStore
from .mqtt import MqttPublisher, MqttTarget, slugify, unit_dimension
from .net.tcp_server import TcpServer
from .sensor.ina3221 import Ina3221

LOGGER = logging.getLogger("server.main")

AGGREGATE_ID_BASE = 100  # aggregate channel ids = 100 + index (rails keep 1..3)
SAVE_EVERY_CYCLES = 20   # persist energy totals every N samples


def _millis() -> int:
    """uint32 ms since the process started (wrap-safe, ~49.7 days)."""
    return int(time.monotonic() * 1000.0) & 0xFFFFFFFF


def _build_mqtt_publisher(
    cfg: ServerConfig,
    rails: list,
    tags: List[str],
) -> Optional[MqttPublisher]:
    """Build the MQTT publisher from ``cfg.mqtt`` (None if not usable).

    Every measured channel gets one averaged JSON topic. Values are averaged
    over ``mqtt.update_interval`` (running sum/count, no per-sample storage);
    ``energy_total`` publishes the latest meter value in the channel's
    configured energy unit (last entry of ``channels[].units``).
    """
    if not cfg.mqtt.enabled or cfg.mqtt.update_interval <= 0:
        return None

    by_name = {channel.name: channel for channel in cfg.mqtt.channels}

    def _units_for(channel, dimension: str, default: str) -> str:
        """Pick the user-selected unit token for ``dimension`` from units[]."""
        if channel is not None and channel.units:
            for token in channel.units:
                if unit_dimension(token) == dimension:
                    return token
        return default

    def _metric_units(channel, rail: bool) -> Dict[str, str]:
        if rail:
            return {
                "voltage": _units_for(channel, "voltage", "V"),
                "current": _units_for(channel, "current", "A"),
                "power": _units_for(channel, "power", "W"),
            }
        return {"power": _units_for(channel, "power", "W")}

    targets: List[MqttTarget] = []
    for item in rails:  # rails: voltage + current + power + energy_total
        channel = by_name.get(item.name)
        targets.append(
            MqttTarget(
                key=item.name,
                label=item.name,
                topic=channel.topic if channel and channel.topic else slugify(item.name),
                averaged_metrics=_metric_units(channel, True),
                energy_unit=_units_for(channel, "energy", "kWh"),
            )
        )
    for tag in tags:  # aggregates: power + energy_total
        channel = by_name.get(tag)
        targets.append(
            MqttTarget(
                key=tag,
                label=tag,
                topic=channel.topic if channel and channel.topic else slugify(tag),
                averaged_metrics=_metric_units(channel, False),
                energy_unit=_units_for(channel, "energy", "kWh"),
            )
        )

    topic_root = "/".join(p for p in (cfg.mqtt.topic_prefix, cfg.mqtt.base_topic) if p)
    node_id = slugify(cfg.mqtt.base_topic) if cfg.mqtt.base_topic else slugify(cfg.name)
    return MqttPublisher(
        host=cfg.mqtt.host,
        port=cfg.mqtt.port,
        topic_root=topic_root,
        targets=targets,
        update_interval_s=float(cfg.mqtt.update_interval),
        discovery=cfg.mqtt.auto_discovery,
        discovery_prefix=cfg.mqtt.auto_discovery_prefix,
        node_id=node_id,
        device_name=cfg.name,
        sensor_model=cfg.sensor.type,
    )


def main(argv: Optional[List[str]] = None) -> int:
    """Load config, open the sensor + TCP output, then sample forever."""
    del argv

    cfg = load_config(DEFAULT_CONFIG_PATH)
    logging.basicConfig(
        level=getattr(logging, cfg.logging.level.upper(), logging.INFO),
        format=cfg.logging.format,
    )

    rails = sorted(cfg.sensor.shunt, key=lambda item: item.channel)
    if not rails:
        LOGGER.error("No sensor.shunt channels configured in %s", DEFAULT_CONFIG_PATH)
        return 2
    tags = sorted({item.aggregate for item in rails})

    shunt_ohms = {item.channel: item.shunt_milliohm / 1000.0 for item in rails}
    sensor = Ina3221(
        bus=cfg.sensor.i2c.bus,
        address=cfg.sensor.i2c.address,
        shunt_ohms=shunt_ohms,
        averaging=cfg.sampling.averaging,
        bus_conversion_us=cfg.sampling.bus_conversion_us,
        shunt_conversion_us=cfg.sampling.shunt_conversion_us,
    )
    try:
        sensor.configure()
    except Exception as exc:  # noqa: BLE001 - report and exit cleanly
        LOGGER.error("Sensor setup failed: %s", exc)
        return 1

    # Energy is tracked for every published channel (rails + aggregates).
    rail_names = [item.name for item in rails]
    energy = EnergyStore(
        channels=rail_names + tags, state_file=DEFAULT_STATE_FILE
    )
    session_mwh: Dict[str, float] = {}
    total_mwh: Dict[str, float] = {}

    tcp = TcpServer(
        host=cfg.bind_host,
        port=cfg.bind_port,
        max_clients=cfg.max_clients,
        allowed_clients=cfg.allowed_clients,
    )
    tcp.start()

    interval_s = sensor.expected_interval_s()
    LOGGER.info(
        "Reading %d rail(s) + %d aggregate(s) every %.1f ms (~%.2f Hz); "
        "TCP broadcast on %s:%d, frame size %d B",
        len(rails), len(tags), interval_s * 1000.0, 1.0 / interval_s,
        cfg.bind_host, cfg.bind_port, FRAME_SIZE,
    )

    # Optional MQTT publishing (values averaged over mqtt.update_interval).
    # Disabled when no host is set (YAML `host: ~`) or when the port is 0.
    mqtt_pub: Optional[MqttPublisher] = None
    if cfg.mqtt.enabled:
        mqtt_pub = _build_mqtt_publisher(cfg, rails, tags)
        if mqtt_pub is not None and not mqtt_pub.start():
            mqtt_pub = None
    else:
        LOGGER.info("MQTT disabled (host not set or port is 0)")

    last_read: Optional[float] = None
    cycles = 0
    # Let the first averaged conversion complete before the first read.
    time.sleep(interval_s)

    try:
        while True:
            cycle_start = time.monotonic()
            readings = {r.channel: r for r in sensor.read_all()}
            now = time.monotonic()

            dt_s = (now - last_read) if last_read is not None else 0.0
            last_read = now
            cycles += 1
            ts_ms = _millis()

            # Aggregate power = sum of the member rails' instantaneous power.
            agg_power_mw: Dict[str, float] = {tag: 0.0 for tag in tags}
            for item in rails:
                agg_power_mw[item.aggregate] += (
                    readings[item.channel].power_w * 1000.0
                )

            # --- assemble the binary sample: rails then aggregates ---------
            frame = bytearray()
            for item in rails:  # rails: voltage + current + power + energy
                reading = readings[item.channel]
                session_mwh[item.name], total_mwh[item.name] = energy.add(
                    item.name, reading.power_w * 1000.0, dt_s
                )
                frame += pack_channel(
                    channel_id=item.channel,  # rails keep their chip number 1..3
                    timestamp_millis=ts_ms,
                    voltage_millivolt=round(reading.voltage_v * 1000.0),
                    current_milliamps=round(reading.current_a * 1000.0),
                    power_milliwatt=round(reading.power_w * 1000.0),
                    energy_milliwatthours=round(session_mwh[item.name]),
                    energy_milliwatthours_total=round(total_mwh[item.name]),
                )
            for index, tag in enumerate(tags):  # aggregates: power + energy only
                power_mw = agg_power_mw[tag]
                session_mwh[tag], total_mwh[tag] = energy.add(tag, power_mw, dt_s)
                frame += pack_channel(
                    channel_id=AGGREGATE_ID_BASE + index,
                    timestamp_millis=ts_ms,
                    power_milliwatt=round(power_mw),
                    energy_milliwatthours=round(session_mwh[tag]),
                    energy_milliwatthours_total=round(total_mwh[tag]),
                )
            tcp.broadcast(bytes(frame))

            # Feed MQTT averages; publish only when mqtt.update_interval elapsed.
            if mqtt_pub is not None:
                for item in rails:
                    reading = readings[item.channel]
                    mqtt_pub.feed(
                        item.name,
                        {"voltage": reading.voltage_v,
                         "current": reading.current_a,
                         "power": reading.power_w},
                        total_mwh.get(item.name, 0.0),
                    )
                for tag in tags:
                    mqtt_pub.feed(
                        tag,
                        {"power": agg_power_mw[tag] / 1000.0},
                        total_mwh.get(tag, 0.0),
                    )
                mqtt_pub.publish_due(now)

            if cycles % SAVE_EVERY_CYCLES == 0:
                energy.save()

            # Pace to the sensor's fresh-value cadence.
            elapsed = time.monotonic() - cycle_start
            wait = interval_s - elapsed
            if wait > 0.0:
                time.sleep(wait)
    except KeyboardInterrupt:
        print("\nInterrupted. Final energy totals:")
        for name in rail_names + tags:
            prefix = "Σ " if name in tags else ""
            print(
                f"  {prefix}{name:<12} "
                f"{session_mwh.get(name, 0.0) / 1000.0:12.4f} Wh "
                f"(total {total_mwh.get(name, 0.0) / 1000.0:12.4f} Wh)"
            )
        energy.save()
        return 0
    except OSError as exc:
        LOGGER.error("Sensor/TCP failure: %s", exc)
        return 1
    finally:
        tcp.close()
        sensor.close()
        energy.save()
        if mqtt_pub is not None:
            mqtt_pub.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
