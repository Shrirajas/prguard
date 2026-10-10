"""Fetch the PR head into a local directory, without credentials."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from backend.config import settings
from backend.models import TaskContext


async def _git(*args: str, cwd: Path | None = None) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", "-c", "credential.helper=", *args,
        cwd=cwd,
        # Minimal env: no tokens, and never prompt for credentials.
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GIT_TERMINAL_PROMPT": "0", "HOME": "/nonexistent"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {err.decode(errors='replace')[-300:]}")


async def checkout_pr(ctx: TaskContext) -> Path:
    dest = settings.runs_dir / "checkouts" / ctx.review_id
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if settings.upstream == "mock":
        from backend.gateway import mock_upstream

        mock_upstream().materialize(ctx.pr_number, dest)
        return dest

    url = f"https://github.com/{ctx.repo_full_name}.git"  # public demo repo
    await _git("clone", "--depth", "1", "--quiet", url, str(dest))
    await _git("fetch", "--depth", "1", "--quiet", "origin", f"pull/{ctx.pr_number}/head", cwd=dest)
    await _git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=dest)
    shutil.rmtree(dest / ".git", ignore_errors=True)
    return dest
