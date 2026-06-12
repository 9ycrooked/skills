#!/usr/bin/env python3
"""
Sync & translate SKILL.md descriptions from upstream (mattpocock/skills)
using the Anthropic SDK. Works against any Anthropic-compatible endpoint
(default: MiniMax via ANTHROPIC_BASE_URL=https://api.minimaxi.com/anthropic).

Usage (typically via GitHub Actions, but can also run locally):
    ANTHROPIC_API_KEY=... python scripts/sync_descriptions.py [--dry-run]

Behaviour:
    1. Fetch upstream/main.
    2. Diff SKILL.md files between upstream/main and the current branch.
    3. For every added or modified SKILL.md whose description is still
       in English, translate it to Chinese via the Anthropic Messages API
       and write the result back.
    4. Skip files whose description already contains CJK characters
       (already localized, prevents loops).
    5. Stage the changed files. Write a side-by-side English/Chinese
       sync report to docs/sync-reports/<UTC-stamp>.md and stage it too.
       If anything was changed and DRY_RUN is not set, create a single
       commit describing the sync.

Env vars:
    ANTHROPIC_API_KEY    (required) API key for the chat endpoint.
    ANTHROPIC_BASE_URL   (optional) Defaults to MiniMax's Anthropic-compatible
                         endpoint: https://api.minimaxi.com/anthropic
    ANTHROPIC_MODEL      (optional) Defaults to MiniMax-M3.
    DRY_RUN              (optional) 'true' to skip commit/push.
    UPSTREAM_REPO        (optional) Defaults to mattpocock/skills.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic
import yaml

UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO") or "mattpocock/skills"
UPSTREAM_REF = "upstream/main"
TARGET_BRANCH = os.environ.get("TARGET_BRANCH") or "localize/descriptions-to-zh"
SKILL_GLOB = "skills/**/SKILL.md"
REPORT_DIR = Path("docs/sync-reports")
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")
DEFAULT_BASE_URL = "https://api.minimaxi.com/anthropic"
DEFAULT_MODEL = "MiniMax-M3"


@dataclass
class ReportEntry:
    path: str
    english: str
    chinese: str


@dataclass
class SyncReport:
    upstream_head: str
    base_url: str
    model: str
    dry_run: bool
    files_inspected: int
    entries: list[ReportEntry] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def run(cmd: list[str], **kw) -> str:
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("text", True)
    kw.setdefault("errors", "replace")
    return subprocess.check_output(cmd, **kw).strip()


def shell(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def add_upstream_remote() -> None:
    remotes = run(["git", "remote"]).splitlines()
    if "upstream" not in remotes:
        shell([
            "git", "remote", "add", "upstream",
            f"https://github.com/{UPSTREAM_REPO}.git",
        ])
    shell(["git", "fetch", "upstream", "main"])


def ensure_on_target_branch(target: str) -> None:
    """Switch to the target branch if we are not already on it.

    The workflow checks out the triggering ref (e.g. main for cron /
    dispatch), but the translation work needs to land on
    `localize/descriptions-to-zh`. Switching here means the same script
    works whether you run it from a manual dispatch, a cron trigger, or
    locally on any branch.
    """
    current = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if current == target:
        return
    print(f"Switching from {current} to {target}")
    # If the target branch has no local ref yet (e.g. first run after a
    # fresh clone), create it tracking origin.
    try:
        run(["git", "show-ref", "--verify", f"refs/heads/{target}"])
        shell(["git", "checkout", target])
    except subprocess.CalledProcessError:
        shell(["git", "checkout", "-b", target, f"origin/{target}"])


def diff_files() -> list[str]:
    """SKILL.md files added or modified on upstream/main vs current HEAD."""
    a = run([
        "git", "diff", "--name-only", "--diff-filter=AM",
        f"{UPSTREAM_REF}..HEAD", "--", SKILL_GLOB,
    ]).splitlines()
    b = run([
        "git", "diff", "--name-only", "--diff-filter=A",
        f"HEAD..{UPSTREAM_REF}", "--", SKILL_GLOB,
    ]).splitlines()
    return sorted({p for p in a + b if p})


def read_file_from_ref(path: str, ref: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "show", f"{ref}:{path}"],
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError:
        return None


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (meta, body) from a SKILL.md file."""
    if not text.startswith("---"):
        raise ValueError("file has no frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("malformed frontmatter")
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        raise ValueError("frontmatter is not a mapping")
    return meta, parts[2]


TRANSLATION_PROMPT = """\
You translate the `description` field of a SKILL.md frontmatter from \
English to natural Chinese. Follow these rules strictly:

1. Translate the prose into fluent, natural Chinese. Avoid translationese.
2. PRESERVE verbatim (do not translate):
   - Trigger keywords and code symbols: TDD, red-green-refactor, \
reset --hard, /grill-me, /caveman, etc.
   - File paths inside backticks: `CONTEXT.md`, `docs/adr/`, etc.
   - Quoted phrases: "caveman mode", "design it twice", etc.
   - Domain terms commonly kept in English in this codebase: \
triage role, tracer bullet, AFK agent, state machine, red-green-refactor, \
progressive disclosure, ubiquitous language, etc.
3. Keep technical accuracy. The translation should let a Chinese reader \
understand when to invoke this skill, the same way the English version does.
4. Output ONLY the translated description string. No markdown fence, \
no preamble, no explanation, no quotes around the output.

English description:
---
{english}
---

Chinese translation:"""


def call_llm(english: str, client: anthropic.Anthropic, model: str) -> str:
    msg = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": TRANSLATION_PROMPT.format(english=english),
        }],
    )
    # Concatenate every text block; ignore thinking blocks.
    parts = [
        block.text for block in msg.content
        if getattr(block, "type", None) == "text"
    ]
    text = "".join(parts).strip()
    # Defensive cleanup if the model added a fence.
    text = re.sub(r"^```(?:[a-zA-Z]*)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip().strip('"').strip()


def translate_and_write(
    path: str,
    client: anthropic.Anthropic,
    model: str,
    dry_run: bool,
    report: SyncReport,
) -> bool:
    en_text = read_file_from_ref(path, UPSTREAM_REF)
    if en_text is None:
        report.skipped.append((path, "file not present in upstream"))
        print(f"  [skip] {path}: not in upstream")
        return False
    try:
        meta, body = parse_frontmatter(en_text)
    except ValueError as e:
        report.skipped.append((path, f"frontmatter: {e}"))
        print(f"  [skip] {path}: {e}")
        return False
    en_desc = meta.get("description")
    if not en_desc or not isinstance(en_desc, str):
        report.skipped.append((path, "no description"))
        print(f"  [skip] {path}: no description")
        return False
    if CJK_PATTERN.search(en_desc):
        report.skipped.append((path, "already contains CJK"))
        print(f"  [skip] {path}: already contains CJK")
        return False

    zh_desc = call_llm(en_desc, client, model)
    print(f"  [ok]   {path}")
    report.entries.append(ReportEntry(path=path, english=en_desc, chinese=zh_desc))

    if dry_run:
        return True

    new_meta = dict(meta)
    new_meta["description"] = zh_desc
    dumped = yaml.safe_dump(new_meta, allow_unicode=True, sort_keys=False, width=4096)
    out = f"---\n{dumped}---\n{body}"
    Path(path).write_text(out, encoding="utf-8")
    shell(["git", "add", path])
    return True


def render_report(report: SyncReport, generated_at: datetime) -> str:
    """Render the sync run as a Markdown document."""
    lines: list[str] = []
    title_at = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"# 同步报告 — {title_at}")
    lines.append("")
    lines.append("## 摘要")
    lines.append("")
    lines.append(f"- 上游 HEAD：`{report.upstream_head}`（{UPSTREAM_REPO}）")
    lines.append(f"- 检查文件数：{report.files_inspected}")
    lines.append(f"- 翻译文件数：{len(report.entries)}")
    lines.append(f"- 跳过文件数：{len(report.skipped)}")
    lines.append(f"- 失败文件数：{len(report.failures)}")
    lines.append(f"- 模型：`{report.model}`，端点：`{report.base_url}`")
    lines.append(f"- 模式：{'DRY RUN（仅预览，未写入）' if report.dry_run else '实际运行'}")
    lines.append("")

    if report.entries:
        lines.append("## 翻译对照")
        lines.append("")
        for entry in report.entries:
            lines.append(f"### `{entry.path}`")
            lines.append("")
            lines.append("**英文：**")
            lines.append("")
            lines.append("> " + entry.english.replace("\n", "\n> "))
            lines.append("")
            lines.append("**中文：**")
            lines.append("")
            lines.append("> " + entry.chinese.replace("\n", "\n> "))
            lines.append("")

    if report.skipped:
        lines.append("## 跳过")
        lines.append("")
        for path, reason in report.skipped:
            lines.append(f"- `{path}` — {reason}")
        lines.append("")

    if report.failures:
        lines.append("## 失败")
        lines.append("")
        for path, reason in report.failures:
            lines.append(f"- `{path}` — {reason}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("由 `scripts/sync_descriptions.py` 自动生成。合并前请人工 review 中文翻译质量。")
    lines.append("")
    return "\n".join(lines)


def write_report(report: SyncReport, dry_run: bool) -> Optional[Path]:
    """Render and write the report. Returns the path (or None on dry-run)."""
    generated_at = datetime.now(timezone.utc)
    stamp = generated_at.strftime("%Y-%m-%d-%H%M-UTC")
    out_path = REPORT_DIR / f"{stamp}.md"
    content = render_report(report, generated_at)
    if dry_run:
        print(f"DRY_RUN：未写入文件，计划输出到 {out_path} "
              f"（{len(content)} 字节，{len(report.entries)} 条翻译）")
        return out_path
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    shell(["git", "add", str(out_path)])
    print(f"Wrote report to {out_path}")
    return out_path


def commit_if_dirty(message: str, dry_run: bool) -> None:
    status = run(["git", "status", "--porcelain"])
    if not status:
        print("Nothing to commit.")
        return
    if dry_run:
        print("DRY_RUN: staged changes that would be committed:")
        shell(["git", "diff", "--cached", "--stat"])
        return
    shell(["git", "commit", "-m", message])


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    if dry_run:
        print("Running in DRY_RUN mode (no commits will be created)")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY env var is required", file=sys.stderr)
        return 1
    base_url = os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL
    model = os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL

    client = anthropic.Anthropic(base_url=base_url, api_key=api_key)
    print(f"Using endpoint {base_url} model {model}")

    add_upstream_remote()
    ensure_on_target_branch(TARGET_BRANCH)
    changed = diff_files()
    print(f"Found {len(changed)} SKILL.md file(s) differing from {UPSTREAM_REF}")

    upstream_head = run(["git", "rev-parse", "--short", UPSTREAM_REF])
    report = SyncReport(
        upstream_head=upstream_head,
        base_url=base_url,
        model=model,
        dry_run=dry_run,
        files_inspected=len(changed),
    )

    if not changed:
        write_report(report, dry_run=dry_run)
        if not dry_run:
            commit_if_dirty(
                f"Sync report (no changes) from {UPSTREAM_REF} ({upstream_head})",
                dry_run=dry_run,
            )
        return 0

    for path in changed:
        try:
            translate_and_write(path, client, model, dry_run=dry_run, report=report)
        except Exception as e:  # noqa: BLE001
            report.failures.append((path, str(e)))
            print(f"  [err]  {path}: {e}", file=sys.stderr)

    print(f"Translated {len(report.entries)} file(s).")
    if dry_run:
        write_report(report, dry_run=True)
        return 0

    write_report(report, dry_run=False)
    msg = (
        f"Auto-translate SKILL.md descriptions from {UPSTREAM_REF} ({upstream_head})\n\n"
        f"Files translated: {len(report.entries)} (skipped: {len(report.skipped)}, "
        f"failed: {len(report.failures)})\n"
        f"Triggered by sync-descriptions workflow via {base_url} ({model}). "
        f"See docs/sync-reports/ for the side-by-side English/Chinese report."
    )
    commit_if_dirty(msg, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
