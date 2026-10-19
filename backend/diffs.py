"""Turn a PR's patches into what a small model can actually use.

- Hunks are rendered with the NEW file's line numbers on every line, so a
  model can cite exact lines without seeing whole files.
- Lock files, build output, minified and generated files are dropped.
- The result is fitted to a token budget (an 8B model on a shared llama.cpp
  slot may only have a few thousand tokens); what did not fit is listed by name.
- For /review, `build_batches` splits the whole change into as many prompts as
  it takes, so every file is reviewed however big the change is.
- `commentable_lines` tells /improve which lines GitHub accepts inline comments on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from backend.gateway.policy_engine import path_denied

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def est_tokens(text: str) -> int:
    """Rough token count for code (about 3 characters per token for Llama/Gemma tokenizers)."""
    return len(text) // 3 + 1


@dataclass
class Hunk:
    header: str
    lines: list[tuple[str, int | None, str]] = field(default_factory=list)  # (kind, new line no, text)


def parse_patch(patch: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    new_no = 0
    current: Hunk | None = None
    for raw in (patch or "").splitlines():
        match = _HUNK.match(raw)
        if match:
            new_no = int(match.group(3))
            current = Hunk(header=raw)
            hunks.append(current)
            continue
        if current is None or raw.startswith(("diff --git", "index ", "--- ", "+++ ")):
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        kind = raw[:1] if raw[:1] in "+- " else " "
        text = raw[1:] if raw[:1] in "+- " else raw
        if kind == "-":
            current.lines.append(("-", None, text))
        else:
            current.lines.append((kind, new_no, text))
            new_no += 1
    return hunks


def commentable_lines(patch: str) -> set[int]:
    """New-side line numbers GitHub accepts a review comment on (added or context lines)."""
    return {no for hunk in parse_patch(patch) for kind, no, _ in hunk.lines if no is not None}


def added_lines(patch: str) -> set[int]:
    return {no for hunk in parse_patch(patch) for kind, no, _ in hunk.lines if kind == "+" and no is not None}


def _render_hunk(hunk: Hunk) -> str:
    context = hunk.header.split("@@")[-1].strip()
    out = [f"@@ {context}" if context else "@@"]
    for kind, no, text in hunk.lines:
        number = f"{no:>5}" if no is not None else "     "
        out.append(f"{number} {kind} {text}")
    return "\n".join(out)


def render_file(path: str, patch: str, status: str = "modified") -> str:
    return "\n".join([f"## File: {path} ({status})", *(_render_hunk(h) for h in parse_patch(patch))])


def split_unified_diff(diff: str) -> list[dict[str, Any]]:
    """Fallback when only a whole unified diff is available."""
    files: list[dict[str, Any]] = []
    for block in re.split(r"(?m)^diff --git ", diff or ""):
        if not block.strip():
            continue
        match = re.search(r"(?m)^\+\+\+ b/(.+)$", block) or re.search(r"^a/\S+ b/(\S+)", block)
        if not match:
            continue
        status = "added" if "\nnew file mode" in block else "removed" if "\ndeleted file mode" in block else "modified"
        files.append({"filename": match.group(1).strip(), "status": status, "patch": block[block.find("@@"):] if "@@" in block else ""})
    return files


@dataclass
class PromptDiff:
    text: str
    included: list[str]
    omitted: list[str]
    ignored: list[str]
    clipped: list[str]
    no_patch: list[str] = field(default_factory=list)

    def note(self) -> str:
        parts = []
        if self.no_patch:
            parts.append(f"{len(self.no_patch)} file(s) NOT reviewed, no diff (binary, too large, rename only, or a secret path): "
                         f"{', '.join(self.no_patch[:15])}")
        if self.omitted:
            parts.append(f"{len(self.omitted)} file(s) left out to fit the model's context: {', '.join(self.omitted[:15])}")
        if self.clipped:
            parts.append(f"clipped: {', '.join(self.clipped[:10])}")
        if self.ignored:
            parts.append(f"{len(self.ignored)} generated/lock/binary file(s) skipped")
        return "; ".join(parts)


def build_prompt_diff(files: list[dict[str, Any]], ignore: list[str], budget_tokens: int) -> PromptDiff:
    """Render as many changed files as fit in `budget_tokens`, most-changed source first."""
    ignored = [f["filename"] for f in files if path_denied(f["filename"], ignore)]
    candidates = [f for f in files if f["filename"] not in ignored and f.get("status") != "removed" and f.get("patch")]
    removed = [f["filename"] for f in files if f.get("status") == "removed"]
    no_patch = [f["filename"] for f in files
                if f["filename"] not in ignored and f.get("status") != "removed" and not f.get("patch")]

    def priority(f: dict[str, Any]) -> tuple[int, int]:
        name = f["filename"].lower()
        is_test = "test" in name
        is_doc = name.endswith((".md", ".rst", ".txt"))
        changed = int(f.get("additions") or 0) + int(f.get("deletions") or 0) or f["patch"].count("\n")
        return (2 if is_doc else 1 if is_test else 0, -changed)

    budget = budget_tokens
    chunks, included, omitted, clipped = [], [], [], []
    for f in sorted(candidates, key=priority):
        rendered = render_file(f["filename"], f["patch"], f.get("status", "modified"))
        cost = est_tokens(rendered)
        if cost <= budget:
            chunks.append(rendered)
            included.append(f["filename"])
            budget -= cost
        elif budget > 400:
            keep = rendered[: budget * 3 - 200]
            keep = keep[: keep.rfind("\n")] if "\n" in keep else keep
            chunks.append(keep + "\n... (rest of this file's diff clipped to fit the context window)")
            included.append(f["filename"])
            clipped.append(f["filename"])
            budget = 0
        else:
            omitted.append(f["filename"])
    if removed:
        chunks.append("## Deleted files: " + ", ".join(removed[:30]))
    return PromptDiff(text="\n\n".join(chunks), included=included, omitted=omitted, ignored=ignored, clipped=clipped,
                      no_patch=no_patch)


@dataclass
class Batch:
    text: str
    spans: list[tuple[str, int, int]]  # (file, first, last new-file line) of each piece of a file in this prompt


def _split_hunk(hunk: Hunk, budget_tokens: int) -> list[Hunk]:
    """A hunk too big for one prompt (a new file is a single hunk) cut into runs of lines that fit."""
    parts, chars = [Hunk(hunk.header)], 0
    for line in hunk.lines:
        cost = len(line[2]) + 9  # the rendered "  123 + " prefix
        if parts[-1].lines and est_tokens("x" * (chars + cost)) > budget_tokens:
            parts.append(Hunk(hunk.header))
            chars = 0
        parts[-1].lines.append(line)
        chars += cost
    return parts


def _clip(text: str, budget_tokens: int) -> str:
    keep = text[: max(budget_tokens * 3 - 200, 200)]
    keep = keep[: keep.rfind("\n")] if "\n" in keep else keep
    return keep + "\n... (rest of this line clipped to fit the context window)"


def build_batches(files: list[dict[str, Any]], ignore: list[str], budget_tokens: int) -> tuple[list[Batch], PromptDiff]:
    """Every reviewable file, packed into prompts of at most `budget_tokens`.

    Files go in path order, so neighbours share a prompt. A file too big for one prompt is split between hunks, and a
    hunk too big for one prompt between lines; only a single line bigger than a prompt is clipped (minified code).
    The PromptDiff is for its note (ignored, no diff, clipped).
    """
    ignored = [f["filename"] for f in files if path_denied(f["filename"], ignore)]
    candidates = [f for f in files if f["filename"] not in ignored and f.get("status") != "removed" and f.get("patch")]
    removed = [f["filename"] for f in files if f.get("status") == "removed"]
    no_patch = [f["filename"] for f in files
                if f["filename"] not in ignored and f.get("status") != "removed" and not f.get("patch")]

    pieces: list[tuple[str, str, int, int]] = []  # file, rendered text, first line, last line
    clipped: list[str] = []
    for f in sorted(candidates, key=lambda f: f["filename"]):
        header = f"## File: {f['filename']} ({f.get('status', 'modified')})"
        part: list[str] = []
        lo = hi = 0
        room = budget_tokens - est_tokens(header) - 10
        for hunk in (h for whole in parse_patch(f["patch"]) for h in _split_hunk(whole, room)):
            text = _render_hunk(hunk)
            if est_tokens(f"{header}\n{text}") > budget_tokens:
                text = _clip(text, room)
                clipped.append(f["filename"])
            if part and est_tokens("\n".join([header, *part, text])) > budget_tokens:
                pieces.append((f["filename"], "\n".join([header, *part]), lo, hi))
                part = []
            numbers = [no for _, no, _ in hunk.lines if no is not None] or [int(_HUNK.match(hunk.header).group(3))]
            lo, hi = (min(numbers), max(numbers)) if not part else (lo, max(hi, *numbers))
            part.append(text)
        if part:
            pieces.append((f["filename"], "\n".join([header, *part]), lo, hi))

    batches: list[Batch] = []
    for name, text, lo, hi in pieces:
        if batches and est_tokens(f"{batches[-1].text}\n\n{text}") <= budget_tokens:
            batches[-1].text += f"\n\n{text}"
            batches[-1].spans.append((name, lo, hi))
        else:
            batches.append(Batch(text, [(name, lo, hi)]))
    if removed:
        deleted = "## Deleted files: " + ", ".join(removed[:30])
        if batches:
            batches[-1].text += f"\n\n{deleted}"
        else:
            batches.append(Batch(deleted, []))
    summary = PromptDiff(text="", included=[f["filename"] for f in candidates], omitted=[], ignored=ignored,
                         clipped=sorted(set(clipped)), no_patch=no_patch)
    return batches, summary


def batch_of(file_path: str, line: int, batches: list[Batch]) -> int:
    """Index of the batch a finding came from: the one holding that file's lines, else any holding the file, else 0."""
    spans = [(i, lo, hi) for i, b in enumerate(batches) for name, lo, hi in b.spans if name == file_path]
    return next((i for i, lo, hi in spans if lo <= line <= hi), spans[0][0] if spans else 0)
