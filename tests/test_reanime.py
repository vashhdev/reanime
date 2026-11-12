"""
offline tests for reanime.py: no network, stdlib only.

    python -m unittest discover -s tests -v

the site is replaced by fixtures shaped like the real responses (SvelteKit
__data.json in devalue encoding, the flix server list, flixcloud's prepare-progress
event stream), so every step of the pipeline runs without touching reanime.cz.
three tests drive a real mpv when one is installed (the sub picker, a whole binge
session, the chapter skipper), and are skipped otherwise.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import reanime  # noqa: E402

# every test writes reanime's cache (the sub-mode mpv script, ipc sockets) and its
# watch history here, never into the real folders
CACHE = None


def setUpModule():
    global CACHE
    CACHE = tempfile.TemporaryDirectory()
    for name, sub in (("cache_dir", "reanime"), ("state_dir", "state")):
        patcher = mock.patch.object(reanime, name, lambda sub=sub: os.path.join(CACHE.name, sub))
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(CACHE.cleanup)


# ------------------------------------------------------------------ fixtures

def flatten(value):
    """encode like SvelteKit's devalue: a flat array where every value is an index."""
    out = []

    def put(v):
        i = len(out)
        out.append(None)
        if isinstance(v, dict):
            out[i] = {k: put(x) for k, x in v.items()}
        elif isinstance(v, list):
            out[i] = [put(x) for x in v]
        else:
            out[i] = v
        return i

    put(value)
    return out


def svelte(*nodes):
    """a SvelteKit __data.json body; None entries are skipped nodes."""
    return json.dumps({"type": "data", "nodes": [
        None if n is None else {"type": "data", "data": flatten(n)} for n in nodes]})


ANIME = {
    "anime_id": "one-piece-xamk74", "anilist_id": 21, "mal_id": 21, "themoviedb_id": 37854,
    "external_seasons": {"tmdb": 0, "tvdb": 0},
    "title": {"english": "ONE PIECE", "romaji": "ONE PIECE", "native": "ONE PIECE"},
}
EPISODES = [
    {"number": 1150, "title": "Egghead A", "subbed": True, "dubbed": True, "playable": True},
    {"number": 1151, "title": "Her and Her Father's Dream!", "subbed": True, "dubbed": True,
     "playable": True},
    {"number": 1152, "title": "Egghead C", "subbed": True, "dubbed": True, "playable": True,
     "filler": True},
    {"number": 1180, "title": "", "subbed": True, "dubbed": False, "playable": True},
]
SERVERS = json.dumps({"success": True, "servers": [
    {"serverName": "HD-1", "dataLink": "https://flixcloud.cc/e/hd1file?v=1", "dataType": "sub"},
    {"serverName": "HD-1", "dataLink": "https://flixcloud.cc/e/hd1file?v=1", "dataType": "dub"},
    {"serverName": "HD-2", "dataLink": "https://flixcloud.cc/e/gfa1lt2dzeev?v=2", "dataType": "sub"},
    {"serverName": "HD-2", "dataLink": "https://flixcloud.cc/e/gfa1lt2dzeev?v=2", "dataType": "dub"},
]})
TOKEN = "eyJ.a+b/c="                    # needs url-quoting: + / =
FILE_URL = "https://fetch8.flixcloud.cc/download/7622-uuid?token=eyJ.a%2Bb%2Fc%3D"

URLS = {
    f"{reanime.SITE}/api/v1/search?limit=20&q=one%20piece": json.dumps({"results": [
        {"anime_id": "one-piece-film-red-yuq2kp", "title": {"english": "One Piece Film: Red"},
         "format": "MOVIE"},
        {"anime_id": "one-piece-xamk74", "title": {"english": "ONE PIECE", "romaji": "ONE PIECE"},
         "format": "TV", "season_year": 1999, "dubbed": 1157},
    ]}),
    f"{reanime.SITE}/watch/one-piece-xamk74/__data.json?ep=1":
        svelte({"sessionUser": None, "isAuthed": False}, None, {"anime": ANIME, "episodes": EPISODES}),
    "https://flixcloud.cc/d/gfa1lt2dzeev/__data.json":
        svelte(None, {"video": {"fileId": "7622-uuid", "filename": "ep.mkv", "sizeHuman": "940 MB"},
                      "download": {"base": "https://fetch8.flixcloud.cc", "token": TOKEN}}),
}
for _n in (1150, 1151, 1152):
    URLS[f"{reanime.SITE}/api/flix/21/{_n}?mal=21"] = SERVERS


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def fake_fetch(url):
    if url in URLS:
        return URLS[url]
    if "/watch/" in url:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    raise AssertionError(f"unexpected url {url}")


class FakeStream:
    """an event-stream response: comment line, then one data: line per event."""

    def __init__(self, events):
        self.lines = [b": keepalive\n"] + [f"data: {json.dumps(e)}\n".encode() for e in events]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.lines)


@contextlib.contextmanager
def offline(events=({"status": "ready", "percent": 100},)):
    """swap the network for the fixtures. yields the event-stream urls opened."""
    opened = []

    def urlopen(req, timeout=None):
        opened.append(req.full_url)
        return FakeStream(events)

    with mock.patch.object(reanime, "fetch", fake_fetch), \
            mock.patch.object(reanime.urllib.request, "urlopen", urlopen):
        yield opened


def run_main(*argv):
    """run reanime.main() with argv; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["reanime", *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            reanime.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class Terminal:
    """just enough of a VT100 to replay the picker: \\r \\n, CSI nA / K / J / m, wrapping."""

    def __init__(self, cols):
        self.cols, self.rows, self.y, self.x = cols, [[]], 0, 0

    def feed(self, raw):
        i = 0
        while i < len(raw):
            m = re.match(r"\x1b\[(\d*)([A-Za-z])", raw[i:])
            if m:
                n, cmd = int(m.group(1) or 1), m.group(2)
                if cmd == "A":
                    self.y = max(0, self.y - n)
                elif cmd == "K":
                    del self.rows[self.y][self.x:]
                elif cmd == "J":
                    del self.rows[self.y][self.x:]
                    del self.rows[self.y + 1:]
                i += len(m.group(0))
                continue
            c = raw[i]
            if c == "\r":
                self.x = 0
            elif c == "\n":
                self._down()
            else:
                if self.x >= self.cols:
                    self._down()
                row = self.rows[self.y]
                row.extend(" " * (self.x - len(row)))
                if self.x < len(row):
                    row[self.x] = c
                else:
                    row.append(c)
                self.x += 1
            i += 1
        return ["".join(r).rstrip() for r in self.rows]

    def _down(self):
        self.y, self.x = self.y + 1, 0
        while len(self.rows) <= self.y:
            self.rows.append([])


# ------------------------------------------------------------------ parsing

class Parsing(unittest.TestCase):
    def test_devalue_handles_refs_shared_values_and_specials(self):
        data = [{"a": 1, "b": 1, "when": 3, "gone": -1, "n": 4}, [2, 2], "x", ["Date", "2026-11-01"], 7]
        out = reanime.devalue(data)
        self.assertEqual(out["a"], ["x", "x"])
        self.assertIs(out["a"], out["b"])             # same index -> same object
        self.assertEqual(out["when"], "2026-11-01")
        self.assertIsNone(out["gone"])
        self.assertEqual(out["n"], 7)

    def test_devalue_roundtrips_fixture_encoding(self):
        self.assertEqual(reanime.devalue(flatten({"anime": ANIME, "episodes": EPISODES})),
                         {"anime": ANIME, "episodes": EPISODES})

    def test_page_data_picks_the_node_with_the_key(self):
        with offline():
            d = reanime.page_data(f"{reanime.SITE}/watch/one-piece-xamk74/__data.json?ep=1", "anime")
        self.assertEqual(d["anime"]["anilist_id"], 21)
        self.assertEqual(len(d["episodes"]), 4)

    def test_fetch_follows_the_site_to_a_new_domain(self):
        """reanime.to now answers every page with a redirect to reanime.cz's homepage."""
        asked = []

        class Response(io.BytesIO):
            def __init__(self, url, body):
                super().__init__(body.encode())
                self.url = url

            def geturl(self):
                return self.url

        moves = {"https://old.example": "https://new.example/",       # the site moved
                 "https://hop1.example": "https://hop2.example/",     # and moved on again
                 "https://hop2.example": "https://hop3.example/"}

        def urlopen(req, timeout=None):
            url = req.full_url
            asked.append(url)
            origin = "/".join(url.split("/")[:3])
            if origin in moves:
                return Response(moves[origin], "<!doctype html>")
            if url == "https://new.example/x/":
                return Response("https://new.example/y/", "moved within the site")
            return Response(url, '{"ok": 1}')

        with mock.patch.object(reanime.urllib.request, "urlopen", urlopen):
            self.assertEqual(reanime.fetch("https://old.example/a/b?c=1"), '{"ok": 1}')
            self.assertEqual(asked, ["https://old.example/a/b?c=1", "https://new.example/a/b?c=1"])
            # a redirect to another page of the same site is just followed
            self.assertEqual(reanime.fetch("https://new.example/x/"), "moved within the site")
            # asking for the homepage gets the homepage
            self.assertEqual(reanime.fetch("https://old.example/"), "<!doctype html>")
            # the new domain is asked once, not followed on to a third
            del asked[:]
            self.assertEqual(reanime.fetch("https://hop1.example/a"), "<!doctype html>")
            self.assertEqual(asked, ["https://hop1.example/a", "https://hop2.example/a"])

    def test_parse_url(self):
        self.assertEqual(reanime.parse_url(
            "https://reanime.cz/watch/one-piece-xamk74?ep=1151&lang=dub&server=HD-2"),
            ("one-piece-xamk74", "1151", "dub"))
        self.assertEqual(reanime.parse_url(          # a link from before the move
            "https://reanime.to/watch/one-piece-xamk74?ep=1151&lang=dub&server=HD-2"),
            ("one-piece-xamk74", "1151", "dub"))
        self.assertEqual(reanime.parse_url("https://reanime.to/anime/one-piece-xamk74"),
                         ("one-piece-xamk74", None, None))
        self.assertEqual(reanime.parse_url("https://reanime.to/watch/x-abc123?lang=fr"),
                         ("x-abc123", None, None))
        self.assertEqual(reanime.parse_url("one piece"), (None, None, None))

    def test_titles_and_numbers(self):
        self.assertEqual(reanime.title_of({"romaji": "Shingeki", "english": "Attack on Titan"}),
                         "Attack on Titan")
        self.assertEqual(reanime.title_of({}), "?")
        self.assertEqual(reanime.titles("plain"), ["plain"])
        self.assertEqual(reanime.epnum({"number": 1151}), "1151")
        self.assertEqual(reanime.epnum({"number": 6.5}), "6.5")
        self.assertEqual(reanime.epnum({"episode_number": 3.0}), "3")
        self.assertIn("[filler]", reanime.ep_label(EPISODES[2]))
        self.assertTrue(reanime.ep_label(EPISODES[1]).startswith(" 1151  Her"))

    def test_select_eps(self):
        eps = EPISODES[:3]
        self.assertEqual(reanime.select_eps(eps, "1151", "dub"), ([1], False))
        self.assertEqual(reanime.select_eps(eps, "1150-1151", "dub"), ([0, 1], True))
        self.assertEqual(reanime.select_eps(eps, "1151-", "dub"), ([1, 2], True))
        with self.assertRaises(SystemExit) as cm:
            reanime.select_eps(eps, "9999", "dub")
        self.assertIn("1150-1152", str(cm.exception))
        with self.assertRaises(SystemExit):
            reanime.select_eps(eps, "abc", "dub")


# ------------------------------------------------------------------ network steps

class Pipeline(unittest.TestCase):
    def test_file_id_prefers_hd2_and_filters_language(self):
        servers = json.loads(SERVERS)["servers"]
        self.assertEqual(reanime.file_id(servers, "dub"), "gfa1lt2dzeev")
        self.assertEqual(reanime.file_id(servers[:2], "sub"), "hd1file")
        other = [{"serverName": "HD-3", "dataLink": "https://elsewhere/e/zzz", "dataType": "dub"}]
        self.assertIsNone(reanime.file_id(other, "dub"))

    def test_servers_url_uses_anilist_then_tmdb(self):
        calls = []
        ok = json.dumps({"success": True, "servers": [1]})
        with mock.patch.object(reanime, "fetch", lambda u: calls.append(u) or ok):
            self.assertEqual(reanime.servers(ANIME, {"number": 1151}), [1])
            reanime.servers({"themoviedb_id": 99, "external_seasons": {"tmdb": 2}}, {"number": 4})
            self.assertEqual(reanime.servers({}, {"number": 1}), [])
        self.assertEqual(calls, [f"{reanime.SITE}/api/flix/21/1151?mal=21",
                                 f"{reanime.SITE}/api/flix/0/4?tmdb=99&season=2"])

    def test_resolve_builds_quoted_download_url(self):
        with offline() as opened:
            url, video = reanime.resolve(ANIME, EPISODES[1], "dub")
        self.assertEqual(url, FILE_URL)
        self.assertEqual(video["sizeHuman"], "940 MB")
        self.assertEqual(opened, [FILE_URL.replace("?token", "/progress?token")])

    def test_resolve_waits_through_building(self):
        events = [{"status": "queued", "percent": 0}, {"status": "building", "percent": 40},
                  {"status": "ready", "percent": 100}]
        err = io.StringIO()
        with offline(events), contextlib.redirect_stderr(err):
            url, _ = reanime.resolve(ANIME, EPISODES[1], "dub")
        self.assertEqual(url, FILE_URL)
        self.assertIn("building 40%", err.getvalue())

    def test_resolve_reports_failed_and_unfinished_prep(self):
        with offline([{"status": "failed", "error": "disk full"}]):
            with self.assertRaisesRegex(RuntimeError, "disk full"):
                reanime.resolve(ANIME, EPISODES[1], "dub", verbose=False)
        with offline([{"status": "building", "percent": 10}]):
            with self.assertRaisesRegex(RuntimeError, "ended before"):
                reanime.resolve(ANIME, EPISODES[1], "dub", verbose=False)

    def test_resolve_without_language_source(self):
        sub_only = json.dumps({"success": True, "servers": json.loads(SERVERS)["servers"][:1]})
        with offline(), mock.patch.dict(URLS, {f"{reanime.SITE}/api/flix/21/1151?mal=21": sub_only}):
            with self.assertRaisesRegex(LookupError, "no dub source"):
                reanime.resolve(ANIME, EPISODES[1], "dub")

    def test_find_anime_prefers_exact_title_and_falls_back_from_bad_slug(self):
        err = io.StringIO()
        with offline(), contextlib.redirect_stderr(err):
            anime, eps = reanime.find_anime("one piece", auto=True)
            self.assertEqual(anime["anime_id"], "one-piece-xamk74")
            anime, _ = reanime.find_anime("one-piece-xamk74", auto=True)       # slug, no search
            self.assertEqual(anime["anime_id"], "one-piece-xamk74")
        self.assertIn("using ONE PIECE", err.getvalue())


# ------------------------------------------------------------------ main()

class Main(unittest.TestCase):
    def test_print_single_episode(self):
        with offline():
            code, out, err = run_main("one piece", "1151", "--dub", "--print")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), FILE_URL)

    def test_print_range_from_pasted_url(self):
        with offline():
            code, out, err = run_main("https://reanime.to/watch/one-piece-xamk74?ep=1150-1152&lang=dub",
                                      "--print")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.split(), [FILE_URL] * 3)

    def test_the_installed_command_ends_in_one_line_not_a_traceback(self):
        """the reanime command calls main(), so main() itself turns these into a message
        (a web page where the data should be is what the move to reanime.cz caused)."""
        for raised, said in [(None, "unexpected response from reanime/flixcloud"),
                             (None, "reanime --update gets the newest reanime"),
                             (urllib.error.URLError("no route"), "network error: <urlopen error no route>"),
                             (KeyboardInterrupt(), "cancelled")]:
            def fetch(url, raised=raised):
                if raised:
                    raise raised
                return "<!doctype html>\n<html>"
            with mock.patch.object(reanime, "fetch", fetch):
                code, out, err = run_main("one-piece-xamk74", "--list")
            self.assertIsInstance(code, str)
            self.assertIn(said, code)

    def test_list(self):
        with offline():
            code, out, _ = run_main("one-piece-xamk74", "--list")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], "S-   1180")
        self.assertIn("SD   1152  Egghead C  [filler]", out)

    def test_m3u(self):
        with offline(), tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.m3u")
            code, out, err = run_main("one piece", "1150-1151", "--dub", "--m3u", path)
            self.assertEqual(code, 0, err)
            text = read(path)
        self.assertTrue(text.startswith("#EXTM3U\n#PLAYLIST:ONE PIECE 1150-1151 (dub)"))
        self.assertIn("#EXTINF:-1,ONE PIECE - 1151 - Her and Her Father's Dream!\n" + FILE_URL, text)

    def test_dub_episode_that_only_has_sub(self):
        with offline():
            code, _, _ = run_main("one piece", "1180", "--dub", "--print")
        self.assertIn("no dub release", str(code))

    def test_setup_reports_player(self):
        with mock.patch.object(reanime, "find_mpv", return_value=["/usr/bin/mpv"]):
            code, out, _ = run_main("--setup")
        self.assertEqual((code, out.strip()), (0, "ready: /usr/bin/mpv"))

    def test_chapter_skipper_loads_unless_no_skip(self):
        played = []
        with offline(), mock.patch.object(reanime, "ensure_player", lambda mpv, yes: ["mpv"]), \
                mock.patch.object(reanime, "watch", lambda cmd, *rest: played.append(cmd)):
            run_main("one piece", "1151", "--dub")
            run_main("one piece", "1151", "--dub", "--no-skip")
        skipper = os.path.join(CACHE.name, "reanime", "skip-chapters.lua")
        self.assertEqual(played, [["mpv", f"--script={skipper}"], ["mpv"]])
        self.assertEqual(read(skipper), reanime.CHAPTER_SKIPPER)

    def test_version_matches_pyproject_and_changelog(self):
        pyproject = read(os.path.join(ROOT, "pyproject.toml"))
        changelog = read(os.path.join(ROOT, "CHANGELOG.md"))
        self.assertEqual(re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1),
                         reanime.__version__)
        self.assertIn(f"## [{reanime.__version__}]", changelog)


# ------------------------------------------------------------------ terminal UI

class Picker(unittest.TestCase):
    def pick(self, keys, cols=120, lines=40, header="\nONE PIECE (sub):"):
        eps = [{"number": n, "title": f"Episode {n} " + "long title " * (8 if n % 7 == 0 else 0)}
               for n in range(1, 1181)]
        keys = iter(keys)
        out = io.StringIO()
        with mock.patch.object(reanime, "interactive_ok", return_value=True), \
                mock.patch.object(reanime, "_getkey", lambda: next(keys)), \
                mock.patch.object(reanime.shutil, "get_terminal_size",
                                  return_value=os.terminal_size((cols, lines))), \
                contextlib.redirect_stdout(out):
            picked = reanime.fuzzy_pick(eps, reanime.ep_label, header, noun="episodes",
                                        initial=len(eps) - 1)
        return picked, Terminal(cols).feed(out.getvalue())

    def test_redraw_never_duplicates_header_or_hint(self):
        moves = [("key", "up")] * 30 + [("key", "down")] * 5 + [("key", "enter")]
        for cols, lines in ((120, 40), (60, 14), (30, 8)):
            picked, screen = self.pick(moves, cols, lines)
            self.assertEqual(picked["number"], 1155)
            self.assertEqual(sum("ONE PIECE (sub):" in s for s in screen), 1, (cols, screen))
            self.assertEqual(sum(s.startswith("type to filter") for s in screen), 1, (cols, screen))
            self.assertLessEqual(len(screen), lines + 2)   # header block + one redraw region

    def test_typing_filters_by_episode_prefix(self):
        keys = [("char", c) for c in "1151"] + [("key", "enter")]
        picked, screen = self.pick(keys)
        self.assertEqual(picked["number"], 1151)
        self.assertIn("1/1180 episodes", screen[-2])

    def test_escape_cancels(self):
        self.assertIsNone(self.pick([("key", "esc")])[0])


@unittest.skipUnless(hasattr(os, "openpty") and reanime.termios, "needs a POSIX terminal")
class Keys(unittest.TestCase):
    """keys typed into a real pseudo-terminal, sent the way Linux and macOS terminals send them."""

    def setUp(self):
        self.master, slave = os.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, slave)
        reanime.tty.setcbreak(slave)            # keys written below are readable at once
        # what a terminal looks like to Python: a text wrapper over a buffered reader
        tty_in = io.TextIOWrapper(io.BufferedReader(io.FileIO(slave, "rb", closefd=False)),
                                  encoding="utf-8")
        for patch in (mock.patch.object(sys, "stdin", tty_in),
                      mock.patch.object(reanime, "_held", bytearray())):
            patch.start()
            self.addCleanup(patch.stop)

    def test_escape_sequences_arrive_whole(self):
        cases = [
            (b"\x1b[A", [("key", "up")]),
            (b"\x1b[B", [("key", "down")]),
            (b"\x1bOA", [("key", "up")]),               # application cursor mode
            (b"\x1b[1;5B", [("key", "down")]),          # Ctrl+Down
            (b"\x1b[3~", [("key", "")]),                # Delete: ignored, not typed as "~"
            (b"\x1b", [("key", "esc")]),
            (b"\x1b[Ax", [("key", "up"), ("char", "x")]),
            (b"ab", [("char", "a"), ("char", "b")]),
            ("é".encode(), [("char", "é")]),
            (b"\r", [("key", "enter")]),
            (b"\x7f", [("key", "backspace")]),
        ]
        for typed, want in cases:
            with self.subTest(typed=typed):
                os.write(self.master, typed)
                self.assertEqual([reanime._getkey() for _ in want], want)

    def test_arrows_move_the_picker_instead_of_closing_it(self):
        os.write(self.master, b"\x1b[B\x1b[B\x1b[A\r")
        with mock.patch.object(reanime, "interactive_ok", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            picked = reanime.fuzzy_pick(["one", "two", "three"], str, "\npick:")
        self.assertEqual(picked, "two")


# ------------------------------------------------------------------ playback

def fake_resolve(anime, ep, lang, verbose=True):
    return f"https://u/{reanime.epnum(ep)}", {"sizeHuman": "1 MB"}


class Playback(unittest.TestCase):
    eps = [{"number": n, "title": f"T{n}"} for n in (1, 2, 3)]

    def test_mpv_args(self):
        self.assertEqual(reanime.mpv_args("dub"), ["--alang=eng,en", "--sid=no"])
        picker = os.path.join(CACHE.name, "reanime", "pick-subs.lua")
        self.assertEqual(reanime.mpv_args("sub"),
                         ["--alang=jpn,ja", "--slang=eng,en", f"--script={picker}"])
        with open(picker, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), reanime.SUB_PICKER)

    def run_loop(self, start, script):
        launched, menus = [], []

        def menu(opts, default, header):
            menus.append((opts, default, header))
            return [v for _, v in opts].index(script.pop(0))

        with mock.patch.object(reanime, "resolve", fake_resolve), \
                mock.patch.object(reanime, "launch", lambda c, u, t, lg: launched.append((u, t))), \
                mock.patch.object(reanime, "menu_pick", menu), \
                contextlib.redirect_stderr(io.StringIO()):
            reanime.play_loop(["mpv"], ANIME, self.eps, start, "dub")
        return launched, menus

    def test_loop_next_replay_previous(self):
        launched, menus = self.run_loop(0, ["next", "replay", "prev", "quit"])
        self.assertEqual([u for u, _ in launched], ["https://u/1", "https://u/2", "https://u/2",
                                                     "https://u/1"])
        self.assertEqual(launched[1][1], "ONE PIECE - 2 - T2")
        opts, default, _ = menus[0]
        self.assertEqual(opts[default][1], "next")

    def test_loop_defaults_to_quit_on_newest_episode(self):
        _, menus = self.run_loop(2, ["quit"])
        opts, default, _ = menus[0]
        self.assertEqual(opts[default][1], "quit")
        self.assertNotIn("next", [v for _, v in opts])

    def test_loop_counts_an_episode_watched_on_moving_to_the_next(self):
        fake = FakeAniList()
        with anilist(fake):
            self.run_loop(0, ["next", "replay", "quit"])        # mpv-android reports nothing
        self.assertEqual(fake.saved, [(21, 1, "CURRENT")])



class FakeMpv:
    """one mpv process plus its IPC connection. the playlist starts with the urls on
    the command line; every file lasts `length` polls of `duration` seconds, then the
    next one plays, or mpv exits after the last. `actions` maps a poll number to
    "next" (Enter in mpv) or "quit" (q). `ending` is where the chapter skipper says
    every file's ending starts (None: no chapters, or no skipper). `changes` maps a poll
    number to settings the viewer changes then ({"audio-delay": 0.9})."""

    def __init__(self, argv, length=3, actions=None, duration=1400.0, volume=55.0, ending=None,
                 changes=None):
        self.argv = argv
        self.playlist = [a for a in argv if a.startswith("https://")]
        self.titles = [a.split("=", 1)[1] for a in argv if a.startswith("--force-media-title=")]
        self.length, self.actions, self.duration, self.volume = length, actions or {}, duration, volume
        self.ending, self.changes = ending, changes or {}
        self.settings = {"audio-delay": 0.0, "sub-scale": 1.0, "sub-pos": 100}
        for a in argv:                          # --audio-delay=0.9 and the like
            k, _, v = a[2:].partition("=")
            if a.startswith("--") and k in self.settings:
                self.settings[k] = float(v)
        self.pos, self.tick, self.polls, self.exited, self.printed = 0, 0, 0, False, []

    # the process
    def poll(self):
        return 0 if self.exited else None

    def wait(self):
        self.exited = True
        return 0

    def kill(self):
        self.exited = True

    # the ipc connection
    def get(self, prop):
        if prop == "playlist-pos":
            self.polls += 1
            self.tick += 1
            self.settings.update(self.changes.get(self.polls, {}))
            action = self.actions.get(self.polls)
            if action == "next" or self.tick > self.length:
                if self.pos + 1 >= len(self.playlist):
                    self.exited = True
                    raise EOFError
                self.pos, self.tick = self.pos + 1, 1
            if action == "quit":
                self.exited = True
                raise EOFError
            return self.pos
        return {"time-pos": self.duration * self.tick / self.length, "duration": self.duration,
                "volume": self.volume, "user-data/reanime/ending": self.ending,
                **self.settings}[prop]

    def command(self, *args, **named):
        if named.get("name", args[0] if args else None) == "loadfile" or args[:1] == ("loadfile",):
            self.playlist.append(named["url"])
            self.titles.append(named["options"].split("%", 2)[2])
        elif args[:1] == ("print-text",):
            self.printed.append(args[1])

    def close(self):
        pass


class FakeAniList:
    """AniList's GraphQL API for one viewer. `entry` is their {status, progress} for the
    show (None: not on their list), `episodes` its length (None: still airing), and
    `token` the only one it accepts."""

    def __init__(self, episodes=None, entry=None, token="tok"):
        self.episodes, self.entry, self.token, self.saved = episodes, entry, token, []
        self.requests = 0

    def __call__(self, req, timeout=None):
        assert req.full_url == reanime.ANILIST_API, req.full_url
        self.requests += 1
        if req.get_header("Authorization") != f"Bearer {self.token}":
            body = json.dumps({"errors": [{"message": "Invalid token", "status": 400}]})
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                         io.BytesIO(body.encode()))
        q = json.loads(req.data.decode())
        v = q["variables"]
        if "Viewer" in q["query"]:
            data = {"Viewer": {"name": "vash"}}
        elif "SaveMediaListEntry" in q["query"]:
            self.saved.append((v["id"], v["progress"], v["status"]))
            self.entry = {"status": v["status"], "progress": v["progress"]}
            data = {"SaveMediaListEntry": {"progress": v["progress"]}}
        else:
            data = {"Media": {"episodes": self.episodes, "mediaListEntry": self.entry}}
        return io.BytesIO(json.dumps({"data": data}).encode())


@contextlib.contextmanager
def anilist(fake, token="tok"):
    """talk to the fake AniList, logged in with `token` (None: logged out)."""
    path = os.path.join(reanime.state_dir(), "anilist.json")
    if token:
        os.makedirs(reanime.state_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"token": token}, fh)
    try:
        with mock.patch.object(reanime.urllib.request, "urlopen", fake):
            yield fake
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


class Session(unittest.TestCase):
    """the desktop binge: one mpv window, next episode appended while one plays."""
    eps = [{"number": n, "title": f"T{n}"} for n in (1, 2, 3)]
    anime = dict(ANIME, _slug="one-piece-xamk74")

    def setUp(self):
        for name in ("history.json", "anilist.json"):        # each test starts with none
            with contextlib.suppress(FileNotFoundError):
                os.remove(os.path.join(reanime.state_dir(), name))

    def run_session(self, idx=0, stop=None, start=0.0, resolve=fake_resolve, lang="dub", **fake):
        mpvs = []

        def popen(argv):
            mpvs.append(FakeMpv(argv, **fake))
            return mpvs[-1]
        with mock.patch.object(reanime, "resolve", resolve), \
                mock.patch.object(reanime.subprocess, "Popen", popen), \
                mock.patch.object(reanime, "mpv_connect", lambda path, proc: proc), \
                mock.patch.object(reanime, "POLL", 0.001), \
                contextlib.redirect_stderr(io.StringIO()):
            out = reanime.play_session(["mpv"], self.anime, self.eps, idx, lang, start, stop)
        self.assertEqual(len(mpvs), 1)          # one window for the whole session
        return out, mpvs[0]

    def test_plays_every_episode_in_one_window(self):
        (idx, pos, status), mpv = self.run_session()
        self.assertEqual(mpv.playlist, ["https://u/1", "https://u/2", "https://u/3"])
        self.assertEqual(mpv.titles, ["ONE PIECE - 1 - T1", "ONE PIECE - 2 - T2",
                                      "ONE PIECE - 3 - T3"])
        self.assertEqual((idx, pos, status), (2, None, "done"))
        self.assertIn("--{", mpv.argv)          # first file's title is file-local
        self.assertTrue(any(a.startswith("--input-ipc-server=") for a in mpv.argv))
        h = reanime.load_history()
        self.assertEqual((h["last"], h["shows"]["one-piece-xamk74"]["episode"], h["volume"]),
                         ("one-piece-xamk74", "3", 55))
        self.assertIsNone(h["shows"]["one-piece-xamk74"]["position"])

    def test_quit_mid_episode_is_remembered_with_position_and_volume(self):
        (idx, pos, status), _ = self.run_session(actions={5: "quit"}, volume=72.0)
        self.assertEqual((idx, status), (1, "stopped"))          # quit 1/3 into episode 2
        self.assertAlmostEqual(pos, 1400 / 3, places=1)
        h = reanime.load_history()
        self.assertEqual(h["shows"]["one-piece-xamk74"]["episode"], "2")
        self.assertEqual(h["volume"], 72)
        # the next run opens mpv at that volume, and a resume starts at the position
        (_, _, _), mpv = self.run_session(idx=1, start=466.7, actions={2: "quit"})
        self.assertIn("--volume=72", mpv.argv)
        self.assertIn("--start=466.7", mpv.argv)

    def test_settings_changed_in_mpv_stick_to_the_show_and_language(self):
        # dub: audio delay raised to +0.9s (Ctrl + nine times) during episode 1
        self.run_session(actions={4: "quit"}, changes={2: {"audio-delay": 0.1 * 9}})
        self.assertEqual(reanime.load_history()["tweaks"],
                         {"one-piece-xamk74/dub": {"audio-delay": 0.9}})
        # the next dub session opens with it, and changing the subtitles adds to it
        _, mpv = self.run_session(actions={3: "quit"}, changes={2: {"sub-scale": 0.5}})
        self.assertIn("--audio-delay=0.9", mpv.argv)
        self.assertEqual(reanime.load_history()["tweaks"],
                         {"one-piece-xamk74/dub": {"audio-delay": 0.9, "sub-scale": 0.5}})
        # sub keeps its own: no audio delay
        _, mpv = self.run_session(actions={2: "quit"}, lang="sub")
        self.assertFalse([a for a in mpv.argv if a.startswith(("--audio-delay", "--sub-scale"))])
        # setting it back to 0 is a change too
        self.run_session(actions={3: "quit"}, changes={2: {"audio-delay": 0.0}})
        self.assertEqual(reanime.load_history()["tweaks"]["one-piece-xamk74/dub"]["audio-delay"], 0)

    def test_quitting_during_the_ending_counts_as_watched(self):
        # quit 2/3 into episode 1 (933s of 1400): past an ending marked at 900s...
        (idx, pos, status), _ = self.run_session(actions={3: "quit"}, ending=900.0)
        self.assertEqual((idx, pos, status), (0, None, "done"))
        self.assertIsNone(reanime.load_history()["shows"]["one-piece-xamk74"]["position"])
        # ...but not before it
        (idx, pos, status), _ = self.run_session(actions={3: "quit"}, ending=1000.0)
        self.assertEqual((idx, status), (0, "stopped"))

    def test_episodes_played_to_the_end_move_anilist_progress(self):
        fake = FakeAniList()
        with anilist(fake):
            self.run_session()                                  # 1, 2 and 3 play through
        self.assertEqual(fake.entry, {"status": "CURRENT", "progress": 3})
        done = [p for _, p, _ in fake.saved]
        self.assertEqual(done, sorted(done))                    # never went down
        # > past episode 1, then quit a third into episode 2: neither was watched
        fake = FakeAniList()
        with anilist(fake):
            self.run_session(actions={2: "next", 4: "quit"})
        self.assertEqual(fake.saved, [])
        # logged out: AniList is never contacted
        with anilist(FakeAniList(), token=None) as fake:
            self.run_session()
        self.assertEqual(fake.requests, 0)

    def test_enter_skips_ahead_and_range_stops_at_its_end(self):
        (idx, _, status), mpv = self.run_session(stop=1, actions={3: "next"})
        self.assertEqual(mpv.playlist, ["https://u/1", "https://u/2"])     # nothing past 2
        self.assertEqual((idx, status), (1, "done"))

    def test_unresolvable_next_episode_is_skipped(self):
        def flaky(anime, ep, lang, verbose=True):
            if str(reanime.epnum(ep)) == "2":
                raise LookupError("no dub source for episode 2")
            return fake_resolve(anime, ep, lang)
        (idx, _, _), mpv = self.run_session(resolve=flaky)
        self.assertEqual(mpv.playlist, ["https://u/1", "https://u/3"])
        self.assertTrue(any("skipping episode 2" in p for p in mpv.printed))

    def test_without_ipc_it_still_plays_the_first_episode(self):
        with mock.patch.object(reanime, "mpv_connect", lambda path, proc: None), \
                mock.patch.object(reanime, "resolve", fake_resolve), \
                mock.patch.object(reanime.subprocess, "Popen", lambda argv: FakeMpv(argv)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reanime.play_session(["mpv"], self.anime, self.eps, 0, "dub"),
                             (0, None, "done"))

    def test_menu_resumes_by_default_after_quitting_mid_episode(self):
        sessions, menus = [(1, 754.0, "stopped"), (1, None, "quit")], []
        calls = []

        def session(cmd, anime, eps, idx, lang, start, stop):
            calls.append((idx, start))
            return sessions.pop(0)

        def menu(opts, default, header):
            menus.append((opts, default, header))
            return default
        with mock.patch.object(reanime, "play_session", session), \
                mock.patch.object(reanime, "menu_pick", menu):
            reanime.binge(["mpv"], self.anime, self.eps, 0, "dub")
        opts, default, header = menus[0]
        self.assertEqual(opts[default][1], "resume")
        self.assertIn("stopped at 12:34", header)
        self.assertEqual(calls, [(0, 0.0), (1, 754.0)])

    def test_resume_point_and_continue_prompt(self):
        eps = [{"number": n, "dubbed": True} for n in (1154, 1155, 1156)]
        hist = {"last": "one-piece-xamk74", "shows": {"one-piece-xamk74": {
            "lang": "dub", "episode": "1155", "position": 754.0}}}
        self.assertEqual(reanime.resume_point(self.anime, eps, "dub", hist), (1, 754.0))
        self.assertIsNone(reanime.resume_point(self.anime, eps, "sub", hist))
        hist["shows"]["one-piece-xamk74"]["position"] = None            # watched: next one
        self.assertEqual(reanime.resume_point(self.anime, eps, "dub", hist), (2, 0.0))
        hist["shows"]["one-piece-xamk74"]["episode"] = "1156"           # newest: caught up
        self.assertEqual(reanime.resume_point(self.anime, eps, "dub", hist), (2, None))

        reanime.remember(self.anime, eps[1], "dub", 754.0)
        labels = []
        with mock.patch.object(reanime, "load_anime", lambda slug: (self.anime, eps)), \
                mock.patch.object(reanime, "interactive_ok", lambda: True), \
                mock.patch.object(reanime, "menu_pick",
                                  lambda opts, d, h: labels.append(opts[0][0]) or 0):
            anime, got_eps, lang, idx, start = reanime.continue_prompt()
        self.assertEqual((lang, idx, start), ("dub", 1, 754.0))
        self.assertEqual(labels, ["ONE PIECE 1155 (dub) at 12:34"])

    def test_plain_reanime_continues_where_it_left_off(self):
        # the whole path from `reanime` with no arguments to mpv: Enter on "continue"
        # plays the saved episode from the saved position
        eps = [{"number": n, "subbed": True} for n in (1, 2, 3)]
        reanime.remember(self.anime, eps[0], "sub", 1181.0)
        played = []
        with mock.patch.object(reanime, "ensure_player", lambda mpv, yes: ["mpv"]), \
                mock.patch.object(reanime, "load_anime", lambda slug: (self.anime, eps)), \
                mock.patch.object(reanime, "interactive_ok", lambda: True), \
                mock.patch.object(reanime, "menu_pick", lambda opts, default, header: default), \
                mock.patch.object(reanime, "play_session",
                                  lambda *a: played.append(a) or (0, None, "quit")):
            code, _, err = run_main("--no-skip")
        self.assertEqual(code, 0, err)
        self.assertEqual(played, [(["mpv"], self.anime, eps, 0, "sub", 1181.0, None)])

    def test_history_survives_a_corrupt_file(self):
        os.makedirs(reanime.state_dir(), exist_ok=True)
        with open(os.path.join(reanime.state_dir(), "history.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(reanime.load_history(), {})
        reanime.remember(self.anime, self.eps[0], "sub", 10.0)
        self.assertEqual(reanime.load_history()["shows"]["one-piece-xamk74"]["lang"], "sub")

    def test_clock(self):
        self.assertEqual([reanime.clock(s) for s in (0, 754, 3723)], ["0:00", "12:34", "1:02:03"])

    def test_real_mpv_plays_a_whole_session_in_one_window(self):
        # three generated 2s "episodes" through a real mpv and its IPC (pipe or socket).
        # REANIME_TEST_MPV makes this mandatory (CI's install stage)
        forced = os.environ.get("REANIME_TEST_MPV")
        cmd = [forced] if forced else reanime.find_mpv("mpv")
        if not cmd:
            self.skipTest("mpv not installed")
        sent, settings = [], {}
        real_command = reanime.MpvIpc.command

        def spy(ipc, *args, **named):
            sent.append(named.get("name") or args[0])
            got = real_command(ipc, *args, **named)
            if args[:1] == ("get_property",) and args[1] in reanime.TWEAKS:
                settings[args[1]] = got
            return got

        def lavfi(anime, ep, lang, verbose=True):
            return f"av://lavfi:testsrc=duration=2:size=64x64:rate={10 + ep['number']}", {}
        # settings saved for this show: mpv has to accept them as options
        reanime.remember(self.anime, self.eps[0], "dub",
                         tweaks={"audio-delay": 0.9, "sub-scale": 0.5, "sub-pos": 102})
        with mock.patch.object(reanime, "resolve", lavfi), \
                mock.patch.object(reanime.MpvIpc, "command", spy), \
                mock.patch.object(reanime, "POLL", 0.1), \
                contextlib.redirect_stderr(io.StringIO()):
            # --idle=yes: what mpv 0.42's mpv.exe does with no console. reanime has to
            # override it, or mpv idles after the last episode and the session never ends
            got = reanime.play_session([*cmd, "--no-config", "--vo=null", "--ao=null",
                                        "--really-quiet", "--idle=yes"],
                                       self.anime, self.eps, 0, "dub")
        self.assertEqual(got, (2, None, "done"))
        self.assertEqual(sent.count("loadfile"), 2)                    # 2 and 3 appended
        self.assertEqual(settings, {"audio-delay": 0.9, "sub-scale": 0.5, "sub-pos": 102})


class AniList(unittest.TestCase):
    """opt-in progress tracking: --anilist logs in, finished episodes move progress up."""
    anime = dict(ANIME, _slug="one-piece-xamk74")

    def progress(self, fake, number, token="tok"):
        """track episode `number` -> what it reports, or None if it did nothing."""
        with anilist(fake, token):
            job = reanime.track(self.anime, {"number": number})
            if job is None:
                return None
            job[0].join(10)
        if "err" in job[1]:
            raise job[1]["err"]
        return job[1]["ok"]

    def test_progress_only_moves_up(self):
        fake = FakeAniList()                                    # One Piece: still airing
        self.assertEqual(self.progress(fake, 1150), "ONE PIECE 1150")   # added to the list
        self.assertEqual(fake.saved, [(21, 1150, "CURRENT")])
        self.assertIsNone(self.progress(fake, 1150))            # already there
        self.assertIsNone(self.progress(fake, 1100))            # rewatching an older one
        self.assertIsNone(self.progress(fake, 1150.5))          # a recap isn't progress
        self.assertEqual(self.progress(fake, 1153), "ONE PIECE 1153")   # skipping ahead counts
        self.assertEqual(len(fake.saved), 2)

    def test_last_episode_completes_and_completed_shows_are_left_alone(self):
        fake = FakeAniList(episodes=24, entry={"status": "CURRENT", "progress": 23})
        self.assertEqual(self.progress(fake, 24), "ONE PIECE 24  (completed)")
        self.assertEqual(fake.saved[-1], (21, 24, "COMPLETED"))
        self.assertIsNone(self.progress(fake, 3))               # a rewatch doesn't touch it
        fake.entry = {"status": "REPEATING", "progress": 2}     # unless marked as rewatching
        self.progress(fake, 3)
        self.assertEqual(fake.saved[-1], (21, 3, "REPEATING"))
        fake.entry = {"status": "PAUSED", "progress": 2}        # picking a show back up
        self.progress(fake, 3)
        self.assertEqual(fake.saved[-1], (21, 3, "CURRENT"))

    def test_logged_out_or_no_anilist_id_sends_nothing(self):
        fake = FakeAniList()
        self.assertIsNone(self.progress(fake, 1150, token=None))
        with anilist(fake):
            self.assertIsNone(reanime.track(dict(self.anime, anilist_id=None), {"number": 1}))
        self.assertEqual(fake.saved, [])

    def test_expired_token_says_to_log_in_again(self):
        with self.assertRaisesRegex(RuntimeError, r"run reanime --anilist again"):
            self.progress(FakeAniList(token="new"), 1150, token="old")

    def test_login_and_logout(self):
        with anilist(FakeAniList(), token=None), \
                mock.patch.object(reanime, "ANILIST_CLIENT", "123"), \
                mock.patch("webbrowser.open") as browser:
            with mock.patch("getpass.getpass", return_value="wrong"):
                code, _, _ = run_main("--anilist")
            self.assertIn("didn't take that token", code)
            self.assertIsNone(reanime.anilist_token())
            with mock.patch("getpass.getpass", return_value=" tok\n"):
                code, out, _ = run_main("--anilist")
            self.assertEqual(code, 0)
            self.assertIn("logged in to AniList as vash", out)
            self.assertIn("client_id=123&response_type=token", browser.call_args[0][0])
            self.assertEqual(reanime.anilist_token(), "tok")
            code, out, _ = run_main("--anilist", "logout")
            self.assertIn("logged out of AniList", out)
            self.assertIsNone(reanime.anilist_token())
        with mock.patch.object(reanime, "ANILIST_CLIENT", ""):
            self.assertIn("isn't set up", run_main("--anilist")[0])


# ------------------------------------------------------------------ player setup

class SubPicker(unittest.TestCase):
    """sub mode's mpv script that skips title-card / signs tracks flagged as default."""

    def test_written_once_and_repaired_when_changed(self):
        path = reanime.sub_picker()
        with mock.patch.object(reanime.os, "makedirs", side_effect=AssertionError("rewrote")):
            self.assertEqual(reanime.sub_picker(), path)        # up to date: left alone
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("-- stale")
        reanime.sub_picker()
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), reanime.SUB_PICKER)

    def test_unwritable_cache_falls_back_to_plain_slang(self):
        blocker = os.path.join(CACHE.name, "not-a-dir")
        with open(blocker, "w") as fh:
            fh.write("x")
        with mock.patch.object(reanime, "cache_dir", lambda: os.path.join(blocker, "reanime")):
            self.assertIsNone(reanime.sub_picker())
            self.assertEqual(reanime.mpv_args("sub"), ["--alang=jpn,ja", "--slang=eng,en"])

    def test_partial_words_match_real_partial_titles_only(self):
        words = re.findall(r'"([^"]+)"', re.search(r"local partial = \{(.*)\}", reanime.SUB_PICKER)[1])

        def partial(title):
            return any(w in title.lower() for w in words)
        # track titles seen on reanime's files
        for title in ("Episode Name (Restyled By Simmsy)", "Episode Name (Orignal)", "Forced",
                      "Signs & Songs", "Dubtitle(AI)"):
            self.assertTrue(partial(title), title)
        for title in ("Full Subtitles (Restyled By Simmsy)", "Full Subtitles (Orignal)",
                      "Track 4 (ENG)", "English", "Dialogue"):
            self.assertFalse(partial(title), title)

    def test_real_mpv_skips_the_title_card_track(self):
        # tests/fixtures/partial-subs.mkv: English "Episode Name" [default], "Signs & Songs",
        # "Full Subtitles". REANIME_TEST_MPV makes this mandatory (CI's install stage)
        forced = os.environ.get("REANIME_TEST_MPV")
        cmd = [forced] if forced else reanime.find_mpv("mpv")
        if not cmd:
            self.skipTest("mpv not installed")
        fixture = os.path.join(ROOT, "tests", "fixtures", "partial-subs.mkv")

        def picked(args):
            log = os.path.join(CACHE.name, "mpv.log")
            subprocess.run([*cmd, "--no-config", "--vo=null", "--ao=null", "--frames=1", *args,
                            "--term-playing-msg=subs=${current-tracks/sub/title:none}",
                            f"--log-file={log}", fixture],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            with open(log, encoding="utf-8", errors="replace") as fh:
                return re.findall(r"\] subs=(.*)", fh.read())

        # control: plain --slang lands on the title card, the bug the picker fixes
        self.assertEqual(picked(["--slang=eng,en"]), ["Episode Name"])
        self.assertEqual(picked(reanime.mpv_args("sub")), ["Full Subtitles"])


class ChapterSkip(unittest.TestCase):
    """the mpv script that skips openings and goes from the ending to the next episode."""

    def kind(self, title):
        """CHAPTER_SKIPPER's matching, in Python: whole words, %d* = digits. "intro" is
        an opening only in files with no chapter named like one."""
        for kind, table in (("opening", "OPENING"), ("ending", "ENDING"), ("intro", "INTRO")):
            words = re.findall(r'"([^"]+)"', re.search(rf"local {table} = \{{(.*)\}}",
                                                       reanime.CHAPTER_SKIPPER)[1])
            if any(re.search(r"(?<![a-z0-9])" + w.replace("%d*", "[0-9]*") + r"(?![a-z0-9])",
                             title.lower()) for w in words):
                return kind
        return None

    def test_chapter_titles(self):
        # chapter titles from reanime's One Piece files (episodes 1 and 1151), and others
        for kind, titles in {
            "opening": ["Opening Song", "OP", "OP2", "Opening Credits"],
            "intro": ["Intro"],
            "ending": ["To Be Continued & Ending Song", "Credits", "On The Next episode",
                       "ED", "End Credits", "Preview", "Next Time"],
            None: ["Gol D Roger Speech", "Part A", "Episode Name", "Eyecatch", "Part C",
                   "Scene 1", "Scene 5", "Introduction", "Prologue", "Edward", "Stop"],
        }.items():
            for title in titles:
                self.assertEqual(self.kind(title), kind, title)

    # real mpv. episodes are built from the fixture with mpv's edl://, one segment (and
    # so one chapter) per part. REANIME_TEST_MPV makes these mandatory (CI's install stage)

    def mpv(self):
        forced = os.environ.get("REANIME_TEST_MPV")
        cmd = [forced] if forced else reanime.find_mpv("mpv")
        if not cmd:
            self.skipTest("mpv not installed")
        return [*cmd, "--no-config", "--vo=null", "--ao=null", "--really-quiet"]

    def edl(self, *parts):
        """an episode of (chapter title, seconds) parts. mpv drops an edl's last segment
        from this fixture, so a short tail goes after them, named like a preview so
        the skipper never stops there."""
        fixture = os.path.join(ROOT, "tests", "fixtures", "partial-subs.mkv")
        source = f"%{len(fixture.encode())}%{fixture}"
        return "edl://" + ";".join(f"{source},start=0,length={n},title={t}"
                                   for t, n in parts + (("Preview", 0.5),))

    def said(self, log, script="skip_chapters"):
        # (some mpv versions end each log line with a space)
        return re.findall(rf"\[i\]\[{script}\] (.*?) *$", read(log), re.M)

    def test_real_mpv_skips_the_opening_and_the_ending(self):
        layouts = {1: ("Gol D Roger Speech", "Opening Song", "Part A",
                       "To Be Continued & Ending Song", "On The Next episode"),
                   2: ("Scene 1", "Intro", "Scene 3", "Credits", "Scene 5")}

        def edl(anime, ep, lang, verbose=True):
            # 1s story, 1.5s opening, 1.5s story, 1.5s ending, 1.5s preview. the story
            # parts outlast the moment after a seek or a file start, when the skipper
            # takes a chapter change for a jump
            return self.edl(*zip(layouts[ep["number"]], (1, 1.5, 1.5, 1.5, 1.5))), {}

        ending = []
        real_get = reanime.MpvIpc.get

        def spy(ipc, prop):
            got = real_get(ipc, prop)
            if prop == "user-data/reanime/ending" and got:
                ending.append(got)
            return got
        log = os.path.join(CACHE.name, "skip.log")
        with mock.patch.object(reanime, "resolve", edl), \
                mock.patch.object(reanime.MpvIpc, "get", spy), \
                mock.patch.object(reanime, "POLL", 0.1), \
                contextlib.redirect_stderr(io.StringIO()):
            got = reanime.play_session([*self.mpv(), f"--log-file={log}", *reanime.skip_args()],
                                       Session.anime, Session.eps, 0, "dub", stop=1)
        # 1: opening skipped, and after the ending only a preview: on to 2. 2: its
        # "Intro" is the opening (no chapter is named one), and its "Credits" skip to
        # "Scene 5", the chapter after them that isn't an ending or a preview
        said = self.said(log)
        self.assertEqual(said[:5], ["skipped Opening Song", "skipped To Be Continued & Ending Song",
                                    "ending: next episode", "skipped Intro", "skipped Credits"])
        self.assertLessEqual(set(said[5:]), {"skipped Preview"})     # if mpv plays the tail
        self.assertEqual(got, (1, None, "done"))
        self.assertIn(4.0, ending)          # where the ending starts, read over the IPC

    def test_real_mpv_rewind_pgdn_and_the_skip_key(self):
        # episode 1 is laid out like Jujutsu Kaisen: the story before the opening is
        # "Intro", and "Juju Stroll" (an extra) sits between the ending and the preview.
        # a driver script does what a viewer would: rewind to the start once the opening
        # is skipped, then PgDn back to the opening, then Tab. episode 2 has no chapters,
        # and gets Tab once
        driver = os.path.join(CACHE.name, "driver.lua")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write("""
local parts, pressed = 0, false
mp.add_timeout(20, function() mp.command("quit") end)        -- the skipper broke
mp.observe_property("chapter", "number", function(_, ch)
    local c = ch and (mp.get_property_native("chapter-list") or {})[ch + 1]
    if not c then return end
    if c.title == "Part A" then
        parts = parts + 1
        if parts == 1 then mp.commandv("seek", "0", "absolute") end     -- rewind
        if parts == 2 then mp.commandv("add", "chapter", "-1") end      -- PgDn
    elseif c.title == "Opening" and parts == 2 then
        mp.add_timeout(0.5, function() mp.commandv("script-binding", "skip_chapters/skip") end)
    end
end)
mp.register_event("file-loaded", function()
    if mp.get_property_number("chapter-list/count", 0) > 0 then return end
    mp.add_timeout(0.5, function()
        pressed = true
        mp.commandv("script-binding", "skip_chapters/skip")
    end)
end)
mp.register_event("playback-restart", function()
    if not pressed then return end
    mp.msg.info(string.format("at %.1f", mp.get_property_number("time-pos", -1)))
    mp.command("quit")
end)
""")
        fixture = os.path.join(ROOT, "tests", "fixtures", "partial-subs.mkv")
        long = "edl://!no_chapters" + f";%{len(fixture.encode())}%{fixture}" * 60     # 120s
        log = os.path.join(CACHE.name, "keys.log")
        subprocess.run([*self.mpv(), f"--log-file={log}", *reanime.skip_args(),
                        f"--script={driver}",
                        self.edl(("Intro", 1), ("Opening", 2), ("Part A", 3), ("Ending", 1.5),
                                 ("Juju Stroll", 1), ("Preview", 1)),
                        long], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        self.assertEqual(self.said(log), [
            "skipped Opening",          # "Intro" played: this file names its opening
            "skipped Opening",          # rewound before it: skipped again
            "skip key: Opening",        # PgDn brought it back, Tab skipped it
            "skipped Ending",           # to the extra after it, which plays
            "skipped Preview",
            "ending: next episode",
            "skip key: 85s",            # no chapters: an opening's worth
        ])
        at = float(self.said(log, "driver")[0].split()[1])
        self.assertTrue(85 <= at < 88, at)


class Setup(unittest.TestCase):
    def patches(self, found, managers=("apt", "flatpak"), tty=False, answer=""):
        """mpv missing until a run of a manager listed in `found` completes."""
        state = {"installed": False, "ran": []}

        def run(cmd, **kw):
            state["ran"].append(cmd)
            if any(m in cmd for m in found):
                state["installed"] = True
            return subprocess.CompletedProcess(cmd, 0)

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(reanime, "ANDROID", False))
        stack.enter_context(mock.patch.object(reanime, "_os_key", return_value="linux"))
        stack.enter_context(mock.patch.object(
            reanime, "find_mpv", lambda mpv: ["/usr/bin/mpv"] if state["installed"] else None))
        stack.enter_context(mock.patch.object(
            reanime.shutil, "which", lambda c: f"/usr/bin/{c}" if c in managers + ("sudo",) else None))
        stack.enter_context(mock.patch.object(reanime.os, "geteuid", create=True, return_value=1000))
        stack.enter_context(mock.patch.object(reanime.subprocess, "run", run))
        stack.enter_context(mock.patch.object(reanime, "interactive_ok", return_value=tty))
        stack.enter_context(mock.patch("builtins.input", return_value=answer))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        return stack, state

    def test_noninteractive_without_yes_explains(self):
        stack, state = self.patches(found=())
        with stack, self.assertRaises(SystemExit) as cm:
            reanime.ensure_player("mpv")
        self.assertIn("sudo apt-get update && sudo apt-get install -y mpv", str(cm.exception))
        self.assertIn("--setup --yes", str(cm.exception))
        self.assertEqual(state["ran"], [])

    def test_yes_installs_and_falls_back_to_next_manager(self):
        stack, state = self.patches(found=("io.mpv.Mpv",))      # apt "fails", flatpak works
        with stack:
            self.assertEqual(reanime.ensure_player("mpv", assume_yes=True), ["/usr/bin/mpv"])
        self.assertEqual(state["ran"][:2], [["sudo", "apt-get", "update"],
                                            ["sudo", "apt-get", "install", "-y", "mpv"]])
        self.assertEqual(state["ran"][-1][:3], ["flatpak", "install", "--user"])

    def test_prompt_yes_and_no(self):
        stack, _ = self.patches(found=("mpv",), tty=True, answer="")
        with stack:
            self.assertEqual(reanime.ensure_player("mpv"), ["/usr/bin/mpv"])
        stack, state = self.patches(found=("mpv",), tty=True, answer="n")
        with stack, self.assertRaises(SystemExit):
            reanime.ensure_player("mpv")
        self.assertEqual(state["ran"], [])

    def test_explicit_missing_path_and_no_manager(self):
        stack, _ = self.patches(found=())
        with stack, self.assertRaisesRegex(SystemExit, "mpv not found at /opt/x/mpv"):
            reanime.ensure_player("/opt/x/mpv")
        stack, _ = self.patches(found=(), managers=())
        with stack, self.assertRaisesRegex(SystemExit, "no supported package manager"):
            reanime.ensure_player("mpv", assume_yes=True)

    def test_sudo_is_dropped_for_root_or_when_missing(self):
        cmd = ["sudo", "apt-get", "install", "-y", "mpv"]
        with mock.patch.object(reanime.os, "geteuid", create=True, return_value=0), \
                mock.patch.object(reanime.shutil, "which", return_value="/usr/bin/sudo"):
            self.assertEqual(reanime._sudo(cmd), cmd[1:])
        with mock.patch.object(reanime.os, "geteuid", create=True, return_value=1000), \
                mock.patch.object(reanime.shutil, "which", return_value="/usr/bin/sudo"):
            self.assertEqual(reanime._sudo(cmd), cmd)
        with mock.patch.object(reanime.os, "geteuid", create=True, return_value=1000), \
                mock.patch.object(reanime.shutil, "which", return_value=None):
            self.assertEqual(reanime._sudo(cmd), cmd[1:])

    def test_find_mpv_in_install_dir_and_flatpak(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, "mpv.exe" if os.name == "nt" else "mpv")
            open(exe, "w").close()
            with mock.patch.object(reanime.shutil, "which", return_value=None), \
                    mock.patch.dict(reanime.MPV_DIRS, {reanime._os_key(): [tmp]}):
                self.assertEqual(reanime.find_mpv("mpv"), [exe])
        with mock.patch.object(reanime.shutil, "which",
                               lambda c: "/usr/bin/flatpak" if c == "flatpak" else None), \
                mock.patch.dict(reanime.MPV_DIRS, {reanime._os_key(): []}), \
                mock.patch.object(reanime.glob, "glob", return_value=[]), \
                mock.patch.object(reanime.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(reanime.find_mpv("mpv"), ["flatpak", "run", "io.mpv.Mpv"])
        with mock.patch.object(reanime.shutil, "which", return_value=None):
            self.assertIsNone(reanime.find_mpv("/definitely/not/here/mpv"))


class Update(unittest.TestCase):
    """--update reinstalls from main into the environment reanime runs from."""

    def update(self, layout="site", venv=True, uv="/bin/uv", new="9.0.0", fail=False, pkg=None):
        """`reanime --update` with reanime.py in site-packages ("site"), a git checkout
        ("git") or on its own ("file"). -> (exit code, output, commands run)"""
        ran = []

        def run(argv, **kw):
            ran.append(argv)
            if "-c" in argv:                            # asking the new reanime its version
                return subprocess.CompletedProcess(argv, 0, stdout=new + "\n")
            return subprocess.CompletedProcess(argv, 1 if fail else 0)
        with tempfile.TemporaryDirectory() as tmp:
            if layout == "git":
                os.mkdir(os.path.join(tmp, ".git"))
            site = tmp if layout == "site" else os.path.join(tmp, "site-packages")
            with mock.patch.object(reanime, "__file__", os.path.join(tmp, "reanime.py")), \
                    mock.patch.object(reanime.sysconfig, "get_paths",
                                      lambda: {"purelib": site, "platlib": site}), \
                    mock.patch.object(reanime.sys, "prefix", "/venv" if venv else "/usr"), \
                    mock.patch.object(reanime.sys, "base_prefix", "/usr"), \
                    mock.patch.object(reanime.shutil, "which", lambda c: uv if c == "uv" else None), \
                    mock.patch.object(reanime.subprocess, "run", run), \
                    mock.patch.dict(os.environ):
                os.environ.pop("REANIME_PKG", None)
                if pkg:
                    os.environ["REANIME_PKG"] = pkg
                code, out, err = run_main("--update")
        return code, out + err, ran

    def test_a_uv_tool_or_pipx_venv_is_reinstalled_in_place(self):
        # in place with uv pip: `uv tool install --force` deletes the venv first, and
        # Windows can't delete the python.exe running reanime
        code, out, ran = self.update()
        self.assertEqual(ran[0], ["/bin/uv", "pip", "install", "--python", sys.executable,
                                  "--reinstall", reanime.PACKAGE])
        self.assertIn("-I", ran[1])                     # not a reanime.py in this folder
        self.assertIn(f"updated reanime {reanime.__version__} -> 9.0.0", out)
        self.assertFalse(code)
        self.assertIn("already the newest", self.update(new=reanime.__version__)[1])

    def test_termux_or_no_uv_uses_pip_like_the_installer(self):
        pip = [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall"]
        self.assertEqual(self.update(venv=False)[2][0], pip + [reanime.PACKAGE])
        self.assertEqual(self.update(uv=None)[2][0], pip + [reanime.PACKAGE])
        self.assertEqual(self.update(pkg="/tmp/reanime.whl")[2][0][-1], "/tmp/reanime.whl")

    def test_a_failed_update_says_to_run_the_installer(self):
        code, _, ran = self.update(fail=True)
        self.assertIn("update failed", code)
        self.assertIn(reanime.INSTALLER, code)
        self.assertEqual(len(ran), 1)
        self.assertIn("doesn't start", self.update(new="")[0])

    def test_a_git_checkout_or_a_single_file_is_left_alone(self):
        code, _, ran = self.update("git")
        self.assertIn("git pull", code)
        self.assertEqual(ran, [])
        code, _, ran = self.update("file")
        self.assertIn("single file", code)
        self.assertIn(reanime.INSTALLER, code)
        self.assertEqual(ran, [])


class Android(unittest.TestCase):
    def test_no_mpv_needed_and_setup_points_to_app(self):
        with mock.patch.object(reanime, "ANDROID", True):
            self.assertIsNone(reanime.ensure_player("mpv"))
            code, out, _ = run_main("--setup")
        self.assertEqual(code, 0)
        self.assertIn("is.xyz.mpv", out)

    def test_intent_and_errors(self):
        seen = []
        ok = subprocess.CompletedProcess([], 0, "Starting: Intent", "")
        with mock.patch.object(reanime.subprocess, "run", lambda c, **k: seen.append(c) or ok):
            reanime.android_open("https://x/ep.mkv")
        self.assertEqual(seen[0], ["am", "start", "--user", "0", "-a", "android.intent.action.VIEW",
                                   "-d", "https://x/ep.mkv", "-n", "is.xyz.mpv/.MPVActivity"])
        bad = subprocess.CompletedProcess([], 0, "", "Error: Activity class does not exist")
        with mock.patch.object(reanime.subprocess, "run", return_value=bad), \
                self.assertRaisesRegex(SystemExit, "mpv-android"):
            reanime.android_open("https://x")
        with mock.patch.object(reanime.subprocess, "run", side_effect=FileNotFoundError), \
                self.assertRaisesRegex(SystemExit, "termux-am"):
            reanime.android_open("https://x")


if __name__ == "__main__":
    unittest.main()
