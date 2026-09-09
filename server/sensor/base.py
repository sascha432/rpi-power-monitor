"""Abstract interface implemented by every sensor driver."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class RawReading:
    """One instant of measurements for a single physical input."""

    channel: int  # 1-based input number (1..3 for the INA3221)
    voltage_v: float
    current_a: float
    power_w: float
    # energy_wh is NOT read from the chip; it is integrated in server.energy.


class PowerSensor(ABC):
    """Minimal contract for a power-sensor driver."""

    @abstractmethod
    def configure(self) -> None:
        """Apply sensor configuration (registers, averaging, mode, ...)."""

    @abstractmethod
    def read_all(self) -> list[RawReading]:
        """Measure every enabled input and return one RawReading each."""

    @abstractmethod
    def close(self) -> None:
        """Release the bus / clean up."""
