# Web dashboard client — how to run & configure

The `client/` package is a **stdlib-only Python web server**. It connects to the
Raspberry Pi over the **raw binary TCP stream** (unchanged — C++/other clients
keep working), keeps the latest reading per channel, and serves an HTML/JS
dashboard over HTTP + WebSocket. You view it in any browser.

```
┌────────────────────┐   raw TCP :7000    ┌─────────────────────────┐   HTTP + WS   ┌─────────┐
│  Raspberry Pi      │ ─────────────────▶ │  dashboard (python -m   │ ────────────▶ │ browser │
│  python -m server  │   (40 B frames)    │  client)  :8080         │   /ws         │   UI    │
└────────────────────┘                    └─────────────────────────┘               └─────────┘
```

## 1. Install & run

From the repository root (so the top-level `server`, `client`, `shared`
packages are importable):

```bash
# on the machine that will show the dashboard
pip install -r requirements/client.txt   # just PyYAML — the rest is stdlib
python -m client
```

The startup banner prints the URL, e.g. `http://127.0.0.1:8080/`. Useful CLI
overrides (everything else comes from `config/client.yaml`):

```bash
python -m client --host 0.0.0.0 --port 9000   # override web bind address/port
python -m client --config /path/to/client.yaml
python -m client --server-config /path/to/server.yaml  # channel ids/names
python -m client -v                            # DEBUG logs
```

The Pi side is unchanged: `pip install -r requirements/server.txt` then
`python -m server` on the Pi.

## 2. Configure `config/client.yaml`

### 2.1 `connection` — reaching the Pi

```yaml
connection:
  host: 192.168.0.4      # Pi's IP (or raspberrypi.local / 127.0.0.1 on the Pi)
  port: 7000             # must match server.bind_port in config/server.yaml
  connect_timeout_s: 5
  read_timeout_s: 10     # reconnect if no frame arrives this long
  reconnect: true
  reconnect_delay_s: 2.0
```

> If the dashboard runs on a different machine than the Pi, the **Pi** must
> accept the connection. In `config/server.yaml`, `server.allowed_clients`
> defaults to *localhost only* — list your dashboard machine's subnet, e.g.:
>
> ```yaml
> server:
>   allowed_clients:
>     - 127.0.0.0/8
>     - 192.168.0.0/24
> ```

### 2.2 `server_config` — where channel ids/names come from

The binary wire carries **only numeric channel ids, never names**, so the
dashboard derives its channel table from the **server's own config**. Point
`server_config` at the `server.yaml` the Pi runs (a relative path is resolved
next to `client.yaml`):

```yaml
server_config: server.yaml     # default; config/server.yaml next to this file
```

The mapping is identical on both sides (`shared/catalog.py`):

* **Rails** (INA3221 inputs): `id` = the chip channel number **1..3**,
  `name` = the corresponding `sensor.shunt[].name` in `server.yaml`.
* **Aggregates**: `id` = **100 + index** over the *sorted* aggregate tags;
  `name` = the tag itself (`sensor.shunt[].aggregate`).

For the shipped `server.yaml` this yields:

| id  | name      | kind      |
|----:|-----------|-----------|
| 1   | 12V Input | rail      |
| 2   | 12V NAS   | rail      |
| 3   | 5V Output | rail      |
| 100 | 12V Rail  | aggregate |
| 101 | 5V Rail   | aggregate |

`kind` decides what is shown: `rail` → voltage/current/power; `aggregate` →
power only (the server sends V/I = 0 on aggregate frames). Renaming a shunt or
aggregate tag in `server.yaml` updates the dashboard automatically — there is
nothing to keep in sync here.

### 2.3 `web` — where the dashboard listens

```yaml
web:
  host: 0.0.0.0   # 0.0.0.0 = LAN-reachable; 127.0.0.1 = this machine only
  port: 8080
  allowed_clients:    # empty/omitted => localhost only; list IPs/CIDRs to allow more
    - 127.0.0.0/8
    - 192.168.0.0/24
```

Open `http://<host>:8080/`. The chart library (uPlot) is vendored under
`client/web/static/vendor/`, so the page works fully offline (no CDN).
`allowed_clients` restricts who may open the page or connect to `/ws` (same
CIDR semantics as the Pi server's `server.allowed_clients`; empty = localhost
only, and a bare IP like `192.168.0.5` means just that host).

### 2.4 `display` — dashboard server settings

```yaml
display:
  title: Power Monitor        # browser tab title
  update_ms: 500              # live-update cadence pushed over the WebSocket
  history_points: 28000       # depth of the BROWSER's rolling chart buffer, in
                              # samples per channel (the server keeps no sample
                              # history). Must be >= 3600 s / update_ms to fill
                              # the longest chart window (1 h); too small trims
                              # the oldest points and the graph stops short of
                              # the window's left edge.
```

The *visual* defaults — preselected metric, theme and energy unit, plus which
options the Settings view offers — are **browser-local constants** in
`client/web/static/app.js` (`DEFAULT_METRIC`, `DEFAULT_THEME`,
`DEFAULT_ENERGY_UNIT`, `THEMES`, `ENERGY_UNITS`), so there is nothing to
configure for them server-side; each visitor's choices live in the
`pwm_settings` cookie.

## 3. What the browser does with settings (cookie)

* On connect the dashboard sends a **`hello`** message with the *catalog*:
  channels (from `server.yaml`), metric metadata (label and unit), the tab
  title and the browser's chart-buffer depth — built from the two config files.
  The page builds itself from that catalog, while the purely visual defaults
  come from its own constants in `app.js`.
* Your **UI choices are stored in a `pwm_settings` cookie** (metric, energy and
  current units, dashboard/channel chart windows, energy-bar day count, theme,
  metric colours, per-channel "plot" toggles and per-channel metric memory).
  They are applied on every load and persist across sessions — no server
  round-trip.
* The **Reset** button clears the cookie and reloads with the defaults.
* A **`daily`** message carries the Pi's one-shot **daily-energy** block (the
  reserved control frames described in `docs/protocol.md`). In the single-
  channel view the read-only **Energy** tile hosts a bar strip for the last N
  days (Settings, 7-90 days, default 7); the "today" bar keeps growing live from
  the cumulative total already present in every `sample` message.
* There is **no sample-history message**: the Python client keeps only the
  latest reading per channel, and the browser accumulates its own chart buffers
  from the `sample` stream. A freshly opened or reloaded chart therefore starts
  empty and fills live (it takes up to the selected window to look full).

## 4. Testing without a Pi (optional)

There is no bundled simulator, but the dashboard talks pure TCP: point
`connection.host/port` at anything that emits the 40-byte frames from
`shared.binary.pack_channel` and the UI will stream. E.g.:

```python
from shared.binary import pack_channel
# ... send pack_channel(channel_id=1, timestamp_millis=..., power_milliwatt=...) etc.
```

If you only fake per-sample frames (no daily block) the energy-history bars
simply stay empty — the real server sends that block once per connection
before the first sample; a plain client can ignore the reserved-id frames.

## 5. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Dashboard starts but shows **"connecting to Pi…"** | Pi unreachable (wrong `connection.host/port`) **or** the Pi's `server.allowed_clients` rejects you — see §2.1. |
| Header shows **"Pi connected"** but cards stay `--` | `server_config` points at a different `server.yaml` than the Pi runs (or the server isn't sending yet) — re-check §2.2. |
| **Voltage/Current** metric shows only rails | Correct — aggregates don't publish V/I; only power. |
| Chart looks empty right after a load/reload | Expected — the client sends no sample history, so the browser builds the chart from live samples; it fills over the selected window (Settings → Dashboard window / Channel window). |
| Browser page loads but WebSocket errors | Another process already bound `web.port`; check the startup banner URL and change `web.port`. |
| Energy totals look "too big/small" | Totals are Wh from the server; switch `Energy` to `Wh`/`kWh` (display-only conversion). |

## 6. The raw stream is untouched

This client only *consumes* the Pi's raw TCP broadcast. You can verify other
clients still work while the dashboard runs, e.g. with netcat and a hex dump of
the 40-byte frames, or the C++ client described in `docs/protocol.md`.
