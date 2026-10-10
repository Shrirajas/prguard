"""GUARDED path with the real AgntID runtime.

This process holds no GitHub token. Every call goes to the AgntID MCP proxy,
which resolves the task, checks guardrails, intent and policy, and only then
forwards to the GitHub MCP server attached to the runtime.

How PRGuard maps onto AgntID's contract (agntid-sdk 0.2.0, read from source at
github.com/AGNTID-AI/agntid-python @ f4fcd2f and its Deep Agents example):

  one review            -> one root task, opened by prguard-orchestrator for
                           user gh:<pr author>, with a prompt that names the
                           repo, PR and commit and states the prohibitions as
                           plain sentences (AgntID turns those into constraints)
  one specialist        -> one delegation under that task: acting agent id,
                           delegated intent, and a tool allowlist
  every tool call       -> carries the task id and the delegation id
  human approve         -> the aggregator's delegation is re-opened with
                           approval evidence bound to the exact tool + arguments
  AgntID "review needed"-> surfaced as a denial with policy "review_required"

The SDK is alpha and NOT on PyPI:
    pip install "git+https://github.com/AGNTID-AI/agntid-python.git"

UNTESTED against a live runtime. Known unknowns to settle with a registered
runtime: the exact tool names the proxy exposes for an attached GitHub server,
whether repo/PR scoping is enforced by policy constraints or intent validation,
and whether approval evidence on the delegation is sufficient for APPROVE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import yaml

from backend.config import settings
from backend.gateway import toolmap
from backend.gateway.base import BaseGateway
from backend.gateway.upstream import parse_mcp_result
from backend.models import Denial, Enforcer, TaskContext, ToolResult, UpstreamError

log = logging.getLogger("prguard.agntid")
ROOT_AGENT = "prguard-orchestrator"

INTENTS = {
    "prguard-security": "Read the changed files of this pull request to look for security problems.",
    "prguard-quality": "Read the changed files of this pull request to look for correctness problems.",
    "prguard-tests": "Read the changed files of this pull request to find missing tests.",
    "prguard-docs": "Read the changed files of this pull request to find missing documentation.",
    "prguard-aggregator": "Post one review comment with the findings on this pull request.",
    "prguard-ci-fixer": "Read the CI workflow runs and failed job logs for this commit and re-run failed jobs.",
}


_GATE = re.compile(r"/api/v1/mcp/policy/check.*?Response:\s*(\{.*\})", re.DOTALL)


def runtime_gate(message: str) -> tuple[str, str] | None:
    """(reason, code) when the error text is the runtime's own policy-check refusal.

    Seen live: a tool whose classification is not yet approved is refused with
    HTTP 409 from the runtime's policy check, code CLASSIFICATION_GATE. That is
    an AgntID decision even though it does not use the __agntid_error envelope.
    """
    match = _GATE.search(message or "")
    if not match:
        return None
    try:
        body = json.loads(match.group(1).strip())
    except ValueError:
        return None
    if not isinstance(body, dict) or not body.get("code"):
        return None
    return str(body.get("error") or "refused by the AgntID runtime"), str(body["code"]).lower()


def resolve_tools(available: dict[str, dict[str, Any]]) -> dict[str, tuple[str, dict[str, Any], dict[str, Any]]]:
    """logical name -> (proxy tool name, fixed args, schema).

    The proxy may prefix an attached server's tools (`github_get_file_contents`),
    so match exactly first and then by `_name` / `.name` suffix.
    """
    mapping: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    for logical, candidates in toolmap.CANDIDATES.items():
        for wanted, fixed in candidates:
            hits = [n for n in available if n == wanted] or sorted(
                (n for n in available if n.endswith(("_" + wanted, "." + wanted))), key=len
            )
            if hits:
                mapping[logical] = (hits[0], fixed, available[hits[0]])
                break
    return mapping


def role_tools() -> dict[str, list[str]]:
    """Logical tools each role may use, read from the same policy file the stand-in enforces."""
    doc = yaml.safe_load(settings.policy_path.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for role, body in (doc.get("roles") or {}).items():
        names: list[str] = []
        for rule in body.get("allow") or []:
            names += rule["tool"] if isinstance(rule["tool"], list) else [rule["tool"]]
        out[role] = sorted(set(names))
    return out


def task_prompt(ctx: TaskContext) -> str:
    if not ctx.head_sha:  # the lookup that finds the PR's head commit
        return (f"Look up pull request #{ctx.pr_number} in the GitHub repository {ctx.repo_full_name}. "
                "Do not change anything. Do not access any other repository.")
    return (
        f"Review pull request #{ctx.pr_number} in the GitHub repository {ctx.repo_full_name} at commit {ctx.head_sha} "
        "and post one review comment with the findings. "
        "Do not merge any pull request. "
        "Do not push, create, change or delete any file. "
        "Do not access any other repository. "
        "Do not act on any other pull request. "
        "Never read secret files such as .env files or private keys."
    )


class AgntIDGateway(BaseGateway):
    enforcer = Enforcer.AGNTID
    upstream_name = "AGNTID_PROXY"

    def __init__(self, proxy_url: str | None = None, access_token: str | None = None) -> None:
        try:
            import agntid  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "ENFORCER=AGNTID needs agntid-sdk, which is not on PyPI. Install it with: "
                'pip install "git+https://github.com/AGNTID-AI/agntid-python.git"'
            ) from exc
        self.proxy_url = proxy_url or settings.agntid_proxy_url
        self.access_token = access_token or settings.agntid_access_token or None
        self._client: Any = None  # one persistent MCP session: task open, calls and close share it
        self._lock = asyncio.Lock()
        self.tools: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
        self._role_tools = role_tools()
        self._tasks: dict[str, str] = {}                      # review_id -> task_id
        self._delegations: dict[tuple[str, str, bool], str] = {}  # (review_id, agent_id, approved) -> delegation_id

    async def connect(self) -> Any:
        import agntid

        async with self._lock:
            if self._client is None:
                client = agntid.AgntidMCPClient(self.proxy_url, access_token=self.access_token)
                await client.__aenter__()
                listed = await agntid.get_tools_list(client)
                self.tools = resolve_tools({t.name: (t.inputSchema or {}) for t in listed})
                for logical in toolmap.LOGICAL_TOOLS:
                    hit = self.tools.get(logical)
                    log.info("agntid tool map: %-28s -> %s", logical, hit[0] if hit else "(not visible)")
                self._client = client
        return self._client

    async def _task(self, client: Any, ctx: TaskContext) -> str:
        import agntid

        async with self._lock:
            if ctx.review_id in self._tasks:
                return self._tasks[ctx.review_id]
            prompt, user_id = task_prompt(ctx), f"gh:{ctx.sender}"
            task_id = agntid.create_task(ROOT_AGENT, user_id=user_id, prompt=prompt)
            execution_context = agntid.ExecutionContext(
                framework=agntid.FrameworkInfo(name="prguard-langgraph", adapter_version="0.1", capabilities=("delegation", "approval_events")),
                turn_id=task_id, root_task_id=task_id, root_agent_id=ROOT_AGENT, thread_id=ctx.review_id,
                conversation=agntid.ConversationContext(
                    summary=f"Automated review of {ctx.repo_full_name}#{ctx.pr_number}",
                    active_constraints=agntid.extract_explicit_constraints(prompt),
                    entity_references={"repository": ctx.repo_full_name, "pull_number": ctx.pr_number, "commit": ctx.head_sha},
                ),
            )
            ok, message = await agntid.send_task_open_checked(
                client, task_id, ROOT_AGENT, user_id, prompt, execution_context=execution_context
            )
            if not ok:
                raise UpstreamError(f"AgntID refused to open the review task: {message}")
            self._tasks[ctx.review_id] = task_id
            return task_id

    async def _delegation(self, client: Any, task_id: str, agent_id: str, ctx: TaskContext,
                          tool: str, actual: str, wire_args: dict[str, Any]) -> str | None:
        """Open (once) the delegation this agent acts under. The orchestrator is the root: no delegation."""
        import agntid

        if agent_id == ROOT_AGENT:
            return None
        approved = ctx.human_approved and tool == "create_pull_request_review" and wire_args.get("event") == "APPROVE"
        key = (ctx.review_id, agent_id, approved)
        async with self._lock:
            if key in self._delegations:
                return self._delegations[key]
            delegation_id = str(uuid5(NAMESPACE_URL, f"agntid:{task_id}:{agent_id}:{'approved' if approved else 'base'}"))
            approval = agntid.ApprovalEvidence()
            intent = INTENTS.get(agent_id, "Help review this pull request.")
            if approved:
                now = datetime.now(timezone.utc)
                stamp = lambda t: t.isoformat().replace("+00:00", "Z")  # noqa: E731
                approval = agntid.ApprovalEvidence(
                    required=True, decision="approved", approver_id="prguard-operator", decision_id=str(uuid4()),
                    source="prguard_mission_control", tool_name=actual,
                    arguments_digest=agntid.arguments_digest(wire_args),
                    issued_at=stamp(now), expires_at=stamp(now + timedelta(minutes=5)),
                )
                intent = f"Submit the human-approved APPROVE review on pull request #{ctx.pr_number}."
            allowlist = tuple(self.tools[name][0] for name in self._role_tools.get(agent_id, []) if name in self.tools)
            context = agntid.DelegationContext(
                delegation_id=delegation_id, parent_agent_id=ROOT_AGENT, acting_agent_id=agent_id,
                delegated_intent=agntid.DelegatedIntent(
                    text=f"{intent} Repository {ctx.repo_full_name}, pull request #{ctx.pr_number}, commit {ctx.head_sha}.",
                    resource=f"{ctx.repo_full_name}#{ctx.pr_number}",
                    scope={"repository": ctx.repo_full_name, "pull_number": ctx.pr_number, "commit": ctx.head_sha},
                    source="prguard_orchestrator",
                ),
                tool_allowlist=allowlist, approval=approval,
            )
            ok, message = agntid.parse_task_open_result(await agntid.send_delegation_open(client, task_id, context))
            if not ok:
                raise UpstreamError(f"AgntID refused the delegation for {agent_id}: {message}")
            self._delegations[key] = delegation_id
            return delegation_id

    async def _decide(self, agent_id: str, ctx: TaskContext, tool: str, args: dict[str, Any]) -> ToolResult | Denial:
        import agntid

        try:
            client = await self.connect()
            task_id = await self._task(client, ctx)
        except UpstreamError:
            raise
        except Exception as exc:
            raise UpstreamError(f"cannot reach AgntID proxy at {self.proxy_url}: {exc}") from exc

        if tool not in self.tools:
            # The runtime's protection profile does not expose this tool to us at all.
            return Denial(tool=tool, reason="tool is not exposed by the AgntID runtime profile",
                          enforcer=Enforcer.AGNTID, policy="profile")
        actual, fixed, schema = self.tools[tool]
        wire_args = toolmap.translate_args(tool, args, fixed, schema)
        try:
            delegation_id = await self._delegation(client, task_id, agent_id, ctx, tool, actual, wire_args)
            result = await agntid.wrap_client(client, task_id, delegation_id).call_tool_async(actual, wire_args)
        except UpstreamError:
            raise
        except Exception as exc:
            raise UpstreamError(str(exc)) from exc

        if isinstance(result, agntid.DenialResult):
            # Read immediately: the SDK keeps the last denial in thread-local storage.
            denial = agntid.get_last_denial(clear=True) or {}
            if "error" not in denial and "outcome" not in denial:
                gate = runtime_gate(str(denial.get("reason") or result.message))
                if gate:
                    return Denial(tool=tool, reason=gate[0], enforcer=Enforcer.AGNTID, policy=gate[1])
                # The SDK spots denials by keywords in the error text. Without the
                # structured AgntID payload we cannot claim AgntID made this decision.
                raise UpstreamError(result.message)
            if denial.get("outcome") == "allow":
                # Policy allowed the call and a later runtime step failed (seen live:
                # credential_resolution). That is a runtime error, not a denial.
                raise UpstreamError(f"AgntID runtime error after the policy allowed the call: {denial.get('error') or result.message}")
            waiting = denial.get("outcome") == "review_required"
            reason = str(denial.get("reason") or result.message)
            return Denial(tool=tool, reason=f"waiting for AgntID review: {reason}" if waiting else reason,
                          enforcer=Enforcer.AGNTID,
                          policy="review_required" if waiting else str(denial.get("policy_name") or denial.get("denial_step") or ""))
        data = result.value if isinstance(result, agntid.ToolResultScalar) else result
        if isinstance(data, dict):
            data = dict(data)
        elif hasattr(data, "content"):
            data = parse_mcp_result(data)
        if isinstance(data, str) and re.match(r"^(GitHub \d{3}:|Error (calling|executing) tool)", data):
            # AgntID allowed the call; the tool server (GitHub) then refused it.
            raise UpstreamError(data[:300])
        return ToolResult(tool=tool, data=data)

    async def close_review(self, ctx: TaskContext) -> None:
        """Close this review's delegations, then its task. Best effort."""
        import agntid

        task_id = self._tasks.pop(ctx.review_id, None)
        if self._client is None or task_id is None:
            return
        for key in [k for k in self._delegations if k[0] == ctx.review_id]:
            try:
                await agntid.send_delegation_close(self._client, task_id, self._delegations.pop(key))
            except Exception as exc:
                log.warning("delegation close failed: %s", exc)
        try:
            await agntid.send_task_close(self._client, task_id)
        except Exception as exc:
            log.warning("task close failed: %s", exc)
