# reanime installer for Windows:
#   irm https://raw.githubusercontent.com/vashhdev/reanime/main/install.ps1 | iex
# installs uv (which fetches its own Python if you have none), then reanime, then mpv.
# re-run it any time to update.
& {
    $ErrorActionPreference = "Stop"
    $pkg = "reanime-mpv @ https://github.com/vashhdev/reanime/archive/refs/heads/main.zip"

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        if ($LASTEXITCODE) { throw "uv install failed" }
    }
    $env:Path = (Join-Path $env:USERPROFILE ".local\bin") + ";" + $env:Path

    uv tool install --force --reinstall $pkg
    if ($LASTEXITCODE) { throw "reanime install failed" }

    reanime --setup
    Write-Host ""
    Write-Host "done. run: reanime   (open a new terminal first if the command is not found)"
}
