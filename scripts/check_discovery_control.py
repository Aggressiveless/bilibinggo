"""Verify real search -> candidate verification -> import over MCP stdio.

No target activity IDs are supplied: all targets must be discovered from the
keyword. This check imports local activity records and never participates.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def check(profile_path, *, keyword, rounds=2, budget=180, import_limit=2, videos=36):
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    sys.path.insert(0, str(Path(profile["source_root"]) / "mcp"))
    from binggo_mcp.threaded_stdio import threaded_stdio
    parameters = StdioServerParameters(command=profile["mcp_python"], args=["-m", "binggo_mcp"], cwd=profile["source_root"],
        env={"PYTHONPATH": str(Path(profile["source_root"]) / "mcp"), "BINGGO_CONTROL_PROFILE": str(profile_path), "PYTHONIOENCODING": "utf-8"})
    report = {"keyword": keyword, "rounds": [], "platform_read_requests": 0, "platform_write_requests": 0, "imported": 0}
    imported_ids = set()
    started = time.monotonic()
    transport = threaded_stdio(parameters) if os.name == "nt" else parameters
    async with Client(transport, read_timeout_seconds=180) as client:
        async def call(name, **args):
            result = await client.call_tool("binggo_" + name, {"instance_id": profile["instance_id"], **args})
            value = result.structured_content
            if result.is_error or not value or not value["ok"]:
                raise RuntimeError(f"{name}: " + json.dumps(value, ensure_ascii=False))
            return value["data"]
        identity = await call("instance_get")
        report.update(application_version=identity["version"], instance_id=identity["instance_id"])
        def writing():
            return {"expected_boot_id": identity["boot_id"], "idempotency_key": uuid.uuid4().hex}
        async def finish(job):
            last = None
            while job["state"] == "running":
                job = await call("job_wait", job_id=job["job_id"], timeout_seconds=5)
                message = (job.get("progress") or {}).get("message")
                if message != last:
                    print(message, flush=True)
                    last = message
            if job["state"] != "success":
                raise RuntimeError("Job did not succeed: " + json.dumps(job, ensure_ascii=False))
            counts = (job.get("result") or {}).get("network_counts") or {}
            for key in ("platform_read_requests", "platform_write_requests"):
                report[key] += counts.get(key, 0)
            assert counts.get("platform_write_requests", 0) == 0, "Search/import issued a platform write"
            return job
        for index in range(rounds):
            print(f"ROUND {index + 1}", flush=True)
            job = await finish(await call("discovery_start", keywords=[keyword], mode="comprehensive_page", video_limit=videos,
                                         request_budget=budget, author_dynamic_limit=2, min_interval_seconds=2, resume=True, **writing()))
            found = await call("discovery_get", job_id=job["job_id"], limit=100)
            candidates = [value for value in found["candidates"] if value["eligible"]]
            assert not imported_ids.intersection(value["dynamic_id"] for value in candidates), "Already imported activities were selected again"
            entry = {"discovery_job_id": job["job_id"], "summary": job.get("result"),
                     "eligible": [{"dynamic_id": value["dynamic_id"], "url": value["target_url"],
                                   "prize": value["raw"].get("prize"), "resolution": value["resolution"],
                                   "evidence_chain": value["evidence_chain"]} for value in candidates]}
            if index:
                assert found["resumed"], "Second round lost its durable cursor"
                assert found["session_id"] == report["rounds"][0]["summary"]["session_id"]
            selected = candidates[:import_limit]
            if selected:
                imported = await finish(await call("discovery_import", discovery_job_id=job["job_id"],
                    candidate_ids=[value["candidate_id"] for value in selected], **writing()))
                entry["import"] = imported.get("result")
                report["imported"] += (imported.get("result") or {}).get("imported", 0)
                entry["persisted"] = []
                for value in selected:
                    activity = await call("activity_get", dynamic_id=value["dynamic_id"])
                    assert activity["can_participate"], activity
                    imported_ids.add(value["dynamic_id"])
                    entry["persisted"].append({k: activity.get(k) for k in ("dynamic_id", "lottery_type", "can_participate", "excluded_reasons")})
            report["rounds"].append(entry)
            print("ROUND_RESULT", json.dumps(entry, ensure_ascii=False), flush=True)
    assert report["imported"] > 0, "No new, verified activity was imported"
    assert report["rounds"][-1]["summary"]["scan_completed"], "Did not scan the selected page"
    report.update(result="passed", elapsed_seconds=round(time.monotonic() - started, 1))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--keyword", default="转发抽奖")
    parser.add_argument("--rounds", type=int, choices=range(1, 4), default=2)
    parser.add_argument("--budget", type=int, choices=range(10, 301), default=180)
    parser.add_argument("--videos", type=int, choices=range(1, 37), default=36)
    parser.add_argument("--import-limit", type=int, choices=range(1, 4), default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(check(args.profile.resolve(), keyword=args.keyword, rounds=args.rounds, budget=args.budget, import_limit=args.import_limit, videos=args.videos))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
