"""Exercise the real stdio transport against a registered application."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import uuid
import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def check(profile_path, *, run_tests=False, mode="auto", lifecycle=False, suite="participation-regression"):
    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    parameters = StdioServerParameters(command=profile["mcp_python"], args=["-m", "binggo_mcp"], cwd=profile["source_root"],
        env={"PYTHONPATH": str(Path(profile["source_root"]) / "mcp"), "BINGGO_CONTROL_PROFILE": str(profile_path), "PYTHONIOENCODING": "utf-8"})
    results = {"mode": mode, "calls": [], "platform_mutations_requested": 0}
    sys.path[:0] = [str(Path(profile["source_root"]) / "mcp")]
    from binggo_mcp.threaded_stdio import threaded_stdio
    transport = threaded_stdio(parameters) if os.name == "nt" else parameters
    results["transport"] = "stdio-anonymous-pipes" if os.name == "nt" else "stdio"
    async with Client(transport, mode=mode, read_timeout_seconds=60) as client:
        tools = await client.list_tools()
        assert len(tools.tools) == (53 if profile.get("enable_development") else 49)
        results["tool_count"] = len(tools.tools)
        instance = profile["instance_id"]
        async def call(name, arguments=None):
            result = await client.call_tool("binggo_" + name, arguments if arguments is not None else {"instance_id": instance})
            value = result.structured_content
            if result.is_error or not value or not value["ok"]:
                raise AssertionError(f"{name}: {value or result.content}")
            results["calls"].append(name)
            return value["data"]
        await call("instances_list", {})
        identity = await call("instance_get")
        results["application_version"] = identity["version"]
        for name in ("capabilities_get", "health_get", "account_get", "summary_get", "sources_list", "activities_list", "watch_users_list", "settings_get", "llm_get", "automation_status", "jobs_list", "logs_query", "test_suites_list"):
            await call(name)
        invalid = await client.call_tool("binggo_account_get", {"instance_id": "wrong-instance"})
        assert invalid.is_error
        results["wrong_instance_rejected"] = True
        if lifecycle:
            restarting = await call("app_restart", {"instance_id": instance, "expected_boot_id": identity["boot_id"], "idempotency_key": uuid.uuid4().hex, "drain_timeout_seconds": 10})
            while restarting["state"] == "running":
                restarting = await call("job_wait", {"instance_id": instance, "job_id": restarting["job_id"], "timeout_seconds": 5})
            assert restarting["state"] == "success", restarting
            current = await call("instance_get")
            assert current["boot_id"] != identity["boot_id"]
            identity = current
            results["controlled_restart"] = "passed"
        if run_tests:
            job = await call("test_run", {"instance_id": instance, "expected_boot_id": identity["boot_id"], "idempotency_key": uuid.uuid4().hex,
                                          "suite_id": suite, "mode": "offline"})
            while job["state"] == "running":
                job = await call("job_wait", {"instance_id": instance, "job_id": job["job_id"], "timeout_seconds": 5})
            assert job["state"] == "success", job
            report = await call("test_get", {"instance_id": instance, "test_run_id": job["test_run_id"]})
            assert report["verdict"] == "passed", report
            results["offline_suite"] = {k: report[k] for k in ("suite_id", "verdict", "passed", "failed", "platform_read_requests", "platform_write_requests")}
    results["result"] = "passed"
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--suite", choices=["participation-regression", "full-regression"], default="participation-regression")
    parser.add_argument("--lifecycle", action="store_true", help="Restart the selected instance; use an isolated profile for smoke tests")
    parser.add_argument("--mode", choices=["auto", "legacy"], default="auto")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    value = asyncio.run(check(args.profile.resolve(), run_tests=args.run_tests, mode=args.mode, lifecycle=args.lifecycle, suite=args.suite))
    if args.output:
        args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
