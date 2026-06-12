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
    5. Stage the changed files. If anything was changed and DRY_RUN
       is not set, create a single commit describing the sync.

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
from pathlib import Path

import anthropic
import yaml

UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO", "mattpocock/skills")
UPSTREAM_REF = "upstream/main"
SKILL_GLOB = "skills/**/SKILL.md"
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")
DEFAULT_BASE_URL = "https://api.minimaxi.com/anthropic"
DEFAULT_MODEL = "MiniMax-M3"


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
) -> bool:
    en_text = read_file_from_ref(path, UPSTREAM_REF)
    if en_text is None:
        return False
    try:
        meta, body = parse_frontmatter(en_text)
    except ValueError as e:
        print(f"  [skip] {path}: {e}")
        return False
    en_desc = meta.get("description")
    if not en_desc or not isinstance(en_desc, str):
        print(f"  [skip] {path}: no description")
        return False
    if CJK_PATTERN.search(en_desc):
        print(f"  [skip] {path}: already contains CJK")
        return False

    zh_desc = call_llm(en_desc, client, model)
    print(f"  [ok]   {path}")

    if dry_run:
        return True

    new_meta = dict(meta)
    new_meta["description"] = zh_desc
    dumped = yaml.safe_dump(new_meta, allow_unicode=True, sort_keys=False, width=4096)
    out = f"---\n{dumped}---\n{body}"
    Path(path).write_text(out, encoding="utf-8")
    shell(["git", "add", path])
    return True


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
    base_url = os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    client = anthropic.Anthropic(base_url=base_url, api_key=api_key)
    print(f"Using endpoint {base_url} model {model}")

    add_upstream_remote()
    changed = diff_files()
    print(f"Found {len(changed)} SKILL.md file(s) differing from {UPSTREAM_REF}")
    if not changed:
        return 0

    translated = 0
    for path in changed:
        try:
            if translate_and_write(path, client, model, dry_run=dry_run):
                translated += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [err]  {path}: {e}", file=sys.stderr)

    print(f"Translated {translated} file(s).")
    upstream_head = run(["git", "rev-parse", "--short", UPSTREAM_REF])
    msg = (
        f"Auto-translate SKILL.md descriptions from {UPSTREAM_REF} ({upstream_head})\n\n"
        f"Files translated: {translated}\n"
        f"Triggered by sync-descriptions workflow via {base_url} ({model}). "
        f"Please review translation quality."
    )
    commit_if_dirty(msg, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
