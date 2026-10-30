#!/usr/bin/env bash
# Install autodeliver-dotnet for the current user (macOS and Linux).
#
#   ./scripts/install.sh                 install from this checkout
#   ./scripts/install.sh --from <src>    install from another checkout, a wheel, or a pip URL
#   ./scripts/install.sh --github        install the latest main from GitHub
#   ./scripts/install.sh --uninstall     remove it again
#
# What it does: a private virtual environment in ~/.autodeliver/venv (override with AUTODELIVER_HOME),
# a normal (non-editable) install into it, and an `autodeliver` link in ~/.local/bin, which is added to
# your shell's PATH if it is not there yet. Re-running it upgrades in place. Nothing needs sudo.
set -euo pipefail

GITHUB_SOURCE="git+https://github.com/Miry075/autodeliver-dotnet-0.2.0-py3-none-any.git#subdirectory=autodeliver"
HOME_DIR="${AUTODELIVER_HOME:-$HOME/.autodeliver}"
VENV="$HOME_DIR/venv"
BIN_DIR="$HOME/.local/bin"
LINK="$BIN_DIR/autodeliver"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKOUT="$(dirname "$SCRIPT_DIR")"

say()  { printf '%s\n' "$*"; }
ok()   { printf 'OK    %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*"; }
die()  { printf 'ERROR %s\n' "$*" >&2; exit 1; }

source_arg=""
uninstall=false
while [ $# -gt 0 ]; do
  case "$1" in
    --from)      [ $# -ge 2 ] || die "--from needs a path or URL"; source_arg="$2"; shift 2 ;;
    --github)    source_arg="$GITHUB_SOURCE"; shift ;;
    --uninstall) uninstall=true; shift ;;
    -h|--help)   sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           die "unknown option: $1 (see --help)" ;;
  esac
done

# --- uninstall -------------------------------------------------------------------------------------
if $uninstall; then
  [ -L "$LINK" ] && rm -f "$LINK" && ok "removed $LINK"
  [ -d "$VENV" ] && rm -rf "$VENV" && ok "removed $VENV"
  say "Done. The PATH line in your shell profile (marked 'autodeliver') is harmless; delete it if you like."
  exit 0
fi

# --- source ----------------------------------------------------------------------------------------
if [ -z "$source_arg" ]; then
  [ -f "$CHECKOUT/pyproject.toml" ] || die "no pyproject.toml next to this script; use --from <path> or --github"
  source_arg="$CHECKOUT"
fi
case "$source_arg" in
  git+*|http://*|https://*) ;;                                   # pip URL
  *) [ -e "$source_arg" ] || die "not found: $source_arg"
     source_arg="$(cd "$(dirname "$source_arg")" && pwd)/$(basename "$source_arg")" ;;
esac

# --- Python 3.10+ ----------------------------------------------------------------------------------
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$candidate")"; break
  fi
done
[ -n "$PY" ] || die "Python 3.10 or later not found (macOS: brew install python@3.12)"
ok "python  $("$PY" --version 2>&1) ($PY)"

# --- virtual environment + install -------------------------------------------------------------------
# Recreate the venv when its Python is gone (e.g. after a Homebrew upgrade) instead of failing later.
if [ -d "$VENV" ] && ! "$VENV/bin/python" -c 'import sys' >/dev/null 2>&1; then
  warn "existing environment is broken; recreating it"
  rm -rf "$VENV"
fi
if [ ! -d "$VENV" ]; then
  mkdir -p "$HOME_DIR"
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
say "Installing from $source_arg ..."
# A normal install pulls the dependencies; the --force-reinstall --no-deps pass then replaces the
# package itself even when the version number did not change (pip would otherwise keep the old code).
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check "$source_arg"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --force-reinstall --no-deps "$source_arg"
version="$("$VENV/bin/autodeliver" --version)" || die "installed, but 'autodeliver --version' failed"
ok "installed  $version  ($VENV)"

# --- command on PATH -------------------------------------------------------------------------------
mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/autodeliver" "$LINK"
ok "command  $LINK"
path_note=""
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    case "${SHELL##*/}" in
      zsh)  profile="$HOME/.zshrc" ;;
      bash) if [ "$(uname)" = Darwin ]; then profile="$HOME/.bash_profile"; else profile="$HOME/.bashrc"; fi ;;
      *)    profile="$HOME/.profile" ;;
    esac
    if ! grep -qs '# autodeliver' "$profile"; then
      printf '\nexport PATH="$HOME/.local/bin:$PATH"  # autodeliver\n' >> "$profile"
      ok "PATH     added ~/.local/bin to $profile"
    fi
    path_note="Open a new Terminal window (or run: source $profile) so 'autodeliver' is found."
    ;;
esac

# --- prerequisites for running it (warnings only) ---------------------------------------------------
command -v git    >/dev/null 2>&1 && ok "git      $(git --version)" || warn "git not found"
command -v dotnet >/dev/null 2>&1 && ok "dotnet   $(dotnet --version 2>/dev/null)" || warn "dotnet SDK not found (https://dot.net)"
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  warn "ANTHROPIC_API_KEY is set: runs are billed to that key, not your Claude subscription"
elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  ok "claude   CLAUDE_CODE_OAUTH_TOKEN (your subscription)"
else
  ok "claude   your Claude Code login will be used (run 'claude' then /login once if you have not)"
fi

say ""
say "Done. Next, in your .NET repository:"
say "  autodeliver init && autodeliver doctor"
say "  autodeliver run --work-item 1 --dry-run --story-file ~/story.json"
[ -n "$path_note" ] && say "" && say "$path_note"
exit 0
