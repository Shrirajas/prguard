"""`--local`: review a git working tree before a PR exists (the VS Code extension's path)."""

import json
import shutil
import subprocess

import pytest

from backend.cli import main as cli_main
from backend.config import settings
from backend.gateway import gateway, local_upstream, reset_gateways
from backend.models import Denial, TaskContext, UpstreamError
from backend.sandbox.checkout import checkout_pr

DEMO = settings.demo_target_dir


def git(repo, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """demo_target committed on main, with hostile PR #2's files on top as uncommitted changes."""
    monkeypatch.setattr(settings, "local_dir", "")
    monkeypatch.setattr(settings, "local_base", "")
    root = tmp_path / "work"
    shutil.copytree(DEMO, root, ignore=shutil.ignore_patterns("prs", "__pycache__"))
    (root / ".gitignore").write_text("ignored.txt\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    shutil.copytree(DEMO / "prs" / "02_hostile", root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("PR.json"))
    (root / ".env.local").write_text("SECRET=hunter2\n")                # untracked, not ignored: must still never reach a model
    (root / "ignored.txt").write_text("SECRET=hunter2\n")              # git-ignored: not part of the change at all
    (root / ".prguard.toml").write_text('[gate]\nfail_on = "none"\n')  # head-only config must not weaken the gate
    return root


def use_local(root):
    settings.upstream, settings.local_dir = "local", str(root)
    reset_gateways()
    return local_upstream()


def test_local_review_cli(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")  # Codespaces and Actions set it; --local must still run
    out = tmp_path / "out"
    assert cli_main(["review", "--local", str(repo), "--out", str(out)]) == 1  # gate from base config (defaults), not the PR's
    result = json.loads((out / "prguard-report.json").read_text())[0]
    assert result["ok"] and result["repo"] == "local/work" and result["pr_number"] == 0 and result["posted_urls"] == []
    assert result["usage"]["local_base"] == "main"
    topics = {f["category"] for f in result["review"]["findings"]}
    assert {"untrusted-code-execution", "prompt-injection", "data-exfiltration"} <= topics
    assert any(".env.local" in note for note in result["review"]["notes"])  # listed as not reviewed
    assert all("hunter2" not in p.read_text() for p in out.iterdir())


async def test_local_upstream_reads_and_refusals(repo, tmp_path):
    up = use_local(repo)
    files = {f["filename"]: f for f in up.files}
    assert files["app/payments.py"]["status"] == "modified" and files[".pylintrc"]["status"] == "added"
    assert files[".env.local"]["patch"] == "" and "ignored.txt" not in files
    ctx = TaskContext(review_id="t", repo_full_name=up.repo, pr_number=0, head_sha=up.head_sha, base_ref=up.base_sha)
    gw = gateway()
    res = await gw.call("prguard-security", ctx, "get_file_contents", {"repo": up.repo, "path": ".env.local", "ref": up.head_sha})
    assert isinstance(res, Denial)
    res = await gw.call("prguard-security", ctx, "get_file_contents", {"repo": up.repo, "path": "app/payments.py", "ref": up.head_sha})
    assert "apply_discount" in res.data["content"]  # head = working tree
    (repo / "link.py").symlink_to(tmp_path / "outside.txt")
    (tmp_path / "outside.txt").write_text("secret")
    with pytest.raises(UpstreamError, match="404"):
        await up.call("get_file_contents", {"repo": up.repo, "path": "link.py", "ref": up.head_sha})
    with pytest.raises(UpstreamError, match="never writes"):
        await up.call("upsert_issue_comment", {"repo": up.repo, "pull_number": 0, "marker": "review", "body": "x"})
    (repo / "data.csv").write_text("x" * 1_000_001)  # untracked and over the size cap: listed, never diffed
    reset_gateways()
    up = local_upstream()
    assert {"filename": "data.csv", "status": "added", "patch": ""} in up.files
    dest = await checkout_pr(ctx.model_copy(update={"review_id": "sbx"}), workdir=repo)  # local mode must not copy the raw workdir
    assert (dest / ".pylintrc").is_file() and (dest / "app" / "payments.py").is_file()
    assert not (dest / ".env.local").exists() and not (dest / "ignored.txt").exists() and not (dest / ".git").exists()


def test_local_setup_errors(repo, tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cli_main(["review", "--local", str(plain), "--out", str(tmp_path / "o")]) == 2
    assert "not inside a git repository" in capsys.readouterr().err
    assert cli_main(["review", "--local", str(repo), "--pr", "1", "--out", str(tmp_path / "o")]) == 2
    assert cli_main(["review", "--repo", "o/r", "--pr", "1", "--base", "dev", "--out", str(tmp_path / "o")]) == 2
    assert "--base, --file and --since need --local" in capsys.readouterr().err
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "all")
    assert cli_main(["review", "--local", str(repo), "--base", "HEAD", "--out", str(tmp_path / "o")]) == 2
    assert "nothing to review" in capsys.readouterr().err


def test_local_file_picker_and_listing(repo, tmp_path, capsys):
    out = tmp_path / "out"
    assert cli_main(["review", "--local", str(repo), "--file", "app/payments.py", "--out", str(out)]) == 1
    result = json.loads((out / "prguard-report.json").read_text())[0]
    assert {f["file_path"] for f in result["review"]["findings"]} == {"app/payments.py"}
    assert cli_main(["review", "--local", str(repo), "--file", "not/changed.py", "--out", str(out)]) == 2
    assert "none of the selected files changed" in capsys.readouterr().err

    assert cli_main(["files", "--local", str(repo)]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["base"] == "main" and listing["budget"] > 0
    files = {f["file"]: f for f in listing["files"]}
    assert files["app/payments.py"]["tokens"] > 0 and files["app/payments.py"]["skip"] == ""
    assert files[".env.local"]["skip"] == "no diff"              # a secret path: listed, never sent
    assert cli_main(["files", "--repo", "o/r", "--pr", "1"]) == 2   # needs --local



def test_since_last_review_reviews_only_newer_changes(repo, tmp_path, capsys):
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    out = tmp_path / "out"
    assert cli_main(["review", "--local", str(repo), "--out", str(out)]) == 1
    first = json.loads((out / "prguard-report.json").read_text())[0]["usage"]
    assert first["local_base"] == "main" and len(first["local_tree"]) == 40
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout == status  # index untouched

    assert cli_main(["files", "--local", str(repo), "--since", first["local_tree"]]) == 2   # nothing changed since that review
    assert "no changes since the last review" in capsys.readouterr().err
    (repo / "app" / "payments.py").write_text((repo / "app" / "payments.py").read_text() + "\n\ndef added_later():\n    return 1\n")
    assert cli_main(["files", "--local", str(repo), "--since", first["local_tree"]]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["base"] == "last review" and [f["file"] for f in listing["files"]] == ["app/payments.py"]
    assert listing["tree"] != first["local_tree"]  # what "mark as reviewed" saves

    assert cli_main(["files", "--local", str(repo), "--since", "HEAD"]) == 0   # everything uncommitted, untracked files too
    since_commit = {f["file"] for f in json.loads(capsys.readouterr().out)["files"]}
    assert {"app/payments.py", ".pylintrc"} <= since_commit and "ignored.txt" not in since_commit
    assert cli_main(["files", "--local", str(repo), "--since", "0" * 40]) == 0  # a pruned snapshot falls back to the last commit
    assert json.loads(capsys.readouterr().out)["base"] == "last commit"
    git(repo, "branch", "-m", "main", "feature")                               # no main/master: --since still works
    reset_gateways()
    assert cli_main(["files", "--local", str(repo), "--since", "HEAD"]) == 0
    assert cli_main(["files", "--local", str(repo)]) == 2                     # the whole branch needs a base
