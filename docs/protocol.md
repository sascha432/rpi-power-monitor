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

| channel_id | name        | kind      | fields                             |
|-----------:|-------------|-----------|------------------------------------|
| 1          | 12V Output  | rail      | voltage, current, power, energy    |
| 2          | 12V Input   | rail      | voltage, current, power, energy    |
| 3          | 5V Output   | rail      | voltage, current, power, energy    |
| 100        | input       | aggregate | power, energy                      |
| 101        | output      | aggregate | power, energy                      |

Rail frames publish `voltage_millivolt`, `current_milliamps`, `power_milliwatt`
and their own two energy counters. Aggregate frames publish `power_milliwatt`
and the two energy counters for the whole group (voltage/current are 0).
Aggregate power is the sum of the member rails' instantaneous power, so
aggregate energy tracks the summed member energy.

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
- Rails integrate their own power (V*I); aggregates integrate the summed
  power of their member rails.
- Both are int64 (signed): net energy may decrease when power flows backwards.

## Types / limits (auto-clamped by the server)

| field               | type  | range / note                              |
|---------------------|-------|-------------------------------------------|
| voltage_millivolt   | uint32| practical max ~26 000 (26 V)              |
| current_milliamps   | int32 | +-2,147,483 mA (signed for reverse flow)  |
| power_milliwatt     | int32 | +-2,147,483 mW (signed)                   |
| energy_milliwatthours* | int64 | +-9.2e18 mWh                         |
| timestamp_millis    | uint32| wraps after ~49.7 days                    |

