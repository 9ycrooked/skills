# ADR 0002: Auto-sync upstream + translate SKILL.md descriptions

Status: Accepted
Date: 2026-06-12

## Context

This fork tracks `mattpocock/skills` upstream. The maintainer writes every
SKILL.md in English, which is fine for the upstream audience but hard to
scan for a Chinese-speaking reader trying to decide which skill to invoke.

We want a workflow that, whenever upstream main changes, keeps the
`localize/descriptions-to-zh` branch in sync by translating only the
`description` field of every added or modified SKILL.md into Chinese.
Translation must be lossless for AI routing: trigger keywords, command
names, file paths and domain terms stay verbatim in English so the
agent's skill-matching still works.

## Decision

A GitHub Actions workflow (`.github/workflows/sync-descriptions.yml`)
runs on a daily schedule and on manual dispatch. It:

1. Checks out the `localize/descriptions-to-zh` branch on this fork.
2. Adds `mattpocock/skills` as the `upstream` remote and fetches `main`.
3. Runs `scripts/sync_descriptions.py`, which:
   - Diffs SKILL.md files between `upstream/main` and `HEAD`.
   - For each added or modified file, extracts the `description` from the
     YAML frontmatter, calls an Anthropic-compatible Messages API to
     translate it to Chinese, and writes the result back.
   - Skips files whose `description` already contains CJK characters
     (already localized, prevents infinite loops).
   - Preserves all other frontmatter fields and the body of SKILL.md
     unchanged.
   - Renders a side-by-side English/Chinese sync report at
     `docs/sync-reports/<UTC-stamp>.md` and stages it alongside the
     translated SKILL.md files, so each run is auditable in git history.
4. Creates a single commit summarizing what was translated, and pushes
   to `localize/descriptions-to-zh` on origin.

The translation prompt is in `scripts/sync_descriptions.py` as
`TRANSLATION_PROMPT` and is easy to edit when the term list grows.

## LLM provider: MiniMax via Anthropic SDK

The script uses the official `anthropic` Python SDK pointed at MiniMax's
Anthropic-compatible endpoint. This gives us:

- An OpenAI-style chat interface (Messages API) without depending on
  OpenAI.
- A 1M-token context window on `MiniMax-M3` for cheap, large prompts.
- Stable behaviour: MiniMax ignores Anthropic-only parameters we don't
  use (`top_k`, `stop_sequences`, etc.).

If you want to point at a different Anthropic-compatible provider, set
`ANTHROPIC_BASE_URL` and `ANTHROPIC_MODEL` accordingly.

## Required configuration

Configure these in the GitHub repository settings:

| Name | Kind | Required | Default | Notes |
|---|---|---|---|---|
| `ANTHROPIC_API_KEY` | Secret | yes | — | API key for the chat endpoint. Get one at https://platform.minimaxi.com . |
| `ANTHROPIC_BASE_URL` | Variable | no | `https://api.minimaxi.com/anthropic` | Any Anthropic-compatible base URL. |
| `ANTHROPIC_MODEL` | Variable | no | `MiniMax-M3` | e.g. `MiniMax-M2.7-highspeed` for faster/cheaper runs. |
| `UPSTREAM_REPO` | Variable | no | `mattpocock/skills` | Override only if the upstream moves. |

## Consequences

Positive:

- Zero-touch sync. The branch self-updates within 24h of any upstream
  change to a SKILL.md.
- The translator only touches `description`, never the body, so any
  drift between the fork and upstream on file bodies is visible as a
  normal `git diff` and can be reviewed in a separate sync branch.
- A `dry_run` workflow_dispatch input lets us preview translations
  without committing or pushing.

Negative / risks:

- LLM translation is non-deterministic. A reviewer should glance at
  each auto-commit; the commit message calls this out.
- We rely on an external API. If the key expires or the endpoint
  changes, daily runs will silently produce no output. We rely on
  GitHub's own workflow failure notifications for that.
- Force-push is intentionally NOT used. If upstream rebase-rewrites
  history, the push will fail rather than silently rewrite the branch;
  resolve by `git pull --rebase` locally and re-run the workflow.

## Operational notes

- Manual trigger: Actions → "Sync upstream + translate descriptions"
  → Run workflow → optionally tick `dry_run`.
- Local preview:
  ```
  export ANTHROPIC_API_KEY=...
  export ANTHROPIC_BASE_URL=https://api.minimaxi.com/anthropic
  python scripts/sync_descriptions.py --dry-run
  ```
- Editing translation style: change `TRANSLATION_PROMPT` in
  `scripts/sync_descriptions.py`; no other file needs to move.
