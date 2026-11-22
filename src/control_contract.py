"""Shared validation for application control and its MCP adapter."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from src.control_store import ControlError


@lru_cache
def catalog():
    # __file__ is under _internal/src in the frozen build as well.
    path = Path(__file__).resolve().parents[1] / "mcp" / "contracts" / "control-v2.tools.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    return {entry["name"]: entry for entry in document["tools"]}


def validate_input(name, arguments):
    entry = catalog().get(name)
    if not entry:
        raise ControlError("NOT_FOUND", "未登记的工具")
    errors = sorted(Draft202012Validator(entry["inputSchema"]).iter_errors(arguments), key=lambda e: list(map(str, e.path)))
    if errors:
        # Do not echo argument values, which might contain credentials.
        raise ControlError("VALIDATION_ERROR", f"参数不符合契约：{'/'.join(map(str, errors[0].path)) or 'arguments'} ({errors[0].validator})")
    return entry


def validate_output(name, result):
    errors = list(Draft202012Validator(catalog()[name]["outputSchema"]).iter_errors(result))
    if errors:
        # Validator exception strings embed the complete instance, including logs.
        # Report only a bounded schema location, never the rejected payload.
        path = "/".join(map(str, errors[0].absolute_path)) or "result"
        raise ControlError("INVALID_RESPONSE", f"工具返回值不符合契约：{path} ({errors[0].validator})")
    return result


def envelope(instance_id, data=None, *, status="completed", error=None, request_id=None):
    import uuid
    return {"ok": error is None, "status": status, "instance_id": instance_id,
            "request_id": request_id or uuid.uuid4().hex, "data": data or {}, "error": error}


def error_result(instance_id, exc, request_id=None):
    from src.log_redact import redact_text
    code = exc.code if isinstance(exc, ControlError) else "INTERNAL_ERROR"
    return envelope(instance_id, status="unknown" if code == "OUTCOME_UNKNOWN" else "failed", request_id=request_id,
                    error={"code": code, "message": (redact_text(str(exc)) or type(exc).__name__)[:4000], "retryable": bool(getattr(exc, "retryable", False)),
                           "retry_after_seconds": None, "job_id": getattr(exc, "job_id", None)})
