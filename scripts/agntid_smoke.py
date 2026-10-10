"""Live smoke test of the AgntID contract PRGuard relies on, using only the
runtime's built-in `demo_add_numbers` tool (no GitHub needed).

    .venv/bin/python scripts/agntid_smoke.py

It opens a root task, calls the tool as the root agent, opens a delegation for
a child agent and calls again, then sends an out-of-range call. If you added
the Quick Start input limits (10-20, 20-30) the last call should be denied.
Every result is printed as the runtime returned it; nothing is assumed.
"""

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agntid  # noqa: E402

from backend.config import settings  # noqa: E402

ROOT, CHILD, USER = "prguard-orchestrator", "prguard-security", "gh:smoke-test"
PROMPT = "Use demo.add_numbers to add 11 and 21. Do not perform any other action."


def show(label: str, result) -> None:
    if isinstance(result, agntid.DenialResult):
        denial = agntid.get_last_denial(clear=True) or {}
        structured = "error" in denial or "outcome" in denial
        print(f"  {label}: DENIED  structured={structured}  reason={denial.get('reason') or result.message!r}"
              f"  policy={denial.get('policy_name')!r}  step={denial.get('denial_step')!r}  outcome={denial.get('outcome')!r}")
    else:
        print(f"  {label}: ALLOWED  result={getattr(result, 'value', result)!r}")


async def main() -> int:
    url = settings.agntid_proxy_url
    print(f"runtime MCP proxy: {url}")
    try:
        client = agntid.AgntidMCPClient(url, access_token=settings.agntid_access_token or os.getenv("AGNTID_ACCESS_TOKEN") or None)
        await client.__aenter__()
    except Exception as exc:
        print(f"cannot connect: {exc}")
        return 1
    try:
        tools = [t.name for t in await agntid.get_tools_list(client)]
        print(f"visible tools ({len(tools)}): {tools}")
        if "demo_add_numbers" not in tools:
            print("demo_add_numbers is not visible: finish runtime Setup (5 of 5) in the portal first.")
            return 1

        task_id = agntid.create_task(ROOT, USER, PROMPT)
        context = agntid.ExecutionContext(
            framework=agntid.FrameworkInfo(name="prguard-langgraph", adapter_version="0.1", capabilities=("delegation",)),
            turn_id=task_id, root_task_id=task_id, root_agent_id=ROOT,
            conversation=agntid.ConversationContext(active_constraints=agntid.extract_explicit_constraints(PROMPT)),
        )
        opened = await agntid.send_task_open_details(client, task_id, ROOT, USER, PROMPT, execution_context=context)
        print(f"task open: ok={opened.ok} reason={opened.reason!r} intent_prepared={opened.intent_prepared}"
              f" engine={opened.snapshot.engine_id if opened.snapshot else None!r}"
              f" shortlist={list(opened.snapshot.relevant_tool_names) if opened.snapshot else None}")
        if not opened.ok:
            return 1

        show("root agent, 11 + 21", await agntid.wrap_client(client, task_id).call_tool_async(
            "demo_add_numbers", {"first_number": 11, "second_number": 21}))

        delegation = agntid.DelegationContext(
            delegation_id=str(uuid4()), parent_agent_id=ROOT, acting_agent_id=CHILD,
            delegated_intent=agntid.DelegatedIntent(text="Add 11 and 21 with demo.add_numbers.", source="prguard_orchestrator"),
            tool_allowlist=("demo_add_numbers",),
        )
        try:
            raw = await agntid.send_delegation_open(client, task_id, delegation)
            ok, message = agntid.parse_task_open_result(raw)
            print(f"delegation open ({CHILD}): ok={ok} message={message!r}")
        except Exception as exc:
            ok = False
            print(f"delegation open ({CHILD}): FAILED {type(exc).__name__}: {str(exc)[:200]}")
        if ok:
            child = agntid.wrap_client(client, task_id, delegation.delegation_id)
            show("child agent, 11 + 21", await child.call_tool_async("demo_add_numbers", {"first_number": 11, "second_number": 21}))
            show("child agent, 50 + 21 (denied if limits are set)", await child.call_tool_async(
                "demo_add_numbers", {"first_number": 50, "second_number": 21}))
            await agntid.send_delegation_close(client, task_id, delegation.delegation_id)
        else:
            show("root agent, 50 + 21 (denied if limits are set)", await agntid.wrap_client(client, task_id).call_tool_async(
                "demo_add_numbers", {"first_number": 50, "second_number": 21}))
        await agntid.send_task_close(client, task_id)
        print("task closed. Check AgntID Reporting for these events.")
        return 0
    finally:
        await client.__aexit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
