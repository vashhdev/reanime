# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org).

## [1.0.0] - 2026-11-01

### Added

- **Stream any anime on reanime.to into mpv**, English dubs included. Search, pick
  dub or sub, pick an episode, and play. After each episode, a menu offers next /
  replay / previous / pick, and the next episode resolves in the background while
  the current one plays.
- **Ranges.** `1151-1160` and `1151-` (through the newest) play straight through as
  mpv playlists, in batches of 8 so the links (6-hour lifetime) are always fresh.
- **Paste a reanime.to url** to jump straight to that episode and language.
- **`--list`**, **`--print`**, **`--m3u [PATH]`** and **`--mpv PATH`**, like onepace.
- **One-line installers** (`install.ps1`, `install.sh`) that set up uv (and with it
  Python), reanime, and mpv in one go.
- **`--setup`** finds or installs mpv: winget/scoop/choco on Windows, brew/MacPorts
  on macOS, apt/dnf/pacman/zypper/apk/xbps/nix on Linux, and flatpak (no root) for
  Steam Deck and immutable distros. A fresh install is found even before it is on
  `PATH`.
- **Android support.** Under Termux, episodes open in the mpv-android app.
