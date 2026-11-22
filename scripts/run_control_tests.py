"""Registered offline worker, with no real credentials and no external sockets."""
from __future__ import annotations
import json
import os
import socket
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "mcp")]


def main():
    if os.environ.get("BINGGO_OFFLINE") != "1":
        raise RuntimeError("This worker requires isolated offline mode")
    home = Path(os.environ["BINGGO_HOME"]).resolve()
    if home == ROOT or home.is_relative_to(ROOT / "data"):
        raise RuntimeError("Unsafe testing home")
    home.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        # Python's Windows 0700 ACL excludes the delegated desktop test identity.
        # Test data is synthetic; inherit the already restricted workspace ACL.
        original_mkdir = os.mkdir
        def inherited_acl_mkdir(path, mode=0o777, *, dir_fd=None):
            return original_mkdir(path, 0o777 if mode == 0o700 else mode, dir_fd=dir_fd)
        os.mkdir = inherited_acl_mkdir
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    def guarded_connect(sock, address):
        if isinstance(address, tuple) and str(address[0]) not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError("Offline suite attempted an external socket connection")
        return original_connect(sock, address)
    def guarded_connect_ex(sock, address):
        if isinstance(address, tuple) and str(address[0]) not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError("Offline suite attempted an external socket connection")
        return original_connect_ex(sock, address)
    socket.socket.connect, socket.socket.connect_ex = guarded_connect, guarded_connect_ex
    import pytest
    from binggo_mcp.runtime import Runtime
    suite = sys.argv[1]
    suites = Runtime.suites()
    if suite not in suites:
        raise RuntimeError("Unknown suite")
    stats = {"passed": 0, "failed": 0, "skipped": 0}
    class Reporter:
        def pytest_runtest_logreport(self, report):
            if report.when == "call" or report.when == "setup" and report.outcome != "passed":
                stats[report.outcome] += 1
    if suite.startswith("frontend-"):
        import subprocess
        command = ["npm.cmd", "run", "test" if suite == "frontend-unit" else "test:e2e"]
        result = subprocess.run(command, cwd=ROOT / "web/frontend", env=os.environ.copy(), timeout=480, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        stats["passed" if result.returncode == 0 else "failed"] = 1
        code = result.returncode
    else:
        code = pytest.main([*suites[suite], "-q", "--tb=short", "--basetemp", str(home.parent / ("pytest-tmp-"+uuid.uuid4().hex)), "-p", "no:cacheprovider"], plugins=[Reporter()])
    Path(os.environ["BINGGO_TEST_REPORT"]).write_text(json.dumps(stats), encoding="utf-8")
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
