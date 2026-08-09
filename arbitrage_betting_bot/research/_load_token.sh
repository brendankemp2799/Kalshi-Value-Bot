# Sourced by run_weekly.sh / run_deep.sh — loads CLAUDE_CODE_OAUTH_TOKEN from a
# dedicated, tightly-permissioned file rather than requiring it inline in crontab
# (crontab entries are viewable via `crontab -l`; this keeps the secret out of that
# and out of any cron log). The token itself is never committed — see .gitignore.
#
# One-time setup (run once, on the machine that will run these scripts):
#   claude setup-token
#   echo "CLAUDE_CODE_OAUTH_TOKEN=<paste the token here>" > research/../.claude_token
#   chmod 600 research/../.claude_token
#
# Also ensure the native `claude` install (~/.local/bin) is on PATH — cron (like
# any non-interactive, non-login shell) never sources ~/.bashrc, so relying on that
# alone silently breaks under cron even though it works fine interactively.
export PATH="$HOME/.local/bin:$PATH"

TOKEN_FILE="$(dirname "${BASH_SOURCE[0]}")/../.claude_token"
if [ -f "$TOKEN_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; source "$TOKEN_FILE"; set +a
else
    echo "WARNING: $TOKEN_FILE not found — falling back to whatever auth the" >&2
    echo "  ambient environment/keychain provides (fine for interactive use," >&2
    echo "  will fail under cron with no login session). See research/README.md." >&2
fi
