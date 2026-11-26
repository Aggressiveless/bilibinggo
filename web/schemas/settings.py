from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ParticipateTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participate_text: str | None = Field(default=None, max_length=233)
    participate_fallback_text: str | None = Field(default=None, max_length=233)
    participate_text_mode: str | None = None


class LlmSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(default="")
    base_url: str = Field(default="")
    model_name: str = Field(default="")


class LlmProfileTestRequest(LlmSettingsRequest):
    profile_id: str | None = Field(default=None, max_length=32)


class LlmProfileRequest(LlmProfileTestRequest):
    name: str = Field(default="", max_length=80)


class LlmProfileOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_ids: list[str]
