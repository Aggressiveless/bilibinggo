
"""Versioned, allowlisted payloads exchanged by Binggo instances."""
from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from web.activity_share import SharedActivity, MAX_LOTTERY_TIME

PEER_PATH = "/api/cluster/peer/v1"
TARGET = 1000
POLL_SECONDS = 5
PAGE_SIZE = 100
KEYWORDS = ("抽奖", "福利", "奖品", "转发抽奖", "抽奖活动", "粉丝福利")
BV = Annotated[str, Field(pattern=r"^BV[0-9A-Za-z]{10}$")]
Token = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
NodeId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
TaskKind = Literal["search", "video"]
LegacyTaskKind = Literal["dynamic", "article", "short", "author"]
LINK_PATTERNS = {
    "video": r"^BV[0-9A-Za-z]{10}$",
    "dynamic": r"^[0-9]{18,19}$",
    "article": r"^[1-9][0-9]{0,11}$",
    "short": r"^[A-Za-z0-9]{3,40}$",
    "author": r"^[1-9][0-9]{0,19}$",
}


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HostRequest(Payload):
    name: str = Field(min_length=1, max_length=40)
    address: str = Field(max_length=256)


class JoinRequest(HostRequest):
    invite: Token


class Register(Payload):
    name: str = Field(min_length=1, max_length=40)
    node: NodeId
    token: Token


class Reference(Payload):
    kind: Literal["video", "dynamic", "article", "short", "author"]
    key: str = Field(max_length=40)

    @model_validator(mode="after")
    def validate_key(self):
        import re
        if not re.fullmatch(LINK_PATTERNS[self.kind], self.key):
            raise ValueError("线索标识无效")
        return self


class Activity(SharedActivity):
    deadline: int | None = Field(default=None, gt=0, le=MAX_LOTTERY_TIME)
    closed: bool = False


class Lease(Payload):
    task_id: int = Field(gt=0)
    lease: NodeId


class Task(Lease):
    # Still accept leases from older v1 coordinators during rolling upgrades.
    kind: TaskKind | LegacyTaskKind
    key: str = Field(max_length=80)
    keyword: str = Field(default="", max_length=40)
    order: Literal["pubdate", "totalrank"] = "pubdate"
    page: int = Field(default=1, ge=1, le=60)
    depth: int = Field(default=0, ge=0, le=4)

    @model_validator(mode="after")
    def validate_task(self):
        if self.kind == "search":
            if self.keyword not in KEYWORDS:
                raise ValueError("未知检索词")
        else:
            Reference(kind=self.kind, key=self.key)
        return self


class Report(Lease):
    outcome: Literal["done", "retry", "cooldown"]
    references: list[Reference] = Field(default_factory=list, max_length=120)
    activities: list[Activity] = Field(default_factory=list, max_length=16)
    more_pages: bool = False
    cooldown: int = Field(default=900, ge=900, le=86400)
    model_calls: int = Field(default=0, ge=0, le=128)
    reason: Literal["platform", "model", "network", "analysis_busy", "worker"] | None = None


class Change(Payload):
    seq: int = Field(gt=0)
    activity: Activity


class Exchange(Payload):
    node: NodeId
    after: int = Field(default=0, ge=0, lt=2**63)
    activities: list[Activity] = Field(default_factory=list, max_length=PAGE_SIZE)
    work: bool = False
    video_detection: bool | None = None
    working: Lease | None = None
    report: Report | None = None


class Analysis(Lease):
    node: NodeId
    key: Token
    fields: dict[str, str | int] | None = Field(default=None, max_length=12)

    @model_validator(mode="after")
    def validate_fields(self):
        allowed = {"prize", "rules", "deadline", "draw", "closed", "open",
                   "model_assessment", "model_reason", "model_confidence", "rules_summary"}
        if self.fields is not None and (
            not self.fields.keys() <= allowed
            or any(isinstance(value, str) and len(value) > 8000 for value in self.fields.values())
            or self.fields.get("model_assessment") not in {"lottery", "entry", "collection", "not_lottery", "uncertain"}
        ):
            raise ValueError("模型结果格式无效")
        return self


class Feed(Payload):
    changes: list[Change] = Field(max_length=PAGE_SIZE)
    task: Task | None
    status: dict


def normalize_address(address: str) -> str:
    try:
        url = urlsplit(address.strip().rstrip("/"))
        if (not url.hostname or not url.hostname.isascii() or url.username is not None
                or url.password is not None or url.path or url.query or url.fragment
                or url.port == 0 or any(c.isspace() for c in address)
                or (url.scheme != "https" and not (
                    url.scheme == "http" and url.hostname in {"127.0.0.1", "::1", "localhost"}
                ))):
            raise ValueError
        return str(httpx.URL(url.geturl())).rstrip("/")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("请输入主服务器的 HTTPS 地址，例如 https://binggo.example.com") from exc
