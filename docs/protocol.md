# Power-monitor TCP protocol (binary)

## Transport

- TCP; the server binds `config/server.yaml` -> `server.bind_host` / `server.bind_port` (default `0.0.0.0:7000`).
- **Binary stream of fixed-size frames**: one frame per channel per sample,
  sent back-to-back with **no framing, no header, no length prefix**.
- Byte order is **little-endian** and the layout is byte-identical to the
  *natural* C struct below on x86-64, so a C++ client can read whole frames
  with a single `memcpy` into `struct PowerSample` - no per-field or
  byte-order conversion.

## Frame layout (40 bytes)

| offset | size | type    | field                       |
|-------:|-----:|---------|-----------------------------|
| 0      | 4    | uint32  | `channel_id`                |
| 4      | 4    | uint32  | `timestamp_millis`          |
| 8      | 4    | uint32  | `voltage_millivolt`         |
| 12     | 4    | int32   | `current_milliamps`         |
| 16     | 4    | int32   | `power_milliwatt`           |
| 20     | 4    | (pad)   | implicit ABI padding        |
| 24     | 8    | int64   | `energy_milliwatthours`     |
| 32     | 8    | int64   | `energy_milliwatthours_total` |

```cpp
#include <cstdint>
// Little-endian; natural x86-64 layout. sizeof(PowerSample) == 40.
struct PowerSample {
    uint32_t channel_id;
    uint32_t timestamp_millis;     // ms since server start (monotonic)
    uint32_t voltage_millivolt;    // rails only; 0 on aggregates
    int32_t  current_milliamps;    // rails only; 0 on aggregates
    int32_t  power_milliwatt;      // aggregates only; 0 on rails
    // 4 implicit padding bytes before the int64 members
    int64_t  energy_milliwatthours;       // since this server run
    int64_t  energy_milliwatthours_total; // persistent across restarts
};
static_assert(sizeof(PowerSample) == 40, "ABI changed");
```

If you compile with packing on a different target, keep the 4 pad bytes
**explicit** so the wire layout stays identical:

```cpp
#pragma pack(push, 1)
struct PowerSamplePacked {
    uint32_t channel_id;
    uint32_t timestamp_millis;
    uint32_t voltage_millivolt;
    int32_t  current_milliamps;
    int32_t  power_milliwatt;
    uint32_t _pad;                     // must stay on the wire
    int64_t  energy_milliwatthours;
    int64_t  energy_milliwatthours_total;
};
#pragma pack(pop)
```

## Channel set and ids

Every sample emits **rails first, then aggregates**, in a fixed order:

- **Rails** (INA3221 inputs) in chip order; `channel_id` = chip channel 1..3.
- **Aggregates** (rails grouped by their `aggregate` tag), sorted by tag;
  `channel_id` = `100 + index`.

For the shipped `config/server.yaml` this means:

| channel_id | name      | kind      | fields                             |
|-----------:|-----------|-----------|------------------------------------|
| 1          | 12V Input | rail      | voltage, current, power, energy    |
| 2          | 12V NAS   | rail      | voltage, current, power, energy    |
| 3          | 5V Output | rail      | voltage, current, power, energy    |
| 100        | 12V Rail  | aggregate | power, energy                      |
| 101        | 5V Rail   | aggregate | power, energy                      |

The mapping is derived from `sensor.shunt[].name` / `.aggregate` by
`shared/catalog.py`, which the server and the dashboard share so both always
agree on which id means which channel.

Rail frames publish `voltage_millivolt`, `current_milliamps`, `power_milliwatt`
and their own two energy counters. Aggregate frames publish `power_milliwatt`
and the two energy counters for the whole group (voltage/current are 0).
Aggregate power is the sum of the member rails' instantaneous power, so
aggregate energy tracks the summed member energy.

> Reserved ids: a small range of `channel_id` values with bit 31 set
> (`0x8000_0000`+) is used by the one-shot **daily-energy block** below and
> never collides with real channels (rails `1..3`, aggregates `100 + idx`).
> Ignore frames whose `channel_id` is in that range if you do not need daily
> history.

## Timing

- `timestamp_millis`: uint32 **milliseconds since the server process started**
  (monotonic; wraps after ~49.7 days). Treat it as a delta source, not wall
  time.
- One sample = one frame per channel sharing the same `timestamp_millis`.
- Sample cadence is NOT configurable; it is derived from the chip:
  `interval = averaging * (bus_conversion_time + shunt_conversion_time)`.

## Energy semantics

Every published channel carries its own pair of counters:

- `energy_milliwatthours`: integrated since this server run (session).
- `energy_milliwatthours_total`: the same counter but **persisted** across
  restarts - the server loads it from `state/energy.json` at startup and saves
  it periodically and on shutdown.
- `state/energy.json` also holds a rolling **per-day** log (per channel, in
  the Pi's local time) that keeps the newest `energy.storage_days` days
  (`config/server.yaml`); the totals remain all-time accumulations.
- With `energy.archive: true` (`config/server.yaml`) the server appends a copy
  of the current `state/energy.json` to `state/energy.json.tar` once an hour, as
  a member named `energy-YYYYmmddHHMMSS.json` (plain uncompressed tar, appended
  in place; no retention policy). It is operator-facing only - never read back
  by the server and never sent over the wire.
- Rails integrate their own power (V*I); aggregates integrate the summed
  power of their member rails.
- Both are int64 (signed): net energy may decrease when power flows backwards.

## One-shot daily-energy block (sent on connect)

When a client connects (and passes the server's allowlist), the server sends a
small block of **reserved control frames** *before* the first sample frame, so
a dashboard can draw a daily-consumption chart straight away.
Every frame is still 40 bytes (the stream stays a multiple of 40 and fixed-size
slicing keeps working). Clients that do not need daily history simply ignore
frames whose `channel_id` is a reserved id (see the note under "Channel set and
ids"). The block is emitted once per accepted connection, from the accept
thread *before* the client is registered for broadcasts, so it always precedes
that client's first sample and the two never interleave.

Reserved `channel_id` values (bit 31 set; never a real rail/aggregate id):

| id          | meaning                                    |
|------------:|--------------------------------------------|
| 0x80000001  | daily block header                         |
| 0x80000002  | daily block value (one per day x channel)  |

The block = **one header frame**, then **one value frame per (day, channel)**
in day-major order (day index 0 = oldest ... n-1 = today), where `n` is the
number of trailing calendar days sent. `n = min(energy.storage_days,
DAILY_MAX)` with `DAILY_MAX = 90`, so a dashboard can show between 7 and 90
days of daily energy (the browser clamps its own 7-90 setting to what the
server actually sent). "Today" uses the Pi's local date.

Header frame (`channel_id` = 0x80000001):

| field                | meaning                                |
|----------------------|----------------------------------------|
| `timestamp_millis`   | today as `YYYYMMDD` (uint32)           |
| `voltage_millivolt`  | number of day buckets (n)              |
| `current_milliamps`  | number of channels in the block        |
| `power_milliwatt`    | schema version (currently 1)           |
| energy fields        | 0                                      |

Value frame (`channel_id` = 0x80000002):

| field                | meaning                                |
|----------------------|----------------------------------------|
| `timestamp_millis`   | day index (0..n-1; n-1 = today)        |
| `voltage_millivolt`  | the channel's normal wire id           |
| `energy_milliwatthours` | all-time total mWh at block build - only on the today row (n-1), else 0 |
| `energy_milliwatthours_total` | that (channel, day) bucket in mWh |

`current_milliamps` / `power_milliwatt` are 0 on value frames. A dashboard uses
the today row's bucket as the base for "energy today so far" and the matching
all-time total as a live-update anchor: because the all-time total is already
present in every periodic sample frame, the current-day bar keeps growing
without re-sending the block:

```
today_live = today_base + (total_now - anchor)
```

## Types / limits (auto-clamped by the server)

| field               | type  | range / note                              |
|---------------------|-------|-------------------------------------------|
| voltage_millivolt   | uint32| practical max ~26 000 (26 V)              |
| current_milliamps   | int32 | +-2,147,483 mA (signed for reverse flow)  |
| power_milliwatt     | int32 | +-2,147,483 mW (signed)                   |
| energy_milliwatthours* | int64 | +-9.2e18 mWh                         |
| timestamp_millis    | uint32| wraps after ~49.7 days                    |

