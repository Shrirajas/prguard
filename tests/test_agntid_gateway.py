"""AgntIDGateway against an in-memory FAKE of the AgntID proxy.

This checks our use of the real agntid-sdk (task, delegation, approval, denial
parsing). It says nothing about how the real runtime decides; the fake's rules
are made up for the test.
"""

import json

import pytest

agntid = pytest.importorskip("agntid")
from fastmcp import FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from backend.gateway.agntid_gw import AgntIDGateway, resolve_tools  # noqa: E402
from backend.models import Denial, Enforcer, Mode, TaskContext, UpstreamError  # noqa: E402


def deny(reason: str, policy: str, verdict: str = "") -> ToolError:
    decision = {"allowed": False, "reason": reason, "policy_name": policy}
    if verdict:
        decision["intent_validation"] = {"verdict": verdict, "verdict_reason": reason}
    return ToolError(json.dumps({"__agntid_error": "Request denied", "__agntid_policy_decision": decision}))


def fake_runtime(seen: dict):
    mcp = FastMCP("fake-agntid-runtime")
    seen.update(tasks={}, delegations={}, closed=[], calls=[])

    @mcp.tool
    def agntid_task_open(task_id: str, agent_id: str, user_id: str, prompt: str, execution_context: dict | None = None) -> dict:
        seen["tasks"][task_id] = {"agent_id": agent_id, "user_id": user_id, "prompt": prompt, "context": execution_context}
        return {"ok": True}

    @mcp.tool
    def agntid_task_close(task_id: str) -> dict:
        seen["closed"].append(("task", task_id))
        return {"ok": True}

    @mcp.tool
    def agntid_delegation_open(task_id: str, delegation_id: str, parent_agent_id: str, acting_agent_id: str,
                               delegated_intent: dict, tool_allowlist: list[str], approval: dict,
                               parent_delegation_id: str = "") -> dict:
        seen["delegations"][delegation_id] = {"task_id": task_id, "agent": acting_agent_id, "parent": parent_agent_id,
                                              "intent": delegated_intent, "allowlist": tool_allowlist, "approval": approval}
        return {"ok": True}

    @mcp.tool
    def agntid_delegation_close(task_id: str, delegation_id: str, status: str = "completed", reason: str | None = None) -> dict:
        seen["closed"].append(("delegation", delegation_id))
        return {"ok": True}

    def record(tool: str, task: str, delegation: str) -> None:
        assert task in seen["tasks"], "tool call without an open task"
        seen["calls"].append((tool, delegation))

    @mcp.tool
    def github_get_pull_request_files(owner: str, repo: str, pullNumber: int, _task_id: str = "", _delegation_id: str = "") -> dict:
        record("files", _task_id, _delegation_id)
        return {"__agntid_result": {"files": ["app/payments.py"]}, "__agntid_execution_flow": {"steps": []}}

    @mcp.tool
    def github_merge_pull_request(owner: str, repo: str, pullNumber: int, _task_id: str = "", _delegation_id: str = "") -> dict:
        raise deny("merge is not permitted for this agent", "prguard-no-merge")

    @mcp.tool
    def github_create_pull_request_review(owner: str, repo: str, pullNumber: int, event: str, body: str = "",
                                          _task_id: str = "", _delegation_id: str = "") -> dict:
        record(f"review:{event}", _task_id, _delegation_id)
        approval = seen["delegations"].get(_delegation_id, {}).get("approval", {})
        if event == "APPROVE" and approval.get("decision") != "approved":
            raise deny("APPROVE needs a human decision", "prguard-approve-hitl", verdict="review_required")
        return {"html_url": "https://example.test/review/1"}

    @mcp.tool
    def github_search_code(query: str, _task_id: str = "", _delegation_id: str = "") -> dict:
        raise ToolError("GitHub says: secondary rate limit policy exceeded")  # an upstream failure, not an AgntID decision

    return mcp


@pytest.fixture
def ctx():
    return TaskContext(review_id="r1", repo_full_name="you/prguard-demo-target", pr_number=2, head_sha="abc123",
                       sender="you", mode=Mode.GUARDED)


def test_resolve_tools_handles_server_prefixes():
    tools = resolve_tools({"github_get_file_contents": {}, "github.merge_pull_request": {}, "pull_request_read": {}, "other": {}})
    assert tools["get_file_contents"][0] == "github_get_file_contents"
    assert tools["merge_pull_request"][0] == "github.merge_pull_request"
    assert tools["get_pull_request"][:2] == ("pull_request_read", {"method": "get"})
    assert "push_files" not in tools


async def test_task_delegation_denial_and_approval(ctx):
    seen: dict = {}
    gateway = AgntIDGateway(proxy_url=fake_runtime(seen))
    scope = {"repo": ctx.repo_full_name, "pull_number": 2}

    # Root agent: a task is opened with our constraints, no delegation.
    result = await gateway.call("prguard-orchestrator", ctx, "get_pull_request_files", scope)
    assert result.data == {"files": ["app/payments.py"]}            # __agntid_* stripped by the SDK
    (task,) = seen["tasks"].values()
    assert task["agent_id"] == "prguard-orchestrator" and task["user_id"] == "gh:you"
    constraints = task["context"]["conversation"]["active_constraints"]
    assert any("merge" in c for c in constraints) and any("other repository" in c for c in constraints)
    assert seen["calls"] == [("files", "")]

    # A specialist acts under its own delegation with a least-privilege allowlist.
    await gateway.call("prguard-security", ctx, "get_pull_request_files", scope)
    (delegation_id, delegation), = seen["delegations"].items()
    assert delegation["agent"] == "prguard-security" and delegation["parent"] == "prguard-orchestrator"
    assert delegation["allowlist"] == ["github_get_pull_request_files", "github_search_code"]
    assert delegation["intent"]["scope"] == {"repository": ctx.repo_full_name, "pull_number": 2, "commit": "abc123"}
    assert seen["calls"][-1] == ("files", delegation_id)

    # A structured AgntID denial becomes a Denial labelled AGNTID.
    denied = await gateway.call("prguard-security", ctx, "merge_pull_request", scope)
    assert isinstance(denied, Denial) and denied.enforcer is Enforcer.AGNTID
    assert denied.policy == "prguard-no-merge" and "not permitted" in denied.reason

    # A tool the runtime does not expose is refused before any call is made.
    hidden = await gateway.call("prguard-security", ctx, "push_files", {"repo": ctx.repo_full_name})
    assert isinstance(hidden, Denial) and hidden.policy == "profile"

    # An upstream error that merely contains the word "policy" is NOT reported as an AgntID decision.
    with pytest.raises(UpstreamError):
        await gateway.call("prguard-security", ctx, "search_code", {"repo": ctx.repo_full_name, "query": "x"})

    # APPROVE without a human: AgntID asks for review.
    approve = {**scope, "event": "APPROVE", "body": "ok"}
    waiting = await gateway.call("prguard-aggregator", ctx, "create_pull_request_review", approve)
    assert isinstance(waiting, Denial) and waiting.policy == "review_required"

    # After the human approves, a new delegation carries evidence bound to this exact call.
    ctx.human_approved = True
    posted = await gateway.call("prguard-aggregator", ctx, "create_pull_request_review", approve)
    assert not isinstance(posted, Denial) and posted.data["html_url"]
    evidence = [d["approval"] for d in seen["delegations"].values() if d["approval"].get("decision") == "approved"]
    assert len(evidence) == 1 and evidence[0]["tool_name"] == "github_create_pull_request_review"
    wire = {"owner": "you", "repo": "prguard-demo-target", "pullNumber": 2, "event": "APPROVE", "body": "ok"}
    assert evidence[0]["arguments_digest"] == agntid.arguments_digest(wire)

    await gateway.close_review(ctx)
    assert [kind for kind, _ in seen["closed"]] == ["delegation"] * 3 + ["task"]


def test_runtime_classification_gate_is_recognised():
    from backend.gateway.agntid_gw import runtime_gate

    live = ('Error executing tool demo_add_numbers: Failed to communicate with runtime: 409 Client Error: Conflict for url: '
            'http+unix://%2Fvar%2Frun%2Fagntid%2Fruntime.sock/api/v1/mcp/policy/check - Response: '
            '{"error":"tool \\"demo.add_numbers\\" is in classification_state \\"CLASSIFICATION_PENDING\\"; calls are blocked '
            'until a curator approves the classification","code":"CLASSIFICATION_GATE"}\n')
    reason, code = runtime_gate(live)
    assert code == "classification_gate" and "CLASSIFICATION_PENDING" in reason
    assert runtime_gate("GitHub says: secondary rate limit policy exceeded") is None


async def test_runtime_error_after_allow_is_not_reported_as_a_denial(ctx):
    """Seen live: policy allowed, then credential resolution failed. Must not show as DENY."""
    mcp = FastMCP("fake")

    @mcp.tool
    def agntid_task_open(task_id: str, agent_id: str, user_id: str, prompt: str, execution_context: dict | None = None) -> dict:
        return {"ok": True}

    @mcp.tool
    def github_get_pull_request(owner: str, repo: str, pull_number: int, _task_id: str = "") -> dict:
        raise ToolError(json.dumps({
            "__agntid_error": "Credential resolution failed: No policy found with credential_type=GITHUB_APP",
            "__agntid_policy_decision": {"allowed": True, "policy_name": "agntid.tools.github.get_pull_request",
                                         "reason": "Policy evaluation allowed the request"}}))

    gateway = AgntIDGateway(proxy_url=mcp)
    with pytest.raises(UpstreamError, match="Credential resolution failed"):
        await gateway.call("prguard-orchestrator", ctx, "get_pull_request", {"repo": ctx.repo_full_name, "pull_number": 2})
