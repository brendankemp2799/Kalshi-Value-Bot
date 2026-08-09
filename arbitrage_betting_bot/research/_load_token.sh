# Sourced by run_weekly.sh / run_deep.sh.
#
# Auth: relies on the persisted local credential (~/.claude/.credentials.json)
# created by running `claude setup-token` (or `claude` interactive login) ONCE on
# this machine. Verified empirically (2026-08-09) to work fine under cron-like
# conditions — same root user, same $HOME, so cron sees the same credential file.
#
# The CLI also prints a separate portable CLAUDE_CODE_OAUTH_TOKEN value meant for
# exactly this kind of headless use, and this file will use it from a git-ignored
# .claude_token if present — but empirically that path returned "401 Invalid bearer
# token" every time it was tried here (multiple regenerations, clean recreation,
# whitespace ruled out, tested standalone with no wrapper scripts involved), while
# the persisted ambient credential worked immediately and reliably. Root cause
# unresolved; going with what's actually verified working rather than the
# documented-but-broken mechanism. Revisit if a newer Claude Code version fixes it.
#
# One-time setup (run once, on the machine that will run these scripts):
#   claude setup-token
#   (just completing the login is enough — the persisted credential is what's used)
#
# Also ensure the native `claude` install (~/.local/bin) is on PATH — cron (like
# any non-interactive, non-login shell) never sources ~/.bashrc, so relying on that
# alone silently breaks under cron even though it works fine interactively.
export PATH="$HOME/.local/bin:$PATH"

TOKEN_FILE="$(dirname "${BASH_SOURCE[0]}")/../.claude_token"
if [ -f "$TOKEN_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; source "$TOKEN_FILE"; set +a
fi
# No warning if absent — the persisted ~/.claude/.credentials.json from the
# one-time interactive setup is the primary, verified mechanism; this file is an
# optional override, not a requirement.
