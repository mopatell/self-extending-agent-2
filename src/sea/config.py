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

    def reload(self) -> None:
        """Re-read every field from the environment (after a settings change)."""
        self.__dict__.update(Settings().__dict__)

    def workspace_for(self, conversation_id: str) -> Path:
        path = self.workspaces_dir / conversation_id
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()


settings = Settings()


# --------------------------------------------------------------------------- editable settings

ENV_FILE = Path(".env")

# env var -> (kind, label). Only these are exposed to the settings UI. Secrets are masked on read.
EDITABLE: dict[str, tuple[str, str]] = {
    "PLANNER_MODEL": ("text", "Planner model"),
    "TOOLSMITH_MODEL": ("text", "ToolSmith model"),
    "WORKER_MODEL": ("text", "Worker model"),
    "GROQ_API_KEY": ("secret", "Groq API key"),
    "OPENROUTER_API_KEY": ("secret", "OpenRouter API key"),
    "OPENAI_API_KEY": ("secret", "OpenAI API key"),
    "ANTHROPIC_API_KEY": ("secret", "Anthropic API key"),
    "AUTO_APPROVE": ("choice:none,safe,all", "Auto-approve"),
    "MAX_CONCURRENT_LLM_CALLS": ("int", "Concurrent LLM calls"),
    "MAX_PARALLEL_STEPS": ("int", "Parallel steps"),
    "MAX_LOOP_STEPS": ("int", "Max tool turns per step"),
    "TOOL_RESULT_MAX_CHARS": ("int", "Tool result max chars"),
    "SANDBOX": ("choice:docker,local", "Sandbox"),
}

MASK = "••••"


def mask(value: str) -> str:
    return f"{MASK}{value[-4:]}" if len(value) > 4 else (MASK if value else "")


def read_settings() -> list[dict[str, str]]:
    """Current values for the settings UI, secrets masked."""
    out = []
    for key, (kind, label) in EDITABLE.items():
        value = os.environ.get(key, "")
        out.append(
            {"key": key, "kind": kind, "label": label, "value": mask(value) if kind == "secret" else value}
        )
    return out


def write_settings(updates: dict[str, str], env_file: Path | None = None) -> None:
    """Persist to .env (keeping lines we don't manage), apply to the process, refresh `settings`."""
    env_file = env_file or ENV_FILE
    clean: dict[str, str] = {}
    for key, value in updates.items():
        if key not in EDITABLE:
            continue
        kind = EDITABLE[key][0]
        if kind == "secret" and value.startswith(MASK):
            continue  # masked value sent back unchanged -> keep the existing secret
        if kind == "int" and value.strip() and not value.strip().isdigit():
            raise ValueError(f"{key} must be a whole number")
        if kind.startswith("choice:") and value not in kind.split(":", 1)[1].split(","):
            raise ValueError(f"{key} must be one of {kind.split(':', 1)[1]}")
        clean[key] = value.strip()

    lines = env_file.read_text().splitlines() if env_file.exists() else []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        name = line.split("=", 1)[0].strip()
        if name in clean and not line.lstrip().startswith("#"):
            lines[i] = f"{name}={clean[name]}"
            seen.add(name)
    lines += [f"{k}={v}" for k, v in clean.items() if k not in seen]
    env_file.write_text("\n".join(lines) + "\n")

    for key, value in clean.items():
        os.environ[key] = value
    settings.reload()
    from sea import llm

    llm._cache.clear()  # providers hold api keys/models; rebuild them on next use
