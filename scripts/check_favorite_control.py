"""Check real favorite plans over stdio; optionally reconcile completed local receipts.

Recovery is restricted to plans with zero platform writes. A change in state
fails the frozen action policy before any new post is sent.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def check(profile_path, recover=False):
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    sys.path.insert(0, str(Path(profile["source_root"]) / "mcp"))
    from binggo_mcp.threaded_stdio import threaded_stdio
    parameters = StdioServerParameters(command=profile["mcp_python"], args=["-m", "binggo_mcp"],
        cwd=profile["source_root"], env={"PYTHONPATH": str(Path(profile["source_root"]) / "mcp"),
        "BINGGO_CONTROL_PROFILE": str(profile_path), "PYTHONIOENCODING": "utf-8"})
    transport = threaded_stdio(parameters) if os.name == "nt" else parameters
    report = {"mode": "live_read", "platform_write_requests": 0, "targets": [], "recovery": "not_requested"}
    async with Client(transport, read_timeout_seconds=180) as client:
        async def call(name, **args):
            response = await client.call_tool("binggo_" + name, {"instance_id": profile["instance_id"], **args})
            value = response.structured_content
            if response.is_error or not value or not value["ok"]:
                raise RuntimeError(json.dumps(value, ensure_ascii=False))
            return value["data"]
        identity = await call("instance_get")
        report["application_version"] = identity["version"]
        def writing():
            return {"expected_boot_id": identity["boot_id"], "idempotency_key": uuid.uuid4().hex}
        network = await call("test_run", suite_id="network-read", mode="live_read", **writing())
        while network["state"] == "running":
            network = await call("job_wait", job_id=network["job_id"], timeout_seconds=5)
        network_report = await call("test_get", test_run_id=network["test_run_id"])
        report["network"] = {k: network_report[k] for k in ["verdict", "platform_read_requests", "platform_write_requests"]}
        assert report["network"]["verdict"] == "passed", report["network"]
        identifiers = ["1265944423706918932", "1269265417023520787", "1269266233012781059"]
        plan = await call("participation_plan", dynamic_ids=identifiers, **writing())
        for identifier in identifiers:
            actions = [v for v in plan["actions"] if v["dynamic_id"] == identifier]
            favorite = next(v for v in actions if v["action"] == "favorite")
            assert favorite.get("skipped") and not favorite["required"], favorite
            report["targets"].append({"dynamic_id": identifier, "favorite_skipped": True,
                "favorite_reason": favorite.get("reason"), "pending_actions": [v["action"] for v in actions if not v.get("skipped")]})
        report["plan_id"] = plan["plan_id"]
        if recover and all(v.get("skipped") for v in plan["actions"]):
            job = await call("participation_execute", plan_id=plan["plan_id"], **writing())
            while job["state"] == "running":
                job = await call("job_wait", job_id=job["job_id"], timeout_seconds=5)
            report["recovery"] = {"job_id": job["job_id"], "state": job["state"], "result": job.get("result")}
            assert job["state"] == "success", job
            counts = (job.get("result") or {}).get("network_counts") or {}
            assert counts.get("platform_write_requests", 0) == 0, counts
            report["platform_write_requests"] = counts.get("platform_write_requests", 0)
            report["verification"] = await call("participation_verify", job_id=job["job_id"])
        elif recover:
            report["recovery"] = "not_run_pending_platform_actions"
    report["result"] = "passed"
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--recover-completed", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(check(args.profile.resolve(), args.recover_completed))
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
