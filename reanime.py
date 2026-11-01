#!/usr/bin/env python3
"""
reanime.py - stream anime from reanime.to in mpv, English dubs included.

Pipeline:
  1. search https://reanime.to/api/v1/search -> anime slug
  2. fetch /watch/<slug>/__data.json -> anilist/mal ids + episode list with sub/dub flags
  3. fetch /api/flix/<anilist id>/<episode> -> flixcloud file id of the sub or dub server
  4. fetch https://flixcloud.cc/d/<id>/__data.json -> download token, wait until
     flixcloud reports the file ready, then hand mpv <base>/download/<fileId>?token=...

The embed player's HLS stream is encrypted end to end, so this goes through
flixcloud's own download route instead: a plain .mkv that mpv streams with
range requests (seeking works, nothing is written to disk). Many files carry
both audio tracks, so --dub / --sub just tell mpv which one to pick.

Usage:
  python reanime.py                                    # INTERACTIVE: search, pick audio + episode
  python reanime.py "one piece"                        # skip the search prompt
  python reanime.py "one piece" 1151 --dub             # play 1151, then a next/replay/prev menu
  python reanime.py "one piece" 1151-1160 --dub        # one mpv playlist of the whole range
  python reanime.py "one piece" 1151- --dub            # 1151 through the newest episode
  python reanime.py "https://reanime.to/watch/one-piece-xamk74?ep=1151&lang=dub"
  python reanime.py one-piece-xamk74 --list            # print every episode then exit
  python reanime.py "one piece" 1151 --dub --print     # print the stream url, don't launch mpv
  python reanime.py --setup                            # find or install mpv, then exit

Players:
  desktop : mpv, installed on first run if missing (winget/scoop/choco, brew/port,
            apt/dnf/pacman/zypper/apk/xbps/nix, flatpak as a last resort)
  android : under Termux, episodes open in the mpv-android app (is.xyz.mpv)

Interactive controls:
  pickers : type to narrow the list, up/down arrows to move, Enter to select, Esc to cancel
  menus   : press the number, or just Enter for the default
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

__version__ = "1.0.0"

SITE = "https://reanime.to"
SEARCH_API = SITE + "/api/v1/search?limit=20&q={}"
WATCH_DATA = SITE + "/watch/{}/__data.json?ep=1"
FLIX_API = SITE + "/api/flix/{}/{}"
FILE_DATA = "https://flixcloud.cc/d/{}/__data.json"
UA = {"User-Agent": "Mozilla/5.0 (reanime.py)"}

# server dataType values per language; the s- variants are the site's alternates
LANGS = {"sub": ("sub", "s-sub"), "dub": ("dub", "s-dub")}
FLAG = {"sub": "subbed", "dub": "dubbed"}
LANG_LABEL = {"dub": "English Dub", "sub": "Japanese audio, English subs"}

URL_RE = re.compile(r"reanime\.to/(?:watch|anime)/([A-Za-z0-9-]+)")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EMBED_RE = re.compile(r"flixcloud\.cc/e/([A-Za-z0-9]+)")

# ------------------------------------------------------------------ interactive
# single-key input: msvcrt on Windows, termios/tty on POSIX.
try:
    import msvcrt                       # Windows
except ImportError:
    msvcrt = None
try:
    import termios, tty, select         # POSIX
except ImportError:
    termios = None

CSI = "\x1b["
HL = CSI + "7m"       # reverse video (highlight)
DIM = CSI + "2m"
RST = CSI + "0m"


def interactive_ok():
    """true if we can drive a raw-key TUI on this terminal."""
    return (bool(msvcrt) or bool(termios)) and sys.stdin.isatty() and sys.stdout.isatty()


def _enable_vt():
    """turn on ANSI escape handling in the Windows console (no-op elsewhere)."""
    if msvcrt:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)


def _getkey():
    """block for one keypress. return ('char', c) or ('key', name)."""
    if msvcrt:
        c = msvcrt.getwch()
        if c in ("\x00", "\xe0"):        # arrow / function-key prefix
            c2 = msvcrt.getwch()
            return "key", {"H": "up", "P": "down", "K": "left", "M": "right"}.get(c2, "")
        if c in ("\r", "\n"):
            return "key", "enter"
        if c == "\x08":
            return "key", "backspace"
        if c == "\x1b":
            return "key", "esc"
        if c == "\x03":
            raise KeyboardInterrupt
        return "char", c
    # POSIX: read one char in cbreak mode (OPOST kept so \n still maps to \r\n)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        c = sys.stdin.read(1)
        if c == "\x1b":                  # ESC alone, or start of arrow sequence
            r, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not r:
                return "key", "esc"
            if sys.stdin.read(1) == "[":
                c3 = sys.stdin.read(1)
                return "key", {"A": "up", "B": "down", "C": "right", "D": "left"}.get(c3, "")
            return "key", "esc"
        if c in ("\r", "\n"):
            return "key", "enter"
        if c in ("\x7f", "\x08"):
            return "key", "backspace"
        if c == "\x03":
            raise KeyboardInterrupt
        return "char", c
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def fuzzy_pick(items, label, header, noun="items", initial=0):
    """
    live-filter picker. type letters to narrow, up/down to move, Enter to select.
    `items` = list, `label(item)` -> str shown & searched. returns item or None.
    prefix matches rank above substring matches. `initial` = starting selection.
    """
    if not interactive_ok():
        return _fallback_pick(items, label, header)
    query, sel, drawn = "", initial, 0
    print(header)                       # printed once; only the lines below are redrawn

    def filtered():
        q = query.lower().replace(" ", "")
        if not q:
            return list(items)
        pre, sub = [], []
        for it in items:
            t = label(it).lower().replace(" ", "")
            if t.startswith(q):
                pre.append(it)
            elif q in t:
                sub.append(it)
        return pre + sub

    while True:
        # the redraw moves the cursor up one line per entry in `out`, so every entry
        # must stay exactly one screen line: clip to the width, and keep the block
        # shorter than the window so it never scrolls
        cols, lines = shutil.get_terminal_size()
        width = max(10, cols - 4)
        max_rows = max(3, min(15, lines - 5))

        view = filtered()
        sel = max(0, min(sel, len(view) - 1)) if view else 0
        # window of rows around selection
        start = max(0, min(sel - max_rows + 1, len(view) - max_rows)) if len(view) > max_rows else 0
        rows = view[start:start + max_rows]

        hint = "type to filter · ↑↓ move · Enter select · Esc cancel"[:cols - 1]
        out = [f"{DIM}{hint}{RST}{CSI}K", f"> {query[-width:]}{CSI}K"]
        for i, it in enumerate(rows):
            real = start + i
            mark = HL + " > " if real == sel else "   "
            out.append(f"{mark}{label(it)[:width]}{RST}{CSI}K")
        if not view:
            out.append(f"{DIM}  (no match){RST}{CSI}K")
        out.append(f"{DIM}  {len(view)}/{len(items)} {noun}{RST}{CSI}K")

        if drawn:
            sys.stdout.write(f"{CSI}{drawn}A")   # move cursor up to redraw
        sys.stdout.write("\r" + "\n".join(out) + f"{CSI}J\n")
        sys.stdout.flush()
        drawn = len(out)

        kind, val = _getkey()
        if kind == "char":
            query += val
            sel = 0
        elif val == "backspace":
            query = query[:-1]
            sel = 0
        elif val == "up":
            sel -= 1
        elif val == "down":
            sel += 1
        elif val == "enter":
            return view[sel] if view else None
        elif val == "esc":
            return None


def menu_pick(options, default_idx, header):
    """numbered menu. press 1-9 to pick, Enter for default. returns index."""
    print(header)
    for i, (lbl, _) in enumerate(options):
        d = f" {DIM}(default){RST}" if i == default_idx else ""
        print(f"  {i + 1}. {lbl}{d}")
    if not interactive_ok():
        raw = input(f"choice [{default_idx + 1}]: ").strip()
        return default_idx if not raw else max(0, min(int(raw) - 1, len(options) - 1))
    sys.stdout.write(f"{DIM}press number, or Enter for default: {RST}")
    sys.stdout.flush()
    while True:
        kind, val = _getkey()
        if kind == "key" and val == "enter":
            idx = default_idx
            break
        if kind == "char" and val.isdigit() and 1 <= int(val) <= len(options):
            idx = int(val) - 1
            break
    print(f"{options[idx][0]}")
    return idx


def _fallback_pick(items, label, header):
    """no-msvcrt fallback: type a substring, plain input()."""
    print(header)
    q = input("filter (substring): ").strip().lower()
    hits = [it for it in items if q in label(it).lower()] or list(items)
    for i, it in enumerate(hits):
        print(f"  {i + 1}. {label(it)}")
    raw = input("number: ").strip()
    return hits[int(raw) - 1] if raw else hits[0]


# ------------------------------------------------------------------ reanime.to

def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def devalue(data):
    """decode a SvelteKit devalue payload: a flat array where every value is an index."""
    memo = {}

    def walk(i):
        if i < 0:                       # -1 undefined, -3 NaN, -4/-5 +-Infinity, -6 -0
            return {-3: float("nan"), -4: float("inf"), -5: float("-inf"), -6: -0.0}.get(i)
        if i in memo:
            return memo[i]
        v = data[i]
        if isinstance(v, list) and v and isinstance(v[0], str):
            out = memo[i] = v[1] if len(v) > 1 else None     # typed value, e.g. ["Date", iso]
        elif isinstance(v, list):
            out = memo[i] = []
            out.extend(walk(x) for x in v)
        elif isinstance(v, dict):
            out = memo[i] = {}
            for k, x in v.items():
                out[k] = walk(x)
        else:
            out = memo[i] = v
        return out

    return walk(0)


def page_data(url, key):
    """fetch a SvelteKit __data.json and return the decoded route node that has `key`."""
    for node in json.loads(fetch(url)).get("nodes") or []:
        if node and node.get("type") == "data":
            obj = devalue(node["data"])
            if isinstance(obj, dict) and obj.get(key):
                return obj
    return None


def title_of(t):
    if isinstance(t, str):
        return t
    t = t or {}
    return t.get("english") or t.get("user_preferred") or t.get("romaji") or t.get("native") or "?"


def titles(t):
    """every title variant (english, romaji, native, ...) as a list."""
    return [t] if isinstance(t, str) else [v for v in (t or {}).values() if isinstance(v, str)]


def epnum(ep):
    n = ep.get("number", ep.get("episode_number", 0))
    return str(int(n)) if float(n).is_integer() else str(n)


def search(query):
    data = json.loads(fetch(SEARCH_API.format(urllib.parse.quote(query))))
    return [r for r in (data.get("results") or data.get("hits") or []) if r.get("anime_id")]


def search_label(r):
    extra = [str(x) for x in (r.get("format"), r.get("season_year")) if x]
    if r.get("dubbed"):
        extra.append(f"dub {r['dubbed']}")
    return title_of(r.get("title")) + (f"  ({', '.join(extra)})" if extra else "")


def load_anime(slug):
    """slug -> (anime, episodes) or None when reanime has no such page."""
    try:
        d = page_data(WATCH_DATA.format(slug), "anime")
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            return None
        raise
    if not d:
        return None
    return d["anime"], d.get("episodes") or []


def parse_url(s):
    """reanime.to watch/anime url -> (slug, ep, lang), anything else -> (None, None, None)."""
    m = URL_RE.search(s)
    if not m:
        return None, None, None
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(s).query)
    lang = (q.get("lang") or [None])[0]
    return m.group(1), (q.get("ep") or [None])[0], lang if lang in LANGS else None


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def find_anime(query, auto):
    """query -> (anime, episodes). `auto` takes the best search hit instead of asking."""
    if query and SLUG_RE.match(query):
        got = load_anime(query)          # a slug like one-piece-xamk74; else fall back to search
        if got:
            return got
    while True:
        if not query:
            query = input("search anime: ").strip()
            if not query:
                sys.exit("cancelled")
        results = search(query)
        if not results:
            if auto or not interactive_ok():
                sys.exit(f"no anime matches '{query}'")
            print(f"no results for '{query}'")
            query = ""
            continue
        if auto or len(results) == 1 or not interactive_ok():
            exact = [r for r in results if norm(query) in {norm(t) for t in titles(r.get("title"))}]
            pick = (exact or results)[0]
            sys.stderr.write(f"using {title_of(pick.get('title'))}  ({pick['anime_id']})\n")
        else:
            pick = fuzzy_pick(results, search_label, "select an anime:", noun="results")
            if pick is None:
                sys.exit("cancelled")
        got = load_anime(pick["anime_id"])
        if not got:
            sys.exit(f"reanime has no watch page for {pick['anime_id']}")
        return got


def servers(anime, ep):
    """mirror the site's flix lookup: anilist id in the path, mal (or tmdb) as query."""
    al, mal, tmdb = anime.get("anilist_id"), anime.get("mal_id"), anime.get("themoviedb_id")
    if not (al or mal or tmdb):
        return []
    q = {}
    if mal:
        q["mal"] = mal
    if not al and tmdb:
        season = (anime.get("external_seasons") or {}).get("tmdb")
        q.update(tmdb=tmdb, season=1 if season is None else season)
    url = FLIX_API.format(al or 0, epnum(ep)) + ("?" + urllib.parse.urlencode(q) if q else "")
    data = json.loads(fetch(url))
    return (data.get("servers") or []) if data.get("success") else []


def file_id(srvs, lang):
    """flixcloud id for `lang`, HD-2 first like the site. HD-1/HD-2 usually share one file."""
    order = {"HD-2": 0, "HD-1": 1}
    for s in sorted(srvs, key=lambda s: order.get(s.get("serverName"), 9)):
        m = EMBED_RE.search(s.get("dataLink") or "")
        if m and s.get("dataType") in LANGS[lang]:
            return m.group(1)
    return None


def wait_ready(url, verbose):
    """follow flixcloud's prepare-progress event stream until the file is ready.

    cached files report ready at once; others are queued/built first, which the
    site warns can take a while for big files.
    """
    shown = False
    req = urllib.request.Request(url, headers={**UA, "Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:])
                except ValueError:
                    continue
                status = ev.get("status")
                if status == "ready":
                    return
                if status == "failed":
                    raise RuntimeError(ev.get("error") or "flixcloud could not prepare the file")
                if verbose:
                    shown = True
                    sys.stderr.write(f"\r  flixcloud is preparing the file: {status} "
                                     f"{ev.get('percent') or 0:.0f}%{CSI}K")
                    sys.stderr.flush()
    finally:
        if shown:
            sys.stderr.write("\n")
    raise RuntimeError("prepare stream ended before the file was ready")


def resolve(anime, ep, lang, verbose=True):
    """episode -> (stream url, file info). raises LookupError/RuntimeError/OSError."""
    fid = file_id(servers(anime, ep), lang)
    if not fid:
        raise LookupError(f"no {lang} source for episode {epnum(ep)}")
    d = page_data(FILE_DATA.format(fid), "download")
    if not d:
        raise LookupError(f"flixcloud has no download for {fid}")
    video, dl = d["video"], d["download"]
    base = f"{dl['base']}/download/{video['fileId']}"
    q = "token=" + urllib.parse.quote(dl["token"], safe="")
    wait_ready(f"{base}/progress?{q}", verbose)
    return f"{base}?{q}", video


def resolve_many(anime, eps, lang, quiet=False):
    """resolve a range in parallel, keeping order. failed episodes are reported and skipped."""
    if not quiet:
        sys.stderr.write(f"resolving {len(eps)} episode{'s' if len(eps) != 1 else ''}...\n")
    with ThreadPoolExecutor(4) as ex:
        futs = [ex.submit(resolve, anime, ep, lang, len(eps) == 1 and not quiet) for ep in eps]
        out = []
        for ep, fut in zip(eps, futs):
            try:
                out.append((ep, *fut.result()))
            except (LookupError, RuntimeError, OSError, ValueError, KeyError) as e:
                sys.stderr.write(f"  skipping episode {epnum(ep)}: {e}\n")
    return out


# ------------------------------------------------------------------ playback

# some releases flag the episode title card or a signs-only track as the default
# English subtitles, so --slang alone shows next to nothing. this mpv script picks
# the English track by its title instead, before mpv selects tracks. if it can't
# load (no Lua, unreadable file), mpv simply picks the way it always did.
SUB_PICKER = """\
-- written by reanime (sub mode): pick full English subtitles over partial tracks
local partial = {"episode name", "episode title", "sign", "song", "forced", "karaoke", "dubtitle"}

local function is_partial(t)
    if t.forced then return true end
    local title = (t.title or ""):lower()
    for _, word in ipairs(partial) do
        if title:find(word, 1, true) then return true end
    end
    return false
end

local function is_english(t)
    local lang = (t.lang or ""):lower()
    return lang == "eng" or lang == "en" or lang:sub(1, 3) == "en-"
end

-- runs after a file opens and before mpv selects its tracks. file-local, so each
-- episode in a playlist gets its own pick
mp.add_hook("on_preloaded", 50, function()
    if mp.get_property("options/sid") ~= "auto" then return end
    local best
    for _, t in ipairs(mp.get_property_native("track-list") or {}) do
        if t.type == "sub" and is_english(t) and not is_partial(t)
                and (not best or (t.default and not best.default)) then
            best = t
        end
    end
    if best then mp.set_property("file-local-options/sid", tostring(best.id)) end
end)
"""


def cache_dir():
    """per-user cache folder. not the temp folder: other users can write there, and a
    flatpak mpv only sees its own private /tmp."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "reanime")


def sub_picker():
    """write SUB_PICKER where mpv can load it. its path, or None if it can't be written."""
    path = os.path.join(cache_dir(), "pick-subs.lua")
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == SUB_PICKER:
                return path
    except (OSError, ValueError):
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(SUB_PICKER)
    except OSError:
        return None
    return path


def mpv_args(lang):
    if lang == "dub":
        # dual-audio files flag the full English subs as default; keep them off over a dub
        return ["--alang=eng,en", "--sid=no"]
    picker = sub_picker()
    return ["--alang=jpn,ja", "--slang=eng,en"] + ([f"--script={picker}"] if picker else [])


def ep_title(name, ep):
    t = ep.get("title") or ""
    return f"{name} - {epnum(ep)}" + (f" - {t}" if t else "")


def ep_label(ep):
    tag = "  [filler]" if ep.get("filler") or ep.get("is_filler") else ""
    return f"{epnum(ep):>5}  {ep.get('title') or ''}{tag}".rstrip()


def write_m3u(path, entries, title):
    """write a standard EXTM3U playlist. entries = [(name, url)]."""
    lines = ["#EXTM3U", f"#PLAYLIST:{title}"]
    for name, url in entries:
        lines.append(f"#EXTINF:-1,{name}")
        lines.append(url)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def launch(cmd, url, title, lang):
    """play one episode. cmd = mpv argv prefix, or None on Android (mpv-android app)."""
    if cmd is None:
        android_open(url)
    else:
        subprocess.run([*cmd, f"--force-media-title={title}", *mpv_args(lang), url])


def play_range(cmd, anime, eps, lang):
    """play eps back to back as mpv playlists of CHUNK episodes. links expire after
    six hours, so each chunk is resolved while the one before it plays."""
    name = title_of(anime.get("title"))
    chunks = [eps[i:i + CHUNK] for i in range(0, len(eps), CHUNK)]
    with ThreadPoolExecutor(1) as ahead:
        nxt = ahead.submit(resolve_many, anime, chunks[0], lang)
        for i in range(len(chunks)):
            resolved = nxt.result()
            if i + 1 < len(chunks):
                nxt = ahead.submit(resolve_many, anime, chunks[i + 1], lang, True)
            if not resolved:
                continue
            # --{ ... --} scopes an option to the files inside it: one title per episode,
            # and no temp playlist file (a flatpak mpv can't see the host's /tmp)
            files = []
            for ep, url, _ in resolved:
                files += ["--{", f"--force-media-title={ep_title(name, ep)}", url, "--}"]
            first, last = epnum(resolved[0][0]), epnum(resolved[-1][0])
            sys.stderr.write(f"{name} {first}-{last}  ({lang}, {len(resolved)} episodes)\n")
            subprocess.run([*cmd, *mpv_args(lang), *files])
            if i + 1 < len(chunks):
                more = f"{epnum(chunks[i + 1][0])}-{epnum(chunks[i + 1][-1])}"
                try:
                    if menu_pick([(f"continue with {more}", True), ("quit", False)], 0,
                                 f"\n{name} {first}-{last} done:") == 1:
                        nxt.cancel()
                        return
                except EOFError:
                    return


def play_loop(cmd, anime, eps, idx, lang):
    """play eps[idx], then offer next/replay/previous. the next episode is resolved
    in the background while the current one plays, so Enter starts it right away."""
    name = title_of(anime.get("title"))
    cache, pre = {}, None

    def prefetch(i):
        try:
            cache[i] = resolve(anime, eps[i], lang, verbose=False)
        except Exception:
            pass                        # resolved again, with errors shown, when it is needed

    while True:
        ep = eps[idx]
        if pre:
            pre.join()
        got = cache.pop(idx, None)
        cache.clear()                   # links expire; never hold one past the next episode
        if not got:
            try:
                got = resolve(anime, ep, lang)
            except (LookupError, RuntimeError, OSError, ValueError, KeyError) as e:
                sys.stderr.write(f"episode {epnum(ep)}: {e}\n")
        pre = None
        if got:
            if idx + 1 < len(eps):
                pre = threading.Thread(target=prefetch, args=(idx + 1,), daemon=True)
                pre.start()
            url, video = got
            title = ep_title(name, ep)
            sys.stderr.write(f"{title}  ({lang}, {video.get('sizeHuman', '?')})\n")
            launch(cmd, url, title, lang)

        opts = []
        if idx + 1 < len(eps):
            opts.append((f"next      {ep_label(eps[idx + 1]).strip()}", "next"))
        if got:
            opts.append(("replay", "replay"))
        if idx > 0:
            opts.append((f"previous  {epnum(eps[idx - 1])}", "prev"))
        opts += [("pick episode", "pick"), ("quit", "quit")]
        default = 0 if opts[0][1] == "next" else len(opts) - 1
        try:
            state = "done" if cmd else "opened in mpv-android"
            choice = opts[menu_pick(opts, default, f"\n{name} {epnum(ep)} {state}:")][1]
        except EOFError:
            return
        if choice == "quit":
            return
        if choice == "next":
            idx += 1
        elif choice == "prev":
            idx -= 1
        elif choice == "pick":
            ep = fuzzy_pick(eps, ep_label, f"{name} ({lang}):", noun="episodes", initial=idx)
            if ep is None:
                return
            idx = eps.index(ep)


CHUNK = 8      # episodes per mpv run in range mode, about 3h of video

# Termux on Android has no desktop for mpv to open a window on, so episodes go to
# the mpv-android app through an intent instead (the same route ani-cli takes).
ANDROID = bool(os.environ.get("TERMUX_VERSION")) or "com.termux" in os.environ.get("PREFIX", "")
MPV_ANDROID = "is.xyz.mpv/.MPVActivity"
MPV_ANDROID_HELP = ("install the mpv-android app (is.xyz.mpv) from Google Play or F-Droid: "
                    "https://github.com/mpv-android/mpv-android")

# os key -> [(package manager, [commands])]. every manager on PATH is offered in
# order; if one fails the next is tried. ids are checked against each repo:
# winget's "mpv.net" is a different player (mpvnet.exe), shinchiro.mpv is real mpv.
MPV_INSTALL = {
    "nt": [("winget", [["winget", "install", "-e", "--id", "shinchiro.mpv",
                        "--accept-source-agreements", "--accept-package-agreements"]]),
           ("scoop", [["scoop", "bucket", "add", "extras"], ["scoop", "install", "extras/mpv"]]),
           ("choco", [["choco", "install", "mpv", "-y"]])],
    "darwin": [("brew", [["brew", "install", "mpv"]]),
               ("port", [["sudo", "port", "install", "mpv"]])],
    "linux": [("apt", [["sudo", "apt-get", "update"], ["sudo", "apt-get", "install", "-y", "mpv"]]),
              ("dnf", [["sudo", "dnf", "install", "-y", "mpv"]]),
              ("pacman", [["sudo", "pacman", "-S", "--needed", "--noconfirm", "mpv"]]),
              ("zypper", [["sudo", "zypper", "install", "-y", "mpv"]]),
              ("apk", [["sudo", "apk", "add", "mpv"]]),
              ("xbps-install", [["sudo", "xbps-install", "-y", "mpv"]]),
              ("pkg", [["sudo", "pkg", "install", "-y", "mpv"]]),           # FreeBSD
              ("nix", [["nix", "profile", "install", "nixpkgs#mpv"]]),
              # immutable distros (Steam Deck, Silverblue): no root needed
              ("flatpak", [["flatpak", "remote-add", "--user", "--if-not-exists", "flathub",
                            "https://dl.flathub.org/repo/flathub.flatpakrepo"],
                           ["flatpak", "install", "--user", "-y", "flathub", "io.mpv.Mpv"]])],
}
# where those installs leave mpv. a fresh install is usually not on this process's
# PATH yet (and winget's shinchiro.mpv never adds itself), so look here directly.
MPV_DIRS = {
    "nt": [r"%ProgramFiles%\MPV Player", r"%ProgramFiles%\mpv", r"%LOCALAPPDATA%\Programs\mpv",
           r"%LOCALAPPDATA%\Microsoft\WinGet\Links", r"%USERPROFILE%\scoop\shims",
           r"%ProgramData%\chocolatey\bin"],
    "darwin": ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
               "/Applications/mpv.app/Contents/MacOS"],
    "linux": ["/usr/bin", "/usr/local/bin", "~/.nix-profile/bin", "/run/current-system/sw/bin",
              "/snap/bin"],
}


def _os_key():
    return "nt" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")


def find_mpv(mpv):
    """argv prefix that runs mpv, or None. checks PATH, the usual install dirs, then flatpak."""
    hit = shutil.which(mpv) or (mpv if os.path.isfile(mpv) else None)
    if hit:
        return [hit]
    if mpv != "mpv":
        return None                     # an explicit --mpv path that isn't there
    exe = "mpv.exe" if os.name == "nt" else "mpv"
    for d in MPV_DIRS[_os_key()]:
        path = os.path.join(os.path.expanduser(os.path.expandvars(d)), exe)
        if os.path.isfile(path):
            return [path]
    if os.name == "nt":
        pkgs = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
        for path in glob.glob(os.path.join(pkgs, "*mpv*", "**", "mpv.exe"), recursive=True):
            return [path]
    if shutil.which("flatpak") and subprocess.run(
            ["flatpak", "info", "io.mpv.Mpv"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return ["flatpak", "run", "io.mpv.Mpv"]
    return None


def _sudo(cmd):
    """drop sudo when already root or when there is none (containers, minimal installs)."""
    root = hasattr(os, "geteuid") and os.geteuid() == 0
    return cmd[1:] if cmd[0] == "sudo" and (root or not shutil.which("sudo")) else cmd


def ensure_player(mpv, assume_yes=False):
    """mpv argv prefix, installing mpv on first run when it is missing (without asking
    when `assume_yes`). returns None on Android, where the mpv-android app plays
    instead. exits if there is no player."""
    if ANDROID:
        return None
    cmd = find_mpv(mpv)
    if cmd:
        return cmd
    if mpv != "mpv":
        sys.exit(f"mpv not found at {mpv}")

    sys.stderr.write("mpv (the video player reanime streams into) is not installed.\n")
    candidates = [(mgr, [_sudo(c) for c in steps])
                  for mgr, steps in MPV_INSTALL[_os_key()] if shutil.which(mgr)]
    if not candidates:
        sys.exit("no supported package manager found. install mpv from "
                 "https://mpv.io/installation/ and retry.")
    if not (assume_yes or interactive_ok()):
        sys.exit("install it with: " + " && ".join(" ".join(c) for c in candidates[0][1])
                 + "\nor let reanime do it: reanime --setup --yes")

    for mgr, steps in candidates:
        shown = " && ".join(" ".join(c) for c in steps)
        if assume_yes:
            sys.stderr.write(f"installing mpv via {mgr}: {shown}\n")
        elif input(f"install mpv now via {mgr}?  ({shown}) [Y/n] ").strip().lower() in ("n", "no"):
            sys.exit("mpv required. aborting.")
        for c in steps:
            subprocess.run(c)           # judged by the result below, not exit codes:
        cmd = find_mpv(mpv)             # "bucket already added" exits non-zero, harmlessly
        if cmd:
            sys.stderr.write(f"mpv ready: {' '.join(cmd)}\n")
            return cmd
        sys.stderr.write(f"{mgr} did not leave a runnable mpv.\n")
    sys.exit("mpv still not available. install it from https://mpv.io/installation/, "
             "open a new terminal, and retry.")


def android_open(url):
    """hand a stream to the mpv-android app. returns at once; the app plays on its own."""
    try:
        r = subprocess.run(["am", "start", "--user", "0", "-a", "android.intent.action.VIEW",
                            "-d", url, "-n", MPV_ANDROID], capture_output=True, text=True)
    except OSError:
        sys.exit("Termux's `am` command is missing: pkg install termux-am")
    if r.returncode or "Error" in r.stdout + r.stderr:
        sys.exit(f"could not open mpv-android. {MPV_ANDROID_HELP}")


def select_eps(eps, spec, lang):
    """'12' / '12-24' / '12-' -> (indices into eps, is_range)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:(-)\s*(\d+(?:\.\d+)?)?)?\s*", spec)
    if not m:
        sys.exit(f"bad episode '{spec}'. use 12, 12-24, or 12- (through the newest)")
    lo = float(m.group(1))
    hi = float(m.group(3)) if m.group(3) else (float("inf") if m.group(2) else lo)
    picked = [i for i, ep in enumerate(eps) if lo <= float(epnum(ep)) <= hi]
    if not picked:
        span = f"{epnum(eps[0])}-{epnum(eps[-1])}" if eps else "none"
        sys.exit(f"episode {spec} has no {lang} release. {lang} episodes available: {span}")
    return picked, bool(m.group(2))


def main():
    ap = argparse.ArgumentParser(description="stream anime from reanime.to in mpv")
    ap.add_argument("anime", nargs="?",
                    help="search text, a reanime slug (one-piece-xamk74), or a reanime.to url")
    ap.add_argument("episode", nargs="?",
                    help="episode or range: 1151, 1151-1160, 1151- (through the newest)")
    lang_flag = ap.add_mutually_exclusive_group()
    lang_flag.add_argument("--dub", action="store_true", help="English dub")
    lang_flag.add_argument("--sub", action="store_true",
                           help="Japanese audio with English subs (the default when an "
                                "episode is given without --dub)")
    ap.add_argument("--list", action="store_true", help="list every episode and exit")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print stream urls instead of launching mpv")
    ap.add_argument("--m3u", nargs="?", const="", metavar="PATH",
                    help="write an .m3u playlist instead of launching mpv. links expire after "
                         "a few hours and only work from your current network")
    ap.add_argument("--mpv", default="mpv", help="path to mpv binary")
    ap.add_argument("--setup", action="store_true",
                    help="find or install mpv and exit (the installers run this)")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="install mpv without asking if it is missing (scripts, CI)")
    ap.add_argument("--version", action="version", version=f"reanime {__version__}")
    args = ap.parse_args()

    _enable_vt()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # titles in scripts a codepage can't show
        except AttributeError:
            pass

    if args.setup:
        cmd = ensure_player(args.mpv, args.yes)
        print(MPV_ANDROID_HELP if cmd is None else f"ready: {' '.join(cmd)}")
        return
    # check the player before any searching, so a first run sets everything up front
    playing = not (args.list or args.print_only or args.m3u is not None)
    cmd = ensure_player(args.mpv, args.yes) if playing else None

    slug, url_ep, url_lang = parse_url(args.anime or "")
    spec = args.episode or url_ep
    got = load_anime(slug) if slug else None
    if slug and not got:
        sys.exit(f"reanime has no watch page for {slug}")
    anime, eps = got or find_anime(args.anime, auto=bool(spec))
    name = title_of(anime.get("title"))

    if args.list:
        for ep in eps:
            have = ("S" if ep.get("subbed") else "-") + ("D" if ep.get("dubbed") else "-")
            print(f"{have}  {ep_label(ep)}")
        return

    lang = "dub" if args.dub else "sub" if args.sub else url_lang
    if not lang:
        counts = {lg: sum(1 for ep in eps if ep.get(FLAG[lg])) for lg in ("dub", "sub")}
        opts = [(f"{LANG_LABEL[lg]}  ({counts[lg]} episodes)", lg)
                for lg in ("dub", "sub") if counts[lg]]
        if not opts:
            sys.exit(f"{name} has no playable episodes yet")
        if spec or len(opts) == 1 or not interactive_ok():
            lang = "sub" if counts["sub"] else "dub"
        else:
            print(f"\n{name}")
            lang = opts[menu_pick(opts, 0, "\naudio:")][1]

    avail = [ep for ep in eps if ep.get(FLAG[lang]) and ep.get("playable", True)]
    if not avail:
        sys.exit(f"{name} has no {lang} episodes")
    if spec:
        picked, is_range = select_eps(avail, spec, lang)
    else:
        ep = fuzzy_pick(avail, ep_label, f"\n{name} ({lang}):", noun="episodes",
                        initial=len(avail) - 1)
        if ep is None:
            sys.exit("cancelled")
        picked, is_range = [avail.index(ep)], False

    if args.print_only or args.m3u is not None:
        resolved = resolve_many(anime, [avail[i] for i in picked], lang)
        if not resolved:
            sys.exit("nothing to play")
        entries = [(ep_title(name, ep), url) for ep, url, _ in resolved]
        if args.print_only:
            print("\n".join(url for _, url in entries))
            return
        first, last = epnum(resolved[0][0]), epnum(resolved[-1][0])
        span = first if first == last else f"{first}-{last}"
        path = args.m3u or f"{anime.get('anime_id', 'reanime')}-{span}-{lang}.m3u"
        write_m3u(path, entries, f"{name} {span} ({lang})")
        print(os.path.abspath(path))
        return

    if is_range and cmd is not None:    # mpv-android takes one url at a time: loop there
        play_range(cmd, anime, [avail[i] for i in picked], lang)
    else:
        play_loop(cmd, anime, avail, picked[0], lang)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\ncancelled")
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f"network error: {e}")
    except ValueError as e:
        sys.exit(f"unexpected response from reanime/flixcloud (site changed or "
                 f"Cloudflare check?): {e}")
