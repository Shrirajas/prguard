"""LocalUpstream: a read-only GitHub stand-in built from a local git working tree.

Used by `prguard review --local DIR` (and the VS Code extension) to review your
own changes before a PR exists. The "PR" is the working tree against its
merge-base with the base branch: committed, staged, unstaged and untracked
files together. With `since` (a snapshot tree from an earlier review, or HEAD)
only what changed after it is reviewed, like a PR's new commits; the repo's
config is still read at the merge-base. Write tools raise, so local mode never
posts anything.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.diffs import split_unified_diff
from backend.gateway.policy_engine import PolicyEngine, path_denied
from backend.models import UpstreamError

MAX_UNTRACKED_BYTES = 1_000_000
BASE_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master", "main", "master")
WRITE_TOOLS = {"create_pull_request_review", "upsert_issue_comment", "create_issue_comment", "update_pull_request_body",
               "rerun_failed_jobs", "merge_pull_request", "push_files", "create_or_update_file", "delete_file", "update_pull_request"}


def _git(cwd: Path, *args: str, ok: tuple[int, ...] = (0,), env: dict[str, str] | None = None, stdin: str | None = None) -> str:
    # --no-ext-diff/--no-textconv are passed per call: repo config must not make a diff run other programs.
    proc = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd, capture_output=True, input=stdin,
                          text=True, errors="replace", timeout=60, check=False, env=env)
    if proc.returncode not in ok:
        raise UpstreamError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _secret_patterns() -> list[str]:
    """The policy's deny_paths: files whose content must never reach a model, in a diff or otherwise."""
    roles = PolicyEngine(settings.policy_path).roles.values()
    return sorted({p for role in roles for rule in role.get("allow") or [] for p in rule.get("deny_paths") or []})


class LocalUpstream:
    name = "LOCAL"

    def __init__(self, path: Path | str, base: str = "", only: list[str] | None = None, since: str = "") -> None:
        path = Path(path).expanduser().resolve()
        if not path.is_dir():
            raise UpstreamError(f"{path} is not a directory")
        try:
            self.root = Path(_git(path, "rev-parse", "--show-toplevel").strip()).resolve()
        except UpstreamError:
            raise UpstreamError(f"{path} is not inside a git repository") from None
        self.repo = f"local/{self.root.name}"
        self.base_name = base or next((ref for ref in BASE_CANDIDATES
                                       if _git(self.root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", ok=(0, 1)).strip()), "")
        if not self.base_name and since:
            self.base_name = "HEAD"  # only the config is read at the base, and the diff comes from `since`
        if not self.base_name:
            raise UpstreamError(f"no base branch found (tried {', '.join(BASE_CANDIDATES)}); pass --base")
        self.base_sha = _git(self.root, "merge-base", "HEAD", self.base_name).strip()
        self.branch = _git(self.root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        self.secrets = _secret_patterns()
        self.tree = self._worktree_tree()  # what this run reviews; the extension passes it back as `since` next time
        self.compared_to = self.base_name   # what the UI says the change is against
        if since:
            since_tree = _git(self.root, "rev-parse", "--verify", "--quiet", f"{since}^{{tree}}", ok=(0, 1)).strip()
            self.compared_to = "last review" if since_tree and since != "HEAD" else "last commit"
            # A snapshot git has since pruned falls back to the last commit; the UI shows which one was used.
            since_tree = since_tree or _git(self.root, "rev-parse", "HEAD^{tree}").strip()
            self.files = self._tree_diff(since_tree)
        else:
            self.files = self._changed_files()
        if not self.files:
            raise UpstreamError(f"nothing to review: no changes since the {self.compared_to}" if since
                                else f"nothing to review: no changes against {self.base_name}")
        if only:  # the extension's file picker: review a subset; the reviewers can still read any file for context
            self.files = [f for f in self.files if f["filename"] in set(only)]
            if not self.files:
                raise UpstreamError(f"nothing to review: none of the selected files changed against {self.compared_to}")
        snapshot = "".join(f"{f['filename']}\0{f['patch']}\0" for f in self.files)
        self.head_sha = hashlib.sha1(f"{self.base_sha}\0{snapshot}".encode()).hexdigest()

    def _worktree_tree(self) -> str:
        """The working tree as a git tree (tracked and untracked files, not ignored ones, untracked ones up to
        MAX_UNTRACKED_BYTES), written through a throwaway index: the real index, refs and files are not touched."""
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
            real_index = Path(_git(self.root, "rev-parse", "--git-path", "index").strip())
            real_index = real_index if real_index.is_absolute() else self.root / real_index
            if real_index.is_file():
                shutil.copy(real_index, env["GIT_INDEX_FILE"])  # its stat cache spares re-hashing unchanged files
            _git(self.root, "add", "-u", env=env)
            small = [rel for rel in _git(self.root, "ls-files", "--others", "--exclude-standard", "-z", env=env).split("\0")
                     if rel and (self.root / rel).is_file() and (self.root / rel).stat().st_size <= MAX_UNTRACKED_BYTES]
            if small:
                _git(self.root, "add", "--pathspec-from-file=-", "--pathspec-file-nul", env=env, stdin="\0".join(small))
            return _git(self.root, "write-tree", env=env).strip()

    def _tree_diff(self, since_tree: str) -> list[dict[str, Any]]:
        diff = _git(self.root, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", since_tree, self.tree)
        files = split_unified_diff(diff)
        for f in files:
            if path_denied(f["filename"], self.secrets):
                f["patch"] = ""
        return files

    def _changed_files(self) -> list[dict[str, Any]]:
        diff = _git(self.root, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", self.base_sha)
        too_big = []
        for rel in filter(None, _git(self.root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")):
            if (self.root / rel).is_file() and (self.root / rel).stat().st_size > MAX_UNTRACKED_BYTES:
                too_big.append({"filename": rel, "status": "added", "patch": ""})  # like GitHub: listed, no diff
                continue
            # --no-index exits 1 when the files differ, which they always do here.
            diff += _git(self.root, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-index", "--", "/dev/null", rel, ok=(0, 1))
        files = split_unified_diff(diff) + too_big
        for f in files:
            if path_denied(f["filename"], self.secrets):
                f["patch"] = ""  # listed as "not reviewed", content never sent
        return files

    def materialize(self, dest: Path) -> None:
        """Copy the working tree for the sandbox: only files git would commit, never ignored ones or secrets."""
        listed = _git(self.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
        for rel in filter(None, listed):
            src = self.root / rel
            if path_denied(rel, self.secrets) or not (src.is_file() or src.is_symlink()):
                continue  # secrets, deleted files, submodules
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel, follow_symlinks=False)  # a link stays a link, so nothing outside the repo is copied in

    def _head_file(self, rel: str) -> str:
        target = (self.root / rel).resolve()
        # Refuse anything that resolves elsewhere (symlinks, ..): the policy checked `rel`, not where it points.
        if target != Path(os.path.normpath(self.root / rel)) or not target.is_file():
            raise UpstreamError(f"404 Not Found: {rel}")
        return target.read_text(encoding="utf-8", errors="replace")

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if args.get("repo", self.repo) != self.repo:
            raise UpstreamError(f"404 Not Found: {args.get('repo')}")
        if tool in WRITE_TOOLS:
            raise UpstreamError(f"local mode never writes to GitHub ({tool})")
        if "pull_number" in args and int(args["pull_number"]) != 0:
            raise UpstreamError(f"404 Not Found: pull request #{args['pull_number']}")

        if tool == "get_pull_request":
            commits = _git(self.root, "log", "--format=- %s", f"{self.base_sha}..HEAD").strip()
            return {"number": 0, "title": f"Local changes on {self.branch} (vs {self.compared_to})",
                    "body": f"Commits not on {self.base_name}:\n{commits}" if commits else "Uncommitted changes only.",
                    "state": "open", "draft": False, "merged": False,
                    "head": {"sha": self.head_sha, "ref": self.branch},
                    "base": {"ref": self.base_sha, "sha": self.base_sha},
                    "user": {"login": "local"}, "html_url": "", "labels": []}
        if tool == "get_pull_request_files":
            return [dict(f) for f in self.files]
        if tool == "get_file_contents":
            rel, ref = str(args.get("path", "")).lstrip("/"), str(args.get("ref", ""))
            if ref == self.head_sha:
                return {"path": rel, "content": self._head_file(rel)}
            if ref == self.base_sha:
                try:
                    return {"path": rel, "content": _git(self.root, "show", f"{self.base_sha}:{rel}")}
                except UpstreamError:
                    raise UpstreamError(f"404 Not Found: {rel}@{ref[:7]}") from None
            raise UpstreamError(f"404 Not Found: ref {ref}")
        if tool == "search_code":
            query = str(args.get("query", ""))
            out = _git(self.root, "grep", "-n", "-I", "-F", "-i", "-e", query, ok=(0, 1)) if query else ""
            hits = []
            for line in out.splitlines():
                rel, _, rest = line.partition(":")
                number, _, text = rest.partition(":")
                if not path_denied(rel, self.secrets):
                    hits.append({"path": rel, "line": int(number or 0), "text": text.strip()})
            return {"total_count": len(hits), "items": hits[:20]}
        if tool == "list_workflow_runs":
            return {"total_count": 0, "workflow_runs": []}  # no CI before a PR exists; --ci-log still works
        raise UpstreamError(f"local mode has no tool {tool}")
