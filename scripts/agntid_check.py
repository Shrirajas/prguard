"""Readiness check for the AgntID path. Talks only to your local runtime.

    .venv/bin/python scripts/agntid_check.py

Prints which GitHub tools the runtime exposes to PRGuard and how they map to
PRGuard's logical tool names, then opens and closes one task.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.gateway import toolmap  # noqa: E402
from backend.gateway.agntid_gw import AgntIDGateway, role_tools  # noqa: E402
from backend.models import Mode, TaskContext  # noqa: E402


async def main() -> int:
    gateway = AgntIDGateway()
    print(f"proxy: {gateway.proxy_url}   oauth token: {'set' if gateway.access_token else 'not set'}")
    try:
        client = await gateway.connect()
    except Exception as exc:
        print(f"FAILED to connect: {exc}\nIs the runtime up and registered?  ./scripts/agntid_runtime.sh status")
        return 1
    print("\nlogical tool                 -> runtime tool")
    for logical in toolmap.LOGICAL_TOOLS:
        hit = gateway.tools.get(logical)
        print(f"  {logical:26} -> {hit[0] if hit else '(not visible: attach the GitHub MCP server or check the profile)'}")
    print("\nper-role allowlists that will be sent with each delegation:")
    for role, names in role_tools().items():
        print(f"  {role:22} {[gateway.tools[n][0] for n in names if n in gateway.tools]}")
    ctx = TaskContext(review_id="readiness-check", repo_full_name="check/check", pr_number=1, head_sha="0" * 40,
                      sender="readiness", mode=Mode.GUARDED)
    try:
        task_id = await gateway._task(client, ctx)
        print(f"\ntask open: ok ({task_id})")
        await gateway.close_review(ctx)
        print("task close: ok")
    except Exception as exc:
        print(f"\ntask open FAILED: {exc}")
        return 1
    missing = [n for n in ("get_pull_request_files", "get_file_contents", "create_pull_request_review") if n not in gateway.tools]
    print("\nREADY: set ENFORCER=AGNTID" if not missing else f"\nNOT READY: runtime does not expose {missing}")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
