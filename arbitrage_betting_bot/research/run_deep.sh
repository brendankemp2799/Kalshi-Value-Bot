#!/bin/bash
# Deep research cycle: Quant Research Agent investigates open hypotheses, then the
# Skeptic reviews the resulting experiments. NOT calendar-scheduled in Phase 1 — run
# manually, or call this from run_weekly.sh when a hypothesis is worth chasing.
#
# Capped at MAX_HYPOTHESES per run so one invocation can't silently burn an unbounded
# amount of Pro-plan usage investigating a long backlog.
set -euo pipefail
cd "$(dirname "$0")/.."

MAX_HYPOTHESES="${MAX_HYPOTHESES:-3}"
LOG="research/findings/run_log.jsonl"
mkdir -p research/findings

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Deep cycle starting (max ${MAX_HYPOTHESES} hypotheses)" >&2

count=0
for f in research/hypotheses/*.md; do
  [ "$(basename "$f")" = "_TEMPLATE.md" ] && continue
  grep -q "^status: open" "$f" || continue
  [ "$count" -ge "$MAX_HYPOTHESES" ] && break
  count=$((count + 1))

  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Quant Research investigating: $f" >&2
  claude -p "Investigate the hypothesis in $f — follow your instructions exactly." \
    --agent quant-research \
    --output-format json \
    --allowedTools "Read Bash Write Glob Grep" \
    >> "$LOG"
done

if [ "$count" -eq 0 ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] No open hypotheses — nothing to investigate." >&2
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Skeptic reviewing proposed experiments" >&2
for f in research/experiments/*.md; do
  [ "$(basename "$f")" = "_TEMPLATE.md" ] && continue
  grep -q "^status: proposed" "$f" || continue

  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Skeptic reviewing: $f" >&2
  claude -p "Review the experiment in $f — follow your instructions exactly, be adversarial." \
    --agent skeptic \
    --output-format json \
    --allowedTools "Read Bash Glob Grep" \
    >> "$LOG"
done

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Deep cycle done — see research/experiments/ and research/failed_ideas/" >&2
