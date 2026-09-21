#!/usr/bin/env sh
# ---------------------------------------------------------------------------
# Unknown Agent installer (Linux-first, POSIX sh)
#
# Recommended usage (download, INSPECT, then run - never pipe blindly):
#   curl -fsSLO https://raw.githubusercontent.com/OWNER/REPO/main/scripts/install.sh
#   less install.sh            # read what you are about to execute
#   sh install.sh
#
# What it does, in order:
#   1. detect OS / Python / tooling
#   2. pick the best install strategy:  pipx > uv tool > managed venv
#   3. install unknown-agent from GitHub (or PyPI with --from-pypi)
#   4. make sure `ua` is on PATH (and tell you exactly what to do if not)
#   5. verify the install (`ua version`, `ua doctor`)
#   6. optionally enable the weekly maintenance timer (systemd user unit)
#   7. offer first-run setup (`ua setup`)
#
# It never uses sudo. It never writes outside your home directory.
# Every action is printed. Re-running is safe (idempotent).
#
# Flags:
#   --from-pypi          install the published package instead of git
#   --git-url URL        override the source repository
#   --ref REF            branch / tag / commit to install (default: main)
#   --strategy S         force pipx | uv | venv
#   --with-service       enable the systemd maintenance timer without asking
#   --no-service         never touch systemd
#   --with-setup         run `ua setup` at the end without asking
#   --no-setup           skip the setup offer
#   --yes                assume yes for every optional question
#   --uninstall          remove unknown-agent installed by this script
#   --help
# ---------------------------------------------------------------------------
set -eu

# ----------------------------- configuration --------------------------------
GIT_URL="${UA_GIT_URL:-https://github.com/OWNER/REPO}"
GIT_REF="${UA_GIT_REF:-main}"
PKG="unknown-agent"
UA_HOME="${UA_HOME:-$HOME/.local/share/unknown-agent}"
BIN_DIR="$HOME/.local/bin"
MIN_PY_MINOR=11

FROM_PYPI=0; STRATEGY=""; WITH_SERVICE=""; WITH_SETUP=""; ASSUME_YES=0; DO_UNINSTALL=0

# ----------------------------- tiny ui ---------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B="$(printf '\033[1m')"; G="$(printf '\033[32m')"; Y="$(printf '\033[33m')"
  R="$(printf '\033[31m')"; D="$(printf '\033[2m')"; N="$(printf '\033[0m')"
else
  B=""; G=""; Y=""; R=""; D=""; N=""
fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
fail() { printf '  %s✗ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }
step() { printf '\n%s%s%s\n' "$B" "$*" "$N"; }

confirm() {
  # confirm "question" -> 0/1 ; honours --yes and non-tty (defaults to no)
  [ "$ASSUME_YES" = 1 ] && return 0
  if [ ! -t 0 ]; then return 1; fi
  printf '  %s [y/N] ' "$1"
  read -r answer || return 1
  case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

usage() { sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

# ----------------------------- arg parsing -----------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --from-pypi)    FROM_PYPI=1 ;;
    --git-url)      GIT_URL="$2"; shift ;;
    --ref)          GIT_REF="$2"; shift ;;
    --strategy)     STRATEGY="$2"; shift ;;
    --with-service) WITH_SERVICE=1 ;;
    --no-service)   WITH_SERVICE=0 ;;
    --with-setup)   WITH_SETUP=1 ;;
    --no-setup)     WITH_SETUP=0 ;;
    --yes|-y)       ASSUME_YES=1 ;;
    --uninstall)    DO_UNINSTALL=1 ;;
    --help|-h)      usage ;;
    *) fail "unknown flag: $1 (see --help)" ;;
  esac
  shift
done

SPEC="$PKG"
[ "$FROM_PYPI" = 0 ] && SPEC="git+${GIT_URL}@${GIT_REF}"

# ----------------------------- uninstall -------------------------------------
uninstall() {
  step "Uninstalling Unknown Agent"
  if command -v ua >/dev/null 2>&1; then
    ua service remove maintenance >/dev/null 2>&1 && ok "removed maintenance timer" || true
  fi
  if command -v pipx >/dev/null 2>&1 && pipx list 2>/dev/null | grep -q "$PKG"; then
    pipx uninstall "$PKG" && ok "pipx package removed"
  fi
  if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q "$PKG"; then
    uv tool uninstall "$PKG" && ok "uv tool removed"
  fi
  if [ -d "$UA_HOME/venv" ]; then
    rm -rf "$UA_HOME/venv" && ok "removed $UA_HOME/venv"
    rm -f "$BIN_DIR/ua" && ok "removed $BIN_DIR/ua launcher"
    rm -f "$UA_HOME/install-receipt.txt"
  fi
  say ""
  say "Kept your data (delete manually if you want a clean slate):"
  say "  ~/.config/unknown-agent    configuration + credentials"
  say "  ~/.local/state/unknown-agent  run journals and logs"
  exit 0
}
[ "$DO_UNINSTALL" = 1 ] && uninstall

# ----------------------------- 1. detect -------------------------------------
step "1/6 Checking the environment"
OS="$(uname -s 2>/dev/null || echo unknown)"
case "$OS" in
  Linux)  ok "Linux detected" ;;
  Darwin) ok "macOS detected (supported; Linux gets the premium path)" ;;
  *)      warn "$OS - on Windows use PowerShell:  py -m pip install --user pipx && pipx install $SPEC" ;;
esac

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    minor="$("$candidate" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 0)"
    major="$("$candidate" -c 'import sys; print(sys.version_info.major)' 2>/dev/null || echo 0)"
    if [ "$major" = 3 ] && [ "$minor" -ge "$MIN_PY_MINOR" ]; then PYTHON="$candidate"; break; fi
  fi
done
if [ -n "$PYTHON" ]; then
  ok "Python: $("$PYTHON" --version 2>&1) ($(command -v "$PYTHON"))"
else
  fail "Python >= 3.$MIN_PY_MINOR is required. Install it first:
    Debian/Ubuntu:  sudo apt install python3.12 python3.12-venv
    Fedora:         sudo dnf install python3.12
    Arch:           sudo pacman -S python"
fi

if [ "$FROM_PYPI" = 0 ] && ! command -v git >/dev/null 2>&1; then
  warn "git is not installed - pip will need it for a git install"
  fail "Install git (e.g. sudo apt install git) or rerun with --from-pypi once the package is published."
fi

# ----------------------------- 2. strategy -----------------------------------
step "2/6 Choosing the install strategy"
if [ -z "$STRATEGY" ]; then
  if command -v pipx >/dev/null 2>&1; then STRATEGY=pipx
  elif command -v uv >/dev/null 2>&1;  then STRATEGY=uv
  else STRATEGY=venv
  fi
fi
case "$STRATEGY" in
  pipx) ok "pipx (isolated app venv, clean upgrades) - the recommended path" ;;
  uv)   ok "uv tool (isolated, fast)" ;;
  venv) ok "managed venv at $UA_HOME/venv (no pipx/uv found - this script manages isolation itself)"
        say "  ${D}tip: 'sudo apt install pipx' (or 'python3 -m pip install --user pipx') gives the smoothest upgrades${N}" ;;
  *) fail "unknown strategy '$STRATEGY' (pipx | uv | venv)" ;;
esac

# ----------------------------- 3. install ------------------------------------
step "3/6 Installing $PKG from ${SPEC}"
case "$STRATEGY" in
  pipx)
    pipx install --force "$SPEC" || fail "pipx install failed"
    ok "installed via pipx"
    ;;
  uv)
    uv tool install --force "$SPEC" || fail "uv tool install failed"
    ok "installed via uv tool"
    ;;
  venv)
    mkdir -p "$UA_HOME" "$BIN_DIR"
    if [ ! -x "$UA_HOME/venv/bin/python" ]; then
      "$PYTHON" -m venv "$UA_HOME/venv" || fail "could not create the venv (is the python3-venv package installed?)"
      ok "created venv at $UA_HOME/venv"
    else
      ok "reusing existing venv at $UA_HOME/venv"
    fi
    "$UA_HOME/venv/bin/python" -m pip install --quiet --upgrade pip
    "$UA_HOME/venv/bin/python" -m pip install --upgrade "$SPEC" || fail "pip install failed"
    ok "package installed into the managed venv"
    # Launcher: a real symlink beats a wrapper script (argv[0], signals, speed).
    ln -sf "$UA_HOME/venv/bin/ua" "$BIN_DIR/ua"
    ok "launcher: $BIN_DIR/ua -> $UA_HOME/venv/bin/ua"
    # Receipt: lets `ua upgrade`, `ua doctor` and --uninstall know their origin.
    {
      echo "installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      echo "strategy=venv"
      echo "source=$( [ "$FROM_PYPI" = 1 ] && echo pypi || echo git )"
      echo "git_url=$GIT_URL"
      echo "git_ref=$GIT_REF"
    } > "$UA_HOME/install-receipt.txt"
    ;;
esac

# ----------------------------- 4. PATH ---------------------------------------
step "4/6 Registering the command"
hash -r 2>/dev/null || true
if command -v ua >/dev/null 2>&1; then
  ok "'ua' resolves to $(command -v ua)"
else
  case ":$PATH:" in
    *":$BIN_DIR:"*) warn "'ua' not found yet - open a new shell, or run: hash -r" ;;
    *)
      warn "$BIN_DIR is not on your PATH"
      PROFILE="$HOME/.profile"
      [ -n "${ZSH_VERSION:-}" ] && PROFILE="$HOME/.zshrc"
      [ -n "${BASH_VERSION:-}" ] && PROFILE="$HOME/.bashrc"
      say "  add it (then restart the shell):"
      say "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> $PROFILE"
      if confirm "Append that line to $PROFILE now?"; then
        printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$PROFILE"
        ok "PATH line appended to $PROFILE (takes effect in new shells)"
      fi
      ;;
  esac
fi

# ----------------------------- 5. verify -------------------------------------
step "5/6 Verifying the installation"
UA_BIN="$(command -v ua 2>/dev/null || echo "$BIN_DIR/ua")"
if [ ! -x "$UA_BIN" ]; then
  # pipx/uv shims also land in ~/.local/bin; last resort: the venv binary.
  [ -x "$UA_HOME/venv/bin/ua" ] && UA_BIN="$UA_HOME/venv/bin/ua"
fi
[ -x "$UA_BIN" ] || fail "cannot locate the 'ua' launcher after install"
VERSION_OUT="$("$UA_BIN" version 2>&1)" || fail "'ua version' failed: $VERSION_OUT"
ok "$VERSION_OUT"
if "$UA_BIN" --non-interactive doctor >/dev/null 2>&1; then
  ok "ua doctor: environment checks pass"
else
  warn "ua doctor reported issues - run '$UA_BIN doctor' to see them (often just missing optional extras)"
fi

# ----------------------------- 6. optional integrations ----------------------
step "6/6 Optional integrations"
if [ "$OS" = "Linux" ] && [ "$WITH_SERVICE" != 0 ]; then
  if [ "$WITH_SERVICE" = 1 ] || confirm "Enable weekly maintenance (systemd user timer: prunes logs/journals, caches update checks)?"; then
    if "$UA_BIN" service install maintenance >/dev/null 2>&1; then
      ok "maintenance timer enabled (inspect: ua service status)"
    else
      warn "systemd user manager unavailable - run 'ua service run-maintenance' occasionally, or add the cron line 'ua service install' prints"
    fi
  else
    say "  ${D}skipped - enable later with: ua service install maintenance${N}"
  fi
fi

say ""
say "${B}${G}Unknown Agent is installed.${N}"
say ""
say "  launch:   ${B}ua${N}            (or: ua --help)"
say "  setup:    ${B}ua setup${N}      guided provider + model onboarding"
say "  verify:   ua doctor"
say "  upgrade:  ua upgrade        remove: sh install.sh --uninstall"
say ""
if [ "$WITH_SETUP" != 0 ]; then
  if [ "$WITH_SETUP" = 1 ] || confirm "Run first-time setup now (ua setup)?"; then
    exec "$UA_BIN" setup
  fi
fi
