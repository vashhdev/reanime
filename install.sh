#!/bin/sh
# reanime installer for macOS, Linux and Android (Termux):
#   curl -fsSL https://raw.githubusercontent.com/vashhdev/reanime/main/install.sh | sh
# installs uv (which fetches its own Python if you have none), then reanime, then mpv.
# re-run it any time to update.
#
# env overrides (used by CI):
#   REANIME_PKG=<path, url or requirement>   install this instead of the latest main
#   REANIME_YES=1                            install mpv without asking
set -e

PKG="${REANIME_PKG:-reanime-mpv @ https://github.com/vashhdev/reanime/archive/refs/heads/main.zip}"

termux=""
if [ -n "${TERMUX_VERSION:-}" ]; then termux=1; fi
case "${PREFIX:-}" in *com.termux*) termux=1 ;; esac

if [ -n "$termux" ]; then
    # uv has no Android build; Termux's own python + pip do the job
    pkg install -y python
    pip install --upgrade --force-reinstall "$PKG"
else
    if ! command -v uv >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi
    export PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"
    uv tool install --force --reinstall "$PKG"
fi

if [ -n "$REANIME_YES" ]; then
    reanime --setup --yes
elif ( : </dev/tty ) 2>/dev/null; then
    # piped from curl, this script is stdin; point the mpv install prompt at the terminal
    reanime --setup </dev/tty
else
    reanime --setup
fi

echo
echo "done. run: reanime   (open a new terminal first if the command is not found)"
