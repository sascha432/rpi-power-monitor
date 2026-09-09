"""Binary wire format for the power-monitor TCP stream.

One fixed-size frame per channel per sample, sent back-to-back as a raw byte
stream (no framing, no length prefixes). The layout is little-endian and
matches the *natural* layout of this C struct on an x86-64 target, so a C++
client can ``memcpy`` a received buffer straight into the struct - no
per-field or byte-order conversion::

    struct PowerSample {                 // little-endian, size 40
        uint32_t channel_id;
        uint32_t timestamp_millis;       // ms since server start (monotonic)
        uint32_t voltage_millivolt;      // rails only; 0 on aggregates
        int32_t  current_milliamps;      // rails only; 0 on aggregates
        int32_t  power_milliwatt;        // rails: own; aggregates: summed
        // 4 implicit padding bytes -> int64 members are 8-byte aligned
        int64_t  energy_milliwatthours;        // since this run (per channel)
        int64_t  energy_milliwatthours_total;  // persistent across restarts
    };

Field semantics
---------------
* timestamp_millis: uint32 ms since the server *process* started (monotonic;
  wraps after ~49.7 days) - use deltas, not absolute wall time.
* Physical rails publish voltage, current, power and their own energy
  counters.
* Aggregated channels publish power + energy only (voltage/current == 0);
  their energy covers the summed member rails.
* energy_milliwatthours / _total are 64-bit *signed* so net energy can be
  negative when power flows the other way.
"""
from __future__ import annotations

import struct
from typing import Tuple

# '<' = little-endian; "IIIIi" = 4*uint32 + int32 (channel, ts, volt, cur,
# pow) -> 20 B; "4x" = the ABI padding before the first int64 -> 24 B; "qq" =
# two int64 -> 40 B total.
FRAME = struct.Struct("<IIIIi4xqq")
FRAME_SIZE: int = FRAME.size  # 40

_UINT32_MAX = (1 << 32) - 1
_INT32_MIN, _INT32_MAX = -(1 << 31), (1 << 31) - 1
_INT64_MIN, _INT64_MAX = -(1 << 63), (1 << 63) - 1


def _clamp(value: int, lo: int, hi: int) -> int:
    return lo if value < lo else hi if value > hi else value


def _u32(value: int) -> int:
    return _clamp(int(value), 0, _UINT32_MAX)


def _i32(value: int) -> int:
    return _clamp(int(value), _INT32_MIN, _INT32_MAX)


def _i64(value: int) -> int:
    return _clamp(int(value), _INT64_MIN, _INT64_MAX)


def pack_channel(
    channel_id: int,
    timestamp_millis: int,
    voltage_millivolt: int = 0,
    current_milliamps: int = 0,
    power_milliwatt: int = 0,
    energy_milliwatthours: int = 0,
    energy_milliwatthours_total: int = 0,
) -> bytes:
    """Pack one channel reading into a ``FRAME_SIZE`` little-endian frame."""
    return FRAME.pack(
        _u32(channel_id),
        _u32(timestamp_millis),
        _u32(voltage_millivolt),
        _i32(current_milliamps),
        _i32(power_milliwatt),
        _i64(energy_milliwatthours),
        _i64(energy_milliwatthours_total),
    )


def unpack_channel(frame: bytes) -> Tuple[int, int, int, int, int, int, int]:
    """Inverse of :func:`pack_channel` (tests / reference clients)."""
    if len(frame) != FRAME_SIZE:
        raise ValueError(f"frame must be {FRAME_SIZE} bytes, got {len(frame)}")
    return FRAME.unpack(frame)
