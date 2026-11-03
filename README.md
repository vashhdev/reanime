# reanime

[![CI](https://github.com/vashhdev/reanime/actions/workflows/ci.yml/badge.svg)](https://github.com/vashhdev/reanime/actions/workflows/ci.yml)

Stream anime from [reanime.to](https://reanime.to) straight into [mpv](https://mpv.io), **English dubs included**. Search for a show, pick dub or sub, pick an episode with a fuzzy arrow-key picker, and mpv plays it, then the next one, and the next, in the same window, hands-free.

Built for the gap [ani-cli](https://github.com/pystardust/ani-cli) leaves: current One Piece dub episodes (Egghead onward) that [One Pace](https://github.com/vashhdev/onepace) hasn't covered yet. Works for any show reanime has.

- **One-line install** - sets up Python (if needed), reanime, and mpv for you.
- **Runs everywhere** - Windows, macOS, Linux, Steam Deck, and Android (Termux + mpv-android).
- **Zero dependencies** - pure Python standard library.
- **Interactive picker** - type to filter, `↑`/`↓` to move, `Enter` to select.
- **Binge mode** - one mpv window keeps going: the next episode is added to the playlist while the current one plays, so it starts on its own (or press `Enter` in mpv to skip ahead). Volume and fullscreen stay put.
- **Skips openings and endings** - when the file marks them as chapters, mpv jumps past the opening, and from the ending straight into the next episode.
- **Picks up where you left off** - quit mid-episode and `Enter` resumes it. Plain `reanime` offers "continue ONE PIECE 1155 at 12:34", and mpv reopens at your last volume.
- **Ranges** - `1151-1160` plays exactly those, back to back.

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
reanime                                   # continue where you left off, or search
reanime "one piece"                       # skip the search prompt
reanime "one piece" 1151 --dub            # play 1151, 1152, 1153... until you quit
reanime "one piece" 1151-1160 --dub       # play just this range
reanime "one piece" 1151- --dub           # 1151 through the newest dub
reanime "https://reanime.to/watch/one-piece-xamk74?ep=1151&lang=dub"   # paste a url from the site
reanime one-piece-xamk74 --list           # every episode: S/D availability, filler tags
reanime "one piece" 1151 --dub --print    # print the stream url instead of playing
```

### Interactive controls

| Where                   | Keys                                                                   |
| ----------------------- | ---------------------------------------------------------------------- |
| Anime / episode picker  | type to narrow · `↑`/`↓` move · `Enter` select · `Esc` cancel          |
| Audio, after-mpv menu   | press the number · `Enter` for the default (your last audio, resume or next episode) |
| In mpv                  | `Enter` or `>` next episode · `<` previous · `PgDn` watch a skipped opening · `q` stop (reanime then offers to resume) |

### Flags

| Flag            | Description                                                       |
| --------------- | ----------------------------------------------------------------- |
| `--dub`         | English dub                                                       |
| `--sub`         | Japanese audio, English subs (default when you pass an episode without `--dub`) |
| `--list`        | list every episode and exit                                       |
| `--print`       | print stream urls, don't launch a player                          |
| `--m3u [PATH]`  | write an `.m3u` playlist instead. Links expire after 6 hours and only work from your current network |
| `--mpv PATH`    | path to the mpv binary (default: found automatically)             |
| `--no-skip`     | play openings and endings instead of skipping them                |
| `--setup`       | find or install mpv, then exit                                    |
| `-y`, `--yes`   | install mpv without asking if it's missing (scripts, CI)          |

### mpv tips

- `#` cycles audio tracks (dub / Japanese on dual-audio files), `j` cycles subtitles. Dub mode starts with subtitles off. Many files have an English `Forced` track that only translates on-screen signs, which is handy with the dub.
- Sub mode picks the full English subtitles by track name. Some files mark the episode-title card or a signs-only track as the default English subtitles, which mpv alone would show. reanime handles this with a small mpv script it keeps in your cache folder, next to the one that skips openings and endings (`%LOCALAPPDATA%\reanime` on Windows, `~/Library/Caches/reanime` on macOS, `~/.cache/reanime` on Linux).
- Episodes queue up one at a time, so `Enter` right after an episode starts may need a second press once the next one is in (a couple of seconds).
- The opening and ending skip goes by chapter names ("Opening Song", "Intro", "Ending Song", "Credits", "Preview" and the like), so it only works on files that have chapters. Of the One Piece episodes checked, 1 and 1151 have them and 500 doesn't, so it just plays through. `PgDn` right after a skip goes back to the opening, and it won't be skipped again. The ending skip also skips anything after the credits, such as the next-episode preview; `<` goes back to the previous episode, which then plays in full. Quitting during an ending counts as watched.
- Where you left off is kept in `%APPDATA%\reanime\history.json` on Windows (`~/Library/Application Support/reanime` on macOS, `~/.local/state/reanime` on Linux). Delete it to forget everything.
- **mpv-android:** the app picks the audio and subtitle tracks itself. For dub-by-default, add `alang=eng` under Settings > Advanced > Edit mpv.conf. If a sub episode only shows the episode title, switch to the "Full Subtitles" track from the subtitle button.

## How it works

1. Searches reanime (`/api/v1/search`) and reads the show's episode list, with sub/dub flags, from its watch page data.
2. Asks reanime's server list (`/api/flix/<anilist id>/<episode>`) which [flixcloud](https://flixcloud.cc) file serves that episode in your language.
3. Gets a direct `.mkv` link from flixcloud's download page, waiting until flixcloud marks the file ready (usually instant).
4. mpv streams that link with range requests, so seeking works and nothing is saved to disk. Many files are dual audio, so `--dub`/`--sub` just tell mpv which audio track to use, and in sub mode which English subtitle track is the full one.

Links are tied to your IP address and expire after 6 hours, so reanime never fetches far ahead: it keeps mpv open with its JSON IPC (a named pipe on Windows, a socket elsewhere) and adds each next episode to the playlist while the one before it plays. mpv-android can't be driven that way, so on Android reanime opens one episode at a time and asks what's next.

## Development

The tests run offline against fixtures shaped like the real site responses, so they need no network. Three of them also drive a real mpv (the subtitle picker, a whole binge session, and the opening and ending skip) when one is installed, and are skipped otherwise:

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
