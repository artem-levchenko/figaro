"""The CLI's own helpers (cli_extras.py, inspect_text.py, bridge_idle.py):
links in arguments, starting the bridge, inspect, shot,
link, image, --lib, exec --shot, --image, and the bridge leaving: when idle, and with its
plugin sockets closed at once.

A fake bridge answers the CLI here; the plugin's side is in
tests/commands.test.js. Three tests start a real bridge on a free port: with
start-bridge.sh, with start-bridge.ps1 on Windows, and directly, to stop it
with SIGTERM.

    pytest tests/test_cli.py
"""
import asyncio
import base64
import http.client
import http.server
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

import bridge
import figaro
import cli_extras
from conftest import FIGMA_UA
import bridge_idle
import inspect_text
import result_text
from test_bridge import FakePlugin, make_client, run

ROOT = Path(__file__).resolve().parent.parent
KEY = "AbCdEfGhIjKlMnOpQrStUv"
URL = f"https://www.figma.com/design/{KEY}/Sandbox-5-?node-id=1-11&t=x"


def png(w, h, color=(200, 30, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def shot(node="1:2", data=b"", fmt="PNG", **extra):
    return {"kind": "shot", "id": node, "name": "Card", "type": "FRAME", "file": "Sandbox 5$",
            "fileKey": KEY, "format": fmt, "scale": 2, "w": 10, "h": 10, "size": len(data),
            "data": base64.b64encode(data).decode(), **extra}


class FakeBridge:
    """Stands in for figaro._request: records each call, answers with
    `reply(payload)`."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, path, payload=None, timeout=65):
        self.calls.append(payload)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        body = reply(payload) if callable(reply) else reply
        return (200 if body.get("ok", True) else 500), body


def cli(monkeypatch, capsys, *argv, bridge_reply=None):
    fake = bridge_reply if isinstance(bridge_reply, FakeBridge) else FakeBridge(
        bridge_reply or {"ok": True, "value": 1})
    monkeypatch.setattr(figaro, "_request", fake)
    monkeypatch.setattr(sys, "argv", ["figaro", *argv])
    with pytest.raises(SystemExit) as done:
        figaro.main()
    out = capsys.readouterr()
    return done.value.code, out.out, out.err, fake.calls


@pytest.fixture(autouse=True)
def no_autostart(monkeypatch, tmp_path):
    monkeypatch.setenv("FIGARO_AUTOSTART", "0")
    monkeypatch.setattr(cli_extras, "_started", False)
    monkeypatch.setattr(cli_extras, "_verdicts", {})
    monkeypatch.setattr(cli_extras, "SHOTS", tmp_path / "shots")


# ─── links in arguments ──────────────────────────────────────────────────

def args_of(*argv):
    args = figaro.build_parser().parse_args(list(argv))
    if args.cmd == "exec":
        cli_extras.fix_exec_args(args)
    return args, cli_extras.target_and_ids(args)


def test_a_layer_link_gives_the_id_and_the_file():
    # The link itself goes as the target: the bridge resolves it, and if the
    # file is not open, tells to open this very link.
    args, target = args_of("tree", URL)
    assert (args.node_id, target) == ("1:11", URL)
    args, target = args_of("tree", "1-11")
    assert (args.node_id, target) == ("1:11", None)
    args, target = args_of("rm", URL, "I5-6;7-8", "sel")
    assert (args.node_ids, target) == (["1:11", "I5:6;7:8", "sel"], URL)


def test_t_takes_a_link_and_a_name_stays_a_name():
    assert args_of("sel", "-T", URL)[1] == URL
    assert args_of("tree", URL, "-T", "Sandbox")[1] == "Sandbox"
    assert args_of("tree", URL, "-T", KEY)[1] == KEY
    assert args_of("tree", URL, "-T", f"https://www.figma.com/design/{KEY}/X")[1] == \
        f"https://www.figma.com/design/{KEY}/X"


def test_links_into_two_files_are_refused():
    other = f"https://www.figma.com/design/{'Q' * 22}/X?node-id=2-3"
    with pytest.raises(SystemExit, match="different files"):
        args_of("rm", URL, other)
    with pytest.raises(SystemExit, match="the layer is from file"):
        args_of("tree", URL, "-T", "Q" * 22)
    with pytest.raises(SystemExit, match="the layer is from file"):
        args_of("tree", URL, "-T", f"https://www.figma.com/design/{'Q' * 22}/X")
    with pytest.raises(SystemExit, match="has no node-id"):
        args_of("tree", f"https://www.figma.com/design/{KEY}/UI")


def test_exec_shot_flag_and_code():
    args, target = args_of("exec", "--shot", "return 1")
    assert (args.code, args.shot) == ("return 1", "auto")
    args, target = args_of("exec", "return 1", "--shot")
    assert (args.code, args.shot) == ("return 1", "auto")
    args, target = args_of("exec", "--shot", URL, "return 1")
    assert (args.code, args.shot, target) == ("return 1", "1:11", URL)
    args, _ = args_of("exec", "--shot", "sel", "return 1")
    assert (args.code, args.shot) == ("return 1", "sel")
    args, _ = args_of("exec", "return 1")
    assert args.shot is None


# ─── the commands ────────────────────────────────────────────────────────

INSPECT = {
    "file": "Sandbox 5$", "fileKey": KEY, "url": URL, "nodes": 3,
    "node": {
        "id": "1:11", "name": "Card", "type": "FRAME", "w": 320, "h": 200,
        "layout": {"mode": "VERTICAL", "gap": 12, "main": "MIN", "cross": "CENTER",
                   "pad": [24, 16, 24, 16]},
        "sizing": "FIXED/HUG", "radius": 12, "styles": {"fill": "Surface/Card"},
        "fills": [{"type": "SOLID", "color": "#ffffff", "var": "surface/card"}],
        "children": [
            {"id": "1:12", "name": "Title", "type": "TEXT", "w": 288, "h": 24, "x": 16, "y": 24,
             "sizing": "FILL/HUG", "text": "Hello world", "textStyle": "Heading/H3",
             "font": {"family": "Inter Semi Bold", "size": 20, "lineHeight": "24px",
                      "letterSpacing": "0px", "align": "LEFT"}, "autoResize": "HEIGHT",
             "segments": [{"text": "Hello ", "font": "Inter Semi Bold", "size": 20},
                          {"text": "world", "font": "Inter Bold", "size": 20,
                           "fills": [{"type": "SOLID", "color": "#ff0000"}]}]},
            {"id": "1:13", "name": "Button", "type": "INSTANCE", "w": 120, "h": 40, "x": 16, "y": 60,
             "component": "Button / Size=M", "props": {"Label#12:0": "Go", "Size": "M"},
             "refs": {"visible": "Show#3:0"}},
        ],
        "hiddenKids": 1,
    },
    "components": {"Button / Size=M": {"id": "1:7177", "key": "c" * 40, "remote": True, "set": "Button",
                                       "setKey": "s" * 40}},
    "styles": {"Surface/Card": {"id": "S:" + "f" * 40 + ",2739:8", "key": "f" * 40, "type": "PAINT",
                                "remote": True}},
    "variables": {"surface/card": {"id": "VariableID:3:4", "key": "v" * 40, "type": "COLOR",
                                   "collection": "Tokens", "remote": False}},
    "collections": {"Tokens": {"key": "k" * 40, "remote": False, "modes": ["Light", "Dark"]}},
}


def test_inspect_prints_compact_text_from_a_quick_read(monkeypatch, capsys):
    rc, out, err, calls = cli(monkeypatch, capsys, "inspect", URL,
                              bridge_reply={"ok": True, "value": INSPECT, "elapsed_ms": 9})
    assert rc == 0
    (payload,) = calls
    assert payload["quick"] is True and payload["target"] == URL
    assert 'h.inspect(await h.resolve("1:11"), {"depth": 8, "hidden": false})' in payload["code"]
    lines = out.splitlines()
    assert lines[0] == '"Card" FRAME 1:11 in "Sandbox 5$" · 3 layers'
    assert lines[1] == URL
    assert any(line.startswith("Card [FRAME] 1:11 320×200 · ↓ gap 12 align min/center pad 24 16")
               and "fixed×hug" in line and "'Surface/Card' #ffffff (surface/card)" in line
               and "+1 hidden" in line for line in lines)
    title = next(line for line in lines if "Title [TEXT]" in line)
    assert "@" not in title  # in an auto-layout flow the position says nothing
    assert '"Hello world"' in title and "Inter Semi Bold 20/24" in title and "style 'Heading/H3'" in title
    assert any("┆" in line and '"world" Inter Bold 20' in line and "#ff0000" in line for line in lines)
    assert any("⟐ Button / Size=M" in line and 'Label="Go"' in line and "← visible Show#3:0" in line
               for line in lines)
    assert any(line.startswith("in this file use the id:") for line in lines)
    assert f"  Button / Size=M — id 1:7177 · key {'c' * 40} · set 'Button' {'s' * 40} · library" in lines
    assert f"  Surface/Card PAINT — id S:{'f' * 40},2739:8 · key {'f' * 40} · library" in lines
    assert f"  surface/card COLOR · Tokens — id VariableID:3:4 · key {'v' * 40} · local" in lines
    assert f"  Tokens: Light, Dark — key {'k' * 40} · local" in lines


def test_inspect_json_and_file(monkeypatch, capsys, tmp_path):
    reply = {"ok": True, "value": INSPECT}
    rc, out, err, calls = cli(monkeypatch, capsys, "inspect", "1:11", "--json", "--depth", "2",
                              "--hidden", bridge_reply=reply)
    assert json.loads(out) == INSPECT
    assert '{"depth": 2, "hidden": true}' in calls[0]["code"]
    dest = tmp_path / "out" / "card.json"
    rc, out, err, calls = cli(monkeypatch, capsys, "inspect", "1:11", "-o", str(dest), bridge_reply=reply)
    assert json.loads(dest.read_text()) == INSPECT
    summary = json.loads(out)
    assert summary["out"] == str(dest.resolve()) and summary["node"] == "1:11" and summary["nodes"] == 3


def test_inspect_cuts_a_long_listing():
    many = dict(INSPECT, node=dict(INSPECT["node"], children=[
        {"id": f"2:{i}", "name": f"Row {i}", "type": "FRAME", "w": 1, "h": 1} for i in range(50)]))
    text = inspect_text.render(many, max_lines=10)
    assert "… 41 more lines" in text
    assert [inspect_text._count(n, "layer") for n in (0, 1, 2, 21)] == [
        "0 layers", "1 layer", "2 layers", "21 layers"]
    assert [inspect_text._count(n, "more child", "more children") for n in (1, 3)] == [
        "1 more child", "3 more children"]


def test_shot_saves_a_tall_picture_in_parts(monkeypatch, capsys, tmp_path):
    data = png(40, 4000)
    rc, out, err, calls = cli(monkeypatch, capsys, "shot", "1:2", "-o", str(tmp_path),
                              bridge_reply={"ok": True, "value": [shot(data=data)]})
    assert rc == 0 and calls[0]["quick"] is True
    parts = sorted(tmp_path.glob("1-2-p*.png"))
    assert [p.name for p in parts] == ["1-2-p1.png", "1-2-p2.png", "1-2-p3.png"]
    from PIL import Image
    heights = [Image.open(p).size for p in parts]
    assert heights == [(40, 1334), (40, 1334), (40, 1332)]
    assert out.count(str(tmp_path)) == 3 and "part 1/3, y 0–1334" in out


def test_shot_default_folder_and_old_parts_go(monkeypatch, capsys):
    cli_extras.save_shots([shot(node="I1:2;3:4", data=png(10, 4000))])
    folder = cli_extras.SHOTS / KEY
    assert len(list(folder.glob("I1-2_3-4-p*.png"))) == 3
    lines = cli_extras.save_shots([shot(node="I1:2;3:4", data=png(10, 100))])
    assert [p.name for p in folder.iterdir()] == ["I1-2_3-4.png"]
    assert lines[0].startswith(str(folder / "I1-2_3-4.png") + "  10×100")


def test_shot_without_pillow_keeps_one_file(monkeypatch, tmp_path):
    data = png(10, 4000)
    monkeypatch.setitem(sys.modules, "PIL", None)
    lines = cli_extras.save_shots([shot(data=data)], tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["1-2.png"]
    assert "Pillow" in lines[-1]


def test_svg_and_jpg_shots(tmp_path):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (30, 20)).save(buf, "JPEG")
    assert cli_extras.image_size(buf.getvalue(), "jpg") == (30, 20)
    assert cli_extras.image_size(png(7, 9), "png") == (7, 9)
    lines = cli_extras.save_shots([shot(data=b"<svg/>", fmt="SVG")], tmp_path)
    assert (tmp_path / "1-2.svg").read_bytes() == b"<svg/>" and lines


def test_link_prints_links(monkeypatch, capsys):
    value = [{"id": "1:2", "name": "Card", "url": "https://www.figma.com/design/K/F?node-id=1-2"},
             {"id": "1:3", "name": "Local", "url": None}]
    rc, out, err, calls = cli(monkeypatch, capsys, "link", "sel", bridge_reply={
        "ok": True, "value": value, "notice": "the file changed meanwhile", "logs": ["a line"]})
    assert out.splitlines() == ['https://www.figma.com/design/K/F?node-id=1-2  "Card"',
                                '(the file has no key, so no link)  "Local"']
    assert calls[0]["quick"] is True
    assert "the file changed meanwhile" in err and "log: a line" in err


def test_shot_leaves_the_callers_other_files_alone(tmp_path):
    for name in ("1-2.md", "1-2.txt", "1-2-notes.png", "1-2-p1.png", "1-2-p2.jpg"):
        (tmp_path / name).write_text("x")
    cli_extras.save_shots([shot(data=png(10, 100))], tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["1-2-notes.png", "1-2.md", "1-2.png", "1-2.txt"]


def test_shot_into_a_file_or_a_folder_it_cant_write_says_so(monkeypatch, capsys, tmp_path):
    taken = tmp_path / "a-file"
    taken.write_text("x")
    rc, out, err, calls = cli(monkeypatch, capsys, "shot", "1:2", "-o", str(taken),
                              bridge_reply={"ok": True, "value": [shot(data=png(10, 10))]})
    assert "can't save the pictures" in str(rc)
    rc, out, err, calls = cli(monkeypatch, capsys, "inspect", "1:2", "-o", str(taken / "x.json"),
                              bridge_reply={"ok": True, "value": {"node": {"id": "1:2"}}})
    assert "can't write" in str(rc)


@pytest.mark.parametrize("argv", [["shot", "1:2", "--tile", "0"], ["shot", "1:2", "--tile", "-5"],
                                  ["tree", "page", "--timeout", "-5"], ["exec", "-t", "0", "return 1"]])
def test_numbers_below_one_are_refused_at_once(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["figaro", *argv])
    with pytest.raises(SystemExit) as done:
        figaro.main()
    assert done.value.code == 2 and "must be 1 or more" in capsys.readouterr().err


def test_exec_from_a_file_that_isnt_there_or_isnt_text(monkeypatch, capsys, tmp_path):
    rc, out, err, calls = cli(monkeypatch, capsys, "exec", "-f", str(tmp_path / "missing.js"))
    assert rc == 2 and "can't read" in err and not calls
    rc, out, err, calls = cli(monkeypatch, capsys, "exec", "-f", str(tmp_path))
    assert rc == 2 and "can't read" in err and not calls
    (tmp_path / "bad.js").write_bytes(b"return '\xff\xfe'")
    rc, out, err, calls = cli(monkeypatch, capsys, "exec", "-f", str(tmp_path / "bad.js"))
    assert rc == 2 and "not UTF-8" in err and not calls
    (tmp_path / "bom.js").write_bytes("\ufeffreturn 1".encode())
    rc, out, err, calls = cli(monkeypatch, capsys, "exec", "-f", str(tmp_path / "bom.js"))
    assert rc == 0 and calls[0]["code"] == "return 1"


def test_a_lib_with_a_bom_keeps_its_first_line(tmp_path):
    lib = tmp_path / "lib.js"
    lib.write_bytes("\ufefffunction a() { return 1 }".encode())
    assert cli_extras.read_libs([str(lib)])[0]["code"] == "function a() { return 1 }"


def test_a_port_that_isnt_a_number(monkeypatch):
    monkeypatch.setenv("FIGARO_PORT", "eighty")
    with pytest.raises(SystemExit, match="not a port number"):
        figaro._env_port()
    monkeypatch.setenv("FIGARO_PORT", "")
    assert figaro._env_port() == 8788


def test_a_token_file_that_isnt_text_is_no_token(monkeypatch, tmp_path):
    bad = tmp_path / "token"
    bad.write_bytes(b"\xff\xfe\x00")
    monkeypatch.setenv("FIGARO_TOKEN_FILE", str(bad))
    import figaro_token
    assert figaro_token.read_token(8788) is None


def test_undo_says_how_many_steps_and_what_is_left(monkeypatch, capsys):
    value = {"undone": {"agent": None, "ago": "5 s ago", "changes": "+3"}, "left": 0, "steps": 3,
             "warning": "not everything was undone: 1 layers of this script are left — …"}
    rc, out, err, _ = cli(monkeypatch, capsys, "undo", "-T", KEY, bridge_reply={"ok": True, "value": value})
    assert rc == 0
    assert out.strip() == "undone: +3 (3 Cmd+Z steps) — a script without -A, 5 s ago; can still undo: 0"
    assert "⚠ not everything was undone" in err


def test_done_says_so_in_the_plugin_window(monkeypatch, capsys):
    reply = {"ok": True, "file": "Sandbox 5$", "text": "Check the card"}
    rc, out, err, calls = cli(monkeypatch, capsys, "done", "Check the card", "-T", KEY, "-A", "designer",
                              bridge_reply=reply)
    assert rc == 0 and calls == [{"text": "Check the card", "target": KEY, "agent": "designer"}]
    assert out.strip() == 'the Figaro window in "Sandbox 5$" shows you done, with your note: Check the card'
    assert "until your next script" in err
    # `wait "<note>"`, its name while 1.1.0 was being made, still says done
    monkeypatch.setattr(figaro, "AGENT", None)  # -A above stays in the module
    rc, out, err, calls = cli(monkeypatch, capsys, "wait", "Check it", "-T", KEY,
                              bridge_reply={"ok": True, "file": "Sandbox 5$", "text": "Check it"})
    assert rc == 0 and calls == [{"text": "Check it", "target": KEY}]
    assert out.strip() == 'the Figaro window in "Sandbox 5$" shows you done, with your note: Check it'
    assert "is `figaro done" in err
    old = {"ok": False, "error": "the plugin in \"Sandbox 5$\" runs an older build that can't show it"}
    rc, _, err, _ = cli(monkeypatch, capsys, "done", "Check the card", bridge_reply=old)
    assert rc == 1 and "older build" in err


# ─── figaro wait ──────────────────────────────────────────────────────────

def status_of(*files, abandoned=(), lost=None):
    """An answer of GET /status, as a bridge of 1.0.0 on gives it (`lost` and
    a file's `stuck` only from 1.3.0)."""
    body = {"version": "1.3.0", "plugin_connected": bool(files), "files": list(files), "pending": 0,
            "abandoned": list(abandoned)}
    if lost is not None:
        body["lost"] = lost
    return body


def window(name="Sandbox 5$", key=KEY, conn="c0ffee01", doc=KEY, running=None, **extra):
    return dict({"name": name, "fileKey": key, "conn": conn, "doc": doc, "plugin": "x", "outdated": False,
                 "queue": {"running": running, "waiting": []}}, **extra)


@pytest.fixture
def quick_wait(monkeypatch):
    """figaro wait on a clock that its own sleeps move."""
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds
    monkeypatch.setattr(figaro, "time", Clock())


def test_wait_returns_once_the_file_is_ready(monkeypatch, capsys, quick_wait):
    free = status_of(window())
    rc, out, err, calls = cli(monkeypatch, capsys, "wait", "-T", URL, bridge_reply=free)
    assert rc == 0 and len(calls) == 1 and err == ""
    assert out.strip() == 'ready: connected, nothing stuck in "Sandbox 5$"'
    # a link without -T is the file to wait for, not a note for done
    two = status_of(window(), window(name="Kit", key="KEY2", conn="c0ffee02", doc="KEY2"))
    rc, out, err, calls = cli(monkeypatch, capsys, "wait", URL, bridge_reply=two)
    assert rc == 0 and len(calls) == 1 and err == ""
    assert out.strip() == 'ready: connected, nothing stuck in "Sandbox 5$"'
    # other agents at work in the file, within their time: the queue takes care of them
    busy = status_of(window(running={"agent": "writer", "for_s": 80},
                            queue={"running": {"agent": "writer", "for_s": 80}, "waiting": ["a", "b", "c"]}))
    rc, out, err, calls = cli(monkeypatch, capsys, "wait", "-T", URL, bridge_reply=busy)
    assert rc == 0 and len(calls) == 1 and out.startswith("ready:")
    # a script that ran out of time, until it ends
    late = status_of(window(), abandoned=[{"rid": "r1", "conn": "c0ffee01", "age_s": 70}])
    rc, out, err, calls = cli(monkeypatch, capsys, "wait", "-T", "sandbox",
                              bridge_reply=FakeBridge(late, late, busy))
    assert rc == 0 and len(calls) == 3 and out.startswith("ready:")
    assert err.strip() == '  waiting: a script that ran out of time may still be running in "Sandbox 5$" (it began 70 s ago)'.strip()


def test_wait_gives_up_with_what_to_tell_the_user(monkeypatch, capsys, quick_wait):
    stuck = status_of(window(stuck={"agent": "designer", "for_s": 40}),
                      abandoned=[{"rid": "r1", "conn": "c0ffee01", "age_s": 100}])
    rc, out, err, _ = cli(monkeypatch, capsys, "wait", "-T", KEY, "--timeout", "5", bridge_reply=stuck)
    assert rc == 1 and out == ""
    assert "not ready after 5 s: Figma isn't answering in \"Sandbox 5$\": a script of designer" in err
    assert ("tell the user: Figma isn't answering in \"Sandbox 5$\": reload the file in Figma (close its "
            "tab and open it again, or quit Figma if the tab won't close), then run Figaro in it") in err
    # an older bridge says only that the script ran out of time: the same advice
    late = status_of(window(), abandoned=[{"rid": "r1", "conn": "c0ffee01", "age_s": 100}])
    rc, _, err, _ = cli(monkeypatch, capsys, "wait", "-T", KEY, "-t", "5", bridge_reply=late)
    assert rc == 1 and "may still be running" in err and "reload the file in Figma" in err
    # not connected: open it and run Figaro there
    rc, _, err, _ = cli(monkeypatch, capsys, "wait", "-T", KEY, "-t", "5", bridge_reply=status_of())
    assert rc == 1 and f"'{KEY}' is not connected" in err
    assert f"tell the user: open https://www.figma.com/design/{KEY} in Figma and run Plugins" in err
    # its window closed while a script ran there
    gone = status_of(lost=[{"name": "Sandbox 5$", "fileKey": KEY, "doc": KEY, "agent": "designer", "ago_s": 20}])
    rc, _, err, _ = cli(monkeypatch, capsys, "wait", "-T", URL, "-t", "5", bridge_reply=gone)
    assert rc == 1 and "Figaro's window in \"Sandbox 5$\" closed while a script of designer ran there" in err
    assert "reload the file in Figma" in err


def test_wait_wont_wait_for_what_waiting_cant_fix(monkeypatch, capsys, quick_wait):
    two = status_of(window(), window(name="Kit", key="KEY2", conn="c0ffee02", doc="KEY2"))
    rc, _, err, calls = cli(monkeypatch, capsys, "wait", bridge_reply=two)
    assert rc == 2 and len(calls) == 1
    assert "2 files connected — name one with -T" in err and "'Sandbox 5$', 'Kit'" in err
    # one file in two windows is one file
    tabs = status_of(window(), window(conn="c0ffee02", sameDocAs="c0ffee01"))
    rc, out, _, _ = cli(monkeypatch, capsys, "wait", bridge_reply=tabs)
    assert rc == 0 and out.startswith("ready:")
    same = status_of(window(), window(conn="c0ffee02", doc="OTHER"))
    rc, _, err, _ = cli(monkeypatch, capsys, "wait", "-T", "Sandbox 5$", bridge_reply=same)
    assert rc == 2 and "2 connected files are named 'Sandbox 5$'" in err


def test_doctors_checks_dont_show_in_the_plugin_window(monkeypatch, capsys):
    status = {"ok": True, "version": "1.1.0", "plugin_connected": True, "files": []}
    trip = lambda p: {"ok": True, "value": 2 if "1 + 1" in p["code"] else {"file": "Draft"}}  # noqa: E731
    rc, out, _, calls = cli(monkeypatch, capsys, "doctor", bridge_reply=FakeBridge(status, trip))
    assert rc == 0 and "round trip works" in out
    assert [(c["quick"], c["probe"]) for c in calls[1:]] == [(True, True), (True, True)]


@pytest.mark.parametrize("button, how", [(True, "Update in the plugin's window installs it"),
                                          (False, "it is updated by hand here"),
                                          (None, "Update in the plugin's window installs it")])
def test_doctor_asks_the_agent_to_tell_the_user_of_a_release(monkeypatch, capsys, button, how):
    status = {"ok": True, "version": "1.1.0", "plugin_connected": True, "files": [],
              "update": {"latest": "1.2.0", "current": "1.1.0", "url": "https://example.com/r"}}
    if button is not None:  # None: a bridge older than the field
        status["update_button"] = button
    trip = lambda p: {"ok": True, "value": 2 if "1 + 1" in p["code"] else {"file": "Draft"}}  # noqa: E731
    rc, out, _, _ = cli(monkeypatch, capsys, "doctor", bridge_reply=FakeBridge(status, trip))
    assert rc == 0 and "Figaro 1.2.0 is released (this is 1.1.0)" in out
    assert f"→ tell the user: {how}" in out and "git pull" not in out


def test_doctor_keeps_quiet_about_a_release_the_user_skipped(monkeypatch, capsys):
    status = {"ok": True, "version": "1.1.0", "plugin_connected": True, "files": [], "skipped": "1.2.0",
              "update": {"latest": "1.2.0", "current": "1.1.0", "url": "https://example.com/r"}}
    trip = lambda p: {"ok": True, "value": 2 if "1 + 1" in p["code"] else {"file": "Draft"}}  # noqa: E731
    rc, out, _, _ = cli(monkeypatch, capsys, "doctor", bridge_reply=FakeBridge(status, trip))
    assert rc == 0 and "✓  Figaro 1.2.0 is released (this is 1.1.0): the user skipped it" in out
    assert "tell the user" not in out


def test_created_names_the_outermost_new_layers():
    url = "https://www.figma.com/design/K/F?node-id="
    items = [{"op": "+", "id": "5:2", "name": "Icon", "url": url + "5-2"}]
    one = {"items": items, "top": [{"op": "+", "id": "5:1", "name": "Button", "url": url + "5-1"}]}
    assert cli_extras.created_line(one) == f'  created: "Button" {url}5-1'
    assert cli_extras.made_by({"value": {"props": []}, "changes": one}) == "5:1"
    assert cli_extras.created_line({"items": items}) == f'  created: "Icon" {url}5-2'  # an older build
    tops = [{"op": "+", "id": f"6:{i}", "name": f"Card {i}", "url": f"{url}6-{i}"} for i in range(1, 6)]
    lines = cli_extras.created_line({"items": [], "top": tops, "topMore": 2}).splitlines()
    assert lines[0] == f'  created: "Card 1" {url}6-1' and len(lines) == 4 and lines[3] == "  … and 4 more"


def test_exec_sends_libs_and_shoots_what_it_made(monkeypatch, capsys, tmp_path):
    lib = tmp_path / "grid.js"
    lib.write_text("export function col(n) { return n * 72; }\n")
    made = {"ok": True, "value": {"frame": "5:6", "w": 10},
            "changes": {"created": 1, "items": [{"op": "+", "id": "5:6", "name": "Grid", "type": "FRAME",
                                                 "url": "https://www.figma.com/design/K/F?node-id=5-6"}]}}
    pictures = {"ok": True, "value": [shot(node="5:6", data=png(10, 10))]}
    fake = FakeBridge(made, pictures)
    rc, out, err, calls = cli(monkeypatch, capsys, "exec", "--lib", str(lib), "--shot",
                              "return lib.col(2)", bridge_reply=fake)
    assert rc == 0
    first, second = calls
    assert first["libs"] == [{"name": "grid.js", "code": lib.read_text()}]
    assert "quick" not in first
    assert second["quick"] is True and '["5:6"]' in second["code"] and "libs" not in second
    assert 'created: "Grid" https://www.figma.com/design/K/F?node-id=5-6' in err
    figaro._emit(dict(made, ok=False, error="read-only: the script changed 1 — rolled back", rolled_back=True))
    assert "created" not in capsys.readouterr().err  # rolled back: the link leads nowhere
    assert (cli_extras.SHOTS / KEY / "5-6.png").exists()


def test_a_lost_lib_is_sent_again_once(monkeypatch, capsys, tmp_path):
    lib = tmp_path / "grid.js"
    lib.write_text("const A = 1")
    lost = {"ok": False, "error": "lib grid.js is not loaded in the plugin", "lib_missing": True}
    fake = FakeBridge(lost, {"ok": True, "value": 1})
    rc, out, err, calls = cli(monkeypatch, capsys, "exec", "--lib", str(lib), "return lib.A", bridge_reply=fake)
    assert rc == 0 and len(calls) == 2 and calls[0] == calls[1]


def test_svg_text_keeps_text_as_text(monkeypatch, capsys):
    reply = {"ok": True, "value": []}
    code = cli(monkeypatch, capsys, "shot", "1:2", "--format", "svg", "--svg-text", bridge_reply=reply)[3][0]["code"]
    assert '{"format": "svg", "outlineText": false}' in code
    code = cli(monkeypatch, capsys, "shot", "1:2", "--format", "svg", bridge_reply=reply)[3][0]["code"]
    assert "outlineText" not in code


def test_shot_absolute_is_sent_only_when_asked(monkeypatch, capsys):
    reply = {"ok": True, "value": []}
    code = cli(monkeypatch, capsys, "shot", "1:2", "--absolute", bridge_reply=reply)[3][0]["code"]
    assert '"absolute": true' in code
    code = cli(monkeypatch, capsys, "shot", "1:2", bridge_reply=reply)[3][0]["code"]
    assert "absolute" not in code


def test_shot_sel_takes_every_selected_layer(monkeypatch, capsys):
    calls = cli(monkeypatch, capsys, "shot", "sel", bridge_reply={"ok": True, "value": []})[3]
    assert "id === 'sel' ? figma.currentPage.selection" in calls[0]["code"]


def test_exec_shot_without_a_target_says_so(monkeypatch, capsys):
    rc, out, err, calls = cli(monkeypatch, capsys, "exec", "--shot", "return 1",
                              bridge_reply={"ok": True, "value": 1})
    assert rc == 1 and "nothing to shoot" in err and len(calls) == 1


def test_lib_from_the_environment_and_a_missing_file(monkeypatch, tmp_path):
    a, b = tmp_path / "a.js", tmp_path / "b.js"
    a.write_text("const A = 1")
    b.write_text("const B = 2")
    monkeypatch.setenv("FIGARO_LIB", os.pathsep.join([str(a), str(b)]))
    assert [x["name"] for x in cli_extras.read_libs(None)] == ["a.js", "b.js"]
    assert [x["name"] for x in cli_extras.read_libs([str(b)])] == ["b.js"]  # --lib wins
    with pytest.raises(SystemExit, match="--lib"):
        cli_extras.read_libs([str(tmp_path / "nope.js")])


def test_lib_error_line_is_printed(monkeypatch, capsys):
    error = {"ok": False, "error": "TypeError: cannot read 'width'", "line": 2, "source": "lib.col(x)",
             "lib": {"name": "grid.js", "line": 7, "source": "return n.width * k;"}}
    rc, out, err, calls = cli(monkeypatch, capsys, "exec", "return lib.col(x)", bridge_reply=error)
    assert rc == 1
    assert "   grid.js, line 7: return n.width * k;" in err


@pytest.mark.parametrize("argv", [["sel"], ["tree", "page", "--depth", "1"],
                                  ["find", "page", "name=Card"]])
def test_builtin_readers_are_quick(monkeypatch, capsys, argv):
    rc, out, err, calls = cli(monkeypatch, capsys, *argv, bridge_reply={"ok": True, "value": []})
    assert calls[0]["quick"] is True and rc == 0


@pytest.mark.parametrize("flt, query", [
    ("name=Card", {"name": "Card"}), ("name~Ca", {"nameHas": "Ca"}), ("type=frame", {"type": "FRAME"}),
    ("text=Hi", {"text": "Hi"}), ("text~a=b", {"textHas": "a=b"}),
])
def test_find_asks_h_find(monkeypatch, capsys, flt, query):
    rc, out, err, calls = cli(monkeypatch, capsys, "find", "page", flt, bridge_reply={"ok": True, "value": []})
    assert f"h.find(root, {json.dumps(query)})" in calls[0]["code"]
    rc, out, err, calls = cli(monkeypatch, capsys, "find", "page", flt, "--hidden",
                              bridge_reply={"ok": True, "value": []})
    assert f"h.find(root, {json.dumps(dict(query, hidden=True))})" in calls[0]["code"]


def test_clone_and_icomp_move_the_view_only_with_focus(monkeypatch, capsys):
    for argv in (["clone", "1:2"], ["icomp", "abc"]):
        calls = cli(monkeypatch, capsys, *argv)[3]
        assert "scrollAndZoomIntoView" not in calls[0]["code"]
        calls = cli(monkeypatch, capsys, *argv, "--focus")[3]
        assert "scrollAndZoomIntoView" in calls[0]["code"]


RM_STUB = """
const nodes = {
  "1:1": {id: "1:1", name: "Group", type: "FRAME", removed: false, kids: ["1:2"]},
  "1:2": {id: "1:2", name: "Child", type: "RECTANGLE", removed: false, kids: []},
  "1:3": {id: "1:3", name: "Other", type: "TEXT", removed: false, kids: []},
};
for (const n of Object.values(nodes)) n.remove = function () {
  this.removed = true; for (const k of this.kids) nodes[k].removed = true; };
const h = {async resolve(id) { if (!nodes[id]) throw new Error("no " + id); return nodes[id]; }};
const figma = {currentPage: {selection: SELECTION.map((id) => nodes[id])}};
const logs = [];
const print = (s) => logs.push(s);
const body = %s;
new Function("figma", "h", "print", "return (async () => {" + body + "})();")(figma, h, print).then(
  (v) => console.log(JSON.stringify({ok: true, v, logs, removed: Object.values(nodes).filter((n) => n.removed).map((n) => n.id)})),
  (e) => console.log(JSON.stringify({ok: false, e: e.message, removed: Object.values(nodes).filter((n) => n.removed).map((n) => n.id)})));
"""


def run_rm(monkeypatch, capsys, *ids, selection=()):
    code = cli(monkeypatch, capsys, "rm", *ids)[3][0]["code"]
    stub = f"const SELECTION = {json.dumps(list(selection))};" + RM_STUB % json.dumps(code)
    # 120 s: on a Windows runner `node` once took more than 30 s to start
    r = subprocess.run(["node", "-e", stub], capture_output=True, encoding="utf-8", timeout=120)
    return json.loads(r.stdout)


def test_rm_finds_every_id_before_removing_any(monkeypatch, capsys):
    got = run_rm(monkeypatch, capsys, "1:3", "9:9")
    assert got == {"ok": False, "e": "node not found: 9:9 — nothing removed", "removed": []}


def test_rm_sel_removes_every_selected_layer(monkeypatch, capsys):
    got = run_rm(monkeypatch, capsys, "sel", selection=["1:1", "1:3"])
    assert got["ok"] and [x["id"] for x in got["v"]] == ["1:1", "1:3"]
    got = run_rm(monkeypatch, capsys, "sel", "1:3")
    assert got == {"ok": False, "e": "node not found: sel (nothing is selected) — nothing removed",
                   "removed": []}


def test_rm_skips_what_went_with_its_parent(monkeypatch, capsys):
    got = run_rm(monkeypatch, capsys, "1:1", "1:2", "1:3")
    assert got["ok"] and [x["id"] for x in got["v"]] == ["1:1", "1:3"]
    assert got["logs"] == ["already removed with a parent or listed twice: 1:2"]
    assert got["removed"] == ["1:1", "1:2", "1:3"]


# ─── starting the bridge ─────────────────────────────────────────────────

def fake_start(tmp_path, monkeypatch, body="open(RUNS, 'a').write('started\\n')"):
    """A start script in Python, so that the tests run on Windows as well."""
    runs = tmp_path / "runs"
    script = tmp_path / "start.py"
    script.write_text(f"import sys\nRUNS = {str(runs)!r}\n{body}\n")
    monkeypatch.setattr(cli_extras, "start_command",
                        lambda port: (script, [sys.executable, str(script)]))
    monkeypatch.setattr(cli_extras, "PLUGIN_WAIT", 0.2)
    monkeypatch.setenv("FIGARO_AUTOSTART", "1")
    return runs


def test_autostart_runs_the_script_once(tmp_path, monkeypatch, capsys):
    runs = fake_start(tmp_path, monkeypatch)
    assert cli_extras.autostart("127.0.0.1", free_port()) is True
    assert cli_extras.autostart("127.0.0.1", free_port()) is False
    assert runs.read_text() == "started\n"
    err = capsys.readouterr().err
    assert "starting it" in err and "the plugin has not connected yet" in err


def test_autostart_can_be_turned_off_and_is_local_only(tmp_path, monkeypatch):
    runs = fake_start(tmp_path, monkeypatch)
    monkeypatch.setenv("FIGARO_AUTOSTART", "0")
    assert cli_extras.autostart("127.0.0.1", 1) is False
    monkeypatch.setenv("FIGARO_AUTOSTART", "1")
    assert cli_extras.autostart("10.0.0.5", 1) is False
    assert not runs.exists()


def test_autostart_does_not_wait_for_the_bridge_it_started(tmp_path, monkeypatch):
    """The bridge outlives the start script and inherits its output (on
    Windows, Start-Process passes it every handle): with a pipe, this call would
    wait until the bridge exits."""
    fake_start(tmp_path, monkeypatch, body=(
        "import subprocess\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(12)'],\n"
        "                 stdout=sys.stdout, stderr=sys.stderr)"))
    t = time.time()
    assert cli_extras.autostart("127.0.0.1", free_port()) is True
    assert time.time() - t < 8


def test_a_failed_start_says_why(tmp_path, monkeypatch, capsys):
    fake_start(tmp_path, monkeypatch, body="print('port is taken by another program'); sys.exit(1)")
    assert cli_extras.autostart("127.0.0.1", 1) is False
    assert "port is taken by another program" in capsys.readouterr().err


def test_windows_starts_the_bridge_with_powershell(monkeypatch):
    script, cmd = cli_extras.start_command(8790, windows=True)
    assert script.name == "start-bridge.ps1" and script.exists()
    assert cmd[0] == "powershell" and cmd[cmd.index("-File") + 1] == str(script)
    assert cmd[-2:] == ["-Port", "8790"]
    script, cmd = cli_extras.start_command(8790, windows=False)
    assert cmd == ["bash", str(script)] and script.name == "start-bridge.sh"


def test_a_sandbox_that_blocks_localhost_is_named(monkeypatch, capsys):
    """Codex's and Cursor's sandboxes refuse the connect itself: no autostart, a hint."""
    def blocked(*a, **k):
        raise cli_extras.Unreached(PermissionError(1, "Operation not permitted"))

    monkeypatch.setattr(cli_extras, "fetch", blocked)
    monkeypatch.setattr(cli_extras, "autostart", lambda *a: pytest.fail("no autostart"))
    status, resp = figaro._request("GET", "/status")
    assert status == 0 and resp["hint"] == figaro.SANDBOXED
    assert figaro.cmd_doctor(None) == 1
    assert figaro.SANDBOXED in capsys.readouterr().out
    assert figaro.cmd_targets(None) == 1
    assert "hint: a sandbox" in capsys.readouterr().err


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(handler):
    """An HTTP server on a free port, in a thread: (port, stop)."""
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop():
        server.shutdown()
        server.server_close()
    return server.server_address[1], stop


def bridge_root(handler):
    """What the real bridge answers on `GET /`: the CLI looks for this mark."""
    body = json.dumps({"service": "figaro-bridge", "version": "0"}).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(body)


class Status(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            return bridge_root(self)
        body = json.dumps({"plugin_connected": True, "files": [{"name": "Draft"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_the_bridge_is_never_reached_through_a_proxy(monkeypatch):
    """http_proxy would get the token and the script, or fail and have the CLI
    restart a bridge that is fine."""
    port, stop = serve(Status)
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.setattr(figaro, "HOST", "127.0.0.1")
    monkeypatch.setattr(figaro, "PORT", port)
    try:
        status, body = figaro._request("GET", "/status")
        assert status == 200 and body["files"] == [{"name": "Draft"}]
        assert cli_extras._connected("127.0.0.1", port) == ["Draft"]
        assert cli_extras._answers("127.0.0.1", port)
    finally:
        stop()


class Hangs(http.server.BaseHTTPRequestHandler):
    """Takes the request and hangs up without an answer, or never answers."""
    def do_GET(self):
        if self.path == "/":
            return bridge_root(self)
        if self.path == "/slow":
            time.sleep(3)
        self.close_connection = True

    def log_message(self, *a):
        pass


class NotJson(Status):
    def do_GET(self):
        if self.path == "/":
            return bridge_root(self)
        self.send_response(502)
        self.end_headers()
        self.wfile.write(b"<html>Bad gateway</html>")


@pytest.mark.parametrize("handler, path, error", [
    (Hangs, "/status", "connection: "),
    (Hangs, "/slow", "request timed out"),
    (NotJson, "/status", "<html>Bad gateway</html>"),
])
def test_an_odd_answer_is_an_error_not_a_traceback(monkeypatch, handler, path, error):
    port, stop = serve(handler)
    monkeypatch.setattr(figaro, "HOST", "127.0.0.1")
    monkeypatch.setattr(figaro, "PORT", port)
    try:
        status, body = figaro._request("GET", path, timeout=1)
        assert body["ok"] is False and body["error"].startswith(error), body
    finally:
        stop()


def test_a_second_start_waits_for_the_first_and_starts_nothing(tmp_path, monkeypatch, capsys):
    """Parallel first calls: one starts the bridge, the others wait for it."""
    runs = fake_start(tmp_path, monkeypatch)
    port = free_port()
    lock = cli_extras._start_lock(port)  # another figaro call is starting it
    server = http.server.HTTPServer(("127.0.0.1", port), Status)

    def the_other_call_is_done():
        time.sleep(0.5)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        lock.unlink()
    threading.Thread(target=the_other_call_is_done, daemon=True).start()
    try:
        t = time.time()
        assert cli_extras.autostart("127.0.0.1", port) is True
        assert time.time() - t >= 0.4 and not runs.exists()
        assert "starting it" not in capsys.readouterr().err
    finally:
        server.shutdown()
        server.server_close()


def test_a_start_lock_left_by_a_call_that_died_is_taken_over():
    port = free_port()
    lock = cli_extras._start_lock(port)
    old = time.time() - cli_extras.START_LOCK_STALE - 5
    os.utime(lock, (old, old))
    t = time.time()
    assert cli_extras._start_lock(port) == lock and time.time() - t < 2
    lock.unlink()


def test_a_refused_request_starts_the_bridge_and_asks_again(monkeypatch):
    port = free_port()
    servers = []

    def start(host, p):
        server = http.server.HTTPServer(("127.0.0.1", p), Status)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return True

    monkeypatch.setattr(cli_extras, "autostart", start)
    monkeypatch.setattr(figaro, "HOST", "127.0.0.1")
    monkeypatch.setattr(figaro, "PORT", port)
    try:
        status, body = figaro._request("GET", "/status")
        assert status == 200 and body["files"] == [{"name": "Draft"}]
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


@pytest.mark.skipif(sys.platform == "win32", reason="start-bridge.sh is for macOS / Linux")
def test_status_starts_a_real_bridge_that_outlives_the_call():
    port = free_port()
    env = dict(os.environ, FIGARO_PORT=str(port), FIGARO_AUTOSTART="1",
               FIGARO_PLUGIN_WAIT="0.3", FIGARO_IDLE_EXIT="120")
    try:
        r = subprocess.run([sys.executable, str(ROOT / "figaro.py"), "status"], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert "starting it" in r.stderr
        assert json.loads(r.stdout)["plugin_connected"] is False
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=3) as resp:
            assert resp.status == 200
        # Under tmux (Linux runners have it) there is no pid file: look for the process.
        ps = subprocess.run(["ps", "-e", "-o", "command="], capture_output=True, text=True).stdout
        assert f"bridge.py --port {port} --idle-exit 120" in ps
    finally:
        subprocess.run(["bash", str(ROOT / "start-bridge.sh"), "--stop"], env=env,
                       capture_output=True, timeout=30)


# A tmux server older than the caller: a new session's command gets the
# server's environment (here a bare one), not the caller's.
FAKE_TMUX = """#!/usr/bin/env bash
case "$1" in
  new-session)
    set -m
    env -i HOME="$HOME" PATH="$PATH" bash -c "${@: -1}" >/dev/null 2>&1 &
    echo $! > "$FAKE_TMUX_PID" ;;
  has-session) [ -s "$FAKE_TMUX_PID" ] ;;
  kill-session) kill -- "-$(cat "$FAKE_TMUX_PID")"; : > "$FAKE_TMUX_PID" ;;
esac
"""


@pytest.mark.skipif(sys.platform == "win32", reason="start-bridge.sh is for macOS / Linux")
def test_a_bridge_in_tmux_gets_the_figaro_settings(tmp_path):
    fake = tmp_path / "bin" / "tmux"
    fake.parent.mkdir()
    fake.write_text(FAKE_TMUX)
    fake.chmod(0o755)
    token = tmp_path / "elsewhere" / "token"
    env = dict(os.environ, PATH=f"{fake.parent}{os.pathsep}{os.environ['PATH']}",
               HOME=str(tmp_path / "home"), FIGARO_PORT=str(free_port()),
               FIGARO_TOKEN_FILE=str(token), FIGARO_IDLE_EXIT="120",
               FAKE_TMUX_PID=str(tmp_path / "tmux.pid"))
    try:
        r = subprocess.run(["bash", str(ROOT / "start-bridge.sh")], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "tmux session" in r.stdout
        assert token.read_text(encoding="utf-8")  # saved where FIGARO_TOKEN_FILE said
    finally:
        r = subprocess.run(["bash", str(ROOT / "start-bridge.sh"), "--stop"], env=env,
                           capture_output=True, text=True, timeout=30)
    assert "stopped" in r.stdout, r.stdout + r.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="start-bridge.ps1 is for Windows")
def test_status_starts_a_real_bridge_on_windows():
    port = free_port()
    env = dict(os.environ, FIGARO_PORT=str(port), FIGARO_AUTOSTART="1",
               FIGARO_PLUGIN_WAIT="0.3", FIGARO_IDLE_EXIT="120")
    stop = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(ROOT / "start-bridge.ps1"), "-Port", str(port), "-Stop"]
    try:
        t = time.time()
        r = subprocess.run([sys.executable, str(ROOT / "figaro.py"), "status"], env=env,
                           capture_output=True, text=True, timeout=90)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "starting it" in r.stderr
        assert json.loads(r.stdout)["plugin_connected"] is False
        assert time.time() - t < 30, "the call waited for the bridge it started"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=3) as resp:
            assert resp.status == 200
    finally:
        r = subprocess.run(stop, capture_output=True, text=True, timeout=60)
    assert "stopped pid" in r.stdout, r.stdout + r.stderr


def test_exec_reads_stdin_as_utf8(monkeypatch):
    """On Windows Python reads a pipe in the ANSI code page."""
    class Stdin:
        def __init__(self, data):
            self.buffer = io.BytesIO(data)

    script = "return 'Total: €5 — paid ✓ 🎨'"
    for data in (script.encode("utf-8"), b"\xef\xbb\xbf" + script.encode("utf-8")):
        monkeypatch.setattr(sys, "stdin", Stdin(data))
        assert figaro.read_stdin() == script
    monkeypatch.setattr(sys, "stdin", Stdin("return '© 2026 — €5'".encode("cp1252")))
    assert figaro.read_stdin().startswith("return '")  # not UTF-8: no crash


def test_start_hint_names_this_computers_script():
    assert cli_extras.start_hint(windows=False) == f"bash {cli_extras.START}"
    hint = cli_extras.start_hint(restart=True, windows=True)
    assert hint == f'powershell -ExecutionPolicy Bypass -File "{cli_extras.START_PS1}" -Restart'


# ─── the bridge leaves when idle ─────────────────────────────────────────

def test_duration():
    assert [bridge_idle.duration(x) for x in ("90", "90s", "30m", "3h", " 1.5h ")] == \
        [90, 90, 1800, 10800, 5400]
    for bad in ("", "3d", "h", "-1"):
        with pytest.raises(ValueError):
            bridge_idle.duration(bad)


def test_idle_bridge_leaves_but_not_while_a_plugin_is_connected(monkeypatch):
    left = []
    monkeypatch.setattr(bridge_idle, "LAST", [time.time() - 100])
    monkeypatch.setattr(bridge, "_live_plugins", lambda: [("conn", {})])
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(bridge_idle.watch(50, every=0.01, exit_fn=left.append), 0.1))
    assert left == []  # a connected plugin keeps it — and counts as being needed
    monkeypatch.setattr(bridge, "_live_plugins", lambda: [])
    monkeypatch.setattr(bridge_idle, "LAST", [time.time() - 100])
    asyncio.run(asyncio.wait_for(bridge_idle.watch(50, every=0.01, exit_fn=left.append), 1))
    assert left == [50]


def test_a_request_counts_as_being_needed(monkeypatch):
    monkeypatch.setattr(bridge_idle, "LAST", [0.0])

    async def go():
        app = bridge.build_app()
        bridge_idle.install(app, 0)
        client = TestClient(TestServer(app))
        await client.start_server()
        assert (await client.get("/status")).status == 200
        await client.close()
        return app

    app = asyncio.run(go())
    assert time.time() - bridge_idle.LAST[0] < 5
    assert "bridge_idle" not in app  # 0: no watcher, the bridge stays for good


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is how start-bridge.sh stops it")
def test_a_stopping_bridge_closes_plugin_sockets_at_once():
    """start-bridge.sh --stop sends SIGTERM. The plugin window has to hear it
    at once, not up to a minute later: only then does it
    stop the keep-awake tone (tests/ui.test.js) and reconnect to a new bridge."""
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "bridge.py"), "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def go():
        async with aiohttp.ClientSession() as s:
            for _ in range(100):
                try:
                    async with s.get(f"http://127.0.0.1:{port}/status") as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.1)
            ws = await s.ws_connect(f"ws://127.0.0.1:{port}/plugin",
                                    headers={"Origin": "null", "User-Agent": FIGMA_UA})
            await ws.send_json({"type": "hello", "version": "2.1", "fileKey": KEY, "name": "Draft"})
            await asyncio.sleep(0.3)
            t0 = time.monotonic()
            proc.terminate()
            try:
                while (await asyncio.wait_for(ws.receive(), 10)).type == aiohttp.WSMsgType.TEXT:
                    pass  # peers and the like
            except asyncio.TimeoutError:
                return None, None
            return time.monotonic() - t0, ws.close_code

    try:
        took, code = asyncio.run(go())
        assert took is not None, "the plugin socket was still open 10 s after SIGTERM"
        assert took < 3 and code == 1001, (took, code)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)


# ─── image ───────────────────────────────────────────────────────────────

def png_header(w, h):
    """The first 24 bytes of a PNG: all the CLI reads of a picture's size."""
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big")


def test_image_sends_the_file_in_the_body_not_in_the_command_line(monkeypatch, capsys, tmp_path):
    pic = tmp_path / "photo.png"
    pic.write_bytes(png(60, 40))
    reply = {"ok": True, "result": '{"id":"1:2","mode":"fit"}', "elapsed_ms": 5}
    rc, out, err, calls = cli(monkeypatch, capsys, "image", URL, str(pic), "--mode", "fit",
                              bridge_reply=reply)
    assert rc == 0 and '"mode":"fit"' in out
    (payload,) = calls
    assert payload["target"] == URL and "quick" not in payload and "read_only" not in payload
    assert payload["images"] == [{"name": "image", "data": base64.b64encode(pic.read_bytes()).decode()}]
    assert 'h.image(n, \'image\', "fit")' in payload["code"] and '"1:11"' in payload["code"]
    assert len(payload["code"]) < 500  # the picture is not in the script
    assert str(pic) in sys.argv and len(" ".join(sys.argv)) < 300  # the command line holds a path only



def test_image_errors_name_the_command(monkeypatch, capsys, tmp_path):
    note = tmp_path / "notes.txt"
    note.write_bytes(b"not a picture")
    rc, out, err, calls = cli(monkeypatch, capsys, "image", URL, str(note))
    assert str(rc).startswith("figaro: image: notes.txt is not a PNG, JPEG or GIF") and not calls

def test_image_modes_and_the_default():
    args, _ = args_of("image", "1:2", "a.png")
    assert args.mode == "fill"
    for mode in ("fill", "fit", "crop", "tile"):
        assert args_of("image", "1:2", "a.png", "--mode", mode)[0].mode == mode
    with pytest.raises(SystemExit):
        args_of("image", "1:2", "a.png", "--mode", "stretch")


@pytest.mark.parametrize("name, data, text", [
    ("a.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ", "not a PNG, JPEG or GIF"),
    ("a.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "createNodeFromSvg"),
    ("a.png", b"", "is empty"),
    ("a.png", png_header(5000, 3000), "5000x3000 px; Figma takes up to 4096"),
    ("a.png", png_header(100, 0), "has no pixels"),
])
def test_a_file_figma_would_refuse_is_refused_before_anything_is_sent(
        monkeypatch, capsys, tmp_path, name, data, text):
    pic = tmp_path / name
    pic.write_bytes(data)
    fake = FakeBridge({"ok": True})
    monkeypatch.setattr(figaro, "_request", fake)
    monkeypatch.setattr(sys, "argv", ["figaro", "image", "1:2", str(pic)])
    with pytest.raises(SystemExit) as done:
        figaro.main()
    assert text in str(done.value.code) and fake.calls == []


def test_image_refuses_a_missing_file_and_a_too_big_one(monkeypatch, tmp_path):
    with pytest.raises(SystemExit, match="nope.png"):
        cli_extras.read_image(str(tmp_path / "nope.png"), "image")
    big = tmp_path / "big.png"
    big.write_bytes(png_header(10, 10) + bytes(1024))
    monkeypatch.setattr(cli_extras, "IMAGE_MAX_BYTES", 1000)
    with pytest.raises(SystemExit, match=r"MB; the limit"):
        cli_extras.read_image(str(big), "image")


def test_the_formats_figma_takes_are_told_by_their_first_bytes(tmp_path):
    from PIL import Image
    for fmt, ext in (("PNG", "png"), ("JPEG", "jpg"), ("GIF", "gif")):
        buf = io.BytesIO()
        Image.new("RGB", (30, 20), (1, 2, 3)).save(buf, fmt)
        assert cli_extras.sniff_image(buf.getvalue()) == (ext, (30, 20)), fmt
    assert cli_extras.sniff_image(b"BM....") is None
    # the name does not matter, the bytes do
    wrong = tmp_path / "photo.jpg"
    wrong.write_bytes(png(8, 8))
    assert cli_extras.read_image(str(wrong), "image")[1] == "png"


def test_exec_image_attaches_named_files(monkeypatch, capsys, tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(png(8, 8))
    b.write_bytes(png(9, 9))
    rc, out, err, calls = cli(monkeypatch, capsys, "exec", "--image", f"logo={a}", "--image",
                              f"hero={b}", 'return h.image(await h.resolve("1:2"), "logo")')
    assert rc == 0
    assert [(i["name"], base64.b64decode(i["data"])) for i in calls[0]["images"]] == [
        ("logo", a.read_bytes()), ("hero", b.read_bytes())]
    rc, out, err, calls = cli(monkeypatch, capsys, "exec", "return 1")
    assert "images" not in calls[0]
    for specs in (["nofile"], ["=x.png"], ["bad name=x.png"], [f"logo={a}", f"logo={b}"]):
        with pytest.raises(SystemExit, match="--image"):
            cli_extras.read_images(specs)


def test_the_cli_and_the_bridge_count_the_files_the_same_way(tmp_path):
    """Several files that pass the CLI must pass the bridge: both count base64
    characters, which round each file up (15 MiB + 1 and 15 MiB - 1 are
    41,943,044 of them, four over)."""
    import bridge_exec
    assert cli_extras.IMAGE_MAX_BASE64 == bridge_exec.IMAGES_SIZE
    assert cli_extras.IMAGE_MAX_BYTES * 4 // 3 == bridge_exec.IMAGES_SIZE
    half = 15 * 1024 * 1024
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(png_header(10, 10) + bytes(half + 1 - 24))
    b.write_bytes(png_header(10, 10) + bytes(half - 1 - 24))
    with pytest.raises(SystemExit, match="over 30 MB together"):
        cli_extras.read_images([f"a={a}", f"b={b}"])
    b.write_bytes(png_header(10, 10) + bytes(half - 4 - 24))
    images = cli_extras.read_images([f"a={a}", f"b={b}"])  # what the CLI lets through, the bridge takes
    assert len(bridge_exec.exec_options({"images": images}).images) == 2
    big = tmp_path / "big.png"
    big.write_bytes(png_header(10, 10) + bytes(cli_extras.IMAGE_MAX_BYTES - 24))
    assert len(bridge_exec.exec_options({"images": cli_extras.read_images([f"big={big}"])}).images) == 1


# ─── only the Figaro bridge gets the token ───────────────────────────────

class Recorder(http.server.BaseHTTPRequestHandler):
    """Another program on the bridge's port: what it answers on `GET /` is
    `root`, and every request it gets is kept with the token header it carried."""
    seen = []
    root = (200, "application/json", b'{"hello": "world"}')

    def _answer(self):
        self.seen.append((self.command, self.path, self.headers.get("X-Figaro-Token")))
        if self.path == "/":
            code, kind, body = self.root
        else:
            code, kind, body = 200, "application/json", b'{"ok": true, "files": []}'
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *a):
        pass


def recorder(root):
    return type("Recorder", (Recorder,), {"seen": [], "root": root})


@pytest.mark.parametrize("root", [
    (200, "application/json", b'{"hello": "world"}'),
    (404, "text/html", b"<html>Not found</html>"),
    (200, "application/json", b'{"service": "something-else"}'),
    (200, "text/html", b"<html>figaro-bridge</html>"),
], ids=["other-json", "html-404", "other-service", "html-200"])
def test_a_port_held_by_another_program_gets_no_token(monkeypatch, capsys, root):
    handler = recorder(root)
    port, stop = serve(handler)
    monkeypatch.setattr(figaro, "HOST", "127.0.0.1")
    monkeypatch.setattr(figaro, "PORT", port)
    assert figaro_token_of(port)  # a token exists: it must not leave
    try:
        status, body = figaro._request("POST", "/exec", {"code": "1"})
        assert status == 0 and body["ok"] is False and body["foreign"] is True
        assert f"port {port} on 127.0.0.1 is held by another program" in body["error"]
        assert "not the Figaro bridge" in body["error"] and "lsof" in body["hint"]
        assert figaro._request("GET", "/status")[0] == 0
        assert [m for m, p, t in handler.seen] == ["GET"]  # one look, then nothing
        assert all(t is None for m, p, t in handler.seen)
        # doctor and wait say the same, in words
        assert figaro.cmd_doctor(None) == 1
        assert "held by another program" in capsys.readouterr().out
        assert figaro.cmd_wait(figaro.build_parser().parse_args(["wait"])) == 2
        assert "held by another program" in capsys.readouterr().err
        assert all(t is None for m, p, t in handler.seen)
    finally:
        stop()


def figaro_token_of(port):
    import figaro_token
    return figaro_token.read_token(port)


def test_the_bridge_gets_the_token_and_is_asked_once(monkeypatch):
    seen = []

    class Bridge(Status):
        def do_GET(self):
            seen.append((self.path, self.headers.get("X-Figaro-Token")))
            super().do_GET()

        def do_POST(self):
            seen.append((self.path, self.headers.get("X-Figaro-Token")))
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            super().do_GET()

    port, stop = serve(Bridge)
    monkeypatch.setattr(figaro, "HOST", "127.0.0.1")
    monkeypatch.setattr(figaro, "PORT", port)
    try:
        assert figaro._request("POST", "/exec", {"code": "1"})[0] == 200
        assert figaro._request("POST", "/exec", {"code": "2"})[0] == 200
        assert figaro._request("GET", "/status")[0] == 200
        paths = [p for p, t in seen]
        assert paths == ["/", "/exec", "/exec", "/status"]  # the look comes once
        assert [t for p, t in seen if p == "/exec"] == [figaro_token_of(port)] * 2
        assert seen[0][1] is None  # the look itself carries nothing
    finally:
        stop()


def test_a_port_that_never_answers_is_not_trusted(monkeypatch):
    class Silent(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(2)

        def log_message(self, *a):
            pass

    port, stop = serve(Silent)
    monkeypatch.setattr(cli_extras, "PROBE_TIMEOUT", 0.3)
    monkeypatch.setattr(cli_extras, "RETRY_PAUSE", 0)
    monkeypatch.setattr(figaro, "HOST", "127.0.0.1")
    monkeypatch.setattr(figaro, "PORT", port)
    try:
        status, body = figaro._request("POST", "/exec", {"code": "1"})
        assert status == 0 and "no answer" in body["error"] and "nothing was sent" in body["error"]
        assert cli_extras._verdicts == {}  # silence may be a bridge that restarts: not kept
    finally:
        stop()


def test_nothing_on_the_port_is_not_a_foreign_program(monkeypatch):
    port = free_port()
    monkeypatch.setattr(figaro, "HOST", "127.0.0.1")
    monkeypatch.setattr(figaro, "PORT", port)
    status, body = figaro._request("GET", "/status")
    assert status == 0 and not body.get("foreign") and body["error"].startswith("connection: ")
    assert cli_extras.probe("127.0.0.1", port)["state"] == "none"


def test_autostart_does_not_start_a_bridge_on_another_programs_port(tmp_path, monkeypatch):
    runs = fake_start(tmp_path, monkeypatch)
    port, stop = serve(recorder((404, "text/html", b"<html>No</html>")))
    try:
        assert cli_extras.autostart("127.0.0.1", port) is False
        assert not runs.exists()
    finally:
        stop()


def test_a_start_goes_on_when_the_port_answers_as_the_bridge(tmp_path, monkeypatch):
    runs = fake_start(tmp_path, monkeypatch)
    port, stop = serve(Status)
    try:
        assert cli_extras.autostart("127.0.0.1", port) is True  # nothing to start, plugins awaited
        assert not runs.exists()
    finally:
        stop()


def test_an_ipv6_host_goes_into_the_url_in_brackets(monkeypatch):
    assert cli_extras.hostport("::1", 8788) == "[::1]:8788"
    assert cli_extras.hostport("[::1]", 8788) == "[::1]:8788"
    assert cli_extras.hostport("127.0.0.1", 8788) == "127.0.0.1:8788"
    assert cli_extras.hostport("localhost", 1) == "localhost:1"
    assert cli_extras.hostport("fe80::1", 2) == "[fe80::1]:2"
    seen = []

    class Connection:  # http.client's, which only notes where it is asked to go
        def __init__(self, host, port, timeout=None):
            seen.append((host, port))

        def request(self, method, path, body=None, headers=None):
            seen.append((method, path))
            raise ConnectionRefusedError(61, "refused")

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(cli_extras, "autostart", lambda *a: False)
    monkeypatch.setattr(figaro, "HOST", "::1")
    monkeypatch.setattr(figaro, "PORT", 8788)
    figaro._request("GET", "/status")
    assert seen == [("::1", 8788), ("GET", "/"), ("::1", 8788), ("GET", "/status")]
    cli_extras._connected("::1", 8788)
    assert seen[-2:] == [("::1", 8788), ("GET", "/status")]


# FIGARO_HOST may come bracketed, as in a URL: Python 3.9's http.client can't resolve "[::1]"
@pytest.mark.parametrize("host", ["::1", "[::1]"])
def test_an_ipv6_bridge_is_asked_with_the_host_in_brackets(monkeypatch, host):
    hosts = []

    class V6(http.server.HTTPServer):
        address_family = socket.AF_INET6

    class Handler(Status):
        def do_GET(self):
            hosts.append(self.headers["Host"])
            super().do_GET()

    try:
        server = V6(("::1", 0), Handler)
    except OSError:
        pytest.skip("no IPv6 loopback here")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setattr(figaro, "HOST", host)
    monkeypatch.setattr(figaro, "PORT", port)
    try:
        status, resp = figaro._request("GET", "/status")
    finally:
        server.shutdown()
        server.server_close()
    assert status == 200 and resp["files"] == [{"name": "Draft"}]
    assert hosts == [f"[::1]:{port}"] * 2  # the look at the port, then the call


# What the CLI loads on every call costs time on every call (tens of ms together); a command that
# needs more imports it where it is used.
HEAVY = ("http.client", "urllib.request", "urllib.error", "subprocess", "tempfile", "ssl")
LOADED = ("import sys; sys.path.insert(0, %r); sys.argv = ['figaro'] + %r\n"
          "import figaro\n"
          "try:\n"
          "    figaro.main()\n"
          "except SystemExit:\n"
          "    pass\n"
          "print('LOADED', *sorted(m for m in %r if m in sys.modules), file=sys.stderr)\n")


def loaded_by(args, **env):
    code = LOADED % (str(ROOT), args, HEAVY)
    r = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True,
                       timeout=60, env=dict(os.environ, FIGARO_AUTOSTART="0", **env))
    return r.stderr.strip().splitlines()[-1].split()[1:]


def test_help_loads_none_of_the_heavy_modules():
    got = loaded_by(["-h"])
    # Windows has no /tmp: there the folder of the pictures asks tempfile for its place.
    assert [m for m in got if not (os.name == "nt" and m == "tempfile")] == []


def test_a_call_to_the_bridge_loads_only_http_client_of_them():
    port, stop = serve(Status)
    try:
        got = loaded_by(["status"], FIGARO_PORT=str(port), FIGARO_HOST="127.0.0.1")
    finally:
        stop()
    # ssl: http.client loads it; tempfile on Windows, as for -h
    assert [m for m in got if m not in ("http.client", "ssl") and not (os.name == "nt" and m == "tempfile")] == []
    assert "http.client" in got


def test_a_proxy_in_the_environment_is_never_used(monkeypatch):
    """The token and the script of a call must not go to http_proxy, 127.0.0.1 included."""
    as_proxy = recorder((200, "application/json", b'{"service": "figaro-bridge"}'))
    proxy, stop_proxy = serve(as_proxy)
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    port, stop = serve(Status)
    try:
        assert cli_extras.probe("127.0.0.1", port)["state"] == "ours"
        status, body = cli_extras.fetch("127.0.0.1", port, "GET", "/status")
        assert status == 200 and json.loads(body)["files"] == [{"name": "Draft"}]
        assert as_proxy.seen == []
    finally:
        stop()
        stop_proxy()


def test_a_real_bridge_is_recognised(monkeypatch):
    """The real root_handler's answer carries the mark the CLI looks for."""
    async def go():
        async with TestClient(TestServer(bridge.build_app())) as client:
            async with client.get("/") as r:
                return await r.json()
    body = asyncio.run(go())
    assert body["service"] == cli_extras.SERVICE


def test_a_bridge_on_ipv6_loopback_is_reached_at_a_bracketed_url(monkeypatch):
    class Server6(http.server.HTTPServer):
        address_family = socket.AF_INET6

    try:
        server = Server6(("::1", 0), Status)
    except OSError:
        pytest.skip("no IPv6 loopback here")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setattr(figaro, "HOST", "::1")
    monkeypatch.setattr(figaro, "PORT", port)
    try:
        status, body = figaro._request("GET", "/status")
        assert status == 200 and body["files"] == [{"name": "Draft"}]
        assert cli_extras._connected("::1", port) == ["Draft"]
    finally:
        server.shutdown()
        server.server_close()


# ─── a look that fails for a moment, a bridge that starts late ───────────

class HangsUp(Status):
    """Hangs up on the first `n` looks at `/`, then answers as the bridge."""
    n = 0
    looks = 0

    def do_GET(self):
        if self.path == "/":
            type(self).looks += 1
            if type(self).looks <= self.n:
                self.close_connection = True
                return
        super().do_GET()


def hangs_up(n):
    return type("HangsUp", (HangsUp,), {"n": n, "looks": 0})


def at(monkeypatch, port):
    monkeypatch.setattr(figaro, "HOST", "127.0.0.1")
    monkeypatch.setattr(figaro, "PORT", port)
    monkeypatch.setattr(cli_extras, "RETRY_PAUSE", 0)


def test_a_look_that_is_hung_up_on_is_made_again_once(monkeypatch):
    port, stop = serve(hangs_up(1))
    at(monkeypatch, port)
    try:
        assert cli_extras.probe("127.0.0.1", port)["state"] == "ours"
    finally:
        stop()


def test_a_hang_up_is_neither_foreign_nor_kept(monkeypatch):
    """A bridge that restarts hangs up for a moment: the call says it didn't
    answer, a long `wait` goes on, and the next look finds the bridge."""
    port, stop = serve(hangs_up(2))
    at(monkeypatch, port)
    try:
        status, body = figaro._request("POST", "/exec", {"code": "1"})
        assert status == 0 and not body.get("foreign") and "did not answer as the bridge" in body["error"]
        assert "hint" not in body and cli_extras._verdicts == {}
        assert figaro._request("GET", "/status")[0] == 200
        assert cli_extras._verdicts[("127.0.0.1", port)]["state"] == "ours"
    finally:
        stop()


def test_wait_goes_on_while_the_bridge_hangs_up(monkeypatch, capsys):
    port, stop = serve(hangs_up(2))
    at(monkeypatch, port)
    try:
        args = figaro.build_parser().parse_args(["wait"])
        assert figaro.cmd_wait(args) == 0
        assert "ready" in capsys.readouterr().out
    finally:
        stop()


def test_an_empty_port_is_looked_at_again(monkeypatch):
    port = free_port()
    assert cli_extras.probe("127.0.0.1", port)["state"] == "none"
    assert cli_extras._verdicts == {}
    server = http.server.HTTPServer(("127.0.0.1", port), Status)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert cli_extras.probe("127.0.0.1", port)["state"] == "ours"
        assert cli_extras.probe("127.0.0.1", port) is cli_extras._verdicts[("127.0.0.1", port)]
    finally:
        server.shutdown()
        server.server_close()


def test_a_port_that_was_ours_and_failed_is_looked_at_again(monkeypatch):
    port, stop = serve(Status)
    at(monkeypatch, port)
    assert figaro._request("GET", "/status")[0] == 200
    assert cli_extras._verdicts[("127.0.0.1", port)]["state"] == "ours"
    stop()
    status, body = figaro._request("GET", "/status")
    assert status == 0 and cli_extras._verdicts == {}


def test_wait_does_not_trust_a_foreign_verdict_from_earlier(monkeypatch, capsys):
    port, stop = serve(Status)
    at(monkeypatch, port)
    cli_extras._verdicts[("127.0.0.1", port)] = {"state": "foreign", "why": "GET / answered HTTP 404"}
    try:
        assert figaro.cmd_wait(figaro.build_parser().parse_args(["wait"])) == 0
    finally:
        stop()


def test_a_401_after_a_look_at_an_empty_port_is_asked_again_with_the_token(monkeypatch):
    """Another call started the bridge between our look and our request."""
    seen = []

    class NeedsToken(Status):
        def do_POST(self):
            token = self.headers.get("X-Figaro-Token")
            seen.append(token)
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            code = 200 if token else 401
            body = json.dumps({"ok": bool(token)}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    port, stop = serve(NeedsToken)
    at(monkeypatch, port)
    real = cli_extras.probe
    calls = []

    def first_look_finds_nothing(host, p, fresh=False):
        calls.append(fresh)
        return {"state": "none"} if len(calls) == 1 else real(host, p, fresh)

    monkeypatch.setattr(cli_extras, "probe", first_look_finds_nothing)
    try:
        status, body = figaro._request("POST", "/exec", {"code": "1"})
        assert (status, body["ok"]) == (200, True)
        assert seen == [None, figaro_token_of(port)]
    finally:
        stop()


class Refuses(http.server.BaseHTTPRequestHandler):
    seen = []
    error = "unexpected Host header: 'x:1'"

    def _answer(self):
        self.seen.append((self.path, self.headers.get("X-Figaro-Token")))
        body = json.dumps({"ok": False, "error": self.error}).encode()
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *a):
        pass


def test_the_bridges_refusal_of_the_host_is_shown_not_called_foreign(monkeypatch, capsys):
    handler = type("Refuses", (Refuses,), {"seen": []})
    port, stop = serve(handler)
    at(monkeypatch, port)
    try:
        status, body = figaro._request("POST", "/exec", {"code": "1"})
        assert status == 403 and "Host header" in body["error"] and not body.get("foreign")
        assert [p for p, t in handler.seen] == ["/"] and all(t is None for p, t in handler.seen)
        assert figaro.cmd_doctor(None) == 1
        assert "bridge refused the request" in capsys.readouterr().out
    finally:
        stop()


def test_another_403_is_a_foreign_program(monkeypatch):
    handler = type("Refuses", (Refuses,), {"seen": [], "error": "forbidden"})
    port, stop = serve(handler)
    at(monkeypatch, port)
    try:
        status, body = figaro._request("POST", "/exec", {"code": "1"})
        assert status == 0 and body["foreign"] is True and "HTTP 403" in body["error"]
    finally:
        stop()


def test_a_bracketed_ipv6_host_counts_as_local_for_autostart(tmp_path, monkeypatch):
    runs = fake_start(tmp_path, monkeypatch)
    assert cli_extras.autostart("[::1]", free_port()) is True
    assert runs.read_text() == "started\n"


# ─── exec's text: made here when the bridge leaves it out ────────────────

def wire(capsys, msg):
    """The bridge's own answers, as they go over HTTP, to an exec whose plugin replies `msg`: first to a
    client that does not say value_only (every CLI so far), then to one that does."""
    async def go():
        c = await make_client()
        bodies = []
        async with FakePlugin(c, reply=lambda code: dict(msg)):
            for extra in ({}, {"value_only": True}):
                r = await c.post("/exec", json={"code": "x", "timeout": 20, **extra})
                bodies.append(await r.read())
        await c.close()
        return bodies
    bodies = run(go())
    capsys.readouterr()  # the bridge's own log lines
    return bodies


def shown(monkeypatch, capsys, body, *argv):
    """What `figaro exec` prints and sends, given the bridge's answer `body` (bytes)."""
    rc, out, err, calls = cli(monkeypatch, capsys, "exec", *argv, "return x",
                              bridge_reply=figaro._answer(body, 200))
    return rc, out, re.sub(r"\(\d+ms\)", "(Nms)", err), calls[0]


VALUE_KINDS = [
    ("a string", {"value": "plain text"}),
    ("an empty string", {"value": ""}),
    ("a number", {"value": 42}),
    ("a float", {"value": 0.1 + 0.2}),
    ("true", {"value": True}),
    ("false", {"value": False}),
    ("null", {"value": None}),
    ("no return", {"value": None, "text": "Done"}),
    ("logs only", {"value": None, "text": "first\nsecond"}),
    ("NaN", {"value": None, "text": "NaN"}),
    ("an older plugin", {"value": {"a": 1}, "text": '{"a": 1}'}),
    ("non-ASCII", {"value": {"名前": "ボタン ✓", "emoji": "🎨", "ü": ["é", "—"]}}),
    ("non-ASCII text", {"value": "Done — ✓ 🎨"}),
    ("an empty array", {"value": []}),
    ("an empty object", {"value": {}}),
    ("an array", {"value": [{"id": "1:2", "name": "Card"}, {"id": "1:3", "name": "Row"}]}),
    ("a mid array", {"value": [{"id": f"1:{i}", "name": f"Layer {i}"} for i in range(15_000)]}),
    ("an object", {"value": {"id": "1:2", "children": [{"id": "1:3", "w": 10.5, "ok": True, "x": None}]}}),
    ("a big array", {"value": [{"id": f"1:{i}", "name": f"Layer {i}"} for i in range(60_000)]}),
    ("a big string", {"value": "line of text\n" * 100_000}),
]


@pytest.mark.parametrize("what, msg", VALUE_KINDS, ids=[k for k, _ in VALUE_KINDS])
def test_exec_prints_the_same_text_whatever_the_bridge_leaves_out(monkeypatch, capsys, what, msg):
    """The text the bridge made (`result` of today's reply) is what `figaro exec` printed until now. It
    has to be what is printed from the reply that has no `result`, and from one that has both."""
    full_body, lean_body = wire(capsys, msg)
    full = figaro._answer(full_body, 200)
    text = full["result"]
    assert isinstance(text, str)
    if what in ("a big array", "a big string"):
        assert len(text) > 1_000_000
    elif "text" not in msg:
        assert "result" not in figaro._answer(lean_body, 200)  # the bridge did leave it out
    expected = text + "\n" if text else ""

    # a bridge that has the field, and one that does not
    old, new = (shown(monkeypatch, capsys, body) for body in (full_body, lean_body))
    assert old == new and old[:2] == (0, expected) and old[3]["value_only"] is True


def test_an_error_prints_the_same_with_the_field(monkeypatch, capsys):
    full_body, lean_body = wire(capsys, {"type": "error", "text": "boom", "stack": "at x", "line": 1,
                                         "source": "x"})
    full = shown(monkeypatch, capsys, full_body)
    lean = shown(monkeypatch, capsys, lean_body)
    assert full[:3] == lean[:3] and full[0] == 1 and "figaro: boom" in full[2]


def test_raw_and_json_replies_are_the_whole_answer_with_result(monkeypatch, capsys):
    msg = {"value": {"a": [1, "é"]}}
    full_body, _ = wire(capsys, msg)
    full = figaro._answer(full_body, 200)
    rc, out, err, payload = shown(monkeypatch, capsys, full_body, "--raw")
    assert "value_only" not in payload  # the bridge sends `result`: --raw prints the reply as it came
    assert (rc, out) == (0, json.dumps(full, indent=2, ensure_ascii=False) + "\n")
    assert "result" in json.loads(out)


@pytest.mark.parametrize("argv", [
    ["sel"], ["tree", "1:2"], ["find", "1:2", "name=x"], ["text", "1:2", "hi"], ["variant", "1:2", "A=B"],
    ["clone", "1:2"], ["rm", "1:2"], ["icomp", "abc"], ["exec", "return 1"],
])
def test_a_command_that_prints_the_text_asks_for_the_value_only(monkeypatch, capsys, argv):
    reply = {"ok": True, "value": {"id": "1:2"}}
    rc, out, err, calls = cli(monkeypatch, capsys, *argv, bridge_reply=reply)
    assert rc == 0 and calls[0]["value_only"] is True
    assert '"id": "1:2"' in out  # made here: the reply had no result
    rc, out, err, calls = cli(monkeypatch, capsys, *argv, "--raw", bridge_reply=reply)
    assert "value_only" not in calls[0]


def test_image_asks_for_the_value_only_unless_raw(monkeypatch, capsys, tmp_path):
    pic = tmp_path / "photo.png"
    pic.write_bytes(png(6, 4))
    reply = {"ok": True, "value": {"id": "1:2", "mode": "fit"}}
    rc, out, err, calls = cli(monkeypatch, capsys, "image", "1:2", str(pic), bridge_reply=reply)
    assert calls[0]["value_only"] is True and '"mode": "fit"' in out
    rc, out, err, calls = cli(monkeypatch, capsys, "image", "1:2", str(pic), "--raw", bridge_reply=reply)
    assert "value_only" not in calls[0]


@pytest.mark.parametrize("argv, value", [
    (["inspect", "1:2"], {"node": {"id": "1:2", "type": "FRAME", "name": "x"}}),
    (["inspect", "1:2", "--json"], {"node": {"id": "1:2", "type": "FRAME", "name": "x"}}),
    (["link", "1:2"], [{"id": "1:2", "name": "x", "url": "u"}]),
    (["shot", "1:2"], [shot(data=png(4, 4))]),
])
def test_a_reader_of_the_value_does_not_ask_for_it_alone(monkeypatch, capsys, argv, value):
    """inspect, link and shot print their own text from `value`: nothing for the bridge to leave out."""
    rc, out, err, calls = cli(monkeypatch, capsys, *argv, bridge_reply={"ok": True, "value": value})
    assert rc == 0 and all("value_only" not in payload for payload in calls)


def test_exec_shot_asks_for_the_value_only_but_the_picture_read_does_not(monkeypatch, capsys):
    fake = FakeBridge({"ok": True, "value": "5:6"}, {"ok": True, "value": [shot(node="5:6", data=png(10, 10))]})
    rc, out, err, calls = cli(monkeypatch, capsys, "exec", "--shot", "return 1", bridge_reply=fake)
    assert rc == 0 and out.startswith("5:6\n")  # the text of the script's value, made here
    first, second = calls
    assert first["value_only"] is True and "value_only" not in second and second["quick"] is True
