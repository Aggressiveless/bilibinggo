"""Start the registered source runtime without moving user data."""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8182)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024..65535")
    os.environ["BINGGO_HOME"] = str(args.home.resolve())
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src import dashboard_server
    dashboard_server.DEV_DASHBOARD_PORT = args.port
    dashboard_server.DASHBOARD_PORT = args.port
    dashboard_server.DASHBOARD_URL = dashboard_server.get_dashboard_url()
    dashboard_server.run_dashboard_server()


if __name__ == "__main__":
    main()
