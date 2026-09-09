"""Wire/data model shared by the server and the GUI client.

These are plain data containers describing what is sent over TCP (see
``docs/protocol.md``). No behaviour lives here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List


class ChannelKind(str, Enum):
    """Where a channel's values come from."""

    PHYSICAL = "physical"  # direct INA3221 input
    VIRTUAL = "virtual"    # combined / computed channel


class ReadingField(str, Enum):
    """Canonical reading fields and their wire names (see docs/protocol.md)."""

    VOLTAGE_V = "voltage_v"
    CURRENT_A = "current_a"
    POWER_W = "power_w"
    ENERGY_WH = "energy_wh"


@dataclass(frozen=True)
class ChannelMeta:
    """Static description of one channel, sent once in a registry message."""

    name: str
    kind: ChannelKind
    fields: List[ReadingField]


@dataclass
class Registry:
    """Server capabilities / channel list (first message per connection)."""

    server_name: str
    sensor: str
    interval_s: float
    channels: List[ChannelMeta] = field(default_factory=list)


@dataclass
class Sample:
    """One measurement cycle broadcast to clients.

    ``readings`` maps channel name -> {ReadingField: float}; only the fields
    that the channel announced in the registry are present.
    """

    ts: float  # epoch seconds (UTC)
    readings: Dict[str, Dict[str, float]] = field(default_factory=dict)


@dataclass
class ErrorMessage:
    """Server -> client error notification."""

    code: str
    message: str
