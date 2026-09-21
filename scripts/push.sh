#!/usr/bin/env sh
# Secure push helper: authenticates with a GitHub PAT WITHOUT persisting it.
#
# The token is read silently from the terminal (or from $GITHUB_TOKEN if
# already exported), lives only in this process's memory, and is fed to git
# through a one-shot credential helper. It never touches:
#   - the remote URL          (stays https://github.com/OWNER/REPO.git)
#   - .git/config             (no credential.helper is stored)
#   - shell history           (read -s takes it from the tty, not argv)
#   - this script or any file
#
# Usage:  sh scripts/push.sh [remote] [branch]
set -eu

REMOTE="${1:-origin}"
BRANCH="${2:-$(git rev-parse --abbrev-ref HEAD)}"

if [ -z "${GITHUB_TOKEN:-}" ]; then
  printf 'GitHub PAT (input hidden): '
  # POSIX sh has no read -s; disable echo manually.
  stty -echo 2>/dev/null || true
  read -r GITHUB_TOKEN
  stty echo 2>/dev/null || true
  printf '\n'
fi
[ -n "$GITHUB_TOKEN" ] || { echo "no token given" >&2; exit 1; }
export GITHUB_TOKEN

git -c credential.helper= \
    -c credential.helper='!f() { echo "username=x-access-token"; echo "password=${GITHUB_TOKEN}"; }; f' \
    push -u "$REMOTE" "$BRANCH"

unset GITHUB_TOKEN
echo "pushed $BRANCH to $REMOTE (token was never written to disk)"
