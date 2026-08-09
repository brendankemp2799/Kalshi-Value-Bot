#!/bin/bash
# Weekly (or threshold-triggered) Performance Analyst pass.
#
# Runs under whatever `claude` auth is already configured — subscription-based
# (claude.ai login / `claude setup-token`), never an API key. See research/README.md
# for how this is scheduled (or run manually: ./research/run_weekly.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p research/findings
LOG="research/findings/run_log.jsonl"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting weekly Performance Analyst pass" >&2

# --allowedTools mirrors (and enforces, defense-in-depth) the tool list already
# declared in .claude/agents/performance-analyst.md's frontmatter — this agent never
# needs Edit or anything outside Read/Bash/Write/Glob/Grep, so there's no bypass-
# permissions flag here; scoping the allowed tools this precisely should let an
# unattended run proceed without needing a broader interactive-prompt bypass.
claude -p "Run your weekly review now — follow your instructions exactly (research/metrics.py, compare to research/findings/, write hypotheses/findings, save the snapshot at the end)." \
  --agent performance-analyst \
  --output-format json \
  --allowedTools "Read Bash Write Glob Grep" \
  >> "$LOG"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Done — see research/findings/ and research/hypotheses/" >&2
