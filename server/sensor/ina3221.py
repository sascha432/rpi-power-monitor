"""INA3221 three-channel I2C power monitor driver (smbus2).

Reference: TI INA3221 datasheet (SBOS469). Register field layout and value
decoding were cross-checked against the Linux hwmon driver
(``drivers/hwmon/ina3221.c``) and the Adafruit CircuitPython driver.

I2C / register facts
--------------------
- 16-bit registers are big-endian: the high byte is transferred first.
- Data is stored left-justified, so the lower 3 bits of each 16-bit word are 0:
    * Shunt voltage: LSB 5 uV -> reading the raw 16-bit word as *signed* and
      scaling by 5 uV is exact (full scale +/-163.8 mV, no programmable gain).
    * Bus voltage:   LSB 8 mV, 12-bit field in bits [15:3]
      -> V = (raw >> 3) * 8 mV.
- CONFIG register (0x00) bit layout:
    bits [2:0]   mode            (0b111 = continuous shunt + bus)
    bits [5:3]   shunt conv time index
    bits [8:6]   bus conv time index
    bits [11:9]  averaging index
    bits [14:12] channel enables  (CH1 -> bit14, CH2 -> bit13, CH3 -> bit12)
    bit  [15]    reset
- Manufacturer ID register is 0xFE (reads 0x5449 = "TI"); die ID is 0xFF.

Polling cadence
---------------
The result registers refresh continuously; the period between two fresh
*averaged* values is set by the chip timing: ::

    interval = averaging * (bus_conv_time + shunt_conv_time)

With averaging = 64 and 1100 us bus + 1100 us shunt that is 64 * 2200 us
= 140.8 ms (~7.1 Hz). Polling faster than this just re-reads the same
averaged value, so the loop paces itself to ``expected_interval_s()``.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Mapping, Optional

from .base import PowerSensor, RawReading

LOGGER = logging.getLogger(__name__)

# --- Register map -----------------------------------------------------------
REG_CONFIG = 0x00
REG_SHUNT_VOLTAGE = (0x01, 0x03, 0x05)  # shunt voltage, CH1..CH3
REG_BUS_VOLTAGE = (0x02, 0x04, 0x06)    # bus voltage, CH1..CH3
REG_MANUFACTURER_ID = 0xFE              # reads 0x5449 ("TI")

# --- Fixed chip characteristics --------------------------------------------
LSB_SHUNT_V = 5e-6          # volts per raw shunt LSB
LSB_BUS_V = 8e-3            # volts per (raw >> 3) bus count
MANUFACTURER_ID_TI = 0x5449

# --- CONFIG register field placement ---------------------------------------
CONFIG_MODE_CONTINUOUS_SHUNT_BUS = 0b111  # bits [2:0]
CONFIG_SHIFT_SHUNT_CT = 3                 # bits [5:3]
CONFIG_SHIFT_BUS_CT = 6                   # bits [8:6]
CONFIG_SHIFT_AVG = 9                      # bits [11:9]
# channel n (1-based) enable bit lives at 1 << (15 - n): CH1=14, CH2=13, CH3=12

# --- Allowed index tables (index into these == register field value) --------
CONV_TIMES_US = (140, 204, 332, 588, 1100, 2116, 4156, 8244)
AVG_SAMPLES = (1, 4, 16, 64, 128, 256, 512, 1024)


def _signed16(raw: int) -> int:
    """Interpret a raw 16-bit register word as two's-complement signed."""
    return raw - 0x10000 if raw & 0x8000 else raw


def _index(value: int, table: tuple, label: str) -> int:
    try:
        return table.index(value)
    except ValueError:
        raise ValueError(
            f"Invalid INA3221 {label}: {value}; choose from {table}"
        ) from None


class Ina3221(PowerSensor):
    """Reads the configured physical inputs of one INA3221 over I2C.

    ``shunt_ohms`` maps the 1-based input number (1..3) to the shunt
    resistance in ohms for that input; only the listed inputs are enabled.

    The resistance may be **negative**: the magnitude is the physical shunt
    resistance, while the sign flips the polarity of the measured current (and
    therefore power/energy) for that input. Use it to make the sign convention
    consistent across channels, e.g. ``-0.025`` for an input whose current
    flows opposite to the other channels. Zero is rejected.
    """

    def __init__(
        self,
        bus: int,
        address: int,
        shunt_ohms: Mapping[int, float],
        averaging: int = 64,
        bus_conversion_us: int = 1100,
        shunt_conversion_us: int = 1100,
    ) -> None:
        if not 1 <= len(shunt_ohms) <= 3:
            raise ValueError("INA3221 needs between 1 and 3 shunt channels")
        invalid = [ch for ch in shunt_ohms if not 1 <= ch <= 3]
        if invalid:
            raise ValueError(
                f"INA3221 channel numbers must be 1..3, got {invalid}"
            )
        if any(resistance == 0 for resistance in shunt_ohms.values()):
            raise ValueError(
                "shunt resistances must be non-zero (negative sign flips the "
                "current/power polarity)"
            )

        self._bus_id = bus
        self._address = address
        self._shunt_ohms: Dict[int, float] = dict(shunt_ohms)
        self._channels: List[int] = sorted(shunt_ohms)

        self._avg_idx = _index(averaging, AVG_SAMPLES, "averaging samples")
        self._bus_ct_idx = _index(
            bus_conversion_us, CONV_TIMES_US, "bus conversion time"
        )
        self._shunt_ct_idx = _index(
            shunt_conversion_us, CONV_TIMES_US, "shunt conversion time"
        )
        self._config = self._build_config()
        self._smbus: Optional[object] = None

    # -- configuration ------------------------------------------------------

    def _averaging_samples(self) -> int:
        return AVG_SAMPLES[self._avg_idx]

    def _build_config(self) -> int:
        enables = 0
        for ch in self._channels:
            enables |= 1 << (15 - ch)  # CH1 -> bit14 ... CH3 -> bit12
        return (
            CONFIG_MODE_CONTINUOUS_SHUNT_BUS
            | (self._shunt_ct_idx << CONFIG_SHIFT_SHUNT_CT)
            | (self._bus_ct_idx << CONFIG_SHIFT_BUS_CT)
            | (self._avg_idx << CONFIG_SHIFT_AVG)
            | enables
        )

    def expected_interval_s(self) -> float:
        """Refresh period between two fresh averaged readings.

        interval = averaging * (bus_conversion_time + shunt_conversion_time)
        """
        per_sample_us = (
            CONV_TIMES_US[self._bus_ct_idx] + CONV_TIMES_US[self._shunt_ct_idx]
        )
        return self._averaging_samples() * per_sample_us * 1e-6

    def configure(self) -> None:
        try:
            from smbus2 import SMBus  # type: ignore  # Raspberry Pi dependency
        except ImportError as exc:  # pragma: no cover - only on the Pi
            raise RuntimeError(
                "smbus2 is not installed; run `pip install -r "
                "requirements/server.txt` on the Raspberry Pi"
            ) from exc

        self._smbus = SMBus(self._bus_id)
        try:
            manufacturer = self._read_reg(REG_MANUFACTURER_ID)
        except OSError as exc:
            self.close()
            raise OSError(
                f"INA3221 not reachable on i2c-{self._bus_id} at "
                f"0x{self._address:02X}: {exc}"
            ) from exc

        if manufacturer != MANUFACTURER_ID_TI:
            LOGGER.warning(
                "Unexpected INA3221 manufacturer ID 0x%04X (expected 0x%04X); "
                "continuing anyway",
                manufacturer,
                MANUFACTURER_ID_TI,
            )

        self._write_reg(REG_CONFIG, self._config)
        LOGGER.info(
            "INA3221 ready on i2c-%d at 0x%02X | CONFIG=0x%04X | avg=%d | "
            "bus_ct=%dus | shunt_ct=%dus | channels=%s | interval=%.1f ms",
            self._bus_id,
            self._address,
            self._config,
            self._averaging_samples(),
            CONV_TIMES_US[self._bus_ct_idx],
            CONV_TIMES_US[self._shunt_ct_idx],
            self._channels,
            self.expected_interval_s() * 1000.0,
        )

    # -- measurement --------------------------------------------------------

    def read_all(self) -> List[RawReading]:
        if self._smbus is None:
            raise RuntimeError("configure() must be called before read_all()")

        readings = []
        for ch in self._channels:
            shunt_raw = self._read_reg(REG_SHUNT_VOLTAGE[ch - 1])
            bus_raw = self._read_reg(REG_BUS_VOLTAGE[ch - 1])

            shunt_v = _signed16(shunt_raw) * LSB_SHUNT_V
            bus_v = (bus_raw >> 3) * LSB_BUS_V
            current_a = shunt_v / self._shunt_ohms[ch]
            power_w = bus_v * current_a

            readings.append(
                RawReading(
                    channel=ch,
                    voltage_v=bus_v,
                    current_a=current_a,
                    power_w=power_w,
                )
            )
        return readings

    # -- low-level I2C ------------------------------------------------------

    def _read_reg(self, reg: int) -> int:
        raw = self._smbus.read_i2c_block_data(self._address, reg, 2)
        return (raw[0] << 8) | raw[1]

    def _write_reg(self, reg: int, value: int) -> None:
        self._smbus.write_i2c_block_data(
            self._address, reg, [(value >> 8) & 0xFF, value & 0xFF]
        )

    def close(self) -> None:
        if self._smbus is not None:
            try:
                self._smbus.close()
            finally:
                self._smbus = None
