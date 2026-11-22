"""Per-control-job platform request accounting and read-only enforcement."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar

_current = ContextVar("binggo_control_network", default=None)


@contextmanager
def network_span(*, read_only=False):
    value = {"platform_read_requests": 0, "platform_write_requests": 0, "read_only": read_only}
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)


def record_request(method, url):
    value = _current.get()
    if value is None:
        return
    if value["platform_read_requests"] + value["platform_write_requests"] >= 110:
        raise RuntimeError("控制任务已达到110次平台请求预算")
    if method != "GET" and value["read_only"]:
        raise RuntimeError("只读控制任务禁止平台写请求")
    field = "platform_read_requests" if method == "GET" else "platform_write_requests"
    value[field] += 1
