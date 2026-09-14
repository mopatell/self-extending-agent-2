"""Where agent-written code and shell commands run.

`DockerSandbox` is the real thing (M2). `LocalSandbox` runs on the host with a
timeout only - it exists for machines without Docker and for tests, and must be
opted into with SANDBOX=local.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sea.config import settings


@dataclass
class Result:
    ok: bool
    output: str


class Sandbox(Protocol):
    async def shell(self, command: str, workspace: Path, timeout: int = 30) -> Result: ...

    async def run_python(
        self, code: str, workspace: Path, stdin: str = "", network: bool = False, timeout: int = 30
    ) -> Result: ...


async def _run(argv: list[str], cwd: Path | None, stdin: str, timeout: int) -> Result:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin.encode()), timeout)
    except TimeoutError:
        proc.kill()
        return Result(False, f"timed out after {timeout}s")
    return Result(proc.returncode == 0, out.decode(errors="replace"))


class LocalSandbox:
    """Host execution. No isolation beyond a timeout. Dev/test only."""

    async def shell(self, command: str, workspace: Path, timeout: int = 30) -> Result:
        return await _run(["bash", "-c", command], workspace, "", timeout)

    async def run_python(
        self, code: str, workspace: Path, stdin: str = "", network: bool = False, timeout: int = 30
    ) -> Result:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(code)
            path = f.name
        try:
            return await _run(["python3", path], workspace, stdin, timeout)
        finally:
            os.unlink(path)


class DockerSandbox:
    """Runs everything in the `sea-sandbox` image: no network by default, memory/cpu/pid limits,
    workspace mounted at /workspace."""

    def __init__(self, image: str | None = None):
        self.image = image or settings.sandbox_image

    def _argv(self, workspace: Path, network: bool, extra: list[str]) -> list[str]:
        return [
            "docker", "run", "--rm", "-i",
            "--network", "bridge" if network else "none",
            "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
            "-v", f"{workspace}:/workspace", "-w", "/workspace",
            self.image, *extra,
        ]  # fmt: skip

    async def shell(self, command: str, workspace: Path, timeout: int = 30) -> Result:
        return await _run(self._argv(workspace, False, ["bash", "-c", command]), None, "", timeout)

    async def run_python(
        self, code: str, workspace: Path, stdin: str = "", network: bool = False, timeout: int = 30
    ) -> Result:
        # Code goes in via a temp file mounted read-only; stdin carries the tool arguments.
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(code)
            path = f.name
        try:
            argv = self._argv(workspace, network, ["python", "/tmp/sea_script.py"])
            argv[argv.index("-w") : argv.index("-w")] = ["-v", f"{path}:/tmp/sea_script.py:ro"]
            return await _run(argv, None, stdin, timeout)
        finally:
            os.unlink(path)


def get_sandbox() -> Sandbox:
    if os.environ.get("SANDBOX", "docker") == "local":
        return LocalSandbox()
    if shutil.which("docker") is None:
        raise RuntimeError("Docker not found. Install Docker, or set SANDBOX=local (no isolation!).")
    return DockerSandbox()


# --------------------------------------------------------------------------- tool drivers

TESTS_PASSED = "__SEA_TESTS_PASSED__"
RESULT_MARK = "__SEA_RESULT__"


async def run_tool_tests(sandbox: Sandbox, source: str, test_code: str, workspace: Path) -> Result:
    """Runs the tool's tests; ok only if every assert passed."""
    script = f"{source}\n\n{test_code}\n\nprint({TESTS_PASSED!r})\n"
    result = await sandbox.run_python(script, workspace, network=False, timeout=30)
    passed = result.ok and TESTS_PASSED in result.output
    return Result(passed, result.output.replace(TESTS_PASSED, "").strip())


async def call_tool(
    sandbox: Sandbox, source: str, name: str, arguments: dict, workspace: Path, network: bool
) -> Result:
    """Calls tool `name` with JSON arguments on stdin; the JSON result comes back after a marker."""
    script = (
        "import json, sys\n"
        f"{source}\n\n"
        "_args = json.load(sys.stdin)\n"
        f"_result = {name}(**_args)\n"
        f"print({RESULT_MARK!r} + json.dumps(_result, default=str))\n"
    )
    result = await sandbox.run_python(
        script, workspace, stdin=json.dumps(arguments), network=network, timeout=60
    )
    if RESULT_MARK not in result.output:
        return Result(False, result.output.strip() or "tool produced no result")
    logs, _, payload = result.output.rpartition(RESULT_MARK)
    value = json.loads(payload.strip())
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return Result(True, (logs.strip() + "\n" + text).strip() if logs.strip() else text)
