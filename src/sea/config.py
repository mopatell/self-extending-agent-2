"""Settings loaded from the environment (and a local .env file if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    planner_model: str = field(default_factory=lambda: _env("PLANNER_MODEL", "groq:openai/gpt-oss-120b"))
    toolsmith_model: str = field(default_factory=lambda: _env("TOOLSMITH_MODEL", "groq:openai/gpt-oss-120b"))
    worker_model: str = field(default_factory=lambda: _env("WORKER_MODEL", "groq:openai/gpt-oss-20b"))

    max_concurrent_llm_calls: int = field(default_factory=lambda: int(_env("MAX_CONCURRENT_LLM_CALLS", "2")))
    max_parallel_steps: int = field(default_factory=lambda: int(_env("MAX_PARALLEL_STEPS", "2")))
    max_loop_steps: int = field(default_factory=lambda: int(_env("MAX_LOOP_STEPS", "12")))
    tool_result_max_chars: int = field(default_factory=lambda: int(_env("TOOL_RESULT_MAX_CHARS", "4000")))

    # "none" | "safe" (auto-approve plans) | "all" (auto-approve plans and risky tool calls)
    auto_approve: str = field(default_factory=lambda: _env("AUTO_APPROVE", "none"))

    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", "data/sea.db")))
    workspaces_dir: Path = field(default_factory=lambda: Path(_env("WORKSPACES_DIR", "workspaces")))
    sandbox_image: str = field(default_factory=lambda: _env("SANDBOX_IMAGE", "sea-sandbox"))

    def workspace_for(self, conversation_id: str) -> Path:
        path = self.workspaces_dir / conversation_id
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()


settings = Settings()
