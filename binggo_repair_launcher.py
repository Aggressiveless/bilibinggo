"""Isolated local repair build: separate data, port and single-instance mutex."""
from pathlib import Path
import os
import sys

base = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
os.environ['BINGGO_HOME'] = str(base / 'user-data')
os.environ['BINGGO_PORTABLE'] = '1'

from src import dashboard_server

dashboard_server.PACKAGED_DASHBOARD_PORT = 8182
dashboard_server.DEV_DASHBOARD_PORT = 8182
dashboard_server.DASHBOARD_PORT = dashboard_server.get_dashboard_port()
dashboard_server.DASHBOARD_URL = dashboard_server.get_dashboard_url()

import binggo_launcher

binggo_launcher.MUTEX_NAME = r'Global\BilibiliBinggoRepostFixDashboard'

if __name__ == '__main__':
    if binggo_launcher.SERVE_FLAG in sys.argv:
        raise SystemExit(binggo_launcher.run_server_mode())
    raise SystemExit(binggo_launcher.main())
