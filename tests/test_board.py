"""The board every plugin window draws its islands from, the window's buttons,
`POST /done`, and Update and Reload.

A fake plugin plays each window; what the window does with the board is
tests/ui.test.js.

    pytest tests/test_board.py
"""
import asyncio
import json
import subprocess

import pytest

import bridge
import bridge_board
import bridge_update
import figaro_token
from test_bridge import make_client, names, run, until
from test_exec import CAPS as OLD_CAPS, FakePlugin

CAPS = OLD_CAPS + ("board",)
NEWER = "9.9.9"


def boards(p):
    return [m for m in p.received if m.get("type") == "board"]


def island(p, name):
    """What the last board says about one file."""
    return next(f for f in boards(p)[-1]["files"] if f["name"] == name)


def agents(p, name):
    """The file's agents in the last board, by name."""
    return {a["name"]: a for a in island(p, name)["agents"]}


async def settle():
    """A pause, for what must NOT happen: a board goes out 50 ms after a change.
    What must happen is waited for with `until`."""
    await asyncio.sleep(0.15)


class Stoppable(FakePlugin):
    """The script "loop" runs until the bridge aborts it, then fails the way h.ck() makes it."""

    async def _answer(self, m):
        if m.get("code") != "loop":
            return await super()._answer(m)
        for _ in range(60):
            if any(x.get("type") == "abort" and x.get("id") == m["id"] for x in self.received):
                await self.ws.send_str(json.dumps({
                    "type": "error", "id": m["id"], "line": 3,
                    "text": "aborted: the bridge gave up on this run (timeout or client disconnect)"}))
                return
            await asyncio.sleep(0.05)
        await self.ws.send_str(json.dumps({"type": "result", "id": m["id"], "value": 1}))


def stop(p, doc, agent=None):
    """The Stop button of an agent's row; without `agent`, an older window's Stop."""
    return p.ws.send_str(json.dumps(dict({"type": "stop", "doc": doc}, **({"agent": agent} if agent else {}))))


# ─── what the islands show ────────────────────────────────────────────────

def test_every_window_sees_what_scripts_did_in_every_file():
    made = {"value": 1, "changes": {"created": 3, "deleted": 0, "changed": 0, "styles": 0, "props": {},
                                    "items": [], "top": [{"id": "5:6", "name": "KPI card", "type": "COMPONENT"}]}}

    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Dashboard", key="KEY1", caps=CAPS, reply=lambda m: made) as a, \
                FakePlugin(c, name="UI kit", key="KEY2", caps=CAPS) as b:
            r = await c.post("/exec", json={"code": "x", "target": "Dashboard", "agent": "designer"})
            assert r.status == 200
            assert await until(lambda: island(a, "Dashboard")["recent"] and island(b, "Dashboard")["recent"])
            f = island(b, "Dashboard")
            assert (f["doc"], f["sig"]) == ("doc:KEY1", "KEY1")
            assert f["recent"][0]["agent"] == "designer" and f["recent"][0]["kind"] == "ok"
            assert f["recent"][0]["summary"] == "Created KPI card"
            assert f["recent"][0]["layers"] == [{"id": "5:6", "name": "KPI card", "type": "COMPONENT"}]
            designer = agents(b, "Dashboard")["designer"]
            assert designer["busy"] is False and designer["since"] <= designer["last"]
            assert (designer["done"], designer["stopped"], designer["error"]) == (None, None, None)
            assert island(a, "UI kit")["agents"] == [] and island(a, "UI kit")["recent"] == []
            board = boards(a)[-1]
            assert board["version"] == bridge.VERSION and board["updating"] is None
            assert board["active_for"] == bridge_board.ACTIVE_FOR
        await c.close()
    run(go())


def test_agents_are_busy_while_their_scripts_run_or_wait():
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Draft", key="KEY1", caps=CAPS, delay=0.8) as p:
            first = asyncio.ensure_future(c.post("/exec", json={"code": "a", "agent": "designer"}))
            assert await until(lambda: p.of("exec"))
            second = asyncio.ensure_future(c.post("/exec", json={"code": "b"}))
            busy = lambda: {n: a["busy"] for n, a in agents(p, "Draft").items()}  # noqa: E731
            expected = {"designer": True, "agent": True}  # running, and waiting in the queue; no -A: "agent"
            assert await until(lambda: busy() == expected), busy()
            assert (await first).status == 200 and (await second).status == 200
            assert await until(lambda: not any(busy().values()))
        await c.close()
    run(go())


def test_an_agent_at_work_keeps_its_run_of_scripts(monkeypatch):
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Draft", key="KEY1", caps=CAPS) as p:
            designer = lambda: agents(p, "Draft")["designer"]  # noqa: E731
            await c.post("/exec", json={"code": "a", "agent": "designer"})
            assert await until(lambda: designer()["since"])
            since = designer()["since"]
            await c.post("/exec", json={"code": "b", "agent": "designer"})
            assert await until(lambda: designer()["last"] > since)
            a = designer()
            assert a["since"] == since and a["last"] > since  # one run: the window's clock goes on
            await c.post("/done", json={"agent": "designer"})
            await c.post("/exec", json={"code": "c", "agent": "designer"})
            assert await until(lambda: designer()["since"] > since)  # done, then a new run
            since = designer()["since"]
            monkeypatch.setattr(bridge_board, "ACTIVE_FOR", 0.1)
            await asyncio.sleep(0.2)  # quiet for longer than that
            await c.post("/exec", json={"code": "d", "agent": "designer"})
            assert await until(lambda: designer()["since"] > since)  # a new run too
        await c.close()
    run(go())


def test_a_failed_script_stays_until_its_agent_gets_past_it():
    fail = {"type": "error", "text": "Cannot read properties of null (reading 'fills')", "line": 14}

    async def go():
        c = await make_client()
        reply = lambda m: fail if m["code"] == "bad" else {"value": 1}  # noqa: E731
        async with FakePlugin(c, name="Draft", key="KEY1", caps=CAPS, reply=reply) as p:
            await c.post("/exec", json={"code": "bad", "agent": "designer"})
            assert await until(lambda: agents(p, "Draft")["designer"]["error"])
            error = agents(p, "Draft")["designer"]["error"]
            assert (error["line"], error["text"]) == (14, fail["text"])
            assert {k: island(p, "Draft")["recent"][0][k] for k in ("kind", "summary", "note")} == \
                {"kind": "err", "summary": "Script failed", "note": "line 14"}
            await c.post("/exec", json={"code": "ok", "agent": "icons"})
            assert await until(lambda: "icons" in agents(p, "Draft"))
            assert agents(p, "Draft")["designer"]["error"] is not None  # another agent can't fix it
            await c.post("/exec", json={"code": "ok", "agent": "designer"})
            assert await until(lambda: agents(p, "Draft")["designer"]["error"] is None)
            await c.post("/exec", json={"code": "bad", "agent": "designer"})
            await c.post("/done", json={"agent": "designer", "text": "Gave up on the fills"})
            assert await until(lambda: agents(p, "Draft")["designer"]["done"])
            assert agents(p, "Draft")["designer"]["error"] is None  # it said it is done
        await c.close()
    run(go())


def test_done_shows_the_note_until_the_agent_runs_again():
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Draft", key="KEY1", caps=CAPS) as p:
            r = await c.post("/done", json={"text": "  Check the card\n and say go ", "agent": "designer"})
            assert r.status == 200
            assert await r.json() == {"ok": True, "file": "Draft", "text": "Check the card and say go"}
            assert await until(lambda: agents(p, "Draft")["designer"]["done"])
            assert agents(p, "Draft")["designer"]["done"]["text"] == "Check the card and say go"
            await c.post("/exec", json={"code": "x", "agent": "icons"})
            assert await until(lambda: "icons" in agents(p, "Draft"))
            assert agents(p, "Draft")["designer"]["done"] is not None  # someone else's script
            await c.post("/exec", json={"code": "x", "agent": "designer"})
            assert await until(lambda: agents(p, "Draft")["designer"]["done"] is None)  # the agent is back at work
            r = await c.post("/done", json={"text": " "})
            assert r.status == 200 and (await r.json())["text"] is None  # done without a note
            assert await until(lambda: agents(p, "Draft")["agent"]["done"])
            assert agents(p, "Draft")["agent"]["done"]["text"] is None
            r = await c.post("/done", json={"text": 5})
            assert r.status == 400
        async with FakePlugin(c, name="Old", key="KEY2", caps=OLD_CAPS):
            r = await c.post("/done", json={"text": "hi", "target": "Old"})
            assert r.status == 409 and "figaro reload" in (await r.json())["error"]
        await c.close()
    run(go())


def test_a_look_after_done_or_a_stop_leaves_it_on_show():
    looks = ({"read_only": True}, {"quick": True}, {"parallel": True})  # -R; shot, link…; --parallel

    async def go():
        c = await make_client()
        async with Stoppable(c, name="Draft", key="KEY1", caps=CAPS) as p:
            async def look():
                last = agents(p, "Draft")["designer"]["last"]
                for extra in looks:
                    r = await c.post("/exec", json=dict({"code": "look", "agent": "designer"}, **extra))
                    assert r.status == 200
                assert await until(lambda: agents(p, "Draft")["designer"]["last"] > last)
                return agents(p, "Draft")["designer"]

            await c.post("/exec", json={"code": "a", "agent": "designer"})
            await c.post("/done", json={"agent": "designer", "text": "Check the card"})
            assert await until(lambda: agents(p, "Draft")["designer"]["done"])
            last = agents(p, "Draft")["designer"]["last"]
            designer = await look()  # a last shot or link for its report
            assert designer["done"]["text"] == "Check the card" and designer["last"] > last
            await c.post("/exec", json={"code": "b", "agent": "designer"})
            assert await until(lambda: agents(p, "Draft")["designer"]["done"] is None)  # it can change the file: at work
            req = asyncio.ensure_future(c.post("/exec", json={"code": "loop", "agent": "designer"}))
            assert await until(lambda: any(m["code"] == "loop" for m in p.of("exec")))
            await stop(p, "doc:KEY1", "designer")
            assert (await req).status == 409
            assert (await look())["stopped"]  # it looks at what the stopped script left
            await c.post("/exec", json={"code": "c", "agent": "designer"})
            assert await until(lambda: agents(p, "Draft")["designer"]["stopped"] is None)
        await c.close()
    run(go())


def test_doctors_checks_are_no_agents_work():
    check = {"code": "return 1 + 1;", "quick": True, "probe": True}

    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Draft", key="KEY1", caps=CAPS) as p:
            for parallel in (False, True):
                assert (await c.post("/exec", json=dict(check, parallel=parallel))).status == 200
            await c.post("/exec", json={"code": "a", "agent": "designer"})
            assert await until(lambda: "designer" in agents(p, "Draft"))
            assert list(agents(p, "Draft")) == ["designer"]  # no "agent" at work in the window
            await stop(p, "doc:KEY1", "designer")  # between its scripts: its next one is refused
            assert await until(lambda: agents(p, "Draft")["designer"]["stopped"])
            assert (await c.post("/exec", json=dict(check, agent="designer"))).status == 200
            r = await c.post("/exec", json={"code": "b", "agent": "designer"})
            assert r.status == 409 and "did not run" in (await r.json())["error"]  # the Stop waited for it
        await c.close()
    run(go())


@pytest.mark.parametrize("changes, expected", [
    ({"created": 4, "top": [{"id": "5:6", "name": "Card", "type": "FRAME", "op": "+"}]},
     {"summary": "Created Card", "layers": [{"id": "5:6", "name": "Card", "type": "FRAME"}]}),
    ({"created": 9, "top": [{"id": f"1:{i}", "name": f"Card {i}", "type": "FRAME"} for i in range(5)],
      "topMore": 2},
     {"summary": "Created 7 layers", "more": 4,
      "layers": [{"id": f"1:{i}", "name": f"Card {i}", "type": "FRAME"} for i in range(3)]}),
    ({"changed": 14, "props": {"fills": 14, "strokes": 2, "effects": 1},
      "items": [{"op": "~", "id": "1:1", "name": "arrow-up", "type": "INSTANCE"},
                {"op": "~", "id": "S:1", "name": "Brand", "type": "STYLE"}]},
     {"summary": "Changed 14 layers", "note": "fills, strokes", "more": 13,
      "layers": [{"id": "1:1", "name": "arrow-up", "type": "INSTANCE"}]}),
    ({"changed": 1, "props": {"name": 1}, "items": [{"op": "~", "id": "2:2", "name": "Filters", "type": "FRAME"}]},
     {"summary": "Renamed 1 layer", "layers": [{"id": "2:2", "name": "Filters", "type": "FRAME"}]}),
    ({"deleted": 3}, {"summary": "Deleted 3 layers", "layers": []}),
    ({"styles": 2}, {"summary": "Changed 2 styles", "layers": []}),
    ({"created": 0, "changed": 0}, None),
    (None, None),
])
def test_what_a_script_did_in_a_few_words(changes, expected):
    assert bridge_board.summary(changes) == expected


# ─── a file that stopped answering ────────────────────────────────────────

class Hanging(FakePlugin):
    """A window whose script "hang" never ends, as when Figma stops answering;
    `finish()` sends its answer at last."""

    async def _answer(self, m):
        if m.get("code") != "hang":
            return await super()._answer(m)
        self.hung = m["id"]

    async def finish(self):
        await self.ws.send_str(json.dumps({"type": "result", "id": self.hung, "value": 1}))


@pytest.fixture
def quick_red(monkeypatch):
    """Seconds, not minutes: the bridge waits no grace past a deadline, a
    script still running 0.6 s after it gave up is stuck, and a file whose
    window closed mid-script shows after 0.6 s and stays 2 s. What must not
    show yet is checked well before those; what must, until it does."""
    monkeypatch.setattr(bridge.bridge_exec, "GATE_GRACE", 0.0)
    monkeypatch.setattr(bridge_board, "STUCK_AFTER", 0.6)
    monkeypatch.setattr(bridge_board, "LOST_GRACE", 0.6)
    monkeypatch.setattr(bridge_board, "LOST_KEEP", 2.0)


def test_a_script_that_never_ends_turns_its_file_red_until_it_does(quick_red):
    async def go():
        c = await make_client()
        async with Hanging(c, name="Dashboard", key="KEY1", caps=CAPS) as a, \
                FakePlugin(c, name="UI kit", key="KEY2", caps=CAPS) as b:
            r = await c.post("/exec", json={"code": "hang", "timeout": 0.2, "agent": "designer",
                                            "target": "Dashboard"})
            assert r.status == 504
            assert await until(lambda: island(b, "Dashboard")["agents"])  # a board that knows of its script
            assert island(b, "Dashboard")["stuck"] is None  # it may yet end
            assert await until(lambda: island(b, "Dashboard")["stuck"] is not None)
            stuck = island(b, "Dashboard")["stuck"]
            assert stuck["agent"] == "designer" and island(a, "Dashboard")["stuck"] == stuck
            assert island(b, "UI kit")["stuck"] is None
            status = await (await c.get("/status")).json()
            dash = next(f for f in status["files"] if f["name"] == "Dashboard")
            assert dash["stuck"]["agent"] == "designer" and dash["stuck"]["for_s"] >= 0
            assert status["abandoned"][0]["agent"] == "designer"
            await a.finish()
            assert await until(lambda: island(b, "Dashboard")["stuck"] is None)
        await c.close()
    run(go())


def test_clear_takes_the_red_off_too(quick_red):
    async def go():
        c = await make_client()
        async with Hanging(c, name="Dashboard", key="KEY1", caps=CAPS) as a:
            assert (await c.post("/exec", json={"code": "hang", "timeout": 0.2})).status == 504
            assert await until(lambda: island(a, "Dashboard")["stuck"] is not None)
            assert island(a, "Dashboard")["stuck"]["agent"] == "agent"
            assert (await c.post("/clear", json={"target": "Dashboard"})).status == 200
            assert await until(lambda: island(a, "Dashboard")["stuck"] is None)
        await c.close()
    run(go())


@pytest.mark.parametrize("timed_out", [False, True])
def test_a_window_that_closes_mid_script_leaves_its_file_red_for_a_while(quick_red, timed_out):
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="UI kit", key="KEY2", caps=CAPS) as b:
            a = await Hanging(c, name="Dashboard", key="KEY1", caps=CAPS).__aenter__()
            call = asyncio.ensure_future(c.post("/exec", json={
                "code": "hang", "timeout": 0.2 if timed_out else 5, "agent": "designer", "target": "Dashboard"}))
            assert await until(lambda: hasattr(a, "hung"))  # the script is in the window
            if timed_out:
                assert (await call).status == 504  # it gave up on its own; then the window goes
            await a.__aexit__()
            r = await call
            assert r.status == (504 if timed_out else 500)
            await settle()  # what must not show yet: a restart (Hot reload) may yet come back
            assert boards(b)[-1]["lost"] == []  # a restart (Hot reload) may yet come back
            assert await until(lambda: boards(b)[-1]["lost"] != [])
            lost = boards(b)[-1]["lost"]
            assert [(f["doc"], f["name"], f["lost"]["agent"]) for f in lost] == [("doc:KEY1", "Dashboard", "designer")]
            assert lost[0]["agents"] == []  # nobody works there now; Recent says what happened
            assert [e["summary"] for e in lost[0]["recent"]] == (["Script timed out"] if timed_out else [])
            status = await (await c.get("/status")).json()
            assert [(f["name"], f["fileKey"], f["agent"]) for f in status["lost"]] == [("Dashboard", "KEY1", "designer")]
            assert await until(lambda: boards(b)[-1]["lost"] == [], 4)  # LOST_KEEP later it goes
        await c.close()
    run(go())


def test_a_window_back_in_time_takes_the_red_off(quick_red):
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="UI kit", key="KEY2", caps=CAPS) as b:
            a = await Hanging(c, name="Dashboard", key="KEY1", caps=CAPS).__aenter__()
            call = asyncio.ensure_future(c.post("/exec", json={"code": "hang", "timeout": 5, "target": "Dashboard"}))
            assert await until(lambda: hasattr(a, "hung"))  # the script is in the window
            await a.__aexit__()
            assert (await call).status == 500
            async with FakePlugin(c, name="Dashboard", key="KEY1", caps=CAPS):
                await asyncio.sleep(0.3)
                assert boards(b)[-1]["lost"] == [] and island(b, "Dashboard")["stuck"] is None
            await asyncio.sleep(0.3)
            assert boards(b)[-1]["lost"] == []  # closed with no script running: it just goes
        await c.close()
    run(go())


# ─── Stop ─────────────────────────────────────────────────────────────────

def test_stop_from_any_window_ends_the_agents_script_with_a_409():
    async def go():
        c = await make_client()
        async with Stoppable(c, name="Draft", key="KEY1", caps=CAPS) as p, \
                FakePlugin(c, name="Other", key="KEY2", caps=CAPS) as other:
            req = asyncio.ensure_future(c.post("/exec", json={"code": "loop", "agent": "designer",
                                                              "target": "Draft"}))
            assert await until(lambda: p.of("exec"))  # running
            await stop(other, "doc:KEY1", "designer")
            r = await req
            body = await r.json()
            assert r.status == 409 and body["stopped"] is True and "pressed Stop" in body["error"]
            assert "line" not in body  # where the Stop caught it is no bug of the script
            assert p.of("abort")
            assert await until(lambda: agents(p, "Draft")["designer"]["stopped"])
            designer = agents(p, "Draft")["designer"]
            assert designer["stopped"] and designer["error"] is None
            assert island(p, "Draft")["recent"][0]["kind"] == "stop"
            # It got its 409 already: once the user answers it, its scripts run.
            assert (await c.post("/exec", json={"code": "x", "agent": "designer", "target": "Draft"})).status == 200
            assert await until(lambda: agents(p, "Draft")["designer"]["stopped"] is None)  # back at work
        await c.close()
    run(go())


def test_stop_is_for_one_agent():
    async def go():
        c = await make_client()
        async with Stoppable(c, name="Draft", key="KEY1", caps=CAPS) as p:
            designer = asyncio.ensure_future(c.post("/exec", json={"code": "loop", "agent": "designer"}))
            assert await until(lambda: p.of("exec"))
            icons = asyncio.ensure_future(c.post("/exec", json={"code": "loop", "agent": "icons"}))
            assert await until(lambda: agents(p, "Draft").get("icons", {}).get("busy"))  # in the queue
            await stop(p, "doc:KEY1", "icons")  # waiting in the queue behind designer
            assert await until(lambda: agents(p, "Draft")["icons"]["stopped"])
            await stop(p, "doc:KEY1", "designer")
            assert (await designer).status == 409
            r = await icons
            body = await r.json()
            assert r.status == 409 and "did not run" in body["error"]
            assert len(p.of("exec")) == 1  # only designer's script reached the plugin
        await c.close()
    run(go())


def test_stop_between_scripts_refuses_the_next_one_once(monkeypatch):
    def held():
        """The bridge took the Stop and keeps it for the agent's next script."""
        return bridge_board.FILES["doc:KEY1"]["agents"]["designer"]["hold"]

    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Draft", key="KEY1", caps=CAPS) as p:
            await c.post("/exec", json={"code": "a", "agent": "designer"})
            await stop(p, "doc:KEY1", "designer")
            assert await until(held)  # the bridge has it: a script that comes now is refused
            assert (await c.post("/exec", json={"code": "b", "agent": "icons"})).status == 200  # not icons
            for parallel in (True, False):
                if not held():  # the first Stop still stands; a refusal uses one up
                    await stop(p, "doc:KEY1", "designer")
                    assert await until(held)
                r = await c.post("/exec", json={"code": "b", "agent": "designer", "parallel": parallel})
                body = await r.json()
                assert r.status == 409 and body["stopped"] is True and "did not run" in body["error"]
            assert [m["code"] for m in p.of("exec")] == ["a", "b"]  # nothing of designer's ran
            await settle()
            assert agents(p, "Draft")["designer"]["stopped"]  # still stopped: it asks the user now
            assert (await c.post("/exec", json={"code": "c", "agent": "designer"})).status == 200
            monkeypatch.setattr(bridge_board, "HOLD", 0.1)
            assert not held()
            await stop(p, "doc:KEY1", "designer")
            assert await until(held)
            await asyncio.sleep(0.2)  # longer than the hold
            assert (await c.post("/exec", json={"code": "d", "agent": "designer"})).status == 200  # too late
        await c.close()
    run(go())


def test_done_lifts_a_stop():
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Draft", key="KEY1", caps=CAPS) as p:
            await stop(p, "doc:KEY1", "designer")
            assert await until(lambda: agents(p, "Draft")["designer"]["stopped"])  # before the done comes
            await c.post("/done", json={"agent": "designer", "text": "Stopped as you asked"})
            assert await until(lambda: agents(p, "Draft")["designer"]["done"])
            designer = agents(p, "Draft")["designer"]
            assert designer["stopped"] is None and designer["done"]["text"] == "Stopped as you asked"
            assert (await c.post("/exec", json={"code": "x", "agent": "designer"})).status == 200
        await c.close()
    run(go())


def test_an_older_windows_stop_ends_whatever_runs_in_the_file():
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Draft", key="KEY1", caps=CAPS, delay=1.0) as p:
            req = asyncio.ensure_future(c.post("/exec", json={"code": "tight loop"}))
            assert await until(lambda: p.of("exec"))  # it runs
            await stop(p, "doc:KEY1")
            r = await req
            # The script ran to its end despite the Stop; its agent still has to hear of it.
            assert r.status == 409 and "pressed Stop" in (await r.json())["error"]
            assert (await c.post("/exec", json={"code": "x"})).status == 200  # it was told: no hold
        await c.close()
    run(go())


# ─── Update and Reload ────────────────────────────────────────────────────

@pytest.fixture
def quick_update(monkeypatch):
    """Update and Reload without git, pip or a real restart."""
    calls = []

    async def pull(version, folder=None):
        calls.append("pull")
        assert version == NEWER  # the release the window offered, not the tip of main
        await asyncio.sleep(0.2)  # the windows see this step
        return None

    async def restart():
        calls.append("restart")
        return True

    monkeypatch.setattr(bridge_update, "pull_release", pull)
    monkeypatch.setattr(bridge_update, "restart", restart)
    monkeypatch.setattr(bridge_update, "RELOAD_WAIT", 0.1)
    monkeypatch.setattr(bridge_update, "RESTART_GRACE", 0.05)
    bridge.UPDATE = {"latest": NEWER, "current": bridge.VERSION, "url": bridge.release_url(NEWER)}
    return calls


def test_update_waits_for_the_scripts_then_pulls_reloads_and_restarts(quick_update, monkeypatch):
    monkeypatch.setattr(bridge, "bridge_outdated", lambda: True)

    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Old", key="KEY1", caps=CAPS, plugin="2000-01-01.1", delay=1.0) as p:
            req = asyncio.ensure_future(c.post("/exec", json={"code": "x", "agent": "designer"}))
            assert await until(lambda: p.of("exec"))  # it runs
            await p.ws.send_str(json.dumps({"type": "update-now"}))
            waiting = {"step": "wait", "what": "update", "to": NEWER}
            assert await until(lambda: boards(p)[-1]["updating"] == waiting)
            assert quick_update == []  # a script runs: the update waits
            assert (await req).status == 200
            assert await until(lambda: "restart" in quick_update)
            assert quick_update == ["pull", "restart"]
            reload = p.of("reload")
            assert reload and "PLUGIN_VERSION" in reload[0]["code"] and reload[0]["html"]
            steps = lambda: [b["updating"]["step"] for b in boards(p) if b.get("updating")]  # noqa: E731
            assert await until(lambda: "restart" in steps())  # the windows saw it
            steps = steps()
            assert steps.index("wait") < steps.index("pull") < steps.index("restart")
        await c.close()
    run(go())


def test_reload_refreshes_only_what_is_older(quick_update, monkeypatch):
    monkeypatch.setattr(bridge, "bridge_outdated", lambda: False)

    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Old", key="KEY1", caps=CAPS, plugin="2000-01-01.1") as old, \
                FakePlugin(c, name="New", key="KEY2", caps=CAPS) as new:
            await new.ws.send_str(json.dumps({"type": "reload-all"}))
            assert await until(lambda: old.of("reload"))
            assert await until(lambda: bridge_update.STATE["step"] is None)
            assert not new.of("reload") and quick_update == []  # no pull, no restart
        async with FakePlugin(c, name="New", key="KEY2", caps=CAPS) as new:
            await new.ws.send_str(json.dumps({"type": "reload-all"}))  # nothing older: nothing to do
            await settle()  # time for it to do something, if it will
            assert bridge_update.STATE["step"] is None and not bridge_update.FAILED
            assert not new.of("reload")
        await c.close()
    run(go())


@pytest.mark.parametrize("off", ["off", "0", "false", "No"])
def test_update_now_is_refused_when_the_update_button_is_off(quick_update, monkeypatch, off):
    monkeypatch.setenv("FIGARO_UPDATE_BUTTON", off)

    async def go():
        c = await make_client()
        async with FakePlugin(c, caps=CAPS) as p:
            await bridge.set_update(NEWER)
            await p.ws.send_str(json.dumps({"type": "update-now"}))
            assert await until(lambda: boards(p)[-1]["failed"])
            board = boards(p)[-1]
            assert quick_update == [] and board["updating"] is None
            assert board["update"] is None  # no offer, so no Update button
            assert board["release"]["latest"] == NEWER  # the window links to it instead
            assert not any(m["type"] == "update" for m in p.received)
            assert board["failed"]["error"] == "turned off"
        await c.close()
    run(go())


def test_the_update_button_is_on_unless_turned_off(monkeypatch):
    for value in (None, "", "on", "1", "yes"):
        if value is None:
            monkeypatch.delenv("FIGARO_UPDATE_BUTTON", raising=False)
        else:
            monkeypatch.setenv("FIGARO_UPDATE_BUTTON", value)
        assert bridge.update_allowed(), value
        bridge.UPDATE = {"latest": NEWER, "current": bridge.VERSION, "url": bridge.release_url(NEWER)}
        board = bridge_board.payload()
        assert board["update"]["latest"] == NEWER and board["release"] is None


def test_the_window_plays_sounds_unless_turned_off(monkeypatch):
    for value, on in ((None, True), ("", True), ("on", True), ("1", True),
                      ("off", False), ("0", False), ("false", False), (" No ", False)):
        if value is None:
            monkeypatch.delenv("FIGARO_SOUNDS", raising=False)
        else:
            monkeypatch.setenv("FIGARO_SOUNDS", value)
        assert bridge.sounds_on() is on and bridge_board.payload()["sounds"] is on, value


def test_skip_hides_a_release_from_every_window_and_the_agents_until_a_newer_one():
    bridge.UPDATE = {"latest": NEWER, "current": bridge.VERSION, "url": bridge.release_url(NEWER)}
    kept = figaro_token.token_path(bridge.PORT).parent / "skipped-release"

    async def go():
        c = await make_client()
        async with FakePlugin(c, name="A", key="KEY1", caps=CAPS) as a, \
                FakePlugin(c, name="B", key="KEY2", caps=CAPS) as b:
            await a.ws.send_str(json.dumps({"type": "skip", "version": "9.9.8"}))  # not the one on offer
            await settle()  # time for it to skip something, if it will
            assert boards(b)[-1]["update"]["latest"] == NEWER and bridge.SKIPPED is None
            await a.ws.send_str(json.dumps({"type": "skip", "version": NEWER}))
            assert await until(lambda: all(boards(p)[-1]["skipped"] == NEWER for p in (a, b)))
            for p in (a, b):
                board = boards(p)[-1]
                assert (board["update"], board["release"], board["skipped"]) == (None, None, NEWER)
            assert kept.read_text(encoding="utf-8").strip() == NEWER
            r = await c.post("/exec", json={"code": "return 1"})
            assert "released" not in ((await r.json()).get("notice") or "")
            status = await (await c.get("/status")).json()
            assert status["update"]["latest"] == NEWER and status["skipped"] == NEWER
            async with FakePlugin(c, name="C", key="KEY3", caps=CAPS) as late:
                assert not late.of("update")  # a window that comes later hears nothing of it
        await c.close()

        bridge.SKIPPED = None  # the bridge starts again: the file keeps the skip
        c = await make_client()
        assert bridge.SKIPPED == NEWER
        async with FakePlugin(c, caps=CAPS) as p:
            assert await until(lambda: boards(p))  # its first board
            assert boards(p)[-1]["update"] is None
            await bridge.set_update("10.0.0")  # a newer release is offered again
            assert await until(lambda: boards(p)[-1]["update"])
            assert boards(p)[-1]["update"]["latest"] == "10.0.0" and p.of("update")
        await c.close()
    run(go())


def test_skip_with_the_update_button_off_hides_the_link(monkeypatch):
    monkeypatch.setenv("FIGARO_UPDATE_BUTTON", "off")
    bridge.UPDATE = {"latest": NEWER, "current": bridge.VERSION, "url": bridge.release_url(NEWER)}
    assert bridge_board.payload()["release"]["latest"] == NEWER
    assert bridge.skip_release(NEWER)
    assert bridge_board.payload()["release"] is None


def test_update_now_with_no_newer_release_pulls_nothing(quick_update):
    bridge.UPDATE = None  # no check, or nothing newer: an old window's button, or anyone else

    async def go():
        c = await make_client()
        async with FakePlugin(c, caps=CAPS) as p:
            await p.ws.send_str(json.dumps({"type": "update-now"}))
            assert await until(lambda: boards(p)[-1]["failed"])
            board = boards(p)[-1]
            assert quick_update == [] and board["updating"] is None
            assert board["failed"]["error"] == "no new release"
        await c.close()
    run(go())


def test_a_pull_that_fails_says_why_and_offers_the_release(monkeypatch):
    async def pull(version, folder=None):
        return "local changes", "git: error: Your local changes would be overwritten by merge"
    monkeypatch.setattr(bridge_update, "pull_release", pull)
    bridge.UPDATE = {"latest": NEWER, "current": bridge.VERSION, "url": bridge.release_url(NEWER)}

    async def go():
        c = await make_client()
        async with FakePlugin(c, caps=CAPS) as p:
            await p.ws.send_str(json.dumps({"type": "update-now"}))
            assert await until(lambda: boards(p)[-1]["failed"])
            board = boards(p)[-1]
            assert board["updating"] is None
            assert board["failed"] == {
                "what": "update", "error": "local changes", "to": NEWER, "url": bridge.release_url(NEWER),
                "detail": "git: error: Your local changes would be overwritten by merge"}
            await p.ws.send_str(json.dumps({"type": "skip", "version": NEWER}))
            assert await until(lambda: boards(p)[-1]["skipped"] == NEWER)
            board = boards(p)[-1]
            assert (board["failed"], board["update"], board["skipped"]) == (None, None, NEWER)  # Skip takes the failure with it
        await c.close()
    run(go())


def test_buttons_count_only_from_a_window_that_said_which_file_it_is(quick_update):
    """A socket that never said hello is no window: its Stop, Update and
    Reload do nothing."""
    async def go():
        c = await make_client()
        async with FakePlugin(c, caps=CAPS) as p:
            stranger = await c.ws_connect("/plugin", headers={"Origin": "null"})
            for m in ({"type": "update-now"}, {"type": "reload-all"},
                      {"type": "stop", "doc": "doc:KEY1", "agent": "designer"}):
                await stranger.send_str(json.dumps(m))
            await settle()  # time for them to do something, if they will
            assert quick_update == [] and bridge_update.STATE["step"] is None
            assert not agents(p, "Draft")
            await stranger.close()
        await c.close()
    run(go())


def test_two_presses_at_once_start_one_update(quick_update, monkeypatch):
    monkeypatch.setattr(bridge, "bridge_outdated", lambda: True)

    async def go():
        assert bridge_update.start(pull=True) is True
        assert bridge_update.start(pull=True) is False  # before the first one got going
        assert await until(lambda: "restart" in quick_update)
        assert quick_update == ["pull", "restart"]
    run(go())


def test_the_line_why_it_failed_goes_when_it_is_old(monkeypatch):
    """The windows drop it on a board that comes once it is old, so the bridge
    sends one then."""
    monkeypatch.setattr(bridge_update, "FAILED_FOR", 0.2)
    bridge.UPDATE = None

    async def go():
        c = await make_client()
        async with FakePlugin(c, caps=CAPS) as p:
            await p.ws.send_str(json.dumps({"type": "update-now"}))
            assert await until(lambda: boards(p)[-1]["failed"])
            assert boards(p)[-1]["failed"]["error"] == "no new release"
            assert await until(lambda: boards(p)[-1]["failed"] is None, 3)
        await c.close()
    run(go())


def test_a_lone_half_of_an_emoji_leaves_the_windows_their_board():
    async def go():
        c = await make_client()
        async with FakePlugin(c, caps=CAPS) as p:
            r = await c.post("/done", json={"agent": "\ud83c bot", "text": "look \udfa8 here"})
            assert r.status == 200
            assert await until(lambda: "\ud83c bot" in agents(p, "Draft"))
            assert agents(p, "Draft")["\ud83c bot"]["done"]["text"] == "look \udfa8 here"
        await c.close()
    run(go())


def test_a_script_sent_during_the_restart_is_refused_and_safe_to_repeat():
    async def go():
        c = await make_client()
        async with FakePlugin(c, caps=CAPS) as p:
            bridge_update.STATE.update(step="restart", what="update", to=NEWER)
            for body in ({"code": "x"}, {"code": "x", "parallel": True}):
                r = await c.post("/exec", json=body)
                assert r.status == 503 and (await r.json())["updating"] is True
            assert not p.of("exec")
            bridge_update.STATE.update(step=None)
            assert (await c.post("/exec", json={"code": "x"})).status == 200
        await c.close()
    run(go())


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", *args], cwd=str(cwd), check=True, capture_output=True)


def test_pull_release_moves_to_the_release_and_never_past_it(tmp_path):
    origin, work, figaro = tmp_path / "origin.git", tmp_path / "work", tmp_path / "figaro"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "symbolic-ref", "HEAD", "refs/heads/main")
    (work / "requirements.txt").write_text("aiohttp\n")
    (work / "a.txt").write_text("1\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "1.0.0")
    _git(work, "tag", "v1.0.0")
    _git(work, "push", "-u", "origin", "main", "v1.0.0")
    _git(tmp_path, "clone", "-b", "main", str(origin), str(figaro))

    def commit(text, tag=None):
        (work / "a.txt").write_text(text)
        _git(work, "commit", "-am", text)
        if tag:
            _git(work, "tag", tag)
        _git(work, "push", "origin", "main", *([tag] if tag else []))

    commit("2\n", "v1.0.1")
    commit("3, not released yet\n")
    assert run(bridge_update.pull_release("1.0.1", figaro)) is None
    assert (figaro / "a.txt").read_text() == "2\n"  # the release, not the tip of main
    assert run(bridge_update.pull_release("1.0.1", figaro)) is None  # once more: nothing to do
    why, detail = run(bridge_update.pull_release("1.0.2", figaro))
    assert why == "no such release" and "v1.0.2" in detail
    commit("4\n", "v1.0.2")
    (figaro / "a.txt").write_text("mine\n")
    why, detail = run(bridge_update.pull_release("1.0.2", figaro))
    assert why == "local changes" and detail.startswith("git: error: Your local changes")
    assert run(bridge_update.pull_release("1.0.2", tmp_path))[0] == "not a git clone"


def test_pull_release_gets_past_an_old_tag_moved_on_github(tmp_path):
    # v1.1.0 was moved once on GitHub. A clone made before that keeps the old one,
    # and a plain `git fetch --tags` then fails as a whole, though the new tag came.
    origin, work, figaro = tmp_path / "origin.git", tmp_path / "work", tmp_path / "figaro"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "symbolic-ref", "HEAD", "refs/heads/main")
    (work / "a.txt").write_text("1\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "1")
    _git(work, "tag", "v1.0.0")
    _git(work, "push", "-u", "origin", "main", "v1.0.0")
    _git(tmp_path, "clone", "-b", "main", str(origin), str(figaro))
    (work / "a.txt").write_text("2\n")
    _git(work, "commit", "-am", "2")
    _git(work, "tag", "-f", "v1.0.0")
    _git(work, "push", "-f", "origin", "main", "v1.0.0")
    (work / "a.txt").write_text("3\n")
    _git(work, "commit", "-am", "3")
    _git(work, "tag", "v1.1.0")
    _git(work, "push", "origin", "main", "v1.1.0")

    assert run(bridge_update.pull_release("1.1.0", figaro)) is None
    assert (figaro / "a.txt").read_text() == "3\n"
    why, detail = run(bridge_update.pull_release("1.2.0", figaro))
    assert why == "no such release" and "v1.2.0" in detail


@pytest.mark.parametrize("url, moved", [
    ("https://github.com/artem-levchenko-2/figaro.git", "https://github.com/artem-levchenko/figaro.git"),
    ("https://github.com/artem-levchenko-2/figaro", "https://github.com/artem-levchenko/figaro"),
    ("https://github.com/Artem-Levchenko-2/figaro/\n", "https://github.com/artem-levchenko/figaro"),
    ("git@github.com:artem-levchenko-2/figaro.git", "git@github.com:artem-levchenko/figaro.git"),
    ("ssh://git@github.com/artem-levchenko-2/figaro.git", "ssh://git@github.com/artem-levchenko/figaro.git"),
    ("https://github.com/artem-levchenko/figaro.git", None),
    ("https://github.com/someone/figaro.git", None),
    ("https://github.com/artem-levchenko-2/figaro-fork.git", None),
    ("/home/me/figaro.git", None),
], ids=["https", "bare", "case", "scp", "ssh", "new", "fork", "other", "path"])
def test_moved_origin(url, moved):
    assert bridge_update.moved_origin(url) == moved


def test_pull_release_moves_a_clone_of_the_old_address(tmp_path):
    # GitHub redirects the repository's old address only while no repository takes
    # that name again: an update points a clone of it at the new one, then fetches.
    origin, work, figaro = tmp_path / "origin.git", tmp_path / "work", tmp_path / "figaro"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "symbolic-ref", "HEAD", "refs/heads/main")
    (work / "a.txt").write_text("1\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "1")
    _git(work, "tag", "v1.0.0")
    _git(work, "push", "-u", "origin", "main", "v1.0.0")
    _git(tmp_path, "clone", "-b", "main", str(origin), str(figaro))
    old, new = "https://github.com/artem-levchenko-2/figaro.git", "https://github.com/artem-levchenko/figaro.git"
    _git(figaro, "remote", "set-url", "origin", old)
    # No network: the new address leads to the local repository, the old one nowhere.
    _git(figaro, "config", f"url.{origin.as_posix()}.insteadOf", new)
    _git(figaro, "config", f"url.{(tmp_path / 'nowhere').as_posix()}.insteadOf", old)
    (work / "a.txt").write_text("2\n")
    _git(work, "commit", "-am", "2")
    _git(work, "tag", "v1.0.1")
    _git(work, "push", "origin", "main", "v1.0.1")

    assert run(bridge_update.pull_release("1.0.1", figaro)) is None
    assert (figaro / "a.txt").read_text() == "2\n"
    url = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=str(figaro),
                         capture_output=True, text=True, check=True).stdout.strip()
    assert url == new


@pytest.mark.parametrize("out, why", [
    ("Updating 1..2\nerror: Your local changes to the following files would be overwritten by merge:",
     "local changes"),
    ("hint: Diverging branches can't be fast-forwarded\nfatal: Not possible to fast-forward, aborting.",
     "local commits"),
    ("fatal: No remote repository specified.  Please, specify either a URL or a\nremote name", "no remote"),
    ("fatal: unable to access 'https://github.com/x/y/': Could not resolve host: github.com", "offline"),
    ("fatal: something new", "git failed"),
])
def test_why_a_pull_failed(out, why):
    # few words: the window's row has room for about 30 letters
    assert bridge_update.why_pull_failed(out) == why
    assert len("Couldn't update: " + why) <= 32


# ─── POST /forget ─────────────────────────────────────────────────────────

def test_forget_takes_named_agents_off_every_windows_board():
    made = {"value": 1, "changes": {"created": 1, "deleted": 0, "changed": 0, "styles": 0, "props": {},
                                    "items": [], "top": [{"id": "5:6", "name": "Card", "type": "FRAME"}]}}

    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Draft", key="KEY1", caps=CAPS, reply=lambda m: made) as a, \
                FakePlugin(c, name="Other", key="KEY2", caps=CAPS, reply=lambda m: made) as b:
            for name in ("stress-0", "stress-1", "designer"):
                await c.post("/exec", json={"code": "x", "target": "Draft", "agent": name})
            await c.post("/exec", json={"code": "x", "target": "Other", "agent": "stress-0"})
            await c.post("/done", json={"target": "Draft", "agent": "stress-1", "text": "done"})
            assert await until(lambda: set(agents(b, "Draft")) == {"stress-0", "stress-1", "designer"})
            r = await c.post("/forget", json={"target": "Draft", "agents": [
                "stress-0", " stress-1 ", "never-ran", "stress-0", " "]})
            assert r.status == 200
            assert await r.json() == {"ok": True, "file": "Draft", "forgotten": ["stress-0", "stress-1"],
                                      "busy": []}
            for window in (a, b):  # a window of another file sees it at once, too
                assert await until(lambda: set(agents(window, "Draft")) == {"designer"})
                assert [e["agent"] for e in island(window, "Draft")["recent"]] == ["designer"]
            assert set(agents(a, "Other")) == {"stress-0"}  # the same name in another file stays
            assert len(island(a, "Other")["recent"]) == 1
            await c.post("/exec", json={"code": "x", "target": "Draft", "agent": "stress-0"})
            assert await until(lambda: set(agents(a, "Draft")) == {"designer", "stress-0"})  # a fresh row
        await c.close()
    run(go())


def test_forget_leaves_an_agent_at_work_or_stopped():
    async def go():
        c = await make_client()
        async with Stoppable(c, name="Draft", key="KEY1", caps=CAPS) as p:
            for name in ("icons", "held"):
                await c.post("/exec", json={"code": "x", "agent": name})
            await stop(p, "doc:KEY1", "held")  # between its scripts: the Stop waits for its next one
            assert await until(lambda: bridge_board.FILES["doc:KEY1"]["agents"]["held"]["hold"])
            running = asyncio.ensure_future(c.post("/exec", json={"code": "loop", "agent": "designer"}))
            assert await until(lambda: any(m["code"] == "loop" for m in p.of("exec")))
            r = await c.post("/forget", json={"agents": ["icons", "designer", "held"]})
            assert await r.json() == {"ok": True, "file": "Draft", "forgotten": ["icons"],
                                      "busy": ["designer"]}
            assert await until(lambda: set(agents(p, "Draft")) == {"designer", "held"})
            await stop(p, "doc:KEY1", "designer")
            assert (await running).status == 409
            assert (await c.post("/exec", json={"code": "y", "agent": "held"})).status == 409  # the Stop held
        await c.close()
    run(go())


def test_forget_wants_a_list_of_names_and_a_file():
    async def go():
        c = await make_client()
        r = await c.post("/forget", json={"agents": ["a"]})
        assert r.status == 503  # no plugin at all
        async with FakePlugin(c, name="Draft", key="KEY1", caps=CAPS) as p:
            await c.post("/exec", json={"code": "x", "agent": "designer"})
            assert await until(lambda: set(agents(p, "Draft")) == {"designer"})
            for bad in ({}, {"agents": "designer"}, {"agents": [5]}, {"agents": ["a"] * 201}):
                r = await c.post("/forget", json=bad)
                assert r.status == 400, bad
            r = await c.post("/forget", json={"agents": ["a"], "target": "Nowhere"})
            assert r.status == 409
            r = await c.post("/forget", json={"agents": []})
            assert r.status == 200 and (await r.json())["forgotten"] == []
            await settle()
            assert set(agents(p, "Draft")) == {"designer"}  # none of that touched the board
        async with FakePlugin(c, name="Old", key="KEY2", caps=OLD_CAPS):
            await c.post("/exec", json={"code": "x", "target": "Old", "agent": "designer"})
            r = await c.post("/forget", json={"agents": ["designer"], "target": "Old"})
            assert r.status == 200  # it asks nothing of a window
            assert (await r.json())["forgotten"] == ["designer"]
        await c.close()
    run(go())
