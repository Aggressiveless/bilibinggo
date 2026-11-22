"""Control jobs reuse the application worker and its platform clients."""
from __future__ import annotations
import re
import time
from src.control_store import ControlError, digest, utc_now
from src.log_redact import redact_obj


def check_cancel(event):
    if event is not None and event.is_set():
        raise RuntimeError("任务已取消")


def run_control_action(action, params, *, on_progress, cancel_event):
    from src.control_telemetry import network_span
    with network_span(read_only=action in {"control_discover", "control_network", "control_llm_test"}) as counts:
        result = _run_control_action(action, params, on_progress=on_progress, cancel_event=cancel_event)
        result["result"]["network_counts"] = {key: value for key, value in counts.items() if key != "read_only"}
        return result


def _run_control_action(action, params, *, on_progress, cancel_event):
    from web.control_service import get_control_service
    service = get_control_service()
    job_id = params["_control_job_id"]
    check_cancel(cancel_event)
    on_progress(step=0, total=1, message="正在执行MCP控制任务")
    if action == "control_discover":
        result = discover(service, job_id, params, cancel_event)
    elif action == "control_import":
        result = import_candidates(service, params)
    elif action == "control_participate":
        result = execute_plan(service, job_id, params["plan_id"], on_progress, cancel_event)
    elif action == "control_network":
        result = diagnose(params["targets"])
    elif action == "control_llm_test":
        from src.llm_config import load_llm_config
        from src.llm_client import test_llm_connection
        config = load_llm_config()
        if not config:
            raise ControlError("LLM_NOT_READY", "未保存LLM配置")
        result = {"result": test_llm_connection(config)}
    elif action == "control_workflow":
        result = workflow(service, job_id, params["workflow"], on_progress, cancel_event)
    else:
        raise ControlError("NOT_FOUND", "未知控制任务")
    check_cancel(cancel_event)
    on_progress(step=1, total=1, message="控制任务完成")
    return {"ok": True, "message": "控制任务完成", "log": "MCP操作结果已保存", "result": redact_obj(result)}


def discover(service, job_id, params, event):
    from src.lottery_discovery import DiscoveryClient, DiscoveryStopped, discover as find, expand_keywords
    class CancellableClient(DiscoveryClient):
        def _http_get(self, *args, **kwargs):
            if event is not None and event.is_set():
                raise DiscoveryStopped("用户取消")
            return super()._http_get(*args, **kwargs)
    words = expand_keywords(params["keywords"]) if params.get("expand_keywords") else params["keywords"]
    comprehensive = params.get("mode") == "comprehensive_page"
    with CancellableClient(service.home / "data/lottery-discovery-cache.json", budget=params.get("request_budget", 96 if comprehensive else 24)) as client:
        try:
            report = find(client, words, video_limit=params.get("video_limit", 36 if comprehensive else 6), now=int(time.time()), comprehensive_page=comprehensive)
        finally:
            client.save()
    rows = []
    for category in ("open", "review", "closed", "joined"):
        for row in report[category]:
            url = row.get("url") or ""
            match = re.search(r"(?:/opus/|/dynamic/|t\.bilibili\.com/)(\d{15,22})", url)
            identifier = match[1] if match else None
            source = row.get("source_video") or url
            rows.append({"candidate_id": digest({"url": url, "source": source, "author": row.get("publisher_uid")})[:24],
                "dynamic_id": identifier, "author_mid": str(row.get("publisher_uid") or "1"), "source_url": source,
                "target_url": url or None, "resolution": "unresolved" if not identifier else ("author_home" if source != url else "direct"),
                "evidence_chain": [{"source": source, "author_mid": row.get("publisher_uid"), "target": url}],
                "rule_quotes": [row[k] for k in ("rules", "deadline", "draw") if isinstance(row.get(k), str) and row[k]],
                "excluded_reasons": [] if category == "open" and identifier else [category, "not_eligible"],
                "eligible": category == "open" and identifier is not None, "checked_at": utc_now(), "raw": row})
    value = {"job_id": job_id, "candidates": rows, "errors": report["errors"], "stopped": bool(report["stopped"]), "requests_used": report["requests"], "raw": report, "keywords": words}
    service.store.put("discovery", str(job_id), redact_obj(value))
    return {"discovery_job_id": job_id, "candidates": len(rows), "eligible": sum(v["eligible"] for v in rows), "requests_used": report["requests"], "partial": bool(value["stopped"] or value["errors"]), "errors": value["errors"]}


def import_candidates(service, params):
    from src.pipeline.refresh_all_pipeline import run_new_links_pipeline
    report = service.store.get("discovery", str(params["discovery_job_id"]))
    if not report:
        raise ControlError("NOT_FOUND", "发现报告不存在")
    mapping = {v["candidate_id"]: v for v in report["candidates"]}
    if not set(params["candidate_ids"]).issubset(mapping):
        raise ControlError("VALIDATION_ERROR", "候选ID不属于该报告")
    selected = [mapping[k] for k in dict.fromkeys(params["candidate_ids"])]
    if any(not v["eligible"] for v in selected):
        raise ControlError("VALIDATION_ERROR", "仅允许导入已解析的可用动态活动")
    result = run_new_links_pipeline([v["target_url"] for v in selected], workers=1)
    if not result.ok:
        raise ControlError("PLATFORM_REJECTED", result.message)
    return {"imported": result.persisted_count, "classified": result.classified_count, "message": result.message}


def execute_plan(service, job_id, plan_id, progress, event):
    from src.bilibili_client import BilibiliClient
    from src.forward_parser import _extract_from_detail_item
    from src.lottery_api import fetch_dynamic_detail
    from src.participate_preflight import ensure_activity_participatable
    from src.participation import participate_activity
    plan = service.get_plan(plan_id)
    report = {"targets": plan["targets"], "action_text": plan["action_text"], "target_texts": plan.get("target_texts", {}), "actions": [], "participation_outcome": "unknown", "results": []}
    service.store.put("participation", str(job_id), report)
    with BilibiliClient(timeout=12, warmup=False) as client:
        for index, target in enumerate(plan["targets"]):
            check_cancel(event)
            service.get_plan(plan_id)
            identifier, kind = target["dynamic_id"], target["lottery_type"]
            detail = fetch_dynamic_detail(client, identifier)
            if not detail or digest(_extract_from_detail_item(detail)) != plan["rule_snapshots"][identifier]:
                raise ControlError("PLAN_STALE", "活动正文已变化，未继续执行")
            ensure_activity_participatable(client, identifier, lottery_type_hint=kind)
            key = f"{plan['account_ref']}:{identifier}"
            prior = service.store.get("participation_intent", key)
            if prior:
                raise ControlError("OUTCOME_UNKNOWN", "目标已有执行记录，请先核验原任务", job_id=prior["job_id"])
            service.store.put("participation_intent", key, {"job_id": job_id, "plan_id": plan_id, "state": "sending", "ts": utc_now()})
            progress(step=index, total=len(plan["targets"]), message=f"正在参与 {identifier}")
            def on_step(step, total, message, action):
                check_cancel(event)
                progress(step=index * total + step, total=len(plan["targets"]) * total, message=message)
            recipe = {v["action"]: v for v in plan["actions"] if v["dynamic_id"] == identifier}
            allowed = {name for name, action in recipe.items() if not action.get("skipped")}
            value = participate_activity(client, dynamic_id=identifier, lottery_type=kind, action_text=plan.get("target_texts", {}).get(identifier, plan["action_text"]), dry_run=False, persist=True, on_step=on_step, allowed_actions=allowed)
            for item in value.actions:
                skipped = bool(recipe.get(item.action, {}).get("skipped"))
                report["actions"].append({"dynamic_id": identifier, "action": item.action, "required": recipe.get(item.action, {}).get("required", True), "attempted": not skipped,
                    "verification": "skipped" if skipped and not recipe.get(item.action, {}).get("required", True) else ("accepted" if item.ok else "failed"), "reason": item.detail, "remote_id": None, "evidence_artifact_ids": []})
            report["results"].append(redact_obj(value.to_dict()))
            report["participation_outcome"] = "joined" if all(v["status"] == "joined" for v in report["results"]) and len(report["results"]) == len(plan["targets"]) else "partial"
            service.store.put("participation", str(job_id), report)
            service.store.put("participation_intent", key, {"job_id": job_id, "state": value.status, "ts": utc_now()})
            if value.status != "joined":
                raise ControlError("PLATFORM_REJECTED", value.message, job_id=job_id)
    return {"participation_outcome": report["participation_outcome"], "participation_job_id": job_id}


def diagnose(targets):
    import httpx
    from src.bilibili_client import BilibiliClient, api_code
    rows = []
    for target in targets:
        started = time.monotonic()
        try:
            if target == "control":
                value = {"ok": True, "source": "application-process"}
            elif target == "bilibili":
                with BilibiliClient(timeout=8, warmup=False) as client:
                    payload = client.request_json("https://api.bilibili.com/x/web-interface/nav", retries=0)
                value = {"ok": api_code(payload) in {0, -101}, "platform_code": api_code(payload), "logged_in": bool((payload.get("data") or {}).get("isLogin"))}
            elif target == "updates":
                from src.update_check import check_for_updates
                payload = check_for_updates().to_dict()
                value = {"ok": payload.get("ok", False), "message": payload.get("message") or payload.get("error")}
            else:
                from src.llm_config import load_llm_config
                config = load_llm_config()
                if not config:
                    raise ControlError("LLM_NOT_READY", "未保存LLM配置")
                with httpx.Client(timeout=8, follow_redirects=False) as client:
                    response = client.get(config.base_url.rstrip("/") + "/models", headers={"Authorization": "Bearer " + config.api_key})
                value = {"ok": response.status_code < 400, "http_status": response.status_code}
            rows.append({"target": target, "elapsed_ms": int((time.monotonic()-started)*1000), **value})
        except Exception as exc:
            from src.log_redact import redact_text
            rows.append({"target": target, "ok": False, "error_kind": type(exc).__name__, "message": redact_text(str(exc))[:500], "elapsed_ms": int((time.monotonic()-started)*1000)})
    return {"probes": rows, "network_state": "online" if all(v["ok"] for v in rows) else "degraded"}


def workflow(service, job_id, config, progress, event):
    from web.actions import run_action
    summary, stages = {}, config["stages"]
    if "refresh" in stages:
        for source_id in config.get("source_ids") or []:
            check_cancel(event)
            run_action("refresh_source", {"source_id": source_id}, on_progress=progress, cancel_event=event)
    if "discover" in stages:
        summary["discovery"] = discover(service, job_id, {"keywords": config.get("keywords") or ["评论区抽奖"], "request_budget": config["request_budget"], "video_limit": 6, "mode": "standard"}, event)
        if summary["discovery"]["partial"]:
            raise ControlError("WORKFLOW_PAUSED", "发现阶段发生错误或预算停止")
        report = service.store.get("discovery", str(job_id))
        chosen = [v["candidate_id"] for v in report["candidates"] if v["eligible"]][:config["max_activities_per_run"]]
        if chosen:
            summary["import"] = import_candidates(service, {"discovery_job_id": job_id, "candidate_ids": chosen})
    if "plan" in stages or "execute" in stages:
        from web.activity_service import pick_triple_participate_targets
        rows = pick_triple_participate_targets()[:config["max_activities_per_run"]]
        if not rows:
            return {**summary, "skipped": "no_eligible_targets"}
        plan = service.make_plan([str(v["dynamic_id"]) for v in rows], 300)
        summary["plan_id"] = plan["plan_id"]
        if "execute" in stages:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            date = datetime.now(ZoneInfo(config["timezone"])).date().isoformat()
            budget = service.store.get("daily_budget", date) or {"reserved": 0}
            count = sum(not v.get("skipped", False) for v in plan["actions"])
            if budget["reserved"] + count > config["max_platform_actions_per_day"]:
                raise ControlError("RATE_LIMITED", "每日动作预算已用尽")
            service.store.put("daily_budget", date, {"reserved": budget["reserved"] + count})
            service.store.put("consumed_plan", plan["plan_id"], {"job_id": job_id})
            summary["participation"] = execute_plan(service, job_id, plan["plan_id"], progress, event)
            if "verify" in stages:
                summary["verification"] = service.verify(job_id)
    return summary
