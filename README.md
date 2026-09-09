# rpi-power-monitor

Two-part Python project that reads an **INA3221** power sensor (3 I²C channels)
on a Raspberry Pi and shows the data on a **web dashboard** in your browser:

- **`server/`** — runs on the Raspberry Pi (Linux). Reads the INA3221 via I2C,
  computes per-channel voltage / current / power / energy, adds **aggregate
  channels** (rails grouped by a tag), and broadcasts a **raw binary TCP
  stream** (fixed 40-byte frames — easy to consume from C++, scripts, …).
- **`client/`** — a **stdlib-only Python web server** (no tkinter). Connects to
  the Pi's raw TCP stream, keeps a rolling history, and serves an offline
  HTML/JS dashboard over HTTP + **WebSocket**. The WebSocket hands the browser
  its config/settings catalog (channels, metrics, units, theme) plus live
  readings; the user's UI choices are stored in a browser cookie.

## Layout

```
rpi-power-monitor/
├── power-monitor.code-workspace
├── README.md
├── .gitignore
├── requirements/            # pip requirements per role
│   ├── common.txt
│   ├── server.txt
│   └── client.txt
├── config/                  # the actual configuration files
│   ├── server.yaml          #   -> edit on the Raspberry Pi
│   └── client.yaml          #   -> edit where the dashboard runs
├── deploy/                  # systemd unit + install helper (Raspberry Pi)
│   ├── rpi-power-monitor.service
│   └── install-service.sh
├── shared/                  # code shared by both sides (contract only)
│   ├── binary.py            #   40-byte binary frame pack/unpack
│   ├── models.py            #   legacy JSON-Lines data model (unused)
│   └── protocol.py          #   legacy JSON-Lines framing (unused)
├── server/                  # Raspberry Pi side
│   ├── main.py              #   entry point (read + broadcast + MQTT)
│   ├── config.py            #   typed loader for config/server.yaml
│   ├── energy.py            #   per-channel Wh counters (+JSON persistence)
│   ├── mqtt.py              #   optional MQTT publisher
│   ├── sensor/
│   │   ├── base.py          #   PowerSensor interface
│   │   └── ina3221.py       #   INA3221 driver (smbus2)
│   ├── channels/            #   (legacy virtual-channel stubs)
│   └── net/
│       └── tcp_server.py    #   threaded raw-TCP broadcaster (+allowlist)
├── client/                  # web-dashboard side (Windows / Linux / Pi)
│   ├── main.py              #   entry point
│   ├── config.py            #   typed loader for config/client.yaml
│   ├── store.py             #   thread-safe rolling history + snapshots
│   ├── net/
│   │   └── tcp_client.py    #   reconnecting binary-frame TCP reader
│   └── web/
│       ├── server.py        #   stdlib HTTP + WebSocket dashboard server
│       ├── ws.py            #   minimal RFC 6455 (stdlib only)
│       └── static/          #   index.html, app.js, style.css, vendor/uPlot
├── docs/
│   ├── protocol.md          # TCP wire protocol specification (binary)
│   └── client_web.md        # web dashboard: how to run & configure
└── state/                   # energy.json persistence (gitignored)
```

## Quick start

Run from the repository root (so the top-level `server`, `client`, `shared`
packages are importable).

**Server (Raspberry Pi):**

```bash
pip install -r requirements/server.txt
python -m server
```

**Web dashboard (any machine that can reach the Pi):**

```bash
pip install -r requirements/client.txt
python -m client
# then open http://<this-machine>:8080/  (see the printed URL)
```

The dashboard connects to the Pi's raw TCP stream (`connection.host/port`) and
serves the UI on `web.host:web.port`. See [`docs/client_web.md`](docs/client_web.md)
for the full configuration guide.

## Run the server as a service (systemd, optional)

To have the server start automatically at boot on the Raspberry Pi, install
the provided systemd unit (run **on the Pi**, from the repo):

```bash
sudo ./deploy/install-service.sh             # install + enable at boot + start
systemctl status rpi-power-monitor
journalctl -u rpi-power-monitor -f           # follow the logs
```

The helper fills in the real paths/user into
[`deploy/rpi-power-monitor.service`](deploy/rpi-power-monitor.service) and
installs it as `rpi-power-monitor`. It auto-detects the repo owner (the user
the service runs as) and a `.venv` python; override with
`--user`, `--group`, `--python` as needed. Uninstall with
`sudo ./deploy/install-service.sh --uninstall`.

Useful systemd facts for this unit:

- It runs `python -m server` from the repo root (working directory), so the
  top-level `server` / `shared` packages and `config/server.yaml` +
  `state/energy.json` resolve as usual.
- The server is stopped with **SIGINT** (`KillSignal=SIGINT`), which the code
  turns into `KeyboardInterrupt` so it saves energy totals and closes sockets
  cleanly. `Restart=always` revives it after a crash.
- The service user must be a member of the `i2c` group to open `/dev/i2c-1`
  (the default Raspberry Pi OS user usually already is):
  `sudo usermod -aG i2c <user>` (then reboot).

## Configuration

- `config/server.yaml` — bind address/port, client allowlist, I2C bus +
  address, physical shunts (name / nominal voltage / shunt / aggregate tag),
  sampling, MQTT (optional).
- `config/client.yaml` — Pi address, dashboard bind host/port, display defaults,
  and the **channel table** that must mirror the rails/aggregates in
  `server.yaml` (the binary wire carries ids only, never names).

Defaults are filled in with **example wiring**; adjust the channel names, shunt
values, and addresses to your actual hardware before use.

> Status: server (INA3221 sampling, aggregate energy, raw TCP, MQTT) is
> implemented; the client is the new web dashboard (replacing the earlier
> tkinter scaffold).

