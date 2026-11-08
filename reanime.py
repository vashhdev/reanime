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
import codecs
import glob
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

__version__ = "1.2.1"

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
    # POSIX: read one key in cbreak mode (OPOST kept so \n still maps to \r\n)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd, termios.TCSANOW)  # not the default TCSAFLUSH: keep keys typed ahead
        b = _byte(fd)
        if b == 0x1b:                    # ESC alone, or the start of an arrow key
            b = _byte(fd, 0.05)
            if b not in (0x5b, 0x4f):    # not "[" or "O": ESC was pressed alone
                if b is not None:
                    _held.insert(0, b)
                return "key", "esc"
            while True:                  # parameters ("1;5" for Ctrl), then a final byte
                b = _byte(fd, 0.05)
                if b is None or 0x40 <= b <= 0x7e:
                    break
            return "key", {0x41: "up", 0x42: "down", 0x43: "right", 0x44: "left"}.get(b, "")
        if b in (0x0d, 0x0a):
            return "key", "enter"
        if b in (0x7f, 0x08):
            return "key", "backspace"
        if b == 0x03:
            raise KeyboardInterrupt
        decode = codecs.getincrementaldecoder(getattr(sys.stdin, "encoding", None) or "utf-8")("replace")
        c = decode.decode(bytes([b]))
        while not c:                     # the rest of a multi-byte character
            b = _byte(fd, 0.05)
            c = decode.decode(b"" if b is None else bytes([b]), final=b is None)
        return "char", c
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


_held = bytearray()                     # bytes read from the terminal, not handled yet


def _byte(fd, wait=None):
    """
    next byte typed, or None if nothing arrives within `wait` seconds. reads the
    file descriptor itself: sys.stdin's buffer would swallow the rest of an arrow
    key's escape sequence, and select() would then report ESC pressed alone.
    """
    if not _held:
        if wait is not None and not select.select([fd], [], [], wait)[0]:
            return None
        _held.extend(os.read(fd, 64))
        if not _held:
            raise EOFError
    return _held.pop(0)


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
    anime = dict(d["anime"], _slug=slug)        # watch history is keyed by the slug
    return anime, d.get("episodes") or []


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

# many files mark the opening and the ending as chapters, under all sorts of names.
# this mpv script jumps past the opening, and past the ending and preview: to an extra
# after the ending if there is one, else to the next episode. files without chapters
# just play through. Tab skips by hand.
CHAPTER_SKIPPER = """\
-- written by reanime: skip openings, and endings and previews (to an extra after the
-- ending if there is one, else to the next episode).
-- Tab skips by hand: to the next chapter, or 85s (an opening) in files without them.
-- titles seen on reanime's files: "Opening Song", "Intro", "To Be Continued & Ending
-- Song", "Credits", "On The Next episode". whole words only, so "Introduction" stays.
-- "Intro" is an opening only in files that name no chapter "Opening" or "OP": some
-- (Jujutsu Kaisen) call the story before the opening "Intro". openings are matched
-- before endings, so "Opening Credits" is an opening
local OPENING = {"opening", "op%d*"}
local INTRO = {"intro"}
local ENDING = {"ending", "credits", "outro", "ed%d*", "preview", "next episode", "next time"}
local KEY, BY_HAND, NEAR = "TAB", 85, 150

local function matches(title, words)
    title = (title or ""):lower()
    for _, w in ipairs(words) do
        if title:find("%f[%w]" .. w .. "%f[%W]") then return true end
    end
    return false
end

local kinds, kinds_of = {}, nil     -- chapter number -> "opening" / "ending", for a file

local function classify(path)
    if kinds_of == path then return kinds end
    local list, named = mp.get_property_native("chapter-list") or {}, false
    kinds, kinds_of = {}, path
    for i, c in ipairs(list) do
        if matches(c.title, OPENING) then
            kinds[i - 1], named = "opening", true
        elseif matches(c.title, ENDING) then
            kinds[i - 1] = "ending"
        end
    end
    for i, c in ipairs(list) do
        if not named and not kinds[i - 1] and matches(c.title, INTRO) then
            kinds[i - 1] = "opening"
        end
    end
    return kinds
end

local at, opened        -- the chapter playing now, and the file it started in
local waiting = false   -- reached an ending before the next episode was queued
local seeking, settled = false, 0

local function next_episode()
    local pos = mp.get_property_number("playlist-pos", 0)
    waiting = pos + 1 >= mp.get_property_number("playlist-count", 0)
    if not waiting then
        mp.msg.info("ending: next episode")
        mp.osd_message("skipped the ending  (< to go back)", 3)
        mp.command("playlist-next")
    end
end

-- past chapter ch to the next one that isn't an opening, ending or preview (after an
-- ending that keeps extras like Jujutsu Kaisen's "Juju Stroll"), else to the next
-- episode once only endings and previews are left
local function skip_from(path, ch, hint)
    local list, kinds = mp.get_property_native("chapter-list") or {}, classify(path)
    local skipped = list[ch + 1] and list[ch + 1].title or "to the next chapter"
    for j = ch + 1, #list - 1 do
        if not kinds[j] then
            mp.osd_message("skipped " .. skipped .. hint, 3)
            return mp.commandv("seek", list[j + 1].time, "absolute+exact")
        end
    end
    for j = math.max(ch, 0), #list - 1 do
        if kinds[j] == "ending" then return next_episode() end
    end
end

local function check()
    local path, ch = mp.get_property("path"), mp.get_property_number("chapter")
    local key = path and ch and (path .. "#" .. ch)
    if key == at then return end
    at, waiting = key, false
    if not key then return end
    local list = mp.get_property_native("chapter-list") or {}
    local c, what, first = list[ch + 1], classify(path)[ch], opened ~= path
    opened = path
    if not (c and what) then return end
    -- skip a chapter playback runs into, or the one a file opens in (a resume). never
    -- one the viewer seeks or jumps to (PgDn back to an opening plays it), but rewind
    -- to before it and it is skipped again
    local pos = mp.get_property_number("time-pos")
    local by_seek = seeking or mp.get_time() - settled < 0.5
    if not first and (by_seek or (pos and pos > c.time + 2)) then return end
    mp.msg.info("skipped " .. c.title)
    skip_from(path, ch, what == "opening" and "  (PgDn to watch it)" or "")
end

-- by hand: past the chapter playing (and any opening, ending or preview right after
-- it), unless the next chapter is far off or there are none: then an opening's worth
mp.add_key_binding(KEY, "skip", function()
    local path, ch = mp.get_property("path"), mp.get_property_number("chapter", -1)
    local pos = mp.get_property_number("time-pos")
    if not (path and pos) then return end
    local list = mp.get_property_native("chapter-list") or {}
    local c, after = list[ch + 1], list[ch + 2]
    if classify(path)[ch] or (after and after.time - pos <= NEAR) then
        mp.msg.info("skip key: " .. (c and c.title or "start"))
        skip_from(path, ch, "")
    else
        mp.msg.info("skip key: " .. BY_HAND .. "s")
        mp.osd_message("skipped " .. BY_HAND .. "s", 2)
        mp.commandv("seek", BY_HAND, "relative+exact")
    end
end)

mp.register_event("seek", function() seeking = true end)
mp.register_event("playback-restart", function() seeking, settled = false, mp.get_time() end)
-- where the ending starts, for reanime to count quitting during it as watched
mp.register_event("file-loaded", function()
    local path, start = mp.get_property("path"), 0
    for i, c in ipairs(mp.get_property_native("chapter-list") or {}) do
        if classify(path)[i - 1] == "ending" then start = c.time break end
    end
    mp.set_property_native("user-data/reanime/ending", start)
    check()
end)
mp.observe_property("chapter", "number", check)
mp.observe_property("playlist-count", "number", function()
    if waiting then next_episode() end
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


def mpv_script(name, source):
    """write an mpv script into the cache folder (only when it changed).
    its path, or None if it can't be written."""
    path = os.path.join(cache_dir(), name)
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == source:
                return path
    except (OSError, ValueError):
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(source)
    except OSError:
        return None
    return path


def sub_picker():
    return mpv_script("pick-subs.lua", SUB_PICKER)


def skip_args():
    """mpv options that load CHAPTER_SKIPPER, or none if it can't be written."""
    path = mpv_script("skip-chapters.lua", CHAPTER_SKIPPER)
    return [f"--script={path}"] if path else []


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


# ------------------------------------------------------------------ history
# where each show was left off, and the last volume, so plain `reanime` can offer
# "continue ONE PIECE 1155 at 12:34" and mpv reopens as loud as it was.

def state_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "reanime")


def load_history():
    try:
        with open(os.path.join(state_dir(), "history.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def show_tweaks(history, anime, lang):
    """the TWEAKS saved for anime in lang: {mpv property: value}."""
    saved = (history.get("tweaks") or {}).get(f"{anime.get('_slug')}/{lang}")
    if not isinstance(saved, dict):
        return {}
    return {k: v for k, v in saved.items()
            if k in TWEAKS and isinstance(v, (int, float)) and not isinstance(v, bool)}


def remember(anime, ep, lang, pos=None, volume=None, tweaks=None):
    """note where anime was left off: pos seconds into ep, or None once ep was watched,
    and the TWEAKS changed for it in lang. history is a convenience, so a failed write
    never stops playback."""
    data = load_history()
    slug = anime.get("_slug")
    if slug:
        shows = data.get("shows") if isinstance(data.get("shows"), dict) else {}
        shows[slug] = {"title": title_of(anime.get("title")), "lang": lang,
                       "episode": str(epnum(ep)),
                       "position": None if pos is None else round(pos, 1),
                       "time": int(time.time())}
        data["shows"], data["last"] = shows, slug
    if slug and tweaks:
        everything = data.get("tweaks") if isinstance(data.get("tweaks"), dict) else {}
        everything[f"{slug}/{lang}"] = {**show_tweaks(data, anime, lang), **tweaks}
        data["tweaks"] = everything
    if isinstance(volume, (int, float)):
        data["volume"] = round(volume)
    path = os.path.join(state_dir(), "history.json")
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def resume_point(anime, eps, lang, history):
    """(index into eps, start seconds) to carry on with anime in lang, or None.
    start is None when the saved episode was the newest and already watched."""
    h = (history.get("shows") or {}).get(anime.get("_slug"))
    if not isinstance(h, dict) or h.get("lang") != lang:
        return None
    for i, ep in enumerate(eps):
        if str(epnum(ep)) == h.get("episode"):
            pos = h.get("position")
            if isinstance(pos, (int, float)):
                return i, float(pos)
            return (i + 1, 0.0) if i + 1 < len(eps) else (i, None)
    return None


def clock(sec):
    sec = int(sec or 0)
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


# ------------------------------------------------------------------ AniList
# opt-in progress tracking. `reanime --anilist` logs in once; from then on every
# episode you finish moves your AniList progress up to it. reanime.to already names
# each show's AniList entry, so there is no title matching. progress only goes up: an
# episode at or below it, or anything you completed (a rewatch), is left alone.
ANILIST_API = "https://graphql.anilist.co"
# reanime's AniList API client (anilist.co/settings/developer, redirect url
# https://anilist.co/api/v2/oauth/pin so AniList shows the token to paste back). a
# fork can use its own with REANIME_ANILIST_CLIENT
ANILIST_CLIENT = os.environ.get("REANIME_ANILIST_CLIENT", "56373")
ANILIST_LOGIN = "https://anilist.co/api/v2/oauth/authorize?client_id={}&response_type=token"
ANILIST_MEDIA = """query ($id: Int) {
  Media(id: $id) { episodes mediaListEntry { status progress } } }"""
ANILIST_SAVE = """mutation ($id: Int, $progress: Int, $status: MediaListStatus) {
  SaveMediaListEntry(mediaId: $id, progress: $progress, status: $status) { progress } }"""
_anilist_lock = threading.Lock()


def anilist_file():
    return os.path.join(state_dir(), "anilist.json")


def anilist_token():
    """the saved AniList access token, or None when not logged in."""
    try:
        with open(anilist_file(), encoding="utf-8") as fh:
            token = json.load(fh).get("token")
    except (OSError, ValueError, AttributeError):
        return None
    return token if isinstance(token, str) and token else None


def anilist(token, query, **variables):
    """one GraphQL request to AniList as the logged-in viewer -> its data."""
    req = urllib.request.Request(
        ANILIST_API, json.dumps({"query": query, "variables": variables}).encode(),
        {**UA, "Content-Type": "application/json", "Accept": "application/json",
         "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            reply = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            reply = json.loads(e.read().decode("utf-8"))
        except ValueError:
            reply = {}
        errors = reply.get("errors") if isinstance(reply, dict) else None
        msg = (errors[0].get("message") if errors and isinstance(errors[0], dict) else "") or ""
        if e.code == 401 or "token" in msg.lower():
            raise RuntimeError("your login expired or was revoked, "
                               "run reanime --anilist again") from None
        raise RuntimeError(msg or f"HTTP {e.code}") from None
    errors = reply.get("errors")
    if errors:
        raise RuntimeError(errors[0].get("message") or "request failed")
    return reply.get("data") or {}


def anilist_progress(token, anime, ep):
    """move AniList progress for anime up to ep -> what changed, or None if nothing did."""
    n = epnum(ep)
    if not n.isdigit() or int(n) < 1:
        return None                     # recaps and specials numbered 12.5 aren't progress
    n = int(n)
    with _anilist_lock:                 # one read-then-save at a time: progress never drops
        media = anilist(token, ANILIST_MEDIA, id=anime["anilist_id"]).get("Media") or {}
        entry = media.get("mediaListEntry") or {}
        if entry.get("status") == "COMPLETED" or (entry.get("progress") or 0) >= n:
            return None
        total = media.get("episodes")
        status = ("COMPLETED" if total and n >= total
                  else "REPEATING" if entry.get("status") == "REPEATING" else "CURRENT")
        anilist(token, ANILIST_SAVE, id=anime["anilist_id"], progress=n, status=status)
    done = "  (completed)" if status == "COMPLETED" else ""
    return f"{title_of(anime.get('title'))} {n}{done}"


def track(anime, ep):
    """when logged in, move AniList progress up to ep in the background. -> a job for
    tracked(), or None."""
    token = anilist_token()
    if not token or not anime.get("anilist_id"):
        return None
    return in_background(anilist_progress, token, anime, ep)


def tracked(jobs, say, wait=0.0):
    """report each job that finished (waiting up to `wait` seconds) -> the ones still running."""
    left = []
    for th, box in jobs:
        th.join(wait)
        if th.is_alive():
            left.append((th, box))
        elif "err" in box:
            say(f"AniList: {box['err']}")
        elif box.get("ok"):
            say(f"AniList: progress {box['ok']}")
    return left


def anilist_login():
    """log in to AniList: approve reanime in the browser, paste the token AniList shows."""
    import getpass
    import webbrowser
    if not ANILIST_CLIENT:
        sys.exit("AniList login isn't set up in this copy of reanime")
    url = ANILIST_LOGIN.format(ANILIST_CLIENT)
    print(f"1. open this link and approve reanime (it may have opened already):\n   {url}")
    print("2. copy the token AniList shows, and paste it here (it won't show as you paste)")
    try:
        webbrowser.open(url)
    except Exception:
        pass                            # no browser here: the link above is enough
    try:
        token = getpass.getpass("token: ").strip()
    except (EOFError, KeyboardInterrupt):
        token = ""
    if not token:
        sys.exit("no token, not logged in")
    try:
        name = (anilist(token, "query { Viewer { name } }").get("Viewer") or {}).get("name")
    except (RuntimeError, OSError, ValueError) as e:
        sys.exit(f"AniList didn't take that token: {e}")
    os.makedirs(state_dir(), exist_ok=True)
    fd = os.open(anilist_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"token": token, "user": name}, fh)
    print(f"logged in to AniList as {name}. episodes you finish now update your progress")


def anilist_logout():
    try:
        os.remove(anilist_file())
    except FileNotFoundError:
        print("not logged in to AniList")
        return
    print("logged out of AniList. to also revoke reanime's access: "
          "https://anilist.co/settings/apps")


# ------------------------------------------------------------------ binge sessions
# on desktop one mpv window plays a whole binge. reanime talks to it over mpv's JSON
# IPC and appends the next episode while the current one plays, so episodes advance
# on their own, Enter or > (mpv's playlist-next) skips ahead, < goes back, and volume
# and fullscreen carry over. links are fetched one episode ahead, so none is older
# than about one episode when mpv opens it (they expire after six hours).

# what you change in mpv for a show sticks to it, for dub and sub separately: audio
# delay (Ctrl + and Ctrl -), subtitle size (F, G) and position (r, R). some releases
# need their dub shifted, or have huge subtitles
TWEAKS = ("audio-delay", "sub-scale", "sub-pos")
POLL = 1.0       # seconds between looks at mpv's playlist position
DONE_AT = 120    # quitting this close to the end, or once the ending starts (when the
                 # file marks it as a chapter), counts as watched


def finished(pos, dur, ending):
    """watched: within DONE_AT of the end, or into the ending the chapter skipper found."""
    return pos is not None and bool((dur and dur - pos < DONE_AT) or (ending and pos >= ending))


class MpvError(Exception):
    pass


class MpvIpc:
    """mpv's JSON IPC, one request at a time. nothing reads in the background: a
    Windows pipe handle serializes reads and writes, so a pending read would block
    every command."""

    def __init__(self, conn):
        self.conn, self.buf, self.rid = conn, b"", 0
        if isinstance(conn, socket.socket):
            self.send, self.recv = conn.sendall, lambda: conn.recv(65536)
        else:
            self.send, self.recv = conn.write, lambda: conn.read(65536)

    def command(self, *args, **named):
        """run an mpv command, positional (["seek", 10]) or named (name=..., url=...)."""
        self.rid += 1
        cmd = dict(name=args[0], **named) if named else list(args)
        self.send((json.dumps({"command": cmd, "request_id": self.rid}) + "\n").encode())
        while True:
            while b"\n" not in self.buf:
                chunk = self.recv()
                if not chunk:
                    raise EOFError("mpv closed the connection")
                self.buf += chunk
            line, self.buf = self.buf.split(b"\n", 1)
            msg = json.loads(line)
            if msg.get("request_id") == self.rid:
                if msg.get("error") != "success":
                    raise MpvError(msg.get("error"))
                return msg.get("data")

    def get(self, prop):
        try:
            return self.command("get_property", prop)
        except MpvError:                # "property unavailable" while a file loads
            return None

    def close(self):
        try:
            self.conn.close()
        except OSError:
            pass


def ipc_path():
    tag = f"reanime-{os.getpid()}-{secrets.token_hex(4)}"
    if os.name == "nt":
        return "\\\\.\\pipe\\" + tag
    # a private folder: whoever can open the socket can make mpv run commands
    os.makedirs(cache_dir(), mode=0o700, exist_ok=True)
    return os.path.join(cache_dir(), tag + ".sock")


def mpv_connect(path, proc, timeout=15):
    """connect to the ipc server of the mpv that proc runs, or None if it never opens
    one (an mpv without ipc, or a flatpak sandbox that can't reach the path)."""
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        conn = None
        try:
            if os.name == "nt":
                conn = open(path, "r+b", buffering=0)
            else:
                conn = socket.socket(socket.AF_UNIX)
                conn.settimeout(10)
                conn.connect(path)
            ipc = MpvIpc(conn)
            ipc.command("disable_event", "all")     # replies only, no event chatter
            return ipc
        except (OSError, EOFError, MpvError, ValueError):
            if conn is not None:
                conn.close()
            time.sleep(0.1)
    return None


def in_background(fn, *args):
    """run fn on a daemon thread (never holds up exit). -> (thread, box with ok/err)."""
    box = {}

    def run():
        try:
            box["ok"] = fn(*args)
        except Exception as e:
            box["err"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, box


def title_option(title):
    # mpv's %bytes% quoting keeps commas and quotes in the title intact
    return f"force-media-title=%{len(title.encode())}%{title}"


def play_session(cmd, anime, eps, idx, lang, start=0.0, stop=None):
    """play eps[idx] from start seconds in one mpv window, appending every following
    episode (through eps[stop], else the newest) while the one before it plays.

    returns (index, seconds, status) for where the viewer left off. status is "done"
    (watched to the end, or mpv couldn't be followed), "stopped" (quit mid-episode at
    seconds), "failed" (eps[idx] couldn't be resolved) or "quit" (Ctrl+C)."""
    name = title_of(anime.get("title"))
    last = len(eps) - 1 if stop is None else stop
    try:
        url, video = resolve(anime, eps[idx], lang)
    except (LookupError, RuntimeError, OSError, ValueError, KeyError) as e:
        sys.stderr.write(f"episode {epnum(eps[idx])}: {e}\n")
        return idx, None, "failed"
    title = ep_title(name, eps[idx])
    sys.stderr.write(f"{title}  ({lang}, {video.get('sizeHuman', '?')})\n")

    path = ipc_path()
    # --idle=no: mpv 0.42's mpv.exe started without a console (pseudo-gui) idles after
    # the last episode instead of closing, and the session only ends when mpv does
    argv = [*cmd, *mpv_args(lang), "--idle=no", f"--input-ipc-server={path}"]
    history = load_history()
    volume = history.get("volume")
    if isinstance(volume, (int, float)):
        argv.append(f"--volume={volume:g}")
    tweaks = show_tweaks(history, anime, lang)
    if tweaks:
        argv += [f"--{k}={v:g}" for k, v in tweaks.items()]
        sys.stderr.write(f"your settings for {name} ({lang}): "
                         + "  ".join(f"{k} {v:g}" for k, v in tweaks.items()) + "\n")
    first = [f"--force-media-title={title}"] + ([f"--start={start:.1f}"] if start else [])
    proc = subprocess.Popen([*argv, "--{", *first, url, "--}"])   # per-file options

    queue = [idx]                     # mpv playlist entry -> index into eps
    cur, pos, dur, vol, status = idx, (start or None), None, None, "done"
    ending = None                     # where the ending starts, from CHAPTER_SKIPPER
    began, now = {}, {}               # TWEAKS as mpv opened, and as they are
    nxt, pending, misses = idx + 1, None, 0
    jobs = []                         # AniList updates on their way
    ipc = mpv_connect(path, proc)

    def say(msg):                     # mpv prints it to the terminal around its status line
        try:
            ipc.command("print-text", f"reanime: {msg}")
        except (EOFError, OSError, ValueError, MpvError):
            pass
    try:
        while ipc and proc.poll() is None:
            try:
                entry = ipc.get("playlist-pos")
                t, d, v = ipc.get("time-pos"), ipc.get("duration"), ipc.get("volume")
                e = ipc.get("user-data/reanime/ending")
                for k in TWEAKS:
                    x = ipc.get(k)
                    if isinstance(x, (int, float)):
                        began.setdefault(k, x)
                        now[k] = x
            except (EOFError, OSError, ValueError):
                break                           # mpv is shutting down
            if isinstance(entry, int) and 0 <= entry < len(queue):
                if queue[entry] != cur:
                    job = finished(pos, dur, ending) and track(anime, eps[cur])
                    if job:                     # it played to its end, not skipped with >
                        jobs.append(job)
                    cur, pos, dur, ending = queue[entry], None, None, None
                if t is not None:
                    pos, dur = t, d
                    ending = e if isinstance(e, (int, float)) and e > 0 else None
            if v is not None:
                vol = v
            # keep one episode queued past the one playing
            if pending is None and entry == len(queue) - 1 and nxt <= last and misses < 3:
                pending = in_background(resolve, anime, eps[nxt], lang, False)
            if pending is not None and not pending[0].is_alive():
                nxt_title = ep_title(name, eps[nxt])
                try:
                    if "err" in pending[1]:
                        raise pending[1]["err"]
                    ipc.command("loadfile", url=pending[1]["ok"][0], flags="append",
                                options=title_option(nxt_title))
                    ipc.command("print-text", f"reanime: up next  {nxt_title}")
                    queue.append(nxt)
                    misses = 0
                except (LookupError, RuntimeError, OSError, ValueError, KeyError, MpvError) as e:
                    misses += 1
                    say(f"skipping episode {epnum(eps[nxt])}: {e}")
                nxt, pending = nxt + 1, None
            jobs = tracked(jobs, say)
            time.sleep(POLL)
    except KeyboardInterrupt:
        status = "quit"
    finally:
        if ipc:
            ipc.close()
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.kill()
            status = "quit"
        if os.name != "nt" and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    watched = finished(pos, dur, ending)
    if ipc and status == "done" and pos is not None and not watched:
        status = "stopped"
    seconds = pos if status in ("stopped", "quit") else None
    changed = {k: round(x, 3) for k, x in now.items() if x != began[k]}
    remember(anime, eps[cur], lang, seconds, vol, changed)
    job = watched and track(anime, eps[cur])
    tracked(jobs + [job] if job else jobs, lambda m: sys.stderr.write(m + "\n"), wait=10)
    return cur, seconds, status


def binge(cmd, anime, eps, idx, lang, start=0.0, stop=None):
    """desktop playback: one mpv window per session, and a menu once it closes, with
    resume as the default after quitting mid-episode and next after finishing one."""
    name = title_of(anime.get("title"))
    while True:
        idx, pos, status = play_session(cmd, anime, eps, idx, lang, start, stop)
        if status == "quit":
            return
        ep, start = eps[idx], 0.0
        last = len(eps) - 1 if stop is None else stop
        if status == "done" and idx >= last and stop is not None:
            sys.stderr.write(f"\n{name} {epnum(eps[stop])}: end of the range\n")
            return
        opts = []
        if status == "stopped":
            opts.append((f"resume    {epnum(ep)} at {clock(pos)}", "resume"))
        if idx + 1 < len(eps):
            opts.append((f"next      {ep_label(eps[idx + 1]).strip()}", "next"))
        if status != "failed":
            opts.append(("replay" if status == "done" else f"restart   {epnum(ep)}", "replay"))
        opts += [("pick episode", "pick"), ("quit", "quit")]
        default = 0 if opts[0][1] in ("resume", "next") else len(opts) - 1
        head = {"stopped": f"stopped at {clock(pos)}", "failed": "unavailable"}.get(status, "done")
        try:
            choice = opts[menu_pick(opts, default, f"\n{name} {epnum(ep)} {head}:")][1]
        except EOFError:
            return
        if choice == "quit":
            return
        if choice == "resume":
            start = pos
        elif choice == "next":
            idx += 1
        elif choice == "pick":
            ep = fuzzy_pick(eps, ep_label, f"{name} ({lang}):", noun="episodes", initial=idx)
            if ep is None:
                return
            idx, stop = eps.index(ep), None


def play_loop(cmd, anime, eps, idx, lang):
    """Android (mpv-android takes one url at a time and can't be followed): open
    eps[idx], then offer next/replay/previous. the next episode is resolved in the
    background while the current one plays, so Enter starts it right away."""
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
            remember(anime, ep, lang)       # the app reports nothing back: count it watched

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
            job = got and track(anime, ep)      # moving on: count this one watched
            if job:
                tracked([job], lambda m: sys.stderr.write(m + "\n"), wait=10)
            idx += 1
        elif choice == "prev":
            idx -= 1
        elif choice == "pick":
            ep = fuzzy_pick(eps, ep_label, f"{name} ({lang}):", noun="episodes", initial=idx)
            if ep is None:
                return
            idx = eps.index(ep)


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


def available(eps, lang):
    return [ep for ep in eps if ep.get(FLAG[lang]) and ep.get("playable", True)]


def continue_prompt():
    """offer to carry on with the show watched last.
    -> (anime, episodes in lang, lang, index, start seconds), or None to search."""
    history = load_history()
    slug = history.get("last")
    h = (history.get("shows") or {}).get(slug) if isinstance(slug, str) else None
    if not isinstance(h, dict) or h.get("lang") not in LANGS or not interactive_ok():
        return None
    got = load_anime(slug)
    if not got:
        return None
    anime, lang = got[0], h["lang"]
    eps = available(got[1], lang)
    point = resume_point(anime, eps, lang, history)
    if not point:
        return None
    i, start = point
    name = title_of(anime.get("title"))
    if start is None:
        label = f"{name} ({lang}), caught up at {epnum(eps[i])}: pick an episode"
    elif start:
        label = f"{name} {epnum(eps[i])} ({lang}) at {clock(start)}"
    else:
        label = f"{name} {ep_label(eps[i]).strip()} ({lang})"
    if menu_pick([(label, 0), ("search for something else", 1)], 0, "\ncontinue watching?"):
        return None
    if start is None:
        ep = fuzzy_pick(eps, ep_label, f"\n{name} ({lang}):", noun="episodes", initial=i)
        if ep is None:
            sys.exit("cancelled")
        i, start = eps.index(ep), 0.0
    return anime, eps, lang, i, start


def watch(cmd, anime, eps, idx, lang, start=0.0, stop=None):
    if cmd is None:
        play_loop(cmd, anime, eps, idx, lang)       # mpv-android
    else:
        binge(cmd, anime, eps, idx, lang, start, stop)


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
    ap.add_argument("--no-skip", action="store_true",
                    help="don't skip openings and endings (skipped when the file marks "
                         "them as chapters)")
    ap.add_argument("--anilist", nargs="?", const="login", choices=("login", "logout"),
                    help="log in to AniList, so episodes you finish update your progress "
                         "there (--anilist logout to stop)")
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

    if args.anilist:
        (anilist_login if args.anilist == "login" else anilist_logout)()
        return
    if args.setup:
        cmd = ensure_player(args.mpv, args.yes)
        print(MPV_ANDROID_HELP if cmd is None else f"ready: {' '.join(cmd)}")
        return
    # check the player before any searching, so a first run sets everything up front
    playing = not (args.list or args.print_only or args.m3u is not None)
    cmd = ensure_player(args.mpv, args.yes) if playing else None
    if cmd and not args.no_skip:
        cmd = [*cmd, *skip_args()]

    if playing and not (args.anime or args.episode or args.dub or args.sub):
        cont = continue_prompt()
        if cont:
            anime, eps, lang, idx, start = cont
            watch(cmd, anime, eps, idx, lang, start)
            return

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

    history = load_history() if playing else {}
    seen = (history.get("shows") or {}).get(anime.get("_slug")) or {}
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
            last_lang = [i for i, (_, lg) in enumerate(opts) if lg == seen.get("lang")]
            lang = opts[menu_pick(opts, (last_lang or [0])[0], "\naudio:")][1]

    avail = available(eps, lang)
    if not avail:
        sys.exit(f"{name} has no {lang} episodes")
    start = 0.0
    if spec:
        picked, is_range = select_eps(avail, spec, lang)
    else:
        # open the picker on where this show was left off, else on the newest episode
        point = resume_point(anime, avail, lang, history)
        ep = fuzzy_pick(avail, ep_label, f"\n{name} ({lang}):", noun="episodes",
                        initial=point[0] if point else len(avail) - 1)
        if ep is None:
            sys.exit("cancelled")
        picked, is_range = [avail.index(ep)], False
        if point and point[0] == picked[0] and point[1]:
            start = point[1]
            sys.stderr.write(f"resuming at {clock(start)}\n")

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

    watch(cmd, anime, avail, picked[0], lang, start, picked[-1] if is_range else None)


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
