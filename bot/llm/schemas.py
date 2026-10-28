"""Pydantic schemas for orchestrator plan and judge verdict."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Material(BaseModel):
    surface: str = Field(description="Surface in the scene: floor, wall, kitchen facade…")
    texture_file_id: int
    texture_name: str | None = None

    @field_validator("surface")
    @classmethod
    def _surface_not_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("surface must not be empty")
        return v


class Job(BaseModel):
    base_file_id: int
    n_variants: int = 4
    materials: list[Material] = Field(default_factory=list)
    prompt: str
    aspect_ratio: str | None = None

    @field_validator("n_variants")
    @classmethod
    def _n_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("n_variants must be >= 1")
        return v


class Plan(BaseModel):
    action: Literal["generate", "ask_clarification", "chat"]
    question: str | None = None
    jobs: list[Job] = Field(default_factory=list)
    reply: str | None = None  # short answer for action=chat

    @field_validator("question")
    @classmethod
    def _question_not_empty(cls, v, info):
        if info.data.get("action") == "ask_clarification" and not (v or "").strip():
            raise ValueError("question is required for ask_clarification")
        return v


class JudgeScores(BaseModel):
    geometry: float = Field(ge=0, le=10, description="geometry/perspective/composition preservation")
    textures: float = Field(ge=0, le=10, description="texture accuracy: color, scale, direction")
    lighting: float = Field(ge=0, le=10, description="realism of light and shadows")
    artifacts: float = Field(ge=0, le=10, description="absence of artifacts, 10 = clean")
    comment: str | None = None


def orchestrator_tool_schema() -> list[dict]:
    """Tool definition for forced structured plan output."""
    return [
        {
            "type": "function",
            "function": {
                "name": "submit_plan",
                "description": (
                    "Return the structured plan for the user's brief. "
                    "Use ask_clarification when key data is missing or contradictory."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["generate", "ask_clarification", "chat"],
                        },
                        "question": {
                            "type": ["string", "null"],
                            "description": "One clarifying question (for ask_clarification)",
                        },
                        "reply": {
                            "type": ["string", "null"],
                            "description": "Short chat reply (for chat action)",
                        },
                        "jobs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "base_file_id": {
                                        "type": "integer",
                                        "description": "id of the mold file (or result file to build upon)",
                                    },
                                    "n_variants": {"type": "integer", "minimum": 1, "maximum": 8},
                                    "prompt": {
                                        "type": "string",
                                        "description": "Image edit prompt, in the scene's language",
                                    },
                                    "aspect_ratio": {
                                        "type": ["string", "null"],
                                        "enum": ["1:1", "4:3", "3:2", "16:9", "4:5", "2:3", "9:16"],
                                    },
                                    "materials": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "surface": {"type": "string"},
                                                "texture_file_id": {"type": "integer"},
                                                "texture_name": {"type": ["string", "null"]},
                                            },
                                            "required": ["surface", "texture_file_id"],
                                            "additionalProperties": False,
                                        },
                                    },
                                },
                                "required": ["base_file_id", "prompt", "n_variants"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["action", "jobs"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def judge_tool_schema() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "submit_scores",
                "description": "Score one candidate render (0-10 on each rubric).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "geometry": {"type": "number", "minimum": 0, "maximum": 10},
                        "textures": {"type": "number", "minimum": 0, "maximum": 10},
                        "lighting": {"type": "number", "minimum": 0, "maximum": 10},
                        "artifacts": {"type": "number", "minimum": 0, "maximum": 10},
                        "comment": {"type": ["string", "null"]},
                    },
                    "required": ["geometry", "textures", "lighting", "artifacts"],
                    "additionalProperties": False,
                },
            },
        }
    ]
