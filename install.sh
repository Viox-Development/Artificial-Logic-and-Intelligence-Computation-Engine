#!/usr/bin/env bash
# ALICE setup for Linux (and macOS). One line, no clone needed:
#   curl -fsSL https://raw.githubusercontent.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/main/install.sh | bash
# Or inside a clone:
#   bash install.sh             # ALICE and the app; the app's setup wizard picks the rest
#   bash install.sh --all       # every optional feature at once, no wizard
#
# Options: --all, --dir <folder> (default ~/.local/share/alice), --no-shortcut
# Environment: ALICE_REF (tag or branch to install; default the latest vX.Y.Z release), ALICE_REPO_URL (clone source)
set -euo pipefail

REPO_URL="${ALICE_REPO_URL:-https://github.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine.git}"
DIR="${XDG_DATA_HOME:-$HOME/.local/share}/alice"
ALL=0
SHORTCUT=1

# Embedded, not read from $0: under `curl | bash`, $0 is "bash" and there is no script file to read.
usage() {
    cat <<'EOF'
ALICE setup for Linux (and macOS). One line, no clone needed:
  curl -fsSL https://raw.githubusercontent.com/Viox-Development/Artificial-Logic-and-Intelligence-Computation-Engine/main/install.sh | bash
Or inside a clone:
  bash install.sh             # ALICE and the app; the app's setup wizard picks the rest
  bash install.sh --all       # every optional feature at once, no wizard

Options: --all, --dir <folder> (default ~/.local/share/alice), --no-shortcut
Environment: ALICE_REF (tag or branch to install; default the latest vX.Y.Z release), ALICE_REPO_URL (clone source)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --all) ALL=1 ;;
        --no-shortcut) SHORTCUT=0 ;;
        --dir) DIR="${2:?--dir needs a folder}"; shift ;;
        --dir=*) DIR="${1#--dir=}" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

# Absolute from here on: the script cds into the install, and ~/.local/bin/alice must not become a relative symlink.
case "$DIR" in
    /*) ;;
    *) DIR="$PWD/$DIR" ;;
esac

if [ -t 1 ]; then
    BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
    BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi
say()  { printf '%s\n' "$*"; }
warn() { printf '%s%s%s\n' "$YELLOW" "$*" "$RESET" >&2; }
die()  { printf '\n%sSetup could not finish: %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }

# The exact command to install missing system packages on this distro (printed, never run with sudo for you).
pkg_hint() {
    if command -v apt-get >/dev/null 2>&1; then echo "sudo apt-get install -y $*"
    elif command -v dnf >/dev/null 2>&1; then echo "sudo dnf install -y $*"
    elif command -v pacman >/dev/null 2>&1; then echo "sudo pacman -S --needed $*"
    elif command -v zypper >/dev/null 2>&1; then echo "sudo zypper install -y $*"
    elif command -v apk >/dev/null 2>&1; then echo "sudo apk add $*"
    elif command -v brew >/dev/null 2>&1; then echo "brew install $*"
    else echo "install with your package manager: $*"
    fi
}

# ------------------------------------------------------------------------------------------------ Python
find_python() {
    local c
    for c in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$c" >/dev/null 2>&1 &&
            "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
            command -v "$c"
            return 0
        fi
    done
    return 1
}

PY="$(find_python)" || die "Python 3.10 or newer was not found. Install it: $(pkg_hint python3 python3-venv python3-pip)"
say "Python: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

# Debian/Ubuntu ship venv without ensurepip; the error otherwise only shows up halfway through.
if ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    v="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    die "Python's venv module is missing. Install it: $(pkg_hint "python3-venv" "python${v}-venv")"
fi

# ------------------------------------------------------------------------------------------------ source
# Run from a clone: install that folder. Piped from curl: clone the latest release into $DIR. Only a real script file
# counts: piped, BASH_SOURCE is empty and $0 is "bash", and dirname of that is the current folder, which would install
# whatever ALICE checkout the user happens to be standing in instead of the release.
SRC=""
script="${BASH_SOURCE[0]:-}"
if [ -n "$script" ] && [ -f "$script" ]; then
    here="$(cd "$(dirname "$script")" && pwd)"
    if [ -f "$here/pyproject.toml" ] && grep -q 'name = "alice-harness"' "$here/pyproject.toml"; then
        SRC="$here"
    fi
fi

if [ -z "$SRC" ]; then
    command -v git >/dev/null 2>&1 || die "Git is needed (ALICE also updates itself with it). Install it: $(pkg_hint git)"
    if [ -d "$DIR/.git" ]; then
        say "ALICE is already in $DIR; reinstalling there (its own updater keeps the code current)."
    elif [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
        die "$DIR exists and is not an ALICE install. Remove it or pick another folder with --dir."
    else
        ref="${ALICE_REF:-}"
        if [ -z "$ref" ]; then
            # Same as the "stable" update channel: only published vX.Y.Z releases, never an unreleased main.
            ref="$(git ls-remote --tags --refs "$REPO_URL" 'v*' 2>/dev/null | sed 's#.*refs/tags/##' |
                grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n 1 || true)"
            [ -n "$ref" ] || { warn "No release tag found; installing main."; ref="main"; }
        fi
        say "Downloading ALICE $ref into $DIR ..."
        mkdir -p "$(dirname "$DIR")"
        git clone -q --depth 1 --branch "$ref" "$REPO_URL" "$DIR" || die "git clone failed (see above)."
        # A tag checkout is a detached HEAD; put it on "main" like a normal clone, which the updater expects.
        git -C "$DIR" checkout -q -B main
    fi
    SRC="$DIR"
fi
cd "$SRC"

# ------------------------------------------------------------------------------------------------ install
if [ ! -x ".venv/bin/python" ]; then
    say "Creating the virtual environment (.venv) ..."
    "$PY" -m venv .venv || die "Could not create the virtual environment (.venv)."
fi
VPY="$SRC/.venv/bin/python"
"$VPY" -m pip install -q --upgrade pip >/dev/null || warn "pip could not upgrade itself; continuing."

say "Installing ALICE (a minute or two) ..."
"$VPY" -m pip install -q -e "." || die "Package install failed, see the error above."

"$SRC/.venv/bin/alice" init >/dev/null

if [ "$ALL" = 1 ]; then
    # Everything at once (scripts, developers). Normally the app's setup wizard installs only what you pick.
    say "Installing every optional feature ..."
    "$VPY" -m pip install -q -e ".[computer,browser,core,voice,home]" ||
        warn "Some optional packages failed; ALICE still runs without them."
    if ! command -v google-chrome >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1 &&
        ! command -v chromium-browser >/dev/null 2>&1 && ! command -v microsoft-edge >/dev/null 2>&1; then
        "$VPY" -m playwright install chromium || warn "Chromium could not be downloaded; the browser tools need it."
    fi
else
    # The app opens the setup wizard on first start: language, a model, and the features you want.
    "$VPY" -c "from alice.features import set_wizard_state; set_wizard_state(wizard='pending')"
fi

# The `alice` command, on PATH without activating the venv.
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
ln -sf "$SRC/.venv/bin/alice" "$BIN/alice"

if [ "$SHORTCUT" = 1 ] && [ "$(uname -s)" = "Linux" ]; then
    "$SRC/.venv/bin/alice" shortcut >/dev/null 2>&1 && say "Added ALICE to the application menu." ||
        warn "Could not add the menu shortcut; run 'alice shortcut' later."
fi

say ""
say "${GREEN}${BOLD}ALICE is installed.${RESET}"
case ":$PATH:" in
    *":$BIN:"*) say "Open it from the application menu, or run: ${BOLD}alice app${RESET}" ;;
    *)
        say "Open it from the application menu, or run: ${BOLD}$BIN/alice app${RESET}"
        warn "$BIN is not on your PATH; add it to use plain 'alice':  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
        ;;
esac
if ! command -v google-chrome >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1 &&
    ! command -v chromium-browser >/dev/null 2>&1 && ! command -v microsoft-edge >/dev/null 2>&1; then
    warn "No Chrome/Chromium/Edge found: the app will open in your default browser. For its own window: $(pkg_hint chromium)"
fi
