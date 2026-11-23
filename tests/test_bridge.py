"""Bridge tests driven by a fake plugin over the real WebSocket.

Everything here runs without Figma: a small asyncio client plays the plugin's
part, which is enough to exercise the parts most likely to regress — the shared
PENDING / PLUGIN_WS state, the slot handover, and the origin/host guard.

    pip install -r requirements-dev.txt
    pytest -q
"""

import asyncio
import json
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bridge  # noqa: E402
from conftest import FIGMA_UA  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Missed:
    """What `until` gives back when the event never came: false, and in the
    failure's message what the test raised last."""

    def __init__(self, seconds, error):
        self.seconds, self.error = seconds, error

    def __bool__(self):
        return False

    def __repr__(self):
        return f"<not true within {self.seconds} s; it last raised {self.error!r}>"


async def until(test, seconds=5.0):
    """Wait for `test()` to come true and return True, or a false `Missed` after `seconds`.

    Tests wait for the event they check, not for a fixed pause: a pause that
    is long enough on a quick machine is too short on a slow one. The limit
    costs nothing unless the event never comes. A board, a name or a message
    that isn't there yet (IndexError, KeyError, StopIteration) is no answer
    yet. Keep a plain pause only where a test checks that something did NOT
    happen."""
    end = asyncio.get_running_loop().time() + seconds
    error = None
    while True:
        try:
            if test():
                return True
        except (IndexError, KeyError, StopIteration) as e:
            error = e
        if asyncio.get_running_loop().time() >= end:
            return Missed(seconds, error)
        await asyncio.sleep(0.01)


def names():
    """The file names the bridge knows its windows by (None: no name said yet)."""
    return sorted((i.get("name") for i in bridge.PLUGINS.values()), key=str)


# Every test starts from a clean bridge: see clean_state in conftest.py.


class FakePlugin:
    """Stands in for plugin/ui.html: echoes exec requests, answers pings."""

    def __init__(self, client, *, answer_ping=True, reply=None):
        self.client = client
        self.answer_ping = answer_ping
        self.reply = reply or (lambda code: {"text": "ok", "value": 42})
        self.ws = None
        self._task = None
        self.seen_codes = []
        self.exec_ids = []
        self.received = []   # every message the bridge sent, in order

    async def __aenter__(self):
        self.ws = await self.client.ws_connect("/plugin", headers={"Origin": "null"})
        await self.ws.send_str(json.dumps({"type": "hello", "version": "test"}))
        self._task = asyncio.create_task(self._pump())
        # `peers` goes to every window once its hello is taken
        assert await until(lambda: any(m["type"] == "peers" for m in self.received)), "no peers after hello"
        return self

    async def __aexit__(self, *exc):
        if self._task:
            self._task.cancel()
        if self.ws and not self.ws.closed:
            await self.ws.close()

    def go_silent(self):
        """Stop answering without closing — a laptop that went to sleep."""
        if self._task:
            self._task.cancel()

    async def _pump(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            self.received.append(m)
            if m.get("type") == "ping":
                if self.answer_ping:
                    await self.ws.send_str(json.dumps({"type": "pong"}))
            elif m.get("type") == "exec":
                self.seen_codes.append(m["code"])
                self.exec_ids.append(m["id"])
                out = self.reply(m["code"])
                await self.ws.send_str(json.dumps(
                    {"type": out.pop("type", "result"), "id": m["id"], **out}))


async def make_client():
    client = TestClient(TestServer(bridge.build_app()))
    await client.start_server()
    return client


# ─── guard ────────────────────────────────────────────────────────────────

def test_local_request_allowed():
    async def go():
        c = await make_client()
        r = await c.get("/status")
        assert r.status == 200
        assert (await r.json())["plugin_connected"] is False
        await c.close()
    run(go())


def test_request_with_origin_is_refused():
    async def go():
        c = await make_client()
        for path in ("/", "/status"):
            r = await c.get(path, headers={"Origin": "https://evil.example"})
            assert r.status == 403, path
        r = await c.post("/exec", data='{"code":"return 1"}',
                         headers={"Content-Type": "text/plain",
                                  "Origin": "https://evil.example"})
        assert r.status == 403
        await c.close()
    run(go())


def test_unexpected_host_is_refused():
    async def go():
        c = await make_client()
        bridge.ALLOWED_HOSTS = {"localhost:8788"}
        r = await c.get("/status", headers={"Host": "evil.example"})
        assert r.status == 403
        assert "Host" in (await r.json())["error"]
        await c.close()
    run(go())


def test_cross_origin_websocket_is_refused():
    async def go():
        c = await make_client()
        with pytest.raises(aiohttp.WSServerHandshakeError) as e:
            await c.ws_connect("/plugin", headers={"Origin": "https://evil.example"})
        assert e.value.status == 403
        await c.close()
    run(go())


# ─── exec round trip ──────────────────────────────────────────────────────

def test_exec_without_plugin_is_503():
    async def go():
        c = await make_client()
        r = await c.post("/exec", json={"code": "return 1"})
        assert r.status == 503
        await c.close()
    run(go())


def test_exec_round_trip():
    async def go():
        c = await make_client()
        async with FakePlugin(c) as plugin:
            r = await c.post("/exec", json={"code": "return 40 + 2"})
            body = await r.json()
            assert r.status == 200 and body["ok"] is True
            assert body["value"] == 42
            assert plugin.seen_codes == ["return 40 + 2"]
            assert bridge.PENDING == {}, "request left behind in PENDING"
        await c.close()
    run(go())


def test_error_response_carries_a_hint():
    async def go():
        c = await make_client()
        reply = lambda code: {"type": "error", "text": "in an unloaded font"}
        async with FakePlugin(c, reply=reply):
            r = await c.post("/exec", json={"code": "n.characters = 'x'"})
            body = await r.json()
            assert r.status == 500 and body["ok"] is False
            assert "h.setText" in body["hint"]
            assert bridge.PENDING == {}
        await c.close()
    run(go())


def test_timeout_returns_504_and_clears_pending():
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"type": "__drop__"}):
            # plugin never answers; the bridge must give up on its own
            r = await c.post("/exec", json={"code": "sleep", "timeout": 0.3})
            assert r.status == 504
            assert bridge.PENDING == {}
        await c.close()
    run(go())


def test_timed_out_script_interlocks_the_file_until_it_finishes():
    """A 504 does not stop the script, so the next exec on that file must get a
    409 instead of racing it; the plugin is told to abort (h.ck), and the
    interlock lifts by itself once the orphan finally replies."""
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"type": "__drop__"}) as plugin:
            r = await c.post("/exec", json={"code": "slow", "timeout": 0.3})
            assert r.status == 504
            orphan = plugin.exec_ids[0]

            r = await c.post("/exec", json={"code": "next", "timeout": 0.3})
            body = await r.json()
            assert r.status == 409 and body["abandoned"] == [orphan[:8]]
            assert {"type": "abort", "id": orphan} in plugin.received
            # what to do, for an agent at a shell
            assert "`figaro wait -T " in body["hint"] and "`figaro clear -T " in body["hint"]

            await plugin.ws.send_str(json.dumps(
                {"type": "result", "id": orphan, "text": "late"}))
            assert await until(lambda: bridge.ABANDONED == {})
            plugin.reply = lambda code: {"text": "ok", "value": 1}
            r = await c.post("/exec", json={"code": "after", "timeout": 2})
            assert r.status == 200
        await c.close()
    run(go())


def test_outdated_plugin_is_told_to_rerun():
    """A plugin running an older build than plugin/code.js gets an `outdated`
    message (the window offers Reload), /status flags it, and every exec reply
    carries a notice so an agent sees it too. A current build gets none."""
    async def go():
        c = await make_client()
        current = bridge.expected_plugin_version()
        assert current, "plugin/code.js should declare PLUGIN_VERSION"
        async with FakePlugin(c) as old, FakePlugin(c) as new:
            # FakePlugin's own hello carries no build id, which is outdated too.
            old.received.clear()
            new.received.clear()
            await old.ws.send_str(json.dumps(
                {"type": "hello", "name": "Old", "plugin": "2000-01-01.1"}))
            await new.ws.send_str(json.dumps(
                {"type": "hello", "name": "New", "plugin": current}))
            peers = lambda p: any(m["type"] == "peers" for m in p.received)  # noqa: E731
            assert await until(lambda: peers(old) and peers(new))  # both hellos taken

            assert any(m["type"] == "outdated" and m["expected"] == current
                       for m in old.received)
            assert not any(m["type"] == "outdated" for m in new.received)

            files = {f["name"]: f for f in (await (await c.get("/status")).json())["files"]}
            assert files["Old"]["outdated"] is True
            assert files["New"]["outdated"] is False

            r = await c.post("/exec", json={"code": "return 1", "target": "Old"})
            assert "re-run" in (await r.json())["notice"]
            r = await c.post("/exec", json={"code": "return 1", "target": "New"})
            assert "notice" not in await r.json()
        await c.close()
    run(go())


def test_with_the_update_button_off_agents_hear_it_is_updated_by_hand(monkeypatch):
    monkeypatch.setenv("FIGARO_UPDATE_BUTTON", "off")

    async def go():
        c = await make_client()
        async with FakePlugin(c):
            await bridge.set_update("99.0.0")
            r = await c.post("/exec", json={"code": "return 1"})
            notice = (await r.json())["notice"]
            assert "tell the user" in notice and "updated by hand" in notice
            # figaro doctor says the same: it learns of the button from /status
            assert (await (await c.get("/status")).json())["update_button"] is False
        monkeypatch.delenv("FIGARO_UPDATE_BUTTON")
        assert (await (await c.get("/status")).json())["update_button"] is True
        await c.close()
    run(go())


def test_status_says_whether_the_window_plays_sounds(monkeypatch):
    async def go():
        c = await make_client()
        monkeypatch.setenv("FIGARO_SOUNDS", "off")
        assert (await (await c.get("/status")).json())["sounds"] is False
        monkeypatch.delenv("FIGARO_SOUNDS")
        assert (await (await c.get("/status")).json())["sounds"] is True
        await c.close()
    run(go())


def test_new_release_is_offered_to_plugins():
    """When a newer vX.Y.Z is released, connected plugins get an `update`
    message (the window offers "Update to X.Y.Z"),
    a plugin connecting later gets it at hello, /status reports it and exec
    replies carry a notice. The same or an older release offers nothing."""
    async def go():
        c = await make_client()
        major, minor, patch = bridge.parse_version(bridge.VERSION)
        newer = f"{major}.{minor + 1}.0"
        async with FakePlugin(c) as early:
            await bridge.set_update(newer)
            assert await until(lambda: any(m["type"] == "update" for m in early.received))
            msg = [m for m in early.received if m["type"] == "update"][-1]
            assert msg["latest"] == newer and msg["current"] == bridge.VERSION
            assert msg["url"].endswith(f"/releases/tag/v{newer}")

            async with FakePlugin(c) as late:
                assert any(m["type"] == "update" for m in late.received)

            status = await (await c.get("/status")).json()
            assert status["update"]["latest"] == newer and status["version"] == bridge.VERSION
            r = await c.post("/exec", json={"code": "return 1"})
            notice = (await r.json())["notice"]
            # the agent tells the user rather than pulling under other agents' scripts
            assert "tell the user" in notice and "Update in the plugin's window" in notice
            assert "git pull" not in notice

            for not_newer in (bridge.VERSION, "0.9.0"):
                await bridge.set_update(not_newer)
                assert (await (await c.get("/status")).json())["update"] is None
        await c.close()
    run(go())


@pytest.mark.parametrize("tags, newest", [
    (["v2.1.0", "v2.2.0", "v2.3.0"], "2.3.0"),
    (["v2.10.0", "v2.9.9"], "2.10.0"),             # numeric, not alphabetical
    (["v3.0.0-rc1", "v2.4.0", "nightly"], "2.4.0"),  # pre-releases and junk ignored
    ([], None),
])
def test_newest_release_from_tags(tags, newest):
    assert bridge.newest_release(tags) == newest


def test_version_is_valid_and_matches_the_changelog():
    """VERSION must be x.y.z and have its own CHANGELOG section — the release
    checklist in AGENTS.md: bump both together."""
    import re
    from pathlib import Path
    assert bridge.parse_version(bridge.VERSION), bridge.VERSION
    changelog = (Path(bridge.__file__).parent / "CHANGELOG.md").read_text(encoding="utf-8")
    section = re.search(rf"^## \[{re.escape(bridge.VERSION)}\]", changelog, re.M)
    assert section, f"CHANGELOG.md has no '## [{bridge.VERSION}]' section"
    pyproject = (Path(bridge.__file__).parent / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{bridge.VERSION}"' in pyproject, "set the same version in pyproject.toml"


def test_peers_counts_only_files_that_said_hello():
    """The plugin bar's "1/2" pill: each identified connection learns its index
    and the total; a socket that has not sent `hello` yet is not a file."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as a, FakePlugin(c) as b:
            silent = await c.ws_connect("/plugin", headers={"Origin": "null"})
            await a.ws.send_str(json.dumps({"type": "hello", "name": "FileA"}))
            assert await until(lambda: "FileA" in names())
            last = lambda p: [m for m in p.received if m["type"] == "peers"][-1]
            assert await until(lambda: last(b) == {"type": "peers", "index": 2, "total": 2})
            assert await until(lambda: last(a) == {"type": "peers", "index": 1, "total": 2})
            assert last(b) == {"type": "peers", "index": 2, "total": 2}
            await silent.close()
        await c.close()
    run(go())


def test_disconnect_fails_inflight_requests():
    async def go():
        c = await make_client()
        plugin = FakePlugin(c, reply=lambda code: {"type": "__drop__"})
        await plugin.__aenter__()
        task = asyncio.create_task(
            c.post("/exec", json={"code": "slow", "timeout": 10}))
        assert await until(lambda: plugin.exec_ids)  # the script is out there
        await plugin.__aexit__()
        r = await asyncio.wait_for(task, timeout=5)
        body = await r.json()
        assert r.status == 500
        assert "disconnected" in body["error"]
        await c.close()
    run(go())


# ─── the single plugin slot ───────────────────────────────────────────────

def test_two_files_coexist_and_need_a_target():
    """Multi-file contract: one plugin per open file, all coexist; an exec with
    two files connected must name its target (409 otherwise)."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as a, FakePlugin(c) as b:
            await a.ws.send_str(json.dumps(
                {"type": "hello", "version": "test", "name": "FileA"}))
            await b.ws.send_str(json.dumps(
                {"type": "hello", "version": "test", "name": "FileB"}))
            assert await until(lambda: names() == ["FileA", "FileB"])

            r = await c.get("/targets")
            files = (await r.json())["files"]
            assert sorted(f["name"] for f in files) == ["FileA", "FileB"]

            r = await c.post("/exec", json={"code": "return 1", "timeout": 2})
            assert r.status == 409  # ambiguous — must name a target

            r = await c.post("/exec", json={"code": "return 1", "timeout": 5,
                                            "target": "FileA"})
            assert r.status == 200
            assert a.seen_codes and not b.seen_codes
        await c.close()
    run(go())


def test_same_file_reconnect_replaces_unresponsive():
    """A plugin re-Run in the same file takes over at `hello` time — but only
    after the incumbent fails a liveness probe, so a socket the OS has not torn
    down yet (a laptop that slept) cannot keep the name to itself."""
    async def go():
        c = await make_client()
        first = FakePlugin(c)
        await first.__aenter__()
        await first.ws.send_str(json.dumps(
            {"type": "hello", "version": "test", "name": "FileA"}))
        assert await until(lambda: names() == ["FileA"])
        first.go_silent()

        async with FakePlugin(c) as second:
            await second.ws.send_str(json.dumps(
                {"type": "hello", "version": "test", "name": "FileA"}))
            # The incumbent is probed before it is dropped; its socket is still
            # open, so the handover costs one probe timeout.
            assert await until(lambda: names() == ["FileA"], bridge.SLOT_PROBE_TIMEOUT + 4)

            r = await c.get("/targets")
            files = (await r.json())["files"]
            assert [f["name"] for f in files] == ["FileA"], files

            r = await c.post("/exec", json={"code": "return 1", "timeout": 5,
                                            "target": "FileA"})
            assert r.status == 200
            assert second.seen_codes, "exec never reached the new plugin"
            assert first.seen_codes == [], "stale socket was still being used"
        await first.__aexit__()
        await c.close()
    run(go())


def test_same_document_in_two_windows_coexists():
    """One file open in two tabs/windows reports the same name AND the same
    docSig. Both connections stay — evicting a live one made the two sides kick
    each other out in a loop — and a target resolves to the first live view,
    which is safe because both edit the same document. The first, not the
    newest: a window that comes later can't take a file over while the window
    that has it answers. When that one goes, the other takes over."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as first, FakePlugin(c) as second:
            for p in (first, second):
                await p.ws.send_str(json.dumps(
                    {"type": "hello", "version": "test",
                     "name": "FileA", "docSig": "abc123", "fileKey": "abc123"}))
                assert await until(lambda: names().count("FileA") == (1 if p is first else 2))

            files = (await (await c.get("/targets")).json())["files"]
            assert [f["name"] for f in files] == ["FileA", "FileA"], files

            for target in ("FileA", "abc123", None):
                r = await c.post("/exec", json={"code": f"to {target}", "timeout": 5,
                                                "target": target})
                assert r.status == 200, await r.text()
            assert len(first.seen_codes) == 3, "a target went past the first live view"
            assert not second.seen_codes

            await first.ws.close()
            assert await until(lambda: names() == ["FileA"])  # the bridge let go of it
            r = await c.post("/exec", json={"code": "after", "timeout": 5, "target": "FileA"})
            assert r.status == 200 and second.seen_codes == ["after"]
        await c.close()
    run(go())


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36",
    "",
    "figma",
], ids=["chrome", "none", "no-version"])
def test_a_page_in_a_browser_cannot_be_the_plugin(ua):
    """A sandboxed iframe on any web page connects with Origin "null", as the
    plugin's window does — but with a browser's User-Agent, not Figma's app."""
    async def go():
        c = await make_client()
        with pytest.raises(aiohttp.WSServerHandshakeError) as e:
            await c.ws_connect("/plugin", headers={"Origin": "null", "User-Agent": ua})
        assert e.value.status == 403
        assert bridge.PLUGINS == {}
        await c.close()
    run(go())


@pytest.mark.parametrize("ua", [
    FIGMA_UA,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Figma/126.9.11 Chrome/150.0.7871.250 Electron/43.7.7 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "FigmaBeta/126.9.11 Chrome/150.0.7871.250 Electron/43.7.7 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "FigmaGov/126.9.11 Chrome/150.0.7871.250 Electron/43.7.7 Safari/537.36",
    # figma-linux, the unofficial app for Linux, names itself in lowercase
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "figma-linux/0.11.5 Chrome/124.0.6367.230 Electron/30.0.8 Safari/537.36",
], ids=["figma-macos", "figma-windows", "figma-beta", "figma-gov", "figma-linux"])
def test_a_window_of_figmas_desktop_app_is_the_plugin(ua):
    async def go():
        c = await make_client()
        ws = await c.ws_connect("/plugin", headers={"Origin": "null", "User-Agent": ua})
        assert len(bridge.PLUGINS) == 1
        await ws.close()
        await c.close()
    run(go())


def test_odd_fields_in_a_hello_break_nothing():
    """A hello whose name, document id or file key isn't text: the window
    counts as one that hasn't said them, and /status, /targets and the routes
    to the other files keep working."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as good, FakePlugin(c) as odd:
            await good.ws.send_str(json.dumps(
                {"type": "hello", "version": "test", "name": "FileA", "docSig": "aaa111"}))
            await odd.ws.send_str(json.dumps(
                {"type": "hello", "version": "test", "name": 123, "docSig": {"a": 1},
                 "fileKey": [1], "plugin": 5, "caps": ["undo", 7]}))
            await odd.ws.send_str("[1, 2]")  # not an object at all
            assert await until(lambda: names() == ["FileA", None]
                               and all(i.get("hello") for i in bridge.PLUGINS.values()))
            await asyncio.sleep(0.1)  # time for the bridge to choke on what came after, if it will
            assert (await c.get("/status")).status == 200
            r = await c.get("/targets")
            assert r.status == 200
            assert [f["name"] for f in (await r.json())["files"]] == ["FileA", None]
            r = await c.post("/exec", json={"code": "x", "timeout": 5, "target": "FileA"})
            assert r.status == 200 and good.seen_codes == ["x"]
            info = [i for i in bridge.PLUGINS.values() if i.get("caps")][0]
            assert info["caps"] == ["undo"]
        await c.close()
    run(go())


def test_only_the_window_a_script_went_to_answers_it():
    """Another window can't slip in an answer to a script that went elsewhere."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as a, FakePlugin(c) as b:
            await a.ws.send_str(json.dumps({"type": "hello", "version": "test", "name": "A", "docSig": "aaa111"}))
            await b.ws.send_str(json.dumps({"type": "hello", "version": "test", "name": "B", "docSig": "bbb222"}))
            assert await until(lambda: names() == ["A", "B"])
            a.go_silent()  # A gets the script and takes its time
            task = asyncio.create_task(c.post("/exec", json={"code": "x", "timeout": 5, "target": "A"}))
            for _ in range(50):
                if bridge.PENDING:
                    break
                await asyncio.sleep(0.02)
            rid = next(iter(bridge.PENDING))
            await b.ws.send_str(json.dumps({"type": "result", "id": rid, "value": "forged"}))
            await asyncio.sleep(0.1)  # nothing may come of it
            assert not task.done(), "a window the script did not go to answered it"
            await a.ws.send_str(json.dumps({"type": "result", "id": rid, "value": "from A"}))
            r = await task
            assert (await r.json())["value"] == "from A"
        await c.close()
    run(go())


def test_two_documents_sharing_a_name_stay_ambiguous():
    """Same name, different docSig = two genuinely different files. Neither may
    be picked for the caller — a write would land in a file they did not choose."""
    async def go():
        c = await make_client()
        async with FakePlugin(c) as first, FakePlugin(c) as second:
            await first.ws.send_str(json.dumps(
                {"type": "hello", "version": "test",
                 "name": "Untitled", "docSig": "aaa"}))
            await second.ws.send_str(json.dumps(
                {"type": "hello", "version": "test",
                 "name": "Untitled", "docSig": "bbb"}))
            assert await until(lambda: names() == ["Untitled", "Untitled"], bridge.SLOT_PROBE_TIMEOUT + 4)

            r = await c.post("/exec", json={"code": "return 1", "timeout": 5,
                                            "target": "Untitled"})
            assert r.status == 409
            assert not first.seen_codes and not second.seen_codes
        await c.close()
    run(go())


# ─── hints ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("error_text,expected", [
    ("Cannot assign to read only property 'fills'", "h.bF()"),
    # what QuickJS, in Figma, says in strict mode (checked live)
    ("'color' is read-only", "copy the array"),
    ("'x' is read-only", "declare it with let"),
    ("object is not extensible", "copy the array"),
    ("no setter for property", "node.resize(w, h)"),
    ("'x' is not defined", "const or let"),
    ("not a function", "figma.createAutoLayout()"),
    ("in an unloaded font Inter Bold", "h.setText"),
    ('in get_currentUser: "currentuser" permission not specified in manifest.json', "figma.currentUser"),
    ("Unable to find a variant matching", "h.variantsOf"),
    ("aborted: figma.importComponentByKeyAsync() did not answer before --timeout ran out — the script "
     "was stopped", "A retry is unlikely to help"),
    ("aborted: --timeout ran out while the script waited for figma.importStyleByKeyAsync() — the script "
     "was stopped; of the 181 s it ran, its calls to Figma took 178 s", "Split the work into several"),
    ("aborted: the loop on line 3 waited for Figma most of its time, and --timeout ran out — the script "
     "was stopped", "Split the work into several"),
    ("aborted: the loop on line 3 was still running when --timeout ran out", "this loop never ended"),
    ("something entirely unrecognised", None),
])
def test_find_hint(error_text, expected):
    hint = bridge.find_hint(error_text)
    if expected is None:
        assert hint is None
    else:
        assert expected in hint


# ─── result shape (the plugin ships each value once) ──────────────────────

@pytest.mark.parametrize("msg, text", [
    ({"value": "plain"}, "plain"),
    ({"value": 42}, "42"),
    ({"value": True}, "true"),
    ({"value": None}, "null"),
    ({"value": {"a": [1, "Done — ✓"]}}, '{\n  "a": [\n    1,\n    "Done — ✓"\n  ]\n}'),
    ({"value": None, "text": "Done"}, "Done"),          # no return value
    ({"value": None, "text": "NaN"}, "NaN"),            # value can't say it
    ({"value": 1, "text": "from an older plugin"}, "from an older plugin"),
])
def test_result_text_is_derived_from_the_value(msg, text):
    assert bridge._result_text(msg) == text


def test_huge_results_skip_the_slow_pretty_printer():
    big = [{"id": i} for i in range(200_000)]
    text = bridge._result_text({"value": big})
    assert "\n" not in text and json.loads(text) == big


# What a client that asks with value_only prints is made from the value it was sent, which went
# through the bridge's JSON: it has to be the text the bridge would have made from its own copy.
TRICKY_VALUES = [
    "plain", "", "Done — ✓ 🎨 \u2028 \x00", True, False, None, 0, -3, 2.5, 1.0, 1e21, 1e-7, -0.0,
    2 ** 53 + 1, [], {}, [[]], {"a": {}}, {"é": "ü", "": None, "a b": [1, 2.25, "x"]},
    [{"id": f"1:{i}", "name": "Card ✓", "w": i / 7} for i in range(50)],
    {"k": "v" * 2000, "n": list(range(300))},
    [{"id": i} for i in range(200_000)],          # past the size where it goes compact
]


@pytest.mark.parametrize("value", TRICKY_VALUES, ids=lambda v: type(v).__name__ + str(len(str(v))))
def test_the_text_of_a_value_survives_the_trip_to_the_client(value):
    import result_text
    sent = json.loads(json.dumps({"value": value}))["value"]  # the reply's JSON, both ways
    assert result_text.render(sent) == result_text.render(value) == bridge._result_text({"value": value})


def test_result_text_loads_without_the_bridge():
    """The CLI imports it: no aiohttp, no bridge."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import result_text; "
            "bad = [m for m in ('aiohttp', 'bridge') if m in sys.modules]; "
            "assert not bad, bad")
    done = subprocess.run([sys.executable, "-I", "-c", code, str(root)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_batched_log_lines_arrive_in_order():
    async def go():
        c = await make_client()
        async with FakePlugin(c, reply=lambda code: {"type": "__drop__"}) as p:
            task = asyncio.create_task(c.post("/exec", json={"code": "x", "timeout": 5}))
            assert await until(lambda: p.exec_ids)
            rid = p.exec_ids[0]
            await p.ws.send_str(json.dumps({"type": "log", "id": rid, "lines": ["a", "b"]}))
            await p.ws.send_str(json.dumps({"type": "log", "id": rid, "lines": ["c"]}))
            await p.ws.send_str(json.dumps({"type": "result", "id": rid, "value": 1}))
            body = await (await task).json()
            assert body["logs"] == ["a", "b", "c"] and body["result"] == "1"
        await c.close()
    run(go())


def test_loopback_listens_on_ipv4_and_ipv6():
    """`localhost` is ::1 first on Windows and macOS; without ::1 every client
    saying `localhost` stalled ~2 s per request on Windows."""
    import socket
    hosts = bridge._bind_hosts("127.0.0.1", 0)
    assert hosts[0] == "127.0.0.1"
    if socket.has_ipv6:
        assert "::1" in hosts or len(hosts) == 1  # IPv6 may be disabled on a box
    assert bridge._bind_hosts("0.0.0.0", 0) == ["0.0.0.0"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows restarts through start-bridge.ps1")
def test_ipv6_survives_a_restart_in_place():
    """Update or Reload restarts the bridge in place, right after it closed the
    plugins' sockets: their [::1] ends wait in TIME_WAIT, which once made the
    ::1 check fail, and the new bridge listened on 127.0.0.1 alone."""
    import socket
    import time
    try:
        server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        # as asyncio sets it on the bridge's listener; on Linux the closed
        # sockets keep it, and a TIME_WAIT without it blocks every bind
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("::1", 0))
    except OSError:
        pytest.skip("no IPv6 here")
    server.listen()
    port = server.getsockname()[1]
    client = socket.create_connection(("::1", port))
    conn, _ = server.accept()
    conn.close()  # the bridge closes first, so its end waits in TIME_WAIT
    client.recv(1)
    client.close()
    server.close()
    time.sleep(0.1)
    assert bridge._bind_hosts("127.0.0.1", port) == ["127.0.0.1", "::1"]
