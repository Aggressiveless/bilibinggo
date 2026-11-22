"""Create a machine-local MCP profile and a reviewable host configuration."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--app-python", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--application", type=Path)
    parser.add_argument("--port", type=int, default=8182)
    args = parser.parse_args()
    from src.control_store import digest
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    home = args.home.resolve()
    profile = {"instance_id": "binggo-"+digest(str(home).casefold())[:16], "source_root": str(ROOT),
               "user_home": str(home), "workspace_id": "binggo-repair", "scratch_root": str(work / "control-runs"),
               "mcp_python": str(args.runtime.resolve()), "test_python": str(args.app_python.resolve()),
               "enable_development": True, "expose_legacy": False,
               "application_command": [str(args.application.resolve()), "--serve"] if args.application else
                   [str(args.app_python.resolve()), str(ROOT / "scripts/run_control_dashboard.py"), "--home", str(home), "--port", str(args.port)],
               "application_environment": {}}
    path = work / "binggo-control-profile.json"
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    config = {"mcpServers": {"binggo": {"type": "stdio", "command": str(args.runtime.resolve()), "args": ["-m", "binggo_mcp"],
                                          "env": {"PYTHONPATH": str(ROOT / "mcp"), "BINGGO_CONTROL_PROFILE": str(path)}}}}
    (work / "binggo-mcp-host.json").write_text(json.dumps(config, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"profile": str(path), "instance_id": profile["instance_id"], "host_config": str(work / "binggo-mcp-host.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
