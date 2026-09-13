"""Entry point for the power-monitor web dashboard client.

Runs a stdlib HTTP + WebSocket dashboard that consumes the Raspberry Pi's raw
binary TCP stream and serves an HTML/JS UI. Run from the repository root:

    python -m client

then open http://<web.host>:<web.port>/ in a browser.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from .config import (
    DEFAULT_CONFIG_PATH,
    MAX_CHART_WINDOW_S,
    ClientConfig,
    load_config,
    min_history_points,
)
from .net.tcp_client import TcpClient
from .store import DataStore
from .web.server import WebDashboardServer

LOGGER = logging.getLogger("client.main")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m client",
        description="Power-monitor web dashboard (stdlib HTTP + WebSocket).",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="path to client.yaml (default: config/client.yaml)",
    )
    parser.add_argument(
        "--server-config",
        help=(
            "path to the server.yaml that defines the channel ids/names "
            "(default: client.yaml 'server_config', else config/server.yaml)"
        ),
    )
    parser.add_argument(
        "--host",
        help="override the dashboard bind host from config/web.host",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="override the dashboard port from config/web.port",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="enable DEBUG logging",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Load config, connect to the Pi stream, and serve the dashboard."""
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = Path(args.config)
    cfg: ClientConfig = load_config(
        config_path, server_config_path=args.server_config
    )
    if args.host:
        cfg.web.host = args.host
    if args.port is not None:
        cfg.web.port = args.port
    if not cfg.channels:
        LOGGER.warning(
            "no channels found in %s - add a sensor.shunt table there",
            cfg.server_config,
        )

    # history_points is the browser chart-buffer depth, so a value too small for
    # the pushed cadence makes the browser trim the oldest points and the graph
    # stops short of the window's left edge (the 1 h / 15 min windows need far
    # more than a few thousand points at ~7 Hz). Raise it and tell the operator.
    needed_points = min_history_points(cfg.display.update_ms)
    if cfg.display.history_points < needed_points:
        LOGGER.warning(
            "display.history_points=%d cannot fill the %d s chart window at "
            "%d ms/sample (needs >= %d); raising it so the graph does not stop "
            "short - set history_points >= %d in config/client.yaml to silence "
            "this",
            cfg.display.history_points,
            MAX_CHART_WINDOW_S,
            cfg.display.update_ms,
            needed_points,
            needed_points,
        )
        cfg.display.history_points = needed_points

    store = DataStore(cfg)
    tcp = TcpClient(cfg.connection, store)
    dash = WebDashboardServer((cfg.web.host, cfg.web.port), cfg, store)

    bind_host = cfg.web.host
    url_host = "127.0.0.1" if bind_host in ("0.0.0.0", "::", "") else bind_host
    print(f"Power-monitor dashboard")
    print(f"  config    : {config_path}")
    print(f"  channels  : {cfg.server_config} ({len(cfg.channels)} channel(s))")
    print(f"  Pi stream : {cfg.connection.host}:{cfg.connection.port}")
    print(f"  dashboard : http://{url_host}:{cfg.web.port}/")
    print("  Ctrl-C to stop")

    tcp.start()
    try:
        dash.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        tcp.stop()
        dash.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

