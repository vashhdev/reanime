# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org).

## [Unreleased]

### Added

- **`Tab` skips by hand** in mpv, one key with no `Fn`: past the chapter that's
  playing (from the ending, to the next episode), or 85 seconds when the file has no
  chapters.

### Fixed

- The opening skip no longer skips the story before the opening in files that call
  it "Intro" (Jujutsu Kaisen): "Intro" counts as the opening only when no chapter
  is named like one.
- Rewinding to before a skipped opening skips it again. Jumping to an opening or
  ending yourself (`PgDn`, a seek) still plays it.

## [1.1.1] - 2026-11-03

### Fixed

- Plain `reanime` crashed on "continue watching?" (`TypeError: list indices must be
  integers`): the episode and the language reached the player swapped.

## [1.1.0] - 2026-11-02

### Added

- **One mpv window for the whole binge.** reanime drives mpv over its JSON IPC and
  adds the next episode to the playlist while the current one plays: episodes
  advance on their own, `Enter` / `>` in mpv skips ahead, `<` goes back, and volume
  and fullscreen stay put. Ranges work the same way instead of batches of 8.
- **Resume.** Quitting mid-episode makes "resume at 12:34" the menu's default. Plain
  `reanime` offers to continue the last show where you stopped, `reanime "one piece"`
  opens the picker on it with your last audio choice, and mpv reopens at the volume
  you last used.
- **Opening and ending skip.** When a file marks them as chapters (One Piece 1 and
  1151 do, 500 doesn't), mpv jumps past the opening, and from the ending or the preview
  straight to the next episode. `PgDn` brings a skipped opening back, quitting
  during the ending counts as watched, and `--no-skip` turns it off.
- **CI pipeline** (GitHub Actions): lint, offline tests on Windows/macOS/Linux with
  Python 3.8 and 3.13, package build, the real installers on fresh machines (plus
  Termux in Docker), a daily live check against reanime.to, and tag-driven releases.
- **Offline test suite** (`tests/`, stdlib `unittest`) covering every pipeline step
  against fixtures, `main()` end to end, the picker redraw, playback, mpv setup and
  Android.
- **`-y` / `--yes`** installs mpv without asking. The installers accept
  `REANIME_YES=1` and `REANIME_PKG=<path or url>` (used by CI).

### Changed

- apt installs refresh the package lists first (`apt-get update`), so mpv installs
  on fresh machines and containers.
- `install.ps1` judges each step by its exit code, so uv's progress output can't
  abort it under Windows PowerShell 5.1.
- `install.sh` also recognises Termux by its `$PREFIX`.

### Fixed

- **Sub mode shows the full English subtitles.** Some files (One Piece episode 1, for
  one) mark the episode-title card as the default English track, so mpv showed only
  the title. A small mpv script now picks the English track by name, skipping
  episode-name, signs, songs, forced and AI "dubtitle" tracks. It runs in desktop
  mpv; mpv-android still picks on its own.
- Episodes without a title no longer leave trailing spaces in the picker and `--list`.

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
