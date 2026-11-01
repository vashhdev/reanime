#!/bin/sh
# reanime installer for macOS, Linux and Android (Termux):
#   curl -fsSL https://raw.githubusercontent.com/vashhdev/reanime/main/install.sh | sh
# installs uv (which fetches its own Python if you have none), then reanime, then mpv.
# re-run it any time to update.
set -e

PKG="reanime-mpv @ https://github.com/vashhdev/reanime/archive/refs/heads/main.zip"

if [ -n "$TERMUX_VERSION" ]; then
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

# piped from curl, this script is stdin; point the mpv install prompt at the terminal
if ( : </dev/tty ) 2>/dev/null; then
    reanime --setup </dev/tty
else
    reanime --setup
fi

echo
echo "done. run: reanime   (open a new terminal first if the command is not found)"
