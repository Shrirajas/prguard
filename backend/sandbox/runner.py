"""Run a PR's linter and tests.

GUARDED: locked Docker container (no network, no host env, read-only).
UNSAFE:  a plain subprocess on this machine with a "victim" env of fake decoys.
         This is the vulnerable pattern PRGuard exists to demonstrate.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.events import bus
from backend.models import Event, Mode, TaskContext

# Fake decoys only. Passed explicitly; the backend's real env is never inherited.
VICTIM_ENV = {
    "PRGUARD_CANARY": "canary-demo-0000",
    "GITHUB_TOKEN": "FAKE-DECOY",
    "ANTHROPIC_API_KEY": "FAKE-DECOY",
    "DATABASE_URL": "FAKE-DECOY",
}

CHECK_SCRIPT = (
    "pylint app; echo __PYLINT_EXIT=$?; "
    "python -m pytest -q -p no:cacheprovider; echo __PYTEST_EXIT=$?"
)
TIMEOUT_S = 60


def docker_command(src: Path, name: str) -> list[str]:
    # Deliberately no -e / --env-file: nothing from the host environment goes in.
    return [
        "docker", "run", "--rm", "--name", name,
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,size=64m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "256", "--memory", "512m", "--cpus", "1",
        "--user", "65534:65534",
        "-v", f"{src}:/src:ro", "-w", "/src",
        settings.sandbox_image, "sh", "-c", CHECK_SCRIPT,
    ]


async def docker_ready() -> str | None:
    """Return None if the sandbox can run, else a short reason."""
    if not shutil.which("docker"):
        return "docker is not installed"
    proc = await asyncio.create_subprocess_exec(
        "docker", "image", "inspect", settings.sandbox_image,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode == 0:
        return None
    text = err.decode(errors="replace").lower()
    if "no such image" in text:
        return f"sandbox image {settings.sandbox_image} is not built"
    return "docker daemon is not running"


async def _run(cmd: list[str], *, cwd: Path | None, env: dict[str, str] | None, kill: list[str] | None) -> tuple[str, str, bool]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_S)
        return out.decode(errors="replace"), err.decode(errors="replace"), False
    except asyncio.TimeoutError:
        proc.kill()
        if kill:
            killer = await asyncio.create_subprocess_exec(
                *kill, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await killer.wait()
        return "", f"timed out after {TIMEOUT_S}s", True


def _exit(marker: str, text: str) -> int | None:
    match = re.search(rf"__{marker}_EXIT=(\d+)", text)
    return int(match.group(1)) if match else None


def _skipped(reason: str) -> dict[str, Any]:
    return {"ran": False, "reason": reason, "exit_codes": {}, "stdout_tail": "", "stderr_tail": "", "duration_ms": 0}


async def run_checks(src: Path, ctx: TaskContext) -> dict[str, Any]:
    common = dict(review_id=ctx.review_id, mode=ctx.mode, source="sandbox")
    started = time.monotonic()

    if ctx.mode is Mode.GUARDED:
        problem = await docker_ready()
        if problem:
            # Fail closed: never fall back to running PR code on the host.
            bus.emit(Event(type="sandbox", payload={"status": "unavailable", "reason": problem}, **common))
            return _skipped(f"sandbox unavailable ({problem}); PR code was not run")
        profile = {"isolation": "docker", "network": "none", "host_env_passed": 0, "filesystem": "read-only",
                   "user": "nobody", "caps": "none"}
        bus.emit(Event(type="sandbox", payload={"status": "started", **profile}, **common))
        name = f"prguard-{ctx.review_id}"
        out, err, timed_out = await _run(docker_command(src.resolve(), name), cwd=None, env=None, kill=["docker", "kill", name])
    else:
        if ctx.repo_full_name != settings.target_repo or ctx.sender.lower() not in settings.allowed_unsafe_authors:
            reason = (f"UNSAFE mode runs PR code on this machine, so it only does that for {settings.target_repo} "
                      f"PRs opened by {sorted(settings.allowed_unsafe_authors)}; this PR is by {ctx.sender}")
            bus.emit(Event(type="sandbox", payload={"status": "refused", "reason": reason}, **common))
            return _skipped(reason)
        bin_dir = str(Path(sys.executable).parent)
        env = {**VICTIM_ENV, "PATH": f"{bin_dir}:/usr/bin:/bin"}
        profile = {"isolation": "none (host subprocess)", "network": "host", "host_env_passed": len(VICTIM_ENV),
                   "env_names": sorted(VICTIM_ENV), "filesystem": "read-write"}
        bus.emit(Event(type="sandbox", payload={"status": "started", **profile}, **common))
        out, err, timed_out = await _run(["sh", "-c", CHECK_SCRIPT], cwd=src, env=env, kill=None)

    result = {
        "ran": True,
        "timed_out": timed_out,
        "exit_codes": {"pylint": _exit("PYLINT", out), "pytest": _exit("PYTEST", out)},
        "stdout_tail": re.sub(r"__\w+_EXIT=\d+\n?", "", out)[-4000:],
        "stderr_tail": err[-1500:],
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    bus.emit(Event(type="sandbox", payload={"status": "finished", **profile, "exit_codes": result["exit_codes"],
                                           "duration_ms": result["duration_ms"], "timed_out": timed_out}, **common))
    return result
