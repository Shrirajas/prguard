"""Logical tool names and their mapping onto whatever the GitHub MCP server exposes.

Agents and policy only ever see the logical names and normalised arguments
(`repo` as "owner/name", `pull_number`, `ref`, `path`, `event`). GitHub MCP
tool names differ between server versions, so the real names are resolved once
at startup from the server's own tool list and logged.
"""

from __future__ import annotations

from typing import Any

# logical name -> candidates as (actual tool name, extra fixed arguments)
CANDIDATES: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "get_pull_request": [("pull_request_read", {"method": "get"}), ("get_pull_request", {})],
    "get_pull_request_files": [("pull_request_read", {"method": "get_files"}), ("get_pull_request_files", {})],
    "get_pull_request_diff": [("pull_request_read", {"method": "get_diff"}), ("get_pull_request_diff", {})],
    "get_file_contents": [("get_file_contents", {})],
    "search_code": [("search_code", {})],
    "create_pull_request_review": [
        ("pull_request_review_write", {"method": "create"}),
        ("create_pull_request_review", {}),
        ("create_and_submit_pull_request_review", {}),
    ],
    "merge_pull_request": [("merge_pull_request", {})],
    "push_files": [("push_files", {})],
    "create_or_update_file": [("create_or_update_file", {})],
    "delete_file": [("delete_file", {})],
    "list_workflow_runs": [("list_workflow_runs", {}), ("actions_list", {"method": "list_workflow_runs"})],
    "get_job_logs": [("get_job_logs", {})],
    "rerun_failed_jobs": [("rerun_failed_jobs", {}), ("actions_run_trigger", {"method": "rerun_failed_jobs"})],
}

LOGICAL_TOOLS = list(CANDIDATES)

# Arguments that exist only for our own policy and bookkeeping, never sent upstream.
LOGICAL_ONLY = {"head_sha"}
# Extra arguments the real GitHub MCP tools need (untested against a live server).
EXTRA_ARGS: dict[str, dict[str, Any]] = {
    "list_workflow_runs": {"workflow_id": "ci.yml"},
    "get_job_logs": {"failed_only": True, "return_content": True, "tail_lines": 300},
}


def resolve(available: dict[str, dict[str, Any]]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Pick the first candidate the server actually has. `available` is name -> inputSchema."""
    mapping: dict[str, tuple[str, dict[str, Any]]] = {}
    for logical, candidates in CANDIDATES.items():
        for actual, fixed in candidates:
            if actual in available:
                mapping[logical] = (actual, fixed)
                break
    return mapping


def translate_args(logical: str, args: dict[str, Any], fixed: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Turn normalised arguments into the shape the real tool expects."""
    props = set((schema or {}).get("properties") or {})
    out: dict[str, Any] = {k: v for k, v in EXTRA_ARGS.get(logical, {}).items() if not props or k in props}
    out.update(fixed)
    for key, value in args.items():
        if key in LOGICAL_ONLY:
            continue
        if key == "repo":
            if logical == "search_code":
                continue
            owner, _, name = str(value).partition("/")
            out["owner"], out["repo"] = owner, name
        elif key == "pull_number":
            out["pullNumber" if "pullNumber" in props or "pull_number" not in props else "pull_number"] = int(value)
        else:
            out[key] = value
    if logical == "search_code":
        out["query"] = f"{args.get('query', '')} repo:{args.get('repo', '')}".strip()
    return out
