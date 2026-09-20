from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AssistantMessageRequest(BaseModel):
    system: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
