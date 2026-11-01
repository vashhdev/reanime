# reanime

[![CI](https://github.com/vashhdev/reanime/actions/workflows/ci.yml/badge.svg)](https://github.com/vashhdev/reanime/actions/workflows/ci.yml)

Stream anime from [reanime.to](https://reanime.to) straight into [mpv](https://mpv.io), **English dubs included**. Search for a show, pick dub or sub, pick an episode with a fuzzy arrow-key picker, and mpv plays it. When it ends, press Enter for the next one, which is already lined up.

Built for the gap [ani-cli](https://github.com/pystardust/ani-cli) leaves: current One Piece dub episodes (Egghead onward) that [One Pace](https://github.com/vashhdev/onepace) hasn't covered yet. Works for any show reanime has.

- **One-line install** - sets up Python (if needed), reanime, and mpv for you.
- **Runs everywhere** - Windows, macOS, Linux, Steam Deck, and Android (Termux + mpv-android).
- **Zero dependencies** - pure Python standard library.
- **Interactive picker** - type to filter, `↑`/`↓` to move, `Enter` to select.
- **Binge mode** - the next episode resolves in the background while the current one plays.
- **Ranges** - `1151-1160` plays straight through as mpv playlists.

## Install

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/vashhdev/reanime/main/install.ps1 | iex
```

**macOS / Linux / Steam Deck / Android (Termux)**:

```bash
curl -fsSL https://raw.githubusercontent.com/vashhdev/reanime/main/install.sh | sh
```

The installer:

1. installs [uv](https://docs.astral.sh/uv/), which downloads its own Python if you don't have one (Termux uses its own `python` package instead),
2. installs reanime as a global `reanime` command,
3. runs `reanime --setup`, which finds mpv or offers to install it with your system's package manager.

Run the same command again any time to update. Then open a new terminal and run `reanime`.

### Manual install

If you already have Python 3.8+:

```bash
uv tool install git+https://github.com/vashhdev/reanime     # or: pipx install git+https://github.com/vashhdev/reanime
reanime --setup                                              # find or install mpv
```

Or just grab the single file and run `python reanime.py`. It works without installing, and offers to install mpv the first time you play something.

## Platforms

| Platform | Player | How mpv gets installed |
| --- | --- | --- |
| Windows 10/11 | mpv | `winget` (falls back to `scoop`, then `choco`) |
| macOS | mpv | `brew` (or MacPorts) |
| Linux | mpv | `apt`, `dnf`, `pacman`, `zypper`, `apk`, `xbps`, or `nix` |
| Steam Deck, Fedora Silverblue, other immutable distros | mpv (flatpak) | `flatpak install --user io.mpv.Mpv`, no root needed |
| Android | [mpv-android](https://github.com/mpv-android/mpv-android) app | install it yourself from Google Play or F-Droid; reanime runs in [Termux](https://termux.dev) and opens each episode in the app |
| iOS | none | `--print` the link and open it in VLC or Outplayer |

## Usage

```bash
reanime                                   # search, pick audio + episode
reanime "one piece"                       # skip the search prompt
reanime "one piece" 1151 --dub            # play 1151, then next / replay / previous
reanime "one piece" 1151-1160 --dub       # play the range straight through
reanime "one piece" 1151- --dub           # 1151 through the newest dub
reanime "https://reanime.to/watch/one-piece-xamk74?ep=1151&lang=dub"   # paste a url from the site
reanime one-piece-xamk74 --list           # every episode: S/D availability, filler tags
reanime "one piece" 1151 --dub --print    # print the stream url instead of playing
```

### Interactive controls

| Where                   | Keys                                                                   |
| ----------------------- | ---------------------------------------------------------------------- |
| Anime / episode picker  | type to narrow · `↑`/`↓` move · `Enter` select · `Esc` cancel          |
| Audio, after-episode menu | press the number · `Enter` for the default (dub, next episode)       |

### Flags

| Flag            | Description                                                       |
| --------------- | ----------------------------------------------------------------- |
| `--dub`         | English dub                                                       |
| `--sub`         | Japanese audio, English subs (default when you pass an episode without `--dub`) |
| `--list`        | list every episode and exit                                       |
| `--print`       | print stream urls, don't launch a player                          |
| `--m3u [PATH]`  | write an `.m3u` playlist instead. Links expire after 6 hours and only work from your current network |
| `--mpv PATH`    | path to the mpv binary (default: found automatically)             |
| `--setup`       | find or install mpv, then exit                                    |
| `-y`, `--yes`   | install mpv without asking if it's missing (scripts, CI)          |

### mpv tips

- `#` cycles audio tracks (dub / Japanese on dual-audio files), `j` cycles subtitles. Dub mode starts with subtitles off. Many files have an English `Forced` track that only translates on-screen signs, which is handy with the dub.
- In a range, `<` / `>` go to the previous / next episode. Ranges play in batches of 8 episodes, and reanime asks before starting the next batch.
- **mpv-android:** the app picks the audio track itself. For dub-by-default, add `alang=eng` under Settings > Advanced > Edit mpv.conf.

## How it works

1. Searches reanime (`/api/v1/search`) and reads the show's episode list, with sub/dub flags, from its watch page data.
2. Asks reanime's server list (`/api/flix/<anilist id>/<episode>`) which [flixcloud](https://flixcloud.cc) file serves that episode in your language.
3. Gets a direct `.mkv` link from flixcloud's download page, waiting until flixcloud marks the file ready (usually instant).
4. mpv streams that link with range requests, so seeking works and nothing is saved to disk. Many files are dual audio, so `--dub`/`--sub` just tell mpv which audio track to use.

Links are tied to your IP address and expire after 6 hours, so a range is fetched in batches, each while the previous one plays.

## Development

The tests run offline against fixtures shaped like the real site responses, so they need no network and no mpv:

```bash
python -m unittest discover -s tests -v
uvx ruff check .
```

Every push and pull request runs the [CI pipeline](.github/workflows/ci.yml) in stages. Each stage only runs if the one before it passed:

| Stage | What it checks |
| --- | --- |
| 1 · lint | ruff on the Python, shellcheck on `install.sh`, `install.ps1` parses |
| 2 · test | the offline suite on Windows, macOS and Linux, with Python 3.8 and 3.13 |
| 3 · build | the package builds and its metadata is valid |
| 4 · install | the real one-line installers on fresh Windows, macOS and Linux machines: uv, reanime, mpv through the system package manager, then mpv plays a test clip. Termux runs in Docker as an experimental job |
| 5 · live | reanime.to and flixcloud still work: search, episode list, and mpv opens real episodes, picking the English track on a dub |
| 6 · release | on a `v*` tag: checks the tag matches the version, then publishes a GitHub release with the changelog notes |

The pipeline also runs daily, so if reanime changes something, the **live** stage fails.

Merging into `main` needs the **all checks passed** job, which is green only when stages 1 to 4 are. The live and Termux checks are advisory, so a site outage doesn't block merges.

To release: bump `__version__` in `reanime.py` and `version` in `pyproject.toml`, add a CHANGELOG section, then `git tag v1.1.0 && git push --tags`.

## Disclaimer

reanime.to is an unofficial streaming site, and this tool only automates what its own pages and flixcloud's download page already do in a browser. It hosts nothing. If you can, support the official releases (One Piece streams on Crunchyroll and Netflix). You are responsible for how you use this software and for complying with the terms of the services it talks to.

## License

[MIT](LICENSE)
