"""Legacy names translate into the single v2 implementation."""
from __future__ import annotations
from jsonschema import Draft202012Validator


async def invoke_legacy(runtime, name, arguments, schema):
    from src.control_contract import catalog
    from src.control_store import ControlError
    if not Draft202012Validator(schema).is_valid(arguments):
        raise ControlError("VALIDATION_ERROR", "旧工具参数不符合契约")
    targets = {alias: e["name"] for e in catalog().values() for alias in e["x-legacyAliases"]}
    target = targets[name]
    args = {"instance_id": runtime.profile["instance_id"]}
    properties = catalog()[target]["inputSchema"]["properties"]
    args.update({k: v for k, v in arguments.items() if k in properties and v is not None})
    if "idempotency_key" in properties:
        args.update(runtime.base_args())
    if name in {"activities_list", "triple_targets_get"} and arguments.get("type"):
        args["lottery_type"] = arguments["type"]
    if name.startswith("job_refresh_"):
        args["scope"] = name.removeprefix("job_refresh_")
    if name in {"job_get", "account_login_cancel"}:
        listing = await runtime.invoke("binggo_jobs_list", {"instance_id": args["instance_id"], "limit": 1})
        rows = listing["data"].get("jobs", []) if listing["ok"] else []
        if not rows or name == "account_login_cancel" and rows[0]["action"] != "login":
            raise ControlError("NOT_FOUND", "没有匹配的任务")
        args["job_id"] = rows[0]["job_id"]
    if name in {"job_participate", "job_participate_triple"}:
        if name == "job_participate":
            ids = [arguments["dynamic_id"]]
        else:
            listing = await runtime.invoke("binggo_activities_list", {"instance_id": args["instance_id"], "status": "未参加", "page_size": 100})
            if not listing["ok"]:
                return listing
            ids = [v["dynamic_id"] for v in listing["data"]["activities"] if v["can_participate"]][:3]
        plan = await runtime.invoke("binggo_participation_plan", {**runtime.base_args(), "dynamic_ids": ids})
        if not plan["ok"]:
            return plan
        args["plan_id"] = plan["data"]["plan_id"]
    if name.startswith("participate_text_"):
        current = await runtime.invoke("binggo_settings_get", {"instance_id": args["instance_id"]})
        if not current["ok"]:
            return current
        args["expected_revision"] = current["data"]["revision"]
        if name == "participate_text_reset":
            args["patch"] = {"reset_to_defaults": True}
        elif name == "participate_text_mode_set":
            args["patch"] = {"participate_text_mode": arguments["mode"]}
        else:
            mode = arguments.get("mode") or current["data"].get("participate_text_mode")
            args["patch"] = {"participate_fallback_text" if mode == "random_comment" else "participate_text": arguments["text"]}
    if name in {"llm_settings_save", "llm_settings_test"}:
        if arguments.get("api_key") or arguments.get("base_url"):
            raise ControlError("VALIDATION_ERROR", "请在本机应用设置中管理密钥和提供商，MCP复用已保存配置")
        if name == "llm_settings_save":
            current = await runtime.invoke("binggo_llm_get", {"instance_id": args["instance_id"]})
            args["expected_revision"] = current["data"]["revision"]
    if name == "auto_start":
        current = await runtime.invoke("binggo_automation_status", {"instance_id": args["instance_id"]})
        args["workflow_revision"] = current["data"]["revision"]
    return await runtime.invoke(target, args)
