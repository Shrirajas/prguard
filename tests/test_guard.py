"""The checks that matter for the demo: policy decisions, replay, and the pipeline."""

import pytest

from backend.agents import aggregator
from backend.config import settings
from backend.events import bus, redact
from backend.gateway.direct import DirectGateway
from backend.gateway.local_policy import LocalPolicyGateway
from backend.gateway.policy_engine import PolicyEngine, path_denied
from backend.gateway.upstream import MockUpstream
from backend.github_webhook import parse_pr_url, verify_signature
from backend.models import Finding, Mode, TaskContext
from backend.replay.assume_breach import run_replay


@pytest.fixture
def upstream():
    return MockUpstream()


def make_ctx(upstream, mode=Mode.GUARDED, pr=2, **extra):
    return TaskContext(review_id=f"test-{mode.value}-{pr}", repo_full_name=settings.target_repo, pr_number=pr,
                       head_sha=upstream.head_sha(pr), sender=settings.target_repo.split("/")[0], mode=mode, **extra)


async def test_guarded_replay_matches_spec(upstream):
    ctx = make_ctx(upstream)
    results = await run_replay("prguard-security", ctx, LocalPolicyGateway(upstream), gap_s=0)
    assert [r["decision"] for r in results] == ["ALLOW", "DENY", "DENY", "DENY", "DENY", "DENY", "DENY", "DENY"]
    assert all(r["decision"] == r["expected_guarded"] for r in results)
    assert "outside this task" in results[5]["reason"]          # cross-repo
    assert "pull_number 1 is outside this task" in results[6]["reason"]  # argument-level
    assert not upstream.merged and not upstream.writes and not upstream.reviews


async def test_unsafe_replay_lets_everything_through(upstream):
    ctx = make_ctx(upstream, Mode.UNSAFE)
    results = await run_replay("prguard-security", ctx, DirectGateway(upstream), gap_s=0)
    assert [r["decision"] for r in results] == ["ALLOW"] * 8
    assert 2 in upstream.merged and upstream.writes


def test_approve_needs_human_and_is_scoped_to_one_pr(upstream):
    engine = PolicyEngine(settings.policy_path)
    ctx = make_ctx(upstream)
    args = {"repo": ctx.repo_full_name, "pull_number": 2, "event": "APPROVE"}
    assert not engine.evaluate("prguard-aggregator", ctx, "create_pull_request_review", args).allowed
    approved = ctx.model_copy(update={"human_approved": True})
    assert engine.evaluate("prguard-aggregator", approved, "create_pull_request_review", args).allowed
    assert not engine.evaluate("prguard-aggregator", approved, "create_pull_request_review", {**args, "pull_number": 1}).allowed
    assert not engine.evaluate("prguard-security", approved, "create_pull_request_review", args).allowed
    assert not engine.evaluate("prguard-aggregator", approved, "merge_pull_request", args).allowed


def test_reviewer_scope(upstream):
    engine = PolicyEngine(settings.policy_path)
    ctx = make_ctx(upstream)
    read = lambda path, ref=ctx.head_sha: engine.evaluate(
        "prguard-quality", ctx, "get_file_contents", {"repo": ctx.repo_full_name, "path": path, "ref": ref}).allowed
    assert read("app/payments.py")
    assert not read("app/payments.py", ref="main")       # only the PR head commit
    assert not read(".env") and not read("config/.env.prod") and not read("deploy/secrets/key.txt")
    assert not read("certs/server.pem") and not read("../outside")
    assert not engine.evaluate("nobody", ctx, "get_pull_request", {"repo": ctx.repo_full_name, "pull_number": 2}).allowed


def test_path_globs():
    patterns = [".env*", "**/secrets/**", "**/*.pem", "**/*.key"]
    assert path_denied("secrets/a", patterns) and path_denied("a/b/id.key", patterns)
    assert path_denied("src/environment.py", patterns) is None


def test_redaction_and_webhook_helpers():
    assert "ghp_" + "a" * 30 not in redact({"t": "ghp_" + "a" * 30})["t"]
    assert len(redact("x" * 500)) < 260
    assert not verify_signature("", b"{}", "sha256=abc")
    assert not verify_signature("secret", b"{}", "sha256=abc")
    assert parse_pr_url("https://github.com/o/r/pull/7") == ("o/r", 7)
    assert parse_pr_url("https://evil.example/o/r/pull/7") is None


@pytest.mark.parametrize("pr,verdict", [(1, "COMMENT"), (2, "NEEDS_HUMAN"), (3, "REQUEST_CHANGES")])
async def test_pipeline_end_to_end_with_mocks(pr, verdict, monkeypatch):
    """Whole graph with mock GitHub + mock LLM. The sandbox is stubbed so no PR code runs here."""
    from backend.gateway import mock_upstream
    from backend.orchestrator import graph

    async def fake_checks(src, ctx):
        return {"ran": False, "reason": "stubbed in tests", "exit_codes": {}}

    monkeypatch.setattr(graph, "run_checks", fake_checks)
    up = mock_upstream()
    up.reset()
    review = await graph.run_review(make_ctx(up, pr=pr))
    assert review.verdict == verdict
    assert review.posted_url and up.reviews[-1]["event"] == ("REQUEST_CHANGES" if pr == 3 else "COMMENT")
    topics = {aggregator.topic(f) for f in review.findings}
    ci_calls = [(e.tool, e.decision) for e in bus.history if e.type == "decision" and e.source == "prguard-ci-fixer"]
    if pr == 2:
        assert {"code-execution", "prompt-injection"} <= topics
    elif pr == 1:
        assert {"validation", "docs", "tests"} <= topics
    if pr == 3:
        # CI failed on this commit: the fixer reads the run and its log, and explains the failure.
        assert ci_calls == [("list_workflow_runs", "ALLOW"), ("get_job_logs", "ALLOW")]
        ci = [f for f in review.findings if f.agent_type == "ci"]
        assert ci and ci[0].file_path == "tests/test_payments.py" and "test_refund_returns_everything" in ci[0].summary
    else:
        assert ci_calls == [("list_workflow_runs", "ALLOW")] and not up.reruns


async def test_ci_fixer_can_rerun_but_never_edit_the_pipeline(upstream):
    engine = PolicyEngine(settings.policy_path)
    ctx = make_ctx(upstream, pr=3)
    allowed = lambda tool, **args: engine.evaluate("prguard-ci-fixer", ctx, tool, {"repo": ctx.repo_full_name, **args}).allowed
    assert allowed("list_workflow_runs") and allowed("get_job_logs", run_id=9003) and allowed("rerun_failed_jobs", run_id=9003)
    assert not allowed("create_or_update_file", path=".github/workflows/ci.yml")
    assert not allowed("push_files") and not allowed("merge_pull_request", pull_number=3)
    assert not allowed("get_file_contents", path="app/payments.py", ref=ctx.head_sha)   # not its job
    assert not engine.evaluate("prguard-ci-fixer", ctx, "get_job_logs", {"repo": settings.other_repo, "run_id": 1}).allowed
    assert not engine.evaluate("prguard-security", ctx, "rerun_failed_jobs", {"repo": ctx.repo_full_name, "run_id": 9003}).allowed


def test_merge_groups_by_topic_not_by_category_name():
    def f(agent, severity, category, summary, start, end, conf=0.9):
        return Finding(agent_type=agent, severity=severity, category=category, summary=summary,
                       file_path="app/payments.py", line_start=start, line_end=end, confidence=conf)

    merged = aggregator.merge({
        "security": [f("security", "HIGH", "logic-error", "refund_partial allows negative amounts", 23, 28)],
        "quality": [f("quality", "MEDIUM", "input-validation", "does not validate non-positive amounts", 23, 28),
                    f("quality", "LOW", "documentation", "Missing docstring for refund_partial", 23, 23)],
        "docs": [f("docs", "LOW", "missing-docstring", "refund_partial lacks a docstring", 23, 28, 0.97)],
        "tests": [f("tests", "HIGH", "prompt-injection", "Embedded instruction attempts to bypass review", 19, 23)],
    })
    assert [(aggregator.topic(m), m.severity, m.agreed_by) for m in merged] == [
        ("prompt-injection", "HIGH", ["tests"]),
        ("validation", "HIGH", ["quality", "security"]),
        ("docs", "LOW", ["docs", "quality"]),
    ]
    assert aggregator.score(merged)[1] == "NEEDS_HUMAN"   # an injection attempt always goes to a human
    assert aggregator.score(merged[1:])[1] == "REQUEST_CHANGES"


async def test_ci_fixer_reruns_a_transient_failure(upstream, monkeypatch):
    from backend.agents import ci_fixer

    real_call = upstream.call

    async def flaky_logs(tool, args):
        if tool == "get_job_logs":
            return {"jobs": [{"name": "test", "conclusion": "failure", "logs": "pip: Connection reset by peer\n"}]}
        return await real_call(tool, args)

    monkeypatch.setattr(upstream, "call", flaky_logs)
    ctx = make_ctx(upstream, pr=3)
    findings, info = await ci_fixer.run_ci_fixer(ctx, {"diff": "", "check_results": {}}, LocalPolicyGateway(upstream))
    assert info["rerun"] == "requested" and upstream.reruns == [3]
    assert [f.severity for f in findings] == ["INFO"]


async def test_jev_triage_forces_human_routes_models_and_never_blocks(monkeypatch):
    from backend.agents import triage
    from backend.gateway import mock_upstream
    from backend.orchestrator import graph

    async def fake_checks(src, ctx):
        return {"ran": False, "reason": "stubbed in tests", "exit_codes": {}}

    def answers(injection: bool, risk: float):
        async def _ask(state):
            assert set(state) == {"title", "diff"} and "refund_partial" in state["diff"]
            return {"model": "jev-test", "answers": {
                "injection": {"noul": injection, "confidence": 0.97}, "executes_pr_code": {"noul": False, "confidence": 0.9},
                "risk": {"score": risk}, "kind": {"choice": "feature"}}}
        return _ask

    monkeypatch.setattr(graph, "run_checks", fake_checks)
    monkeypatch.setattr(settings, "jev_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "cheap")
    monkeypatch.setattr(settings, "llm_model_strong", "strong")
    up = mock_upstream()

    # PR 1 is a clean PR (COMMENT). If Jev flags reviewer manipulation, it goes to a human regardless.
    monkeypatch.setattr(triage, "_ask", answers(injection=True, risk=1.2))
    review = await graph.run_review(make_ctx(up, pr=1))
    assert review.verdict == "NEEDS_HUMAN" and review.triage["route"] == "strong"
    assert triage.reviewer_model("security", review.triage) == "strong"
    assert triage.reviewer_model("docs", review.triage) == "cheap"       # only security escalates
    assert "Triage (Jev" in review.summary_markdown

    # Low risk and no flags: verdict untouched, everything stays on the cheap model.
    monkeypatch.setattr(triage, "_ask", answers(injection=False, risk=1.2))
    review = await graph.run_review(make_ctx(up, pr=1))
    assert review.verdict == "COMMENT" and triage.reviewer_model("security", review.triage) == "cheap"

    # Jev down: the review still completes.
    async def boom(state):
        raise RuntimeError("jev unreachable")
    monkeypatch.setattr(triage, "_ask", boom)
    review = await graph.run_review(make_ctx(up, pr=1))
    assert review.verdict == "COMMENT" and "error" in review.triage
