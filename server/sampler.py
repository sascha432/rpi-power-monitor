"""Measurement loop: sensor -> channels -> Sample publisher.

Runs at the sensor refresh period derived from the chip timing (see
``sensor.ina3221.Ina3221.expected_interval_s``); every cycle it:
  1. reads physical channels (sensor.read_all),
  2. integrates energy and attaches ``energy_wh`` per channel,
  3. evaluates virtual (combined) channels,
  4. broadcasts the resulting shared.models.Sample to all TCP subscribers.
"""
from __future__ import annotations

from typing import Callable, List

from shared.models import Sample

# A sink is an async callable receiving each new Sample.
Subscriber = Callable[[Sample], None]


class Sampler:
    """Periodic sampler that fans out Samples to subscribers."""

    def __init__(self) -> None:
        self._subscribers: List[Subscriber] = []
        # TODO(server): hold the PowerSensor, EnergyAccumulator, physical and
        # virtual channel evaluators (from config).

    def add_subscriber(self, sink: Subscriber) -> None:
        """Register a callback that receives every new Sample."""
        raise NotImplementedError

    async def run(self, interval_s: float) -> None:
        """Measure and publish forever (cancel externally to stop)."""
        raise NotImplementedError
