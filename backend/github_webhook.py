"""GitHub webhook handling: verify the signature, then derive a trusted TaskContext."""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from typing import Any

from backend.models import Mode, TaskContext

_PR_URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)/?$")
REVIEWABLE_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check X-Hub-Signature-256. With no secret configured, nothing verifies."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def new_review_id() -> str:
    return uuid.uuid4().hex[:12]


def context_from_webhook(payload: dict[str, Any], mode: Mode) -> TaskContext | None:
    if payload.get("action") not in REVIEWABLE_ACTIONS or "pull_request" not in payload:
        return None
    pr = payload["pull_request"]
    return TaskContext(
        review_id=new_review_id(),
        repo_full_name=payload["repository"]["full_name"],
        pr_number=int(pr["number"]),
        head_sha=pr["head"]["sha"],
        base_ref=pr["base"]["ref"],
        sender=pr["user"]["login"],
        mode=mode,
    )


def parse_pr_url(url: str) -> tuple[str, int] | None:
    match = _PR_URL.match(url.strip())
    return (match.group(1), int(match.group(2))) if match else None
