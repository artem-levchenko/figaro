"""Figma comments: the token the designer gives in the window's settings, the check
that reads it, the ask for it on an island, `POST /comments`, and `figaro comments`.

Figma's REST API is a fake server here: no test reaches api.figma.com. What the window
does with the token block is tests/ui.test.js; the script that finds a pin's layer is
tests/test_comments_script.py.

    pytest tests/test_comments.py
"""
import asyncio
import json
import os
import stat
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import bridge
import bridge_board
import bridge_comments
import cli_extras
import comments_text
import figaro
import figaro_token
from test_board import CAPS, boards, island, settle
from test_bridge import make_client, run
from test_exec import FakePlugin

TOKEN = "figd_" + "x" * 30 + "a1c4"
MASKED = "figd_…a1c4"
KEY = "K1aaaaaaaaaaaaaaaaaaaa"


def frame_pin(**extra):
    return dict({"id": "10", "parent_id": "", "user": {"handle": "Dana"}, "created_at": "2026-10-26T15:12:19.827Z",
                 "resolved_at": None, "message": "make this bigger", "order_id": "1",
                 "client_meta": {"node_id": "4:12", "node_offset": {"x": 120, "y": 48}, "stable_path": ["1:2", "4:12"]}},
                **extra)


def page_pin():
    return {"id": "11", "parent_id": "", "user": {"handle": "Sam"}, "created_at": "2026-10-26T16:00:00.000Z",
            "resolved_at": None, "message": "test comment for Figaro", "order_id": "2", "reactions": [],
            "client_meta": {"node_id": "1:2", "node_offset": {"x": 3288, "y": 924}, "stable_path": ["1:2"]}}


def reply(**extra):
    return dict({"id": "12", "parent_id": "10", "user": {"handle": "Sam"}, "created_at": "2026-10-26T15:30:00.000Z",
                 "resolved_at": None, "message": "agreed", "order_id": "3", "client_meta": None}, **extra)


class FakeFigma:
    """api.figma.com: the version history and the comments of one file, for one token."""

    def __init__(self, monkeypatch, *, token=TOKEN, comments=None, status=None):
        self.token, self.comments, self.status = token, comments if comments is not None else [], status
        self.seen = []        # (path, X-Figma-Token) of every request
        self.monkeypatch = monkeypatch
        self.server = None
        self.gate = None      # an Event: the version history waits for it
        self.got = []         # (method, path, JSON body) of every write
        self.write_status = None  # a write's answer instead of a 200, with Figma's message
        self.next_id = 2000

    async def __aenter__(self):
        app = web.Application()
        app.router.add_get("/v1/files/{key}/versions", self.versions)
        app.router.add_get("/v1/files/{key}/comments", self.listing)
        app.router.add_post("/v1/files/{key}/comments", self.post)
        app.router.add_post("/v1/files/{key}/comments/{cid}/reactions", self.react)
        app.router.add_delete("/v1/files/{key}/comments/{cid}", self.delete)
        self.server = TestServer(app)
        await self.server.start_server()
        self.monkeypatch.setattr(bridge_comments, "API", str(self.server.make_url("")).rstrip("/"))
        return self

    async def __aexit__(self, *exc):
        await self.server.close()

    def answer(self, request, body):
        got = request.headers.get("X-Figma-Token")
        self.seen.append((request.path, got))
        if self.status:
            return web.json_response({"status": self.status, "err": "no"}, status=self.status)
        if got != self.token:  # what Figma answers a bad token (checked live)
            return web.json_response({"status": 401, "err": "Invalid token"}, status=401)
        return web.json_response(body)

    async def versions(self, request):
        if self.gate:
            await self.gate.wait()
        return self.answer(request, {"versions": [], "pagination": {}})

    async def listing(self, request):
        return self.answer(request, {"comments": self.comments})

    async def write(self, request, body):
        got = request.headers.get("X-Figma-Token")
        sent = await request.json() if request.body_exists else None
        self.seen.append((request.path, got))
        self.got.append((request.method, request.path, sent))
        if got != self.token:
            return web.json_response({"status": 401, "err": "Invalid token"}, status=401)
        if self.write_status:
            status, said = self.write_status
            return web.json_response({"error": True, "status": status, "message": said}, status=status)
        return web.json_response(body)

    async def post(self, request):
        self.next_id += 1
        sent = await request.json()
        return await self.write(request, {"id": str(self.next_id), "parent_id": sent.get("comment_id", ""),
                                          "message": sent.get("message"), "client_meta": sent.get("client_meta")})

    async def react(self, request):
        return await self.write(request, {"error": False, "status": 200})

    async def delete(self, request):
        return await self.write(request, {"error": False, "status": 200})


def window(p, **m):
    return p.ws.send_str(json.dumps(m))


def token_file():
    return figaro_token.figma_token_path(bridge.PORT)


def figma_of(p):
    return boards(p)[-1]["figma"]


async def save(p, token=TOKEN, doc="doc:K1aaaaaaaaaaaaaaaaaaaa"):
    await window(p, type="figma-token", token=token, doc=doc)
    await asyncio.sleep(0.4)


# ─── the token ────────────────────────────────────────────────────────────

def test_a_token_figma_takes_is_kept_in_a_file_of_its_own_and_nowhere_else(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="A", key=KEY, caps=CAPS) as p:
                assert figma_of(p) == {"token": None, "check": None, "why": None, "rejected": False}
                await save(p)
                assert figma_of(p) == {"token": MASKED, "check": None, "why": None, "rejected": False}
                assert ("/v1/files/K1aaaaaaaaaaaaaaaaaaaa/versions", TOKEN) in fig.seen  # checked on this file's version history
                path = token_file()
                assert path.read_text(encoding="utf-8") == TOKEN
                assert path.parent == figaro_token.token_path(bridge.PORT).parent
                if os.name != "nt":
                    assert stat.S_IMODE(path.stat().st_mode) == 0o600
                assert [f.name for f in path.parent.glob("figma-token*")] == ["figma-token"]  # no .part left
                # never in the settings file, the board, /status, a reply or a log
                assert not (path.parent / "settings.json").exists()
                for m in p.received:
                    assert TOKEN not in json.dumps(m)
                status = await c.get("/status")
                assert TOKEN not in await status.text()
                targets = await c.get("/targets")
                assert TOKEN not in await targets.text()
                root = await c.get("/")
                assert TOKEN not in await root.text()
            await c.close()
            # the bridge starts again: the file keeps it
            bridge_comments.reset()
            c = await make_client()
            assert bridge_comments.has_token() and bridge_comments.board_part()["token"] == MASKED
            await c.close()
    run(go())


def test_a_token_figma_refuses_is_not_kept(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch, token="figd_other") as fig:
            c = await make_client()
            async with FakePlugin(c, name="A", key=KEY, caps=CAPS) as p:
                await save(p)
                assert figma_of(p) == {"token": None, "check": "error", "why": "rejected", "rejected": False}
                assert not token_file().exists()
                assert not bridge_comments.has_token()
                for m in p.received:
                    assert TOKEN not in json.dumps(m)
                fig.token = TOKEN  # the designer pastes it again, now a good one
                await save(p)
                assert figma_of(p)["token"] == MASKED and figma_of(p)["check"] is None
            await c.close()
    run(go())


@pytest.mark.parametrize("status, why", [(404, "nofile"), (429, "busy"), (500, "busy"), (401, "rejected"), (403, "rejected")])
def test_the_check_says_why_figma_didnt_take_it(monkeypatch, status, why):
    async def go():
        async with FakeFigma(monkeypatch, status=status):
            c = await make_client()
            async with FakePlugin(c, name="A", key=KEY, caps=CAPS) as p:
                await save(p)
                assert figma_of(p)["check"] == "error" and figma_of(p)["why"] == why
            await c.close()
    run(go())


def test_the_check_says_when_figma_doesnt_answer(monkeypatch):
    async def go():
        server = TestServer(web.Application())
        await server.start_server()
        url = str(server.make_url("")).rstrip("/")
        await server.close()  # nothing listens there now
        monkeypatch.setattr(bridge_comments, "API", url)
        c = await make_client()
        async with FakePlugin(c, name="A", key=KEY, caps=CAPS) as p:
            await save(p)
            # Windows tries a refused connection again for about two seconds before it gives up
            for _ in range(100):
                if figma_of(p)["check"] != "checking":
                    break
                await asyncio.sleep(0.1)
            assert figma_of(p) == {"token": None, "check": "error", "why": "network", "rejected": False}
        await c.close()
    run(go())


def test_the_check_needs_a_file_with_a_key(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="A", key=None, caps=CAPS) as p:
                await save(p)
                assert figma_of(p)["why"] == "open" and fig.seen == []
            await c.close()
    run(go())


def test_a_token_that_is_no_token_never_goes_to_figma(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="A", key=KEY, caps=CAPS) as p:
                for bad in ("", "   ", "two words", "x" * 600, "figd_" + chr(0x431), 42, None):
                    await window(p, type="figma-token", token=bad, doc="doc:K1aaaaaaaaaaaaaaaaaaaa")
                await settle()
                assert fig.seen == [] and not bridge_comments.has_token()
            await c.close()
    run(go())


def test_remove_forgets_the_token_in_every_window(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch):
            c = await make_client()
            async with FakePlugin(c, name="A", key=KEY, caps=CAPS) as a, \
                    FakePlugin(c, name="B", key="K2aaaaaaaaaaaaaaaaaaaa", caps=CAPS) as b:
                await save(a)
                assert figma_of(b)["token"] == MASKED
                await window(b, type="figma-token-remove")
                await settle()
                for p in (a, b):
                    assert figma_of(p)["token"] is None
                assert not token_file().exists()
            await c.close()
    run(go())


def test_remove_while_a_check_runs_wins_over_the_check(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            gate = fig.gate = asyncio.Event()
            c = await make_client()
            async with FakePlugin(c, name="A", key=KEY, caps=CAPS) as p:
                await window(p, type="figma-token", token=TOKEN, doc="doc:" + KEY)
                await settle()
                assert figma_of(p)["check"] == "checking"
                await window(p, type="figma-token-remove")
                await settle()
                gate.set()
                await asyncio.sleep(0.4)
                assert not bridge_comments.has_token() and not token_file().exists()
                assert figma_of(p)["token"] is None and figma_of(p)["check"] is None
            await c.close()
    run(go())


def test_remove_handled_before_the_check_first_runs_still_wins(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch):
            c = await make_client()
            async with FakePlugin(c, name="A", key=KEY, caps=CAPS) as p:
                # nothing between the two yields, so the check's task hasn't run yet
                await bridge_comments.window_message({"type": "figma-token", "token": TOKEN, "doc": "doc:" + KEY})
                await bridge_comments.window_message({"type": "figma-token-remove"})
                await asyncio.sleep(0.4)
                assert not bridge_comments.has_token() and not token_file().exists()
                assert figma_of(p)["token"] is None
            await c.close()
    run(go())


def test_a_newer_save_wins_over_a_check_that_started_before_it(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            gate = fig.gate = asyncio.Event()
            c = await make_client()
            async with FakePlugin(c, name="A", key=KEY, caps=CAPS) as p:
                await window(p, type="figma-token", token="figd_first", doc="doc:" + KEY)
                await settle()
                await window(p, type="figma-token", token=TOKEN, doc="doc:" + KEY)
                await settle()
                gate.set()
                await asyncio.sleep(0.4)
                # the first one, which Figma refuses, must not leave an error over the second
                assert figma_of(p) == {"token": MASKED, "check": None, "why": None, "rejected": False}
                assert token_file().read_text(encoding="utf-8") == TOKEN
            await c.close()
    run(go())


def test_a_failed_write_leaves_no_part_file(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch):
            c = await make_client()
            async with FakePlugin(c, name="A", key=KEY, caps=CAPS) as p:
                def boom(*a, **k):
                    raise OSError(28, "No space left on device")
                monkeypatch.setattr(os, "replace", boom)
                await save(p)
                assert not list(token_file().parent.glob("figma-token*"))
                assert bridge_comments.has_token()  # kept until the bridge restarts
            await c.close()
    run(go())


# ─── POST /comments ───────────────────────────────────────────────────────

def test_threads_keep_open_comments_with_their_replies_and_count_the_resolved():
    listed = [reply(id="13", created_at="2026-10-26T15:45:00.000Z", message="done soon"), frame_pin(), page_pin(), reply(),
              frame_pin(id="20", resolved_at="2026-10-26T17:00:00.000Z"),
              {"id": "30", "parent_id": "", "user": {"handle": "Old"}, "created_at": "2025-01-01T00:00:00.000Z",
               "message": "an old pin", "client_meta": {"x": 10.5, "y": 20}},
              {"id": "31", "parent_id": "", "message": "no pin, no user", "created_at": "2026-01-01T00:00:00Z",
               "client_meta": {"stable_path": ["5:6", "5:9"]}}, "junk"]
    found, skipped = bridge_comments.threads(listed)
    assert skipped == 1
    assert [t["id"] for t in found] == ["30", "31", "10", "11"]  # by when they began
    by = {t["id"]: t for t in found}
    assert [r["message"] for r in by["10"]["replies"]] == ["agreed", "done soon"]  # by when they came
    assert by["10"]["pin"] == {"node": "4:12", "x": 120, "y": 48}
    assert by["11"]["pin"] == {"node": "1:2", "x": 3288, "y": 924}
    assert by["30"]["pin"] == {"node": None, "x": 10.5, "y": 20}      # an older comment: a bare point
    assert by["31"]["pin"] == {"node": "5:9", "x": None, "y": None}   # only the path says where
    assert by["31"]["author"] == "someone"


def comments_of(c, **body):
    return c.post("/comments", json=body)


def test_comments_lists_the_open_ones_with_their_pins(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch, comments=[frame_pin(), page_pin(), reply(),
                                                    frame_pin(id="20", resolved_at="2026-10-26T17:00:00Z")]) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                await save(p)
                r = await comments_of(c, agent="designer")
                body = await r.json()
                assert r.status == 200 and body["ok"] and body["connected"] is True
                assert body["file"] == "Draft" and body["fileKey"] == KEY and body["skipped"] == 1
                assert [t["id"] for t in body["comments"]] == ["10", "11"]
                assert body["comments"][0]["replies"][0]["message"] == "agreed"
                assert TOKEN not in json.dumps(body)
                assert fig.seen[-1] == ("/v1/files/K1aaaaaaaaaaaaaaaaaaaa/comments", TOKEN)
            await c.close()
    run(go())


def test_comments_of_a_file_that_isnt_open_are_listed_without_a_window(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch, comments=[page_pin()]) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Other", key="K2aaaaaaaaaaaaaaaaaaaa", caps=CAPS) as p:
                await save(p, doc="doc:K2aaaaaaaaaaaaaaaaaaaa")
                key = "AbCdEfGhIjKlMnOpQrStUv"
                r = await comments_of(c, target=f"https://www.figma.com/design/{key}/Sandbox?node-id=1-2")
                body = await r.json()
                assert r.status == 200 and body["connected"] is False and body["fileKey"] == key
                assert fig.seen[-1][0] == f"/v1/files/{key}/comments"
                r = await comments_of(c, target=key)
                assert (await r.json())["fileKey"] == key
                r = await comments_of(c, target="No such file")  # not a key, not connected: as every command
                assert r.status == 409
            await c.close()
    run(go())


def test_comments_with_no_file_open_and_none_named_is_503():
    async def go():
        c = await make_client()
        r = await comments_of(c)
        assert r.status == 503
        await c.close()
    run(go())


def test_without_a_token_the_designer_is_asked_on_the_files_island(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch, comments=[page_pin()]):
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p, \
                    FakePlugin(c, name="Other", key="K2aaaaaaaaaaaaaaaaaaaa", caps=CAPS) as q:
                r = await comments_of(c, target="Draft", agent="designer")
                body = await r.json()
                assert r.status == 403 and body["needs_token"] and body["asked"] and not body["declined"]
                assert "designer was asked in the Figaro window" in body["error"]
                assert "again" in body["error"] and "without the comments" in body["error"]
                await settle()
                assert island(p, "Draft")["ask"] == {"agent": "designer"}
                assert island(q, "Other")["ask"] is None
                # a token comes: the ask is gone, and the next call works
                await save(q, doc="doc:K2aaaaaaaaaaaaaaaaaaaa")
                assert island(p, "Draft")["ask"] is None
                r = await comments_of(c, target="Draft", agent="designer")
                assert r.status == 200
            await c.close()
    run(go())


def test_not_now_stops_the_asking_until_the_bridge_restarts(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch):
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p, \
                    FakePlugin(c, name="Other", key="K2aaaaaaaaaaaaaaaaaaaa", caps=CAPS) as q:
                await comments_of(c, target="Draft", agent="designer")
                await comments_of(c, target="Other", agent="writer")
                await settle()
                assert island(p, "Draft")["ask"] and island(q, "Other")["ask"]
                await window(p, type="ask-hide", doc="doc:K1aaaaaaaaaaaaaaaaaaaa")
                await settle()
                assert island(p, "Draft")["ask"] is None and island(q, "Other")["ask"] == {"agent": "writer"}
                r = await comments_of(c, target="Draft", agent="designer")
                body = await r.json()
                assert r.status == 403 and body["declined"] and not body["asked"]
                assert "chose not to add a Figma token" in body["error"] and "without" in body["error"]
                await settle()
                assert island(p, "Draft")["ask"] is None  # not asked again
                assert island(q, "Other")["ask"] == {"agent": "writer"}  # another file is
            await c.close()
            bridge_comments.reset()  # the bridge restarts
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                r = await comments_of(c, target="Draft")
                assert (await r.json())["asked"] is True
            await c.close()
    run(go())


def test_a_file_that_isnt_open_cant_be_asked(monkeypatch):
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Other", key="K2aaaaaaaaaaaaaaaaaaaa", caps=CAPS) as p:
            r = await comments_of(c, target="AbCdEfGhIjKlMnOpQrStUv")
            body = await r.json()
            assert r.status == 403 and body["needs_token"] and body["asked"] is False
            assert "isn't open with Figaro" in body["error"] and "Figaro window" in body["error"]
            await settle()
            assert island(p, "Other")["ask"] is None
        await c.close()
    run(go())


def test_the_ask_goes_after_a_while(monkeypatch):
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
            await comments_of(c, target="Draft", agent="designer")
            await settle()
            assert island(p, "Draft")["ask"]
            monkeypatch.setattr(bridge_comments, "ASK_FOR", 0)
            await bridge_board.send()
            await asyncio.sleep(0.1)
            assert island(p, "Draft")["ask"] is None
        await c.close()
    run(go())


def test_a_token_figma_stops_taking_is_said_to_the_designer_not_the_agent(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                await save(p)
                fig.token = "figd_changed"  # revoked or expired since
                r = await comments_of(c, target="Draft")
                body = await r.json()
                assert r.status == 403 and body["token_rejected"] is True
                assert "file_comments:read" in body["error"] and "Figma token" in body["error"]
                assert TOKEN not in json.dumps(body)
                fig.status = 429
                r = await comments_of(c, target="Draft")
                assert r.status == 429 and "try again" in (await r.json())["error"]
                fig.status = 404
                r = await comments_of(c, target="Draft")
                assert r.status == 409 and "no such file" in (await r.json())["error"]
            await c.close()
    run(go())


def test_comments_when_figma_doesnt_answer_is_502(monkeypatch):
    async def go():
        server = TestServer(web.Application())
        await server.start_server()
        url = str(server.make_url("")).rstrip("/")
        await server.close()
        monkeypatch.setattr(bridge_comments, "API", url)
        c = await make_client()
        async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS):
            bridge_comments.STATE["token"] = TOKEN
            r = await comments_of(c, target="Draft")
            assert r.status == 502 and TOKEN not in await r.text()
        await c.close()
    run(go())


def test_a_plugin_that_doesnt_tell_the_file_key_is_asked_to_reload():
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Draft", key=None, caps=CAPS):
            r = await comments_of(c, target="Draft")
            assert r.status == 409 and "figaro reload" in (await r.json())["error"]
        await c.close()
    run(go())


def test_the_bridge_names_the_route_and_wants_the_api_token():
    async def go():
        c = await make_client()
        root = await (await c.get("/")).json()
        assert "POST /comments" in root["endpoints"]
        r = await c.post("/comments", json={}, headers={"X-Figaro-Token": "wrong"})
        assert r.status == 401
        await c.close()
    run(go())


# ─── figaro comments ──────────────────────────────────────────────────────

THREADS = [
    {"id": "10", "author": "Dana", "at": "2026-10-26T15:12:19.827Z", "message": "make this bigger\nand bluer",
     "pin": {"node": "4:12", "x": 120, "y": 48},
     "replies": [{"id": "12", "author": "Sam", "at": "2026-10-26T15:30:00.000Z", "message": "agreed"}]},
    {"id": "11", "author": "Sam", "at": "2026-10-26T16:00:00.000Z", "message": "test comment for Figaro",
     "pin": {"node": "1:2", "x": 3288, "y": 924}, "replies": []},
]
LISTED = {"ok": True, "file": "Sandbox 5$", "fileKey": "AbCdEfGhIjKlMnOpQrStUv", "connected": True,
          "comments": THREADS, "skipped": 2}
ANSWERS = [
    {"id": "10", "node": {"id": "4:12", "name": "Pricing", "type": "FRAME"}, "page": {"id": "1:2", "name": "Page 1"},
     "at": {"x": 520, "y": 348},
     "under": [{"id": "4:20", "name": "Card", "type": "FRAME"},
               {"id": "4:21", "name": "Get started", "type": "TEXT", "text": "Get started"}]},
    {"id": "11", "node": {"id": "1:2", "name": "Page 1", "type": "PAGE"}, "page": {"id": "1:2", "name": "Page 1"},
     "at": {"x": 3288, "y": 924}, "under": [],
     "nearest": [{"id": "1:55", "name": "Hero", "type": "FRAME", "away": 366},
                 {"id": "1:56", "name": "Footer", "type": "FRAME", "away": 900}]},
]


def run_cli(monkeypatch, capsys, *replies, argv=("comments",)):
    from test_cli import FakeBridge, cli
    return cli(monkeypatch, capsys, *argv, bridge_reply=FakeBridge(*replies))


def test_the_report_names_each_pins_layer_with_a_link(monkeypatch, capsys):
    code, out, err, calls = run_cli(monkeypatch, capsys, LISTED, {"ok": True, "value": ANSWERS})
    assert code == 0, err
    assert out.splitlines()[0] == '2 open comments in "Sandbox 5$" (2 resolved, left out)'
    link = "https://www.figma.com/design/AbCdEfGhIjKlMnOpQrStUv/Sandbox-5-?node-id="
    # a frame pin: the frame, its link, the point, the deepest layer under it, and the frame-vs-text nuance
    assert "#10  Dana  2026-10-26 15:12 UTC" in out
    assert "  | make this bigger\n  | and bluer" in out
    assert f'pinned to FRAME "Pricing" 4:12  {link}4-12 (page "Page 1"), 120 px right and 48 px down from its corner' in out
    assert f'deepest layer under the pin: TEXT "Get started" 4:21  {link}4-21  text: "Get started"' in out
    assert "path below the pinned layer: Card > Get started" in out
    assert "not necessarily the one the comment is about" in out
    # a reply
    assert "reply, Sam, 2026-10-26 15:30 UTC:\n    | agreed" in out
    # a pin on a page's empty canvas: the page, the point, nothing under it, the nearest layers
    assert 'on the page "Page 1" (1:2) at canvas (3288, 924)' in out
    assert "no layer is under the pin: it is on the page's empty canvas" in out
    assert f'nearest layers: FRAME "Hero" 1:55  {link}1-55 (366 px away); FRAME "Footer" 1:56' in out
    assert "A comment is often pinned to a frame, not to the layer it talks about" in out
    assert set(calls[0] or {}) <= {"target", "agent"}  # nothing else goes to the bridge, and no token


def test_the_report_sends_the_script_for_the_pins_as_a_quick_read(monkeypatch, capsys):
    from test_cli import FakeBridge
    sent = FakeBridge(LISTED, {"ok": True, "value": ANSWERS})
    monkeypatch.setattr(figaro, "_request", lambda method, path, payload=None, timeout=65: (
        sent.calls.append((path, payload)) or (200, [LISTED, {"ok": True, "value": ANSWERS}][len(sent.calls) - 1])))
    monkeypatch.setattr(sys, "argv", ["figaro", "comments"])
    with pytest.raises(SystemExit):
        figaro.main()
    (path1, _), (path2, exec_body) = sent.calls
    assert path1 == "/comments" and path2 == "/exec"
    assert exec_body["quick"] is True
    assert '"node": "4:12"' in exec_body["code"] and '"node": "1:2"' in exec_body["code"]


def test_no_open_comments_and_no_token_lines_in_the_report(monkeypatch, capsys):
    empty = dict(LISTED, comments=[], skipped=0)
    code, out, err, calls = run_cli(monkeypatch, capsys, empty)
    assert code == 0 and out.strip() == 'no open comments in "Sandbox 5$"' and len(calls) == 1  # nothing to look up


def test_without_layer_names_the_report_has_ids_and_says_so(monkeypatch, capsys):
    code, out, err, _ = run_cli(monkeypatch, capsys, LISTED, {"ok": False, "error": "Figma isn't answering"})
    assert code == 0
    assert "pinned to 4:12 at (120, 48) in it" in out and "pinned to 1:2 at (3288, 924) in it" in out
    assert "layer names are missing: Figma isn't answering" in err
    closed = dict(LISTED, connected=False)
    code, out, err, calls = run_cli(monkeypatch, capsys, closed)
    assert code == 0 and len(calls) == 1 and "listed by id only" in err


def test_the_report_when_a_layer_is_gone_or_a_pin_has_no_layer(monkeypatch, capsys):
    threads = [dict(THREADS[0], id="1", pin={"node": "9:9", "x": 1, "y": 2}),
               dict(THREADS[0], id="2", pin={"node": None, "x": 10, "y": 20}, replies=[])]
    code, out, err, _ = run_cli(monkeypatch, capsys, dict(LISTED, comments=threads, skipped=0),
                                {"ok": True, "value": [{"id": "1", "gone": True}]})
    assert "pinned to 9:9, which is not in the file any more (deleted)" in out
    assert "pinned to no layer, at canvas point (10, 20): the page isn't known" in out


def test_a_frame_with_nothing_under_the_pin_lists_its_children(monkeypatch, capsys):
    answer = {"id": "10", "node": {"id": "4:12", "name": "Pricing", "type": "FRAME"}, "page": {"id": "1:2", "name": "P"},
              "at": {"x": 5, "y": 5}, "under": [], "kids": 14,
              "children": [{"id": "4:13", "name": "Title", "type": "TEXT", "text": "Plans"}]}
    code, out, err, _ = run_cli(monkeypatch, capsys, dict(LISTED, comments=[THREADS[0]]), {"ok": True, "value": [answer]})
    assert "its children (first 1 of 14):" in out and 'TEXT "Title" 4:13  text: "Plans"' in out


def test_no_token_the_agent_is_told_how_to_go_on(monkeypatch, capsys):
    ask = {"ok": False, "needs_token": True, "asked": True, "declined": False, "error": bridge_comments._no_token(True, False)}
    code, out, err, _ = run_cli(monkeypatch, capsys, ask)
    assert code == 1 and out == ""
    assert "the designer was asked in the Figaro window" in err and "without the comments" in err
    declined = {"ok": False, "needs_token": True, "declined": True, "error": bridge_comments._no_token(False, True)}
    code, out, err, _ = run_cli(monkeypatch, capsys, declined)
    assert code == 1 and "chose not to add a Figma token" in err


def test_an_older_bridge_has_no_comments(monkeypatch, capsys):
    monkeypatch.setattr(figaro, "_request", lambda *a, **k: (404, {"ok": False, "error": "404: Not Found"}))
    monkeypatch.setattr(sys, "argv", ["figaro", "comments"])
    with pytest.raises(SystemExit) as done:
        figaro.main()
    err = capsys.readouterr().err
    assert done.value.code == 1
    assert "older than `figaro comments`" in err and "restart it" in err and "start-bridge" in err


def test_the_file_is_a_link_a_key_or_dash_t(monkeypatch, capsys):
    from test_cli import FakeBridge, KEY, URL
    for argv, target in ((("comments", URL), URL), (("comments", KEY), KEY), (("comments", "-T", "Draft"), "Draft"),
                         (("comments", "x", "-T", "Draft"), "Draft")):
        fake = FakeBridge(dict(LISTED, comments=[], skipped=0))
        monkeypatch.setattr(figaro, "_request", fake)
        monkeypatch.setattr(figaro, "TARGET", None)
        monkeypatch.setattr(sys, "argv", ["figaro", *argv])
        with pytest.raises(SystemExit):
            figaro.main()
        capsys.readouterr()
        assert fake.calls[0]["target"] == target


def test_raw_gives_the_json_with_the_pins(monkeypatch, capsys):
    code, out, err, _ = run_cli(monkeypatch, capsys, LISTED, {"ok": True, "value": ANSWERS}, argv=("comments", "--raw"))
    body = json.loads(out)
    assert code == 0 and body["comments"][0]["id"] == "10" and body["pins"]["11"]["under"] == []


def test_every_command_names_comments_in_the_help():
    assert "comments" in figaro.DISPATCH
    assert "figaro comments" in figaro.__doc__


def test_the_times_are_read_as_figma_writes_them():
    assert comments_text.when("2026-10-26T15:12:19.827Z") == "2026-10-26 15:12 UTC"
    assert comments_text.when("soon") == "soon" and comments_text.when(None) == ""


def test_the_bridge_code_digest_covers_the_new_module():
    assert any(p.name == "bridge_comments.py" for p in bridge._BRIDGE_CODE)


def test_a_target_that_is_not_text_is_400(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS):
                bridge_comments.STATE["token"] = TOKEN
                for bad in (5, ["Draft"], {"a": 1}, True):
                    r = await comments_of(c, target=bad)
                    assert r.status == 400 and "'target'" in (await r.json())["error"]
                assert fig.seen == []
            await c.close()
    run(go())


def test_a_file_key_from_the_plugin_that_is_not_a_key_never_goes_to_figma(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            for bad in ("../../v1/me", "abc/def", "K1aaaaaaaaaaaaaaaaaaaa?x=1", KEY + "\n"):
                async with FakePlugin(c, name="Draft", key=bad, caps=CAPS) as p:
                    bridge_comments.STATE["token"] = TOKEN
                    r = await comments_of(c, target="Draft")
                    assert r.status == 409 and "isn't one" in (await r.json())["error"]
                    bridge_comments.STATE["token"] = None
                    await window(p, type="figma-token", token=TOKEN, doc="doc:" + str(bad))
                    await settle()
                    assert figma_of(p)["check"] == "error" and figma_of(p)["why"] == "open"
            assert fig.seen == []
            await c.close()
    run(go())


def test_a_saved_token_figma_refuses_shows_as_rejected_and_asks_again(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch, comments=[page_pin()]) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                await save(p)
                assert island(p, "Draft")["ask"] is None
                fig.token = "figd_changed"
                r = await comments_of(c, target="Draft", agent="designer")
                assert r.status == 403 and (await r.json())["token_rejected"] is True
                await settle()
                assert figma_of(p)["rejected"] is True and figma_of(p)["token"] == MASKED
                assert island(p, "Draft")["ask"] == {"agent": "designer"}
                assert TOKEN not in json.dumps(p.received[-1])
                # a new token that works clears both
                fig.token = "figd_" + "y" * 30 + "b2d3"
                await save(p, token=fig.token)
                assert figma_of(p)["rejected"] is False and island(p, "Draft")["ask"] is None
            await c.close()
    run(go())


def test_remove_clears_a_rejected_token(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                await save(p)
                fig.token = "figd_changed"
                await comments_of(c, target="Draft")
                await window(p, type="figma-token-remove")
                await settle()
                assert figma_of(p) == {"token": None, "check": None, "why": None, "rejected": False}
            await c.close()
    run(go())


def test_a_403_on_the_comments_is_not_a_refused_token(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                await save(p)
                fig.status = 403  # a scope the token lacks, or a file its account can't open
                r = await comments_of(c, target="Draft", agent="designer")
                assert r.status == 403 and "scopes" in (await r.json())["error"]
                await settle()
                assert figma_of(p)["rejected"] is False and figma_of(p)["token"] == MASKED
                assert island(p, "Draft")["ask"] is None
            await c.close()
    run(go())


def test_threads_left_out_by_the_limit_are_counted(monkeypatch):
    monkeypatch.setattr(bridge_comments, "THREADS_MAX", 2)

    async def go():
        many = [frame_pin(id=str(100 + i), created_at=f"2026-10-26T15:12:{i:02d}Z") for i in range(5)]
        async with FakeFigma(monkeypatch, comments=many):
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                await save(p)
                body = await (await comments_of(c, target="Draft")).json()
                assert len(body["comments"]) == 2 and body["cut"] == 3
            await c.close()
    run(go())


def test_the_report_says_how_many_open_comments_were_left_out():
    text = comments_text.render(dict(LISTED, comments=LISTED["comments"][:1], cut=7), None)
    assert "7 more open, left out" in "\n".join(text)
    assert "more open" not in "\n".join(comments_text.render(dict(LISTED, comments=LISTED["comments"][:1]), None))


# ─── POST /comment ────────────────────────────────────────────────────────

def comment(c, **body):
    return c.post("/comment", json=body)


def posted_file():
    return figaro_token.token_path(bridge.PORT).parent / "posted-comments.json"


def test_a_new_comment_goes_on_the_layer_signed_and_is_kept_as_figaros(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                await save(p)
                r = await comment(c, target="Draft", agent="designer", message="  Fix the label  ",
                                  node="4:21", at={"x": 12, "y": 10})
                body = await r.json()
                assert r.status == 200 and body["ok"], body
                assert body == {"ok": True, "action": "post", "id": "2001", "parent": None,
                                "message": "[AI agent] Fix the label", "signed": True,
                                "file": "Draft", "fileKey": KEY, "connected": True}
                assert fig.got == [("POST", f"/v1/files/{KEY}/comments",
                                    {"message": "[AI agent] Fix the label",
                                     "client_meta": {"node_id": "4:21", "node_offset": {"x": 12, "y": 10}}})]
                assert fig.seen[-1][1] == TOKEN and TOKEN not in json.dumps(body)
                assert json.loads(posted_file().read_text(encoding="utf-8")) == {KEY: ["2001"]}
                # a text the agent signed itself isn't signed twice; with no point, the corner
                body = await (await comment(c, target="Draft", message="[AI agent] Done", node="4:21")).json()
                assert body["message"] == "[AI agent] Done" and body["signed"] is False
                assert fig.got[-1][2]["client_meta"]["node_offset"] == {"x": 0, "y": 0}
            await c.close()
    run(go())


def test_with_the_switch_off_a_comment_goes_as_written(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                await save(p)
                assert boards(p)[-1]["settings"]["sign"] is True  # on, until it is switched
                await window(p, type="set", name="sign", on=False)
                await settle()
                assert boards(p)[-1]["settings"]["sign"] is False
                body = await (await comment(c, target="Draft", message="Fix the label", node="4:21")).json()
                assert body["message"] == "Fix the label" and body["signed"] is False
                assert fig.got[-1][2]["message"] == "Fix the label"
            await c.close()
    run(go())


def test_a_reply_goes_in_the_thread_and_a_reaction_on_the_comment(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                await save(p)
                body = await (await comment(c, target="Draft", message="Done", reply="10")).json()
                assert body["ok"] and body["action"] == "reply" and body["parent"] == "10" and body["id"] == "2001"
                assert body["message"] == "[AI agent] Done"
                assert fig.got[-1] == ("POST", f"/v1/files/{KEY}/comments",
                                       {"message": "[AI agent] Done", "comment_id": "10"})
                assert json.loads(posted_file().read_text(encoding="utf-8")) == {KEY: ["2001"]}
                body = await (await comment(c, target="Draft", react="10")).json()
                assert body["ok"] and body["emoji"] == ":white_check_mark:"
                assert fig.got[-1] == ("POST", f"/v1/files/{KEY}/comments/10/reactions", {"emoji": ":white_check_mark:"})
                body = await (await comment(c, target="Draft", react="10", emoji=":eyes:")).json()
                assert fig.got[-1][2] == {"emoji": ":eyes:"}
            await c.close()
    run(go())


def test_figaro_deletes_only_the_comments_it_posted(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                await save(p)
                r = await comment(c, target="Draft", delete="10")  # the designer's own: Figma would let it
                body = await r.json()
                assert r.status == 403 and "only the comments it posted" in body["error"] and fig.got == []
                mine = (await (await comment(c, target="Draft", message="Fix", node="4:21")).json())["id"]
                r = await comment(c, target="Draft", delete=mine)
                assert r.status == 200 and (await r.json())["action"] == "delete"
                assert fig.got[-1] == ("DELETE", f"/v1/files/{KEY}/comments/{mine}", None)
                assert json.loads(posted_file().read_text(encoding="utf-8")) == {}
                r = await comment(c, target="Draft", delete=mine)  # once deleted, it is no longer Figaro's
                assert r.status == 403
            await c.close()
    run(go())


def test_a_token_that_can_only_read_is_told_what_it_needs(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                await save(p)
                fig.write_status = (403, "Invalid scope(s): file_comments:read. This endpoint requires the file_comments:write scope")
                r = await comment(c, target="Draft", agent="designer", message="Fix", node="4:21")
                body = await r.json()
                assert r.status == 403 and "file_comments:write" in body["error"] and body["token_rejected"] is False
                await settle()
                assert figma_of(p)["rejected"] is False and island(p, "Draft")["ask"] is None
                assert not posted_file().exists()
            await c.close()
    run(go())


def test_figmas_own_words_come_back_on_a_bad_request(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                await save(p)
                fig.write_status = (400, "You can't post a reply to an existing reply.")
                r = await comment(c, target="Draft", message="Done", reply="12")
                body = await r.json()
                assert r.status == 400
                assert "You can't post a reply to an existing reply." in body["error"]
                assert body["error"] == ("Figma refused it: You can't post a reply to an existing reply. Reply to the "
                                         "comment that starts the thread, by the id `figaro comments` prints after #.")
                fig.write_status = (404, "Not found")
                r = await comment(c, target="Draft", react="99")
                body = await r.json()
                assert r.status == 404 and body["missing"] is True and "#99" in body["error"]
            await c.close()
    run(go())


def test_a_refused_token_on_a_write_asks_for_one_that_posts(monkeypatch):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                r = await comment(c, target="Draft", agent="writer", message="Fix", node="4:21")
                body = await r.json()
                assert r.status == 403 and body["needs_token"] and "figaro comment`" in body["error"]
                await settle()
                assert island(p, "Draft")["ask"] == {"agent": "writer", "what": "write"}
                await save(p)
                fig.token = "figd_changed"  # Figma stops taking the saved one
                r = await comment(c, target="Draft", agent="writer", message="Fix", node="4:21")
                assert r.status == 403 and (await r.json())["token_rejected"] is True
                await settle()
                assert figma_of(p)["rejected"] is True
                assert island(p, "Draft")["ask"] == {"agent": "writer", "what": "write"}
            await c.close()
    run(go())


@pytest.mark.parametrize("body, says", [
    ({"message": "Fix"}, "'node'"),
    ({"message": "Fix", "node": "1-2"}, "'node'"),
    ({"message": "Fix", "node": "4:21", "at": {"x": "1", "y": 2}}, "'at'"),
    ({"message": "Fix", "node": "4:21", "at": {"x": float("inf"), "y": 2}}, "'at'"),
    ({"node": "4:21"}, "'message'"),
    ({"message": "  ", "reply": "10"}, "'message'"),
    ({"message": "x" * 4001, "node": "4:21"}, "at most 4000"),
    ({"message": "Done", "reply": "10/../../v1/me"}, "'reply'"),
    ({"react": "10", "emoji": "✅"}, "'emoji'"),
    ({"react": 10}, "'react'"),
    ({"delete": "10?x=1"}, "'delete'"),
    ({"react": "10", "delete": "10"}, "one of"),
])
def test_what_a_write_needs_is_checked_before_figma_is_asked(monkeypatch, body, says):
    async def go():
        async with FakeFigma(monkeypatch) as fig:
            c = await make_client()
            async with FakePlugin(c, name="Draft", key=KEY, caps=CAPS) as p:
                await save(p)
                r = await c.post("/comment", data=json.dumps(dict(body, target="Draft")),
                                 headers={"Content-Type": "application/json"})
                assert r.status == 400 and says in (await r.json())["error"]
                assert fig.got == []
            await c.close()
    run(go())


def test_the_bridge_names_the_write_route():
    async def go():
        c = await make_client()
        root = await (await c.get("/")).json()
        assert "POST /comment" in root["endpoints"]
        r = await c.post("/comment", json={}, headers={"X-Figaro-Token": "wrong"})
        assert r.status == 401
        await c.close()
    run(go())


# ─── figaro comment ───────────────────────────────────────────────────────

LAYER = {"id": "4:21", "name": "Get started", "type": "TEXT", "w": 80, "h": 20, "page": {"id": "1:2", "name": "Page 1"}}
POSTED = {"ok": True, "action": "post", "id": "2001", "parent": None, "message": "[AI agent] Fix the label",
          "signed": True, "file": "Sandbox 5$", "fileKey": "AbCdEfGhIjKlMnOpQrStUv", "connected": True}


def test_comment_looks_the_layer_up_then_posts_it_with_a_link(monkeypatch, capsys):
    from test_cli import FakeBridge
    sent = []

    def fake(method, path, payload=None, timeout=65):
        sent.append((path, payload))
        return 200, ({"ok": True, "value": LAYER} if path == "/exec" else POSTED)
    monkeypatch.setattr(figaro, "_request", fake)
    monkeypatch.setattr(figaro, "AGENT", None)  # main() sets it from -A: back to none after this test
    monkeypatch.setattr(sys, "argv", ["figaro", "comment", "4-21", "Fix", "the", "label", "-A", "writer"])
    with pytest.raises(SystemExit) as done:
        figaro.main()
    out = capsys.readouterr().out
    assert done.value.code == 0
    (p1, exec_body), (p2, body) = sent
    assert p1 == "/exec" and exec_body["quick"] is True and '"4:21"' in exec_body["code"]
    assert p2 == "/comment" and body == {"agent": "writer", "message": "Fix the label", "node": "4:21",
                                         "at": {"x": 12, "y": 10}}
    link = "https://www.figma.com/design/AbCdEfGhIjKlMnOpQrStUv/Sandbox-5-?node-id=4-21"
    assert out.splitlines() == [f'posted comment #2001 on TEXT "Get started" 4:21  {link} (page "Page 1")',
                                "  | [AI agent] Fix the label"]


def test_comment_takes_a_layer_link_as_its_file_and_at_for_the_pin(monkeypatch, capsys):
    url = "https://www.figma.com/design/AbCdEfGhIjKlMnOpQrStUv/Sandbox?node-id=4-21"
    code, out, err, calls = run_cli(monkeypatch, capsys, {"ok": True, "value": LAYER}, POSTED,
                                    argv=("comment", url, "Fix", "--at", "40,12.5"))
    assert code == 0, err
    assert calls[0]["target"] == url and calls[1]["target"] == url
    assert calls[1]["node"] == "4:21" and calls[1]["at"] == {"x": 40, "y": 12.5}
    code, out, err, calls = run_cli(monkeypatch, capsys, {"ok": True, "value": LAYER}, POSTED,
                                    argv=("comment", url, "Fix", "-T", "K2aaaaaaaaaaaaaaaaaaaa"))
    assert "but -T is K2aaaaaaaaaaaaaaaaaaaa" in str(code) and calls == []


@pytest.mark.parametrize("argv, says", [
    (("comment", "4:21"), "then the text"),
    (("comment", "page", "Fix"), "not on the page"),
    (("comment", "--reply", "10"), "the text is missing"),
    (("comment", "--delete", "10", "now"), "takes nothing else"),
    (("comment", "--react", "10", ":eyes:", ":fire:"), "one shortcode"),
    (("comment", "--reply", "10", "Done", "--at", "1,2"), "--at is for a new comment"),
    (("comment", "4:21", "Fix", "--at", "left"), "--at is X,Y"),
])
def test_comment_says_what_it_needs_before_it_asks_anything(monkeypatch, capsys, argv, says):
    code, out, err, calls = run_cli(monkeypatch, capsys, POSTED, argv=argv)
    assert code not in (0, None) and says in err + str(code) and calls == []


def test_comment_on_a_layer_that_isnt_there_posts_nothing(monkeypatch, capsys):
    code, out, err, calls = run_cli(monkeypatch, capsys, {"ok": True, "value": {"gone": True}}, POSTED,
                                    argv=("comment", "9:9", "Fix"))
    assert "no layer 9:9 in this file" in str(code) and len(calls) == 1
    code, out, err, calls = run_cli(monkeypatch, capsys, {"ok": True, "value": {"gone": True}}, POSTED,
                                    argv=("comment", "sel", "Fix"))
    assert "nothing is selected" in str(code) and len(calls) == 1 and '"sel"' in calls[0]["code"]
    code, out, err, calls = run_cli(monkeypatch, capsys, {"ok": True, "value": {"isPage": True, "id": "1:2"}}, POSTED,
                                    argv=("comment", "1:2", "Fix"))
    assert "not on the page" in str(code) and len(calls) == 1
    code, out, err, calls = run_cli(monkeypatch, capsys, {"ok": False, "error": "no file is connected"}, POSTED,
                                    argv=("comment", "4:21", "Fix"))
    assert code == 1 and "no file is connected" in err and "open in Figma with Figaro" in err and len(calls) == 1


def test_reply_react_and_delete_send_the_comment_id(monkeypatch, capsys):
    monkeypatch.setattr(figaro, "AGENT", None)  # an earlier test's -A stays in the module
    code, out, err, calls = run_cli(monkeypatch, capsys, dict(POSTED, action="reply", parent="10", message="[AI agent] Done"),
                                    argv=("comment", "--reply", "#10", "Done"))
    assert code == 0 and calls == [{"reply": "10", "message": "Done"}]
    assert out.splitlines() == ["replied in the thread of #10: #2001", "  | [AI agent] Done"]
    code, out, err, calls = run_cli(monkeypatch, capsys, dict(POSTED, action="react", id="10", emoji=":white_check_mark:"),
                                    argv=("comment", "--react", "10"))
    assert code == 0 and calls == [{"react": "10"}] and out.strip() == "reacted :white_check_mark: to #10"
    code, out, err, calls = run_cli(monkeypatch, capsys, dict(POSTED, action="react", id="10", emoji=":eyes:"),
                                    argv=("comment", "--react", "10", ":eyes:"))
    assert calls == [{"react": "10", "emoji": ":eyes:"}]
    code, out, err, calls = run_cli(monkeypatch, capsys, dict(POSTED, action="delete", id="2001"),
                                    argv=("comment", "--delete", "2001", "-T", "Sandbox"))
    assert code == 0 and calls == [{"delete": "2001", "target": "Sandbox"}] and out.strip() == "deleted comment #2001"


def test_comment_fails_with_the_bridges_words_and_an_older_bridge_says_so(monkeypatch, capsys):
    from test_cli import FakeBridge
    refused = {"ok": False, "error": "Figma refused (403): the token may only read comments"}
    code, out, err, _ = run_cli(monkeypatch, capsys, refused, argv=("comment", "--react", "10"))
    assert code == 1 and "the token may only read comments" in err
    monkeypatch.setattr(figaro, "_request", lambda *a, **k: (404, {"ok": False, "error": "404: Not Found"}))
    monkeypatch.setattr(sys, "argv", ["figaro", "comment", "--react", "10"])
    with pytest.raises(SystemExit) as done:
        figaro.main()
    assert done.value.code == 1 and "older than `figaro comment`" in capsys.readouterr().err
    monkeypatch.setattr(figaro, "_request", lambda *a, **k: (404, {"ok": False, "missing": True, "error": "Figma has no comment #10 in this file"}))
    with pytest.raises(SystemExit):
        figaro.main()
    assert "no comment #10" in capsys.readouterr().err


def test_every_command_names_comment_in_the_help():
    assert "comment" in figaro.DISPATCH
    assert "figaro comment " in figaro.__doc__
