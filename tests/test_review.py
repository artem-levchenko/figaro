"""The Reviewer agent: the file's switch, the layers agents changed, the check on
`figaro done`, the board's `reviewer`, the designer's answer, `POST /notes` and
`figaro notes`.

A fake plugin answers the check script; what the window draws from the board is
tests/ui.test.js, and h.audit itself is tests/audit.test.js.

    pytest tests/test_review.py
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

import bridge
import bridge_review
import bridge_settings
import figaro
import figaro_token
import review_text
from test_board import CAPS, boards, island, settle
from test_bridge import make_client, run, until
from test_cli import cli
from test_exec import FakePlugin

ROOT = Path(__file__).resolve().parent.parent

CHANGED = {"created": 1, "deleted": 0, "changed": 1, "styles": 1, "props": {"fills": 1},
           "items": [{"id": "5:7", "op": "~", "name": "Title", "type": "TEXT"},
                     {"id": "5:8", "op": "-", "name": "Gone", "type": "FRAME"},
                     {"id": "5:9", "op": "~", "name": "Style", "type": "STYLE"}],
           "top": [{"id": "5:6", "name": "KPI card", "type": "COMPONENT"}]}
FOUND = {"layers": 3, "roots": 1, "skipped": 0, "errors": 0, "partial": None,
         "notes": [{"rule": "unbound-color", "text": "#2F6BFF, bg/brand matches", "count": 2,
                    "ids": ["5:6", "5:7"]},
                   {"rule": "missing-font", "text": "Inter Bold is missing", "count": 1,
                    "ids": ["5:7"]}],
         "info": {"5:6": {"name": "KPI card", "type": "COMPONENT"},
                  "5:7": {"name": "Title", "type": "TEXT"}}}
CLEAN = dict(FOUND, notes=[])


def checks(p):
    """The check scripts a fake plugin got: the reviewer's own."""
    return [m for m in p.of("exec") if m.get("agent") == "reviewer"]


def plugin(c, found=FOUND, cls=FakePlugin, **kw):
    """A fake plugin whose writes report CHANGED and whose check answers `found`."""
    def reply(m):
        if m.get("agent") == "reviewer":
            if callable(found):
                return found(m)
            return {"value": found}
        return {"value": 1, "changes": CHANGED}
    return cls(c, name="Draft", key="KEY1", caps=CAPS, reply=reply, **kw)


def row(p):
    return island(p, "Draft")["reviewer"]


async def turn_on(p, on=True):
    doc = island(p, "Draft")["doc"]
    await p.ws.send_str(json.dumps({"type": "reviewer", "doc": doc, "on": on}))
    assert await until(lambda: row(p)["on"] is on)


async def write(c, agent="designer"):
    r = await c.post("/exec", json={"code": "x", "agent": agent, "target": "Draft"})
    assert r.status == 200


async def done(c, agent="designer"):
    r = await c.post("/done", json={"text": "Look", "agent": agent, "target": "Draft"})
    assert r.status == 200
    return await r.json()


async def answer(p, send):
    await p.ws.send_str(json.dumps({"type": "review-answer", "doc": island(p, "Draft")["doc"],
                                    "send": send}))


async def notes(c, wait=False, agent="designer"):
    r = await c.post("/notes", json={"target": "Draft", "agent": agent, "wait": wait})
    assert r.status == 200
    return await r.json()


def kept():
    return figaro_token.token_path(bridge.PORT).parent / "reviewer.json"


# ─── the file's switch ────────────────────────────────────────────────────

def test_the_switch_is_off_in_every_file_until_turned_on_and_is_kept_by_file_key():
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="Draft", key="KEY1", caps=CAPS) as p, \
                FakePlugin(c, name="Other", key="KEY2", caps=CAPS) as q:
            off = {"on": False, "state": "off", "agent": None, "layers": 0, "since": None, "at": None,
                   "notes": [], "expired": False, "sent": 0, "left": 0, "intentional": 0}
            assert row(p) == off and island(q, "Other")["reviewer"] == off
            await turn_on(p)
            assert row(p)["state"] == "waiting" and island(q, "Other")["reviewer"]["on"] is False
            assert json.loads(kept().read_text(encoding="utf-8")) == {"KEY1": True}
            assert [f.name for f in kept().parent.glob("reviewer.json*")] == ["reviewer.json"]
        await c.close()

        bridge_review.ON.clear()  # the bridge starts again: the file keeps the switch
        c = await make_client()
        async with FakePlugin(c, name="Draft", key="KEY1", caps=CAPS) as p:
            assert row(p)["on"] is True
            await turn_on(p, False)
            assert json.loads(kept().read_text(encoding="utf-8")) == {}
        await c.close()
    run(go())


def test_the_global_switch_off_takes_the_reviewer_off_every_board():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            assert "reviewer" in island(p, "Draft")
            await p.ws.send_str(json.dumps({"type": "set", "name": "reviewer", "on": False}))
            assert await until(lambda: "reviewer" not in island(p, "Draft"))
            await write(c)
            reply = await done(c)
            assert "review" not in reply and not checks(p)  # no check, though the file's switch is on
            await p.ws.send_str(json.dumps({"type": "set", "name": "reviewer", "on": True}))
            assert await until(lambda: "reviewer" in island(p, "Draft") and row(p)["on"])
        await c.close()
    run(go())


# ─── what agents changed ──────────────────────────────────────────────────

def test_the_layers_a_script_changed_are_remembered_per_agent():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await write(c, "anna")
            doc = island(p, "Draft")["doc"]
            # the new top-most layer and the changed one; not the deleted one, not a style
            assert list(bridge_review.TRACK[doc]["anna"]) == ["5:6", "5:7"]
            r = await c.post("/exec", json={"code": "x", "agent": "bob", "read_only": True,
                                            "target": "Draft"})
            assert r.status == 200 and "bob" not in bridge_review.TRACK[doc]  # a read changes nothing
            await write(c, "bob")
            assert set(bridge_review.TRACK[doc]) == {"anna", "bob"}
        await c.close()
    run(go())


def test_track_keeps_the_newest_layers_up_to_a_cap(monkeypatch):
    monkeypatch.setattr(bridge_review, "TRACK_MAX", 3)
    items = [{"id": f"1:{i}", "op": "~", "type": "FRAME"} for i in range(5)]
    bridge_review.track("doc:K", "anna", {"items": items})
    bridge_review.track("doc:K", "anna", {"items": items[:1]})
    assert list(bridge_review.TRACK["doc:K"]["anna"]) == ["1:3", "1:4", "1:0"]
    bridge_review.track("doc:K", "anna", None)
    bridge_review.track("doc:K", "anna", {"items": "x", "top": [7]})


# ─── figaro done ──────────────────────────────────────────────────────────

def test_done_with_the_switch_off_checks_nothing():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await write(c)
            reply = await done(c)
            assert reply["ok"] is True and "review" not in reply
            assert not checks(p) and not bridge_review.TRACK.get(island(p, "Draft")["doc"], {}).get("designer")
            await turn_on(p)
            reply = await done(c)  # nothing changed since: nothing to check
            assert "review" not in reply and not checks(p)
        await c.close()
    run(go())


def test_done_checks_the_changed_layers_and_the_notes_wait_for_the_designer():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            await write(c)
            reply = await done(c)
            review = reply["review"]
            assert (review["state"], review["layers"], review["notes"]) == ("notes", 3, 2)
            assert review["text"] == (
                "Done. The reviewer is on in this file: it checked the 3 layers you changed and shows "
                "its notes to the designer, not to you.\n"
                "Wait for their answer: figaro notes -A designer --wait   (up to 10 min)")
            (check,) = checks(p)
            assert check["readOnly"] is True and check["quick"] is True
            assert '["5:6", "5:7"]' in check["code"] and "h.audit" in check["code"]
            assert await until(lambda: row(p)["state"] == "notes" and island(p, "Draft")["agents"][0]["done"])
            f = island(p, "Draft")
            assert f["agents"][0]["name"] == "designer" and len(f["agents"]) == 1  # the reviewer is no agent
            assert f["agents"][0]["done"]["text"] == "Look" and f["agents"][0]["busy"] is False
            r = row(p)
            assert (r["state"], r["agent"], r["layers"], r["since"], r["expired"]) == \
                ("notes", "designer", 3, None, False)
            assert [(n["id"], n["rule"], n["title"], n["text"], n["more"]) for n in r["notes"]] == [
                ("n1", "missing-font", "Font is missing", "Inter Bold is missing", 0),
                ("n2", "unbound-color", "Colour isn't a token", "#2F6BFF, bg/brand matches", 0)]
            assert r["notes"][1]["layers"] == [{"id": "5:6", "name": "KPI card", "type": "COMPONENT"},
                                               {"id": "5:7", "name": "Title", "type": "TEXT"}]
            assert f["recent"][0] == {"agent": "reviewer", "at": f["recent"][0]["at"], "kind": "ok",
                                      "summary": "Checked 3 layers", "note": "2 notes"}
            # what was checked is not checked again
            assert "review" not in await done(c) and len(checks(p)) == 1
        await c.close()
    run(go())


def test_a_check_that_finds_nothing_says_all_good():
    async def go():
        c = await make_client()
        async with plugin(c, CLEAN) as p:
            await turn_on(p)
            await write(c)
            review = (await done(c))["review"]
            assert review == {"state": "good", "layers": 3, "notes": 0,
                              "text": "Done. The reviewer checked the 3 layers you changed: all good."}
            assert await until(lambda: (row(p)["state"], row(p)["notes"]) == ("good", []))
            assert island(p, "Draft")["recent"][0]["note"] == "all good"
            assert (await notes(c))["state"] == "none"
        await c.close()
    run(go())


def test_layers_that_are_gone_are_no_check():
    async def go():
        c = await make_client()
        async with plugin(c, dict(CLEAN, layers=0)) as p:
            await turn_on(p)
            await write(c)
            assert "review" not in await done(c)
            assert row(p)["state"] == "waiting"
        await c.close()
    run(go())


def test_a_check_that_fails_says_so_and_is_tried_again_by_the_next_done():
    async def go():
        c = await make_client()
        fail = {"on": True}

        def found(m):
            return {"type": "error", "text": "boom"} if fail["on"] else {"value": FOUND}
        async with plugin(c, found) as p:
            await turn_on(p)
            await write(c)
            review = (await done(c))["review"]
            assert review["state"] == "none" and "couldn't check" in review["text"] and "boom" in review["text"]
            assert row(p)["state"] == "waiting"
            fail["on"] = False
            assert (await done(c))["review"]["state"] == "notes"  # the layers were kept
        await c.close()
    run(go())


class SlowCheck(FakePlugin):
    """The check runs until the bridge aborts it, as a script stopped by h.ck() does."""

    async def _answer(self, m):
        if m.get("agent") != "reviewer":
            return await super()._answer(m)
        for _ in range(100):
            if any(x.get("type") == "abort" and x.get("id") == m["id"] for x in self.received):
                await self.ws.send_str(json.dumps({"type": "error", "id": m["id"],
                                                   "text": "aborted: the bridge gave up on this run"}))
                return
            await asyncio.sleep(0.05)


def test_stop_in_the_reviewers_row_ends_the_check():
    async def go():
        c = await make_client()
        async with plugin(c, cls=SlowCheck) as p:
            await turn_on(p)
            await write(c)
            task = asyncio.ensure_future(done(c))
            assert await until(lambda: checks(p) and row(p)["state"] == "checking")
            assert row(p)["layers"] == 2 and row(p)["since"] is not None
            await p.ws.send_str(json.dumps({"type": "stop", "doc": island(p, "Draft")["doc"],
                                            "agent": "reviewer"}))
            reply = await asyncio.wait_for(task, 5)
            assert reply["review"] == {"state": "none", "layers": 2, "notes": 0, "text":
                                       "The designer stopped the reviewer's check, so nothing was reviewed."}
            assert not bridge_review.TRACK[island(p, "Draft")["doc"]].get("designer")  # not checked again
            assert await until(lambda: row(p)["state"] == "waiting")
            assert await until(lambda: any(m.get("type") == "abort" for m in p.received))
            assert island(p, "Draft")["agents"][0]["stopped"] is None  # no agent was stopped
            assert (await notes(c))["state"] == "none"
        await c.close()
    run(go())


# ─── the designer's answer ────────────────────────────────────────────────

def test_the_designer_sends_notes_and_the_agent_reads_them_once():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            await write(c)
            await done(c)
            await answer(p, ["n2"])
            assert await until(lambda: row(p)["state"] == "sent")
            r = row(p)
            assert (r["sent"], r["left"], r["notes"], r["agent"]) == (1, 1, [], "designer")
            recent = island(p, "Draft")["recent"][0]
            assert (recent["agent"], recent["summary"], recent["note"]) == \
                ("you", "Sent 1 note to designer", "1 left as is")
            got = await notes(c)
            assert (got["state"], got["total"], got["left"]) == ("sent", 2, 1)
            (note,) = got["notes"]
            assert note["title"] == "Colour isn't a token"
            assert note["layers"][0]["url"].startswith("https://www.figma.com/design/KEY1/")
            assert "node-id=5-6" in note["layers"][0]["url"]
            assert (await notes(c))["state"] == "none"  # once
        await c.close()
    run(go())


def test_all_fine_sends_nothing():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            await write(c)
            await done(c)
            await answer(p, [])
            assert await until(lambda: row(p)["state"] == "sent")
            assert (row(p)["sent"], row(p)["left"]) == (0, 2)
            recent = island(p, "Draft")["recent"][0]
            assert (recent["agent"], recent["summary"]) == ("you", "All fine, nothing sent to designer")
            assert (await notes(c))["state"] == "fine"
            await answer(p, ["n1"])  # nothing waits any more
            await settle()
            assert (await notes(c))["state"] == "none"
        await c.close()
    run(go())


# ─── notes marked on purpose ──────────────────────────────────────────────

def marks():
    return figaro_token.token_path(bridge.PORT).parent / "reviewer-intentional.json"


async def mark(p, note, at=None, layers=None):
    r = row(p)
    m = {"type": "review-intentional", "doc": island(p, "Draft")["doc"],
         "note": note, "at": r["at"] if at is None else at}
    if layers is not None:
        m["layers"] = layers
    await p.ws.send_str(json.dumps(m))


def test_a_note_marked_on_purpose_leaves_and_is_not_raised_again_in_the_file():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            await write(c)
            await done(c)
            assert await until(lambda: row(p)["state"] == "notes")
            await mark(p, "n2")  # n1 is the missing font, the louder rule
            assert await until(lambda: [n["id"] for n in row(p)["notes"]] == ["n1"])
            assert row(p)["state"] == "notes" and row(p)["intentional"] == 2
            assert "_ids" not in row(p)["notes"][0]
            assert island(p, "Draft")["recent"][0]["summary"] == "Marked on purpose: Colour isn't a token"
            assert json.loads(marks().read_text(encoding="utf-8")) == {"KEY1": {"unbound-color": ["5:6", "5:7"]}}
            await answer(p, ["n1"])
            got = await notes(c)
            assert (got["state"], got["total"], got["left"]) == ("sent", 2, 0)
            assert [n["rule"] for n in got["notes"]] == ["missing-font"]
            (m,) = got["marked"]
            assert (m["rule"], m["title"], [l["id"] for l in m["layers"]]) == \
                ("unbound-color", "Colour isn't a token", ["5:6", "5:7"])
            # the next check finds the same: the marked rule on those layers is not raised
            await write(c)
            await done(c)
            assert await until(lambda: row(p)["state"] == "notes")
            assert [n["rule"] for n in row(p)["notes"]] == ["missing-font"]
            await answer(p, [])
            assert "marked" not in await notes(c)  # only the round they were marked in says so
        await c.close()

        bridge_review.MARKED.clear()  # the bridge starts again: the file keeps its marks
        bridge_review.load()
        assert bridge_review.MARKED == {"KEY1": {"unbound-color": {"5:6": None, "5:7": None}}}
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            assert row(p)["intentional"] == 2
            await p.ws.send_str(json.dumps({"type": "review-unmark", "doc": island(p, "Draft")["doc"]}))
            assert await until(lambda: row(p)["intentional"] == 0)
            assert json.loads(marks().read_text(encoding="utf-8")) == {}
            await write(c)
            await done(c)
            assert await until(lambda: row(p)["state"] == "notes")
            assert [n["rule"] for n in row(p)["notes"]] == ["missing-font", "unbound-color"]
        await c.close()
    run(go())


THREE = dict(FOUND, layers=4, notes=[
    {"rule": "unbound-color", "text": "#2F6BFF, bg/brand matches", "count": 3, "ids": ["5:6", "5:7", "5:8"]},
    {"rule": "missing-font", "text": "Inter Bold is missing", "count": 1, "ids": ["5:7"]}],
    info=dict(FOUND["info"], **{"5:8": {"name": "Badge", "type": "FRAME"}}))


def test_one_layer_of_a_note_is_marked_on_purpose_and_the_rest_stay():
    async def go():
        c = await make_client()
        async with plugin(c, THREE) as p:
            await turn_on(p)
            await write(c)
            await done(c)
            assert await until(lambda: row(p)["state"] == "notes")
            n2 = lambda: next(n for n in row(p)["notes"] if n["rule"] == "unbound-color")
            await mark(p, "n2", layers=["9:9"])  # not a layer of the note: nothing happens
            await settle()
            assert [l["id"] for l in n2()["layers"]] == ["5:6", "5:7", "5:8"] and row(p)["intentional"] == 0
            await mark(p, "n2", layers=[{}, ["5:7"], None])  # not ids: nothing happens, and the window stays
            await settle()
            assert [l["id"] for l in n2()["layers"]] == ["5:6", "5:7", "5:8"] and row(p)["intentional"] == 0
            await mark(p, "n2", layers=["5:7"])
            assert await until(lambda: [l["id"] for l in n2()["layers"]] == ["5:6", "5:8"])
            assert n2()["id"] == "n2" and n2()["more"] == 0
            assert row(p)["state"] == "notes" and [n["id"] for n in row(p)["notes"]] == ["n1", "n2"]
            assert row(p)["intentional"] == 1
            assert json.loads(marks().read_text(encoding="utf-8")) == {"KEY1": {"unbound-color": ["5:7"]}}
            first = row(p)["at"]
            await answer(p, ["n2"])  # the rest can still be sent to the agent
            got = await notes(c)
            assert got["state"] == "sent"
            assert got["total"] == 2  # n1 and n2: the note that kept two of its layers counts once
            assert [[l["id"] for l in n["layers"]] for n in got["notes"]] == [["5:6", "5:8"]]
            (m,) = got["marked"]
            assert (m["rule"], [l["id"] for l in m["layers"]]) == ("unbound-color", ["5:7"])
            # the next check raises the other layers again, and not the marked one
            await write(c)
            await done(c)
            assert await until(lambda: row(p)["state"] == "notes" and row(p)["at"] != first)
            assert [l["id"] for l in n2()["layers"]] == ["5:6", "5:8"]
            # the last layers: marking them one by one leaves the note as marking the whole note does
            await mark(p, "n2", layers=["5:6"])
            assert await until(lambda: [l["id"] for l in n2()["layers"]] == ["5:8"])
            await mark(p, "n2", layers=["5:8"])
            assert await until(lambda: [n["id"] for n in row(p)["notes"]] == ["n1"])
            assert row(p)["intentional"] == 3
            await mark(p, "n1")  # no layers: the whole note, as an older window does
            assert await until(lambda: row(p)["state"] == "sent")
        await c.close()
    run(go())


def test_the_marked_layers_are_counted_once_however_many_rules_they_are_marked_for():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            await write(c)
            await done(c)
            assert await until(lambda: row(p)["state"] == "notes")
            await mark(p, "n1")
            await mark(p, "n2")
            assert await until(lambda: row(p)["state"] == "sent")
            # 5:7 carries two rules, 3 marks in all: 2 layers
            assert sum(len(v) for v in bridge_review.MARKED["KEY1"].values()) == 3
            assert row(p)["intentional"] == 2
        await c.close()
    run(go())


def test_marking_every_note_on_purpose_answers_all_fine():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            await write(c)
            await done(c)
            assert await until(lambda: row(p)["state"] == "notes")
            await mark(p, "n2", at=row(p)["at"] + 5)  # an answer to replaced notes
            await settle()
            assert len(row(p)["notes"]) == 2 and row(p)["intentional"] == 0
            await mark(p, "n2")
            assert await until(lambda: len(row(p)["notes"]) == 1)
            await mark(p, "n1")
            assert await until(lambda: row(p)["state"] == "sent")
            # 5:7 is marked for two rules: the file's marked layers are counted once
            assert (row(p)["sent"], row(p)["left"], row(p)["intentional"]) == (0, 0, 2)
            got = await notes(c)
            assert got["state"] == "fine" and [m["rule"] for m in got["marked"]] == ["unbound-color", "missing-font"]
        await c.close()
    run(go())


def test_unmarked_takes_only_the_marked_layers_of_the_marked_rule():
    found = {"layers": 4, "notes": [
        {"rule": "unbound-color", "text": "a", "count": 3, "ids": ["1:1", "1:2", "1:3"]},
        {"rule": "unbound-color", "text": "b", "count": 1, "ids": ["1:4"]},
        {"rule": "missing-font", "text": "c", "count": 1, "ids": ["1:1"]},
        {"rule": "default-name", "text": "d", "count": 40, "ids": ["1:5"]}]}
    out = bridge_review._unmarked(found, {"unbound-color": {"1:1": None, "1:4": None}, "default-name": {}})
    assert out["notes"] == [{"rule": "unbound-color", "text": "a", "count": 2, "ids": ["1:2", "1:3"]},
                            {"rule": "missing-font", "text": "c", "count": 1, "ids": ["1:1"]},
                            {"rule": "default-name", "text": "d", "count": 40, "ids": ["1:5"]}]
    assert out["layers"] == 4 and bridge_review._unmarked(found, None) is found


def test_marks_are_capped_per_file(monkeypatch):
    monkeypatch.setattr(bridge_review, "MARKS_MAX", 2)

    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            await write(c)
            await done(c)
            assert await until(lambda: row(p)["state"] == "notes")
            await mark(p, "n1")
            await mark(p, "n2")
            assert await until(lambda: row(p)["state"] == "sent")
            # the font's mark came first and goes first
            assert bridge_review.MARKED["KEY1"] == {"unbound-color": {"5:6": None, "5:7": None}}
        await c.close()
    run(go())


def test_a_broken_marks_file_is_no_marks(tmp_path):
    marks().parent.mkdir(parents=True, exist_ok=True)
    for text in ("{", "[]", json.dumps({"K": [1], "L": {"rule": "x"}, "M": {"r": [1, "2:2"], "s": []}})):
        marks().write_text(text, encoding="utf-8")
        bridge_review.load()
        assert bridge_review.MARKED == ({"M": {"r": {"2:2": None}}} if text.startswith('{"') else {})


def test_recent_names_the_agent_the_answer_went_to():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            for send, said, got in ((["n1"], "Sent 1 note to copywriter", "sent"),
                                    ([], "All fine, nothing sent to copywriter", "fine")):
                await write(c, agent="copywriter")
                await done(c, agent="copywriter")
                assert await until(lambda: row(p)["state"] == "notes")
                await answer(p, send)
                assert await until(lambda: row(p)["state"] == "sent")
                recent = island(p, "Draft")["recent"][0]
                assert (recent["agent"], recent["summary"]) == ("you", said)
                assert (await notes(c, agent="copywriter"))["state"] == got
        await c.close()
    run(go())


def test_the_agent_waits_for_the_answer_and_stops_after_ten_minutes(monkeypatch):
    monkeypatch.setattr(bridge_review, "HOLD", 0.4)
    monkeypatch.setattr(bridge_review, "EXPIRE", 1.0)

    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            await write(c)
            await done(c)
            assert (await notes(c))["state"] == "pending"  # without wait: at once
            waiting = asyncio.ensure_future(notes(c, wait=True))
            assert (await waiting)["state"] == "pending"  # held HOLD seconds, then asks again
            quick = asyncio.ensure_future(notes(c, wait=True))
            await asyncio.sleep(0.1)
            await answer(p, ["n1", "n2"])
            got = await asyncio.wait_for(quick, 3)  # the answer ends the wait at once
            assert got["state"] == "sent" and len(got["notes"]) == 2 and got["left"] == 0

            await write(c)
            await done(c)
            assert await until(lambda: row(p)["state"] == "notes" and not row(p)["expired"])
            assert await until(lambda: row(p)["expired"], 3)  # the window offers Copy for chat
            assert (await notes(c, wait=True))["state"] == "expired"
            await answer(p, ["n1"])  # they send after all: the agent still gets it
            got = await until_notes(c, "sent")
            assert got["state"] == "sent" and await until(lambda: row(p)["expired"] is False)
        await c.close()
    run(go())


async def until_notes(c, state):
    got = await notes(c)
    assert got["state"] == state, got
    return got


def test_notes_of_another_agent_are_not_yours_and_a_newer_check_replaces_them():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            await write(c, "anna")
            await done(c, "anna")
            assert (await notes(c, agent="bob"))["state"] == "none"
            await write(c, "bob")
            await done(c, "bob")
            assert await until(lambda: row(p)["agent"] == "bob")
            assert (await notes(c, agent="anna"))["state"] == "withdrawn"
            assert (await notes(c, agent="bob"))["state"] == "pending"
        await c.close()
    run(go())


def test_turning_the_file_switch_off_withdraws_the_notes():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            await write(c)
            await done(c)
            await turn_on(p, False)
            assert row(p)["state"] == "off" and row(p)["notes"] == []
            assert (await notes(c))["state"] == "withdrawn"
        await c.close()
    run(go())


# ─── what the check script does ───────────────────────────────────────────

def test_notes_are_merged_by_rule_and_text_in_the_order_of_the_rules():
    found = {"notes": [
        {"rule": "default-name", "text": "named Frame 1", "count": 1, "ids": ["1:1"]},
        {"rule": "low-contrast", "text": "2.3:1", "count": 2, "ids": ["1:2", "1:3"]},
        {"rule": "low-contrast", "text": "2.3:1", "count": 8, "ids": ["1:3", "1:4", "1:5", "1:6", "1:7"]},
        {"rule": "nonsense", "text": "x", "ids": []}, {"rule": "zero-size"}, "x"],
        "info": {"1:2": {"name": "Caption", "type": "TEXT"}}}
    merged = bridge_review._merge(found)
    assert [(n["id"], n["rule"], n["title"]) for n in merged] == [
        ("n1", "low-contrast", "Text contrast is too low"), ("n2", "default-name", "Layer has a default name")]
    low = merged[0]
    assert [l["id"] for l in low["layers"]] == ["1:2", "1:3", "1:4", "1:5", "1:6"]
    assert low["more"] == 5 and low["layers"][0] == {"id": "1:2", "name": "Caption", "type": "TEXT"}
    assert low["layers"][1]["name"] == "Untitled"  # a layer that went away meanwhile
    assert bridge_review._merge({}) == []


def test_every_rule_of_h_audit_has_a_title():
    code = (ROOT / "plugin" / "code.js").read_text(encoding="utf-8")
    block = re.search(r"const AUDIT_RULES = \[(.*?)\];", code, re.S).group(1)
    assert re.findall(r'"([a-z-]+)"', block) == list(bridge_review.TITLES)


def test_the_check_script_is_valid_javascript_with_the_ids_in_it(tmp_path):
    import shutil
    import subprocess
    code = bridge_review._script(['1:2', 'I5:6;7:8', '"quoted"'])
    assert 'const ids = ["1:2", "I5:6;7:8", "\\"quoted\\""];' in code and "__" not in code
    node = shutil.which("node")
    if node:
        # a file, not /dev/stdin: Linux runners hand node a pipe it can't open by path, Windows has none
        script = tmp_path / "check.js"
        script.write_text("(async function (h, figma) {" + code + "});", encoding="utf-8")
        r = subprocess.run([node, "--check", str(script)], text=True, capture_output=True)
        assert r.returncode == 0, r.stderr


# ─── the CLI ──────────────────────────────────────────────────────────────

SENT = {"ok": True, "state": "sent", "total": 3, "left": 1, "file": "Draft", "notes": [
    {"id": "n1", "title": "Colour isn't a token", "text": "#2F6BFF, bg/brand matches", "more": 2,
     "layers": [{"id": "I2:1068", "name": "Get started", "type": "INSTANCE", "url": "https://x/1"}]},
    {"id": "n2", "title": "Font is missing", "text": "Inter Bold is missing", "more": 0,
     "layers": [{"id": "5:7", "name": "Title", "type": "TEXT", "url": None}]}]}


def test_notes_layer_lines_follow_the_text_column_of_wide_numbers():
    import review_text
    notes = [{"title": f"T{i}", "text": "x", "more": 1 if i == 10 else 0,
              "layers": [{"id": "1:1", "name": "Payouts", "type": "TEXT"}]} for i in range(1, 11)]
    lines = review_text.render({"state": "sent", "notes": notes, "total": 10, "left": 0}, "designer")
    at = lines.index("10. T10: x")
    assert lines[at + 1] == '    "Payouts" (TEXT) 1:1' and lines[at + 2] == "    and 1 more layer"
    assert lines[lines.index("9. T9: x") + 1] == '   "Payouts" (TEXT) 1:1'


def test_the_answer_links_every_layer_a_detail_names():
    import bridge_review
    doc = "doc:links"
    note = {"id": "n1", "title": "T", "text": "s", "more": 0, "layers": [{"id": "1:1", "name": "A", "type": "FRAME"}],
            "details": [{"text": "a", "layers": ["1:1", "1:2"]}]}
    plain ={"id": "n2", "title": "T", "text": "s", "more": 0, "layers": [{"id": "1:3", "name": "B", "type": "TEXT"}]}
    bridge_review.ANSWERS.setdefault(doc, {})["designer"] = {"kind": "sent", "notes": [note, plain], "total": 2, "left": 0}
    state, extra = bridge_review._result(doc, "designer", {"fileKey": "KEY1", "name": "Dash"})
    got, other = extra["notes"]
    assert state == "sent" and set(got["links"]) == {"1:1", "1:2"} and got["links"]["1:2"].endswith("node-id=1-2")
    assert "links" not in other  # a note without details has its links on its layers


def test_the_details_name_every_layer_the_check_could_read():
    tail = " has no style or variable; bind it to one of the file's"
    ids = [f"1:{i}" for i in range(1, 8)]
    found = {"notes": [{"rule": "unbound-color", "text": "fill #111111" + tail, "count": 4, "ids": ids[:4]},
                       {"rule": "unbound-color", "text": "fill #2E6BFF" + tail, "count": 3, "ids": ids[4:]}],
             "info": {i: {"name": f"Layer {i}", "type": "TEXT"} for i in ids if i != "1:7"}}  # 1:7 was gone
    (note,) = bridge_review._merge(found)
    assert [l["id"] for l in note["layers"]] == ids[:5] and note["more"] == 2
    assert note["details"][1]["layers"] == ["1:5", "1:6", "1:7"]  # ids, as the 1.8.0 window reads them
    assert note["names"]["1:6"] == {"name": "Layer 1:6", "type": "TEXT"} and "1:7" not in note["names"]
    assert set(note["names"]) == set(ids[:6])


def test_the_designer_and_the_agent_get_the_names_of_the_details_layers():
    tail = " has no style or variable; bind it to one of the file's"
    cards = [f"6:{i}" for i in range(1, 6)]
    found = dict(FOUND, notes=[{"rule": "unbound-color", "text": "fill #111111" + tail, "count": 5, "ids": cards},
                               {"rule": "unbound-color", "text": "fill #2E6BFF" + tail, "count": 1, "ids": ["5:7"]}],
                 info=dict(FOUND["info"], **{i: {"name": "Card", "type": "FRAME"} for i in cards}))

    async def go():
        c = await make_client()
        async with plugin(c, found=found) as p:
            await turn_on(p)
            await write(c)
            await done(c)
            assert await until(lambda: row(p)["state"] == "notes")
            (shown,) = row(p)["notes"]
            assert "5:7" not in [l["id"] for l in shown["layers"]]  # five layers named, all cards
            assert shown["names"]["5:7"] == {"name": "Title", "type": "TEXT"}  # for Copy for chat
            await answer(p, ["n1"])
            assert await until(lambda: row(p)["state"] == "sent")
            (note,) = (await notes(c))["notes"]
            lines = review_text.render({"state": "sent", "notes": [note], "total": 1, "left": 0}, "designer")
            assert '     "Title" (TEXT) 5:7  ' + note["links"]["5:7"] in lines
        await c.close()
    run(go())


def test_notes_with_details_print_each_original_line_with_its_layers():
    import review_text
    note = {"title": "Colour isn't a token", "text": "fills #FFFFFF and #000000 have no style", "more": 0,
            "layers": [{"id": "1:1", "name": "Card", "type": "FRAME", "url": "https://x/1"}],
            "details": [{"text": "fill #FFFFFF", "layers": ["1:1"]}, {"text": "fill #000000", "layers": ["1:2", "1:4"]}],
            "detailsMore": 2, "links": {"1:1": "https://x/1", "1:2": "https://x/2", "1:4": "https://x/4"},
            "names": {"1:1": {"name": "Card", "type": "FRAME"}, "1:2": {"name": "Title", "type": "TEXT"}}}
    plain = {"title": "Default name", "text": "Frame 1", "more": 0,
             "layers": [{"id": "3:1", "name": "Frame 1", "type": "FRAME", "url": "https://x/3"}]}
    lines = review_text.render({"state": "sent", "notes": [note, plain], "total": 2, "left": 0}, "designer")
    assert lines[2:] == [
        "1. Colour isn't a token: fills #FFFFFF and #000000 have no style",
        "   - fill #FFFFFF",
        '     "Card" (FRAME) 1:1  https://x/1',
        "   - fill #000000",
        '     "Title" (TEXT) 1:2  https://x/2',
        "     1:4  https://x/4",  # a layer the check couldn't read, or a 1.8.0 bridge: its id
        "     and 2 more lines like these",
        "2. Default name: Frame 1",
        '   "Frame 1" (FRAME) 3:1  https://x/3',
        "",
        "Fix these two, then run figaro done again."]


def test_notes_print_the_bare_id_for_names_of_an_odd_shape():
    import review_text
    for names in ({"1:2": "Title"}, [["1:2", {"name": "Title"}]], None):
        note = {"title": "T", "text": "s", "more": 0, "layers": [],
                "details": [{"text": "a", "layers": ["1:2"]}], "names": names}
        lines = review_text.render({"state": "sent", "notes": [note], "total": 1, "left": 0}, "designer")
        assert lines[4] == "     1:2", names


def test_notes_prints_what_the_designer_sent(monkeypatch, capsys):
    rc, out, err, calls = cli(monkeypatch, capsys, "notes", "-A", "designer", "-T", "KEY1", bridge_reply=SENT)
    assert rc == 0 and calls == [{"target": "KEY1", "agent": "designer", "wait": False}]
    assert out.splitlines() == [
        "The designer sent you 2 of the reviewer's 3 notes:",
        "",
        "1. Colour isn't a token: #2F6BFF, bg/brand matches",
        "   \"Get started\" (INSTANCE) I2:1068  https://x/1",
        "   and 2 more layers",
        "2. Font is missing: Inter Bold is missing",
        "   \"Title\" (TEXT) 5:7",
        "",
        "They left 1 as it is. Fix these two, then run figaro done again."]


def test_notes_prints_the_notes_marked_on_purpose(monkeypatch, capsys):
    marked = [{"rule": "content-clipped", "title": "Content is cut off", "text": "x",
               "layers": [{"id": "5:6", "name": "Logo", "type": "RECTANGLE"}], "more": 2}]
    rc, out, _, _ = cli(monkeypatch, capsys, "notes", "-A", "designer",
                        bridge_reply={"ok": True, "state": "fine", "marked": marked})
    assert rc == 0 and out.splitlines() == [
        "The designer looked at the reviewer's notes: all fine, nothing to fix.",
        "",
        "The designer marked these as on purpose: leave them as they are. The reviewer won't raise "
        "them on these layers again:",
        "- Content is cut off: \"Logo\" 5:6, 2 more layers"]
    rc, out, _, _ = cli(monkeypatch, capsys, "notes", "-A", "designer", bridge_reply=dict(SENT, marked=marked))
    assert out.splitlines()[-5:] == [
        "",
        "The designer marked these as on purpose: leave them as they are. The reviewer won't raise "
        "them on these layers again:",
        "- Content is cut off: \"Logo\" 5:6, 2 more layers",
        "",
        "They left 1 as it is. Fix these two, then run figaro done again."]


@pytest.mark.parametrize("reply, expected", [
    ({"state": "fine"}, "The designer looked at the reviewer's notes: all fine, nothing to fix."),
    ({"state": "expired"}, review_text.WAITED),
    ({"state": "none"}, "No notes from the reviewer for designer in this file."),
    ({"state": "pending"}, "The designer hasn't answered yet. Wait for it: figaro notes -A designer "
                           "--wait   (up to 10 min)"),
    ({"state": "withdrawn"}, "The reviewer's notes for you were withdrawn (the reviewer was turned off, "
                             "or it checked newer work). Finish your work."),
])
def test_notes_other_answers(monkeypatch, capsys, reply, expected):
    rc, out, _, _ = cli(monkeypatch, capsys, "notes", "-A", "designer", bridge_reply=dict(reply, ok=True))
    assert rc == 0 and out.strip() == expected
    assert "10 minutes" in review_text.WAITED and "figaro notes" in review_text.WAITED


def test_notes_wait_asks_again_until_the_answer(monkeypatch, capsys):
    pending = {"ok": True, "state": "pending"}
    fake = __import__("test_cli").FakeBridge(pending, pending, {"ok": True, "state": "fine"})
    rc, out, _, calls = cli(monkeypatch, capsys, "notes", "-A", "designer", "--wait", bridge_reply=fake)
    assert rc == 0 and len(calls) == 3 and all(c["wait"] is True for c in calls)
    assert "all fine" in out


def test_notes_wait_gives_up_after_its_minutes(monkeypatch, capsys):
    import cli_extras
    monkeypatch.setattr(cli_extras, "NOTES_WAIT", -1)
    rc, out, _, calls = cli(monkeypatch, capsys, "notes", "-A", "designer", "--wait",
                            bridge_reply={"ok": True, "state": "pending"})
    assert rc == 0 and len(calls) == 1 and out.strip() == review_text.WAITED


def test_notes_from_an_old_bridge(monkeypatch, capsys):
    class Old:
        def __call__(self, *a, **k):
            return 404, {"ok": False, "error": "not found"}
    monkeypatch.setattr(figaro, "_request", Old())
    monkeypatch.setattr("sys.argv", ["figaro", "notes"])
    with pytest.raises(SystemExit) as e:
        figaro.main()
    assert e.value.code == 1
    assert "older than `figaro notes`" in capsys.readouterr().err


def test_done_prints_the_reviewers_line(monkeypatch, capsys):
    text = "Done. The reviewer checked the 2 layers you changed: all good."
    reply = {"ok": True, "file": "Draft", "text": "Look",
             "review": {"state": "good", "layers": 2, "notes": 0, "text": text}}
    rc, out, _, _ = cli(monkeypatch, capsys, "done", "Look", bridge_reply=reply)
    assert rc == 0 and out.splitlines()[-1] == text
    rc, out, _, _ = cli(monkeypatch, capsys, "done", "Look",
                        bridge_reply={"ok": True, "file": "Draft", "text": "Look"})
    assert rc == 0 and len(out.splitlines()) == 1


# ─── a done that goes away, a window that closes, a stale answer, a forgotten agent ───

def test_a_done_cancelled_mid_check_puts_the_row_and_the_layers_back():
    async def go():
        c = await make_client()
        async with plugin(c, cls=SlowCheck) as p:
            await turn_on(p)
            await write(c)
            doc = island(p, "Draft")["doc"]
            task = asyncio.ensure_future(done(c))
            assert await until(lambda: checks(p) and row(p)["state"] == "checking")
            task.cancel()  # the agent's call went away: its client closed the connection
            await asyncio.gather(task, return_exceptions=True)
            assert await until(lambda: row(p)["state"] == "waiting")
            assert list(bridge_review.TRACK[doc]["designer"]) == ["5:6", "5:7"]
            p.__class__ = FakePlugin  # the next check answers
            p.reply = lambda m: {"value": FOUND if m.get("agent") == "reviewer" else 1}
            assert (await done(c))["review"]["state"] == "notes"
        await c.close()
    run(go())


def test_a_window_that_closes_while_the_check_waits_is_no_answer():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            await write(c)
            conn = next(iter(bridge.PLUGINS))
            doc = island(p, "Draft")["doc"]
            bridge_review._row(doc)
            gone = bridge.PLUGINS.pop(conn)  # as if its window went while the check was queued
            request = type("R", (), {"transport": type("T", (), {"is_closing": lambda self: False})()})()
            found, why = await bridge_review._check(request, conn, doc, ["5:6"])
            assert found is None and why == "the file's window closed"
            bridge.PLUGINS[conn] = gone
        await c.close()
    run(go())


def test_an_answer_to_replaced_notes_is_ignored_and_one_without_at_works():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            await write(c)
            await done(c)
            doc = island(p, "Draft")["doc"]
            assert await until(lambda: row(p)["state"] == "notes")
            at = row(p)["at"]
            assert at == bridge_review.ASKING[doc]["at"]
            await p.ws.send_str(json.dumps({"type": "review-answer", "doc": doc, "send": ["n1"], "at": at - 5}))
            await settle()
            assert row(p)["state"] == "notes" and (await notes(c))["state"] == "pending"
            await p.ws.send_str(json.dumps({"type": "review-answer", "doc": doc, "send": ["n1"], "at": at}))
            assert await until(lambda: row(p)["state"] == "sent")
            await write(c)
            await done(c)
            await answer(p, [])  # an older window: no at
            assert await until(lambda: row(p)["state"] == "sent" and row(p)["left"] == 2)
        await c.close()
    run(go())


def test_forgetting_an_agent_withdraws_its_notes_and_layers():
    async def go():
        c = await make_client()
        async with plugin(c) as p:
            await turn_on(p)
            await write(c)
            await done(c)
            await write(c, "bob")
            doc = island(p, "Draft")["doc"]
            r = await c.post("/forget", json={"target": "Draft", "agents": ["designer", "bob"]})
            assert (await r.json())["forgotten"] == ["designer", "bob"]
            assert await until(lambda: row(p)["state"] == "waiting" and row(p)["notes"] == [])
            assert "bob" not in bridge_review.TRACK.get(doc, {}) and doc not in bridge_review.ASKING
            assert (await notes(c))["state"] == "none"
        await c.close()
    run(go())



# ─── one note per rule ────────────────────────────────────────────────────

def test_four_colours_make_one_note_with_the_values_listed_and_every_line_kept():
    tail = " has no style or variable; bind it to one of the file's"
    found = {"notes": [
        {"rule": "unbound-color", "text": "fill #FFFFFF" + tail, "count": 2, "ids": ["1:1", "1:2"]},
        {"rule": "unbound-color", "text": "fill #000000" + tail, "count": 1, "ids": ["1:3"]},
        {"rule": "unbound-color", "text": "stroke #D1D1D6" + tail, "count": 1, "ids": ["1:4"]},
        {"rule": "unbound-color", "text": "fill #2E6BFF and stroke #FFFFFF" + tail, "count": 2,
         "ids": ["1:5", "1:6"]},
        {"rule": "missing-font", "text": "Inter is missing", "count": 1, "ids": ["1:1"]}],
        "info": {}}
    font, colour = bridge_review._merge(found)
    assert (font["rule"], colour["rule"]) == ("missing-font", "unbound-color")
    assert (font["id"], colour["id"]) == ("n1", "n2") and "details" not in font
    assert [l["id"] for l in colour["layers"]] == ["1:1", "1:2", "1:3", "1:4", "1:5"]
    assert colour["more"] == 1
    assert colour["text"] == ("fills and strokes #FFFFFF, #000000, #D1D1D6 and #2E6BFF have no style or "
                              "variable; bind them to the file's")
    assert [d["layers"] for d in colour["details"]] == [["1:1", "1:2"], ["1:3"], ["1:4"], ["1:5", "1:6"]]
    assert colour["details"][0]["text"] == "fill #FFFFFF" + tail and "detailsMore" not in colour


def test_fills_only_say_fills_and_a_single_text_stays_as_it_is():
    tail = " has no style or variable; bind it to one of the file's"
    found = {"notes": [{"rule": "unbound-color", "text": "fill #111111" + tail, "ids": ["1:1"]},
                       {"rule": "unbound-color", "text": "fill #222222" + tail, "ids": ["1:2"]},
                       {"rule": "default-name", "text": 'named "Frame 1"', "count": 3, "ids": ["1:3"]}]}
    notes = {n["rule"]: n for n in bridge_review._merge(found)}
    assert notes["unbound-color"]["text"] == \
        "fills #111111 and #222222 have no style or variable; bind them to the file's"
    assert notes["default-name"]["text"] == 'named "Frame 1"' and "details" not in notes["default-name"]
    assert notes["default-name"]["more"] == 2  # three layers counted, one listed


# Two or more different lines per rule, as the functions of plugin/code.js write them.
LINES = {
    "missing-font": ["uses a font that is not installed here, so it shows in a stand-in; switch it to an installed font"],
    "text-clipped": [
        'is cut off by the frame "Card", which clips its content; make room for the text or turn the clipping off',
        'is cut off by the frame "Header" and "Footer", which clips its content; make room for the text or turn the clipping off',
        "runs out of its own box (20 px high, the text needs 41); make the box taller or the text shorter",
        "runs out of its own box on the side; make the box wider or the text shorter"],
    "content-clipped": [
        'sticks out of the frame "Card", which clips its content, so 10 px of it is cut off on the right; move it inside, or turn the clipping off',
        'its stroke is cut off by the frame "Row", which clips its content; set the stroke inside, or make room for it',
        'is cut by the rounded corners (radius 10) of the frame "Logo", which clips its content; fine for an avatar or a photo, otherwise round the layer itself or turn the clipping off'],
    "zero-size": ["is 0 × 12 px, so nothing shows; give it a size or delete it",
                  "text with no visible characters; write the text or delete the layer"],
    "low-contrast": ["contrast 2.3:1 of #AAAAAA on #FFFFFF, needs 4.5:1; use a stronger colour",
                     "contrast 1.9:1 of #BBBBBB on #FFFFFF, needs 3:1 for large text; use a stronger colour"],
    "low-contrast-ui": ["icon contrast 2.1:1 of #CCCCCC on #FFFFFF, needs 3:1; use a stronger colour",
                        "icon contrast 1.5:1 of #DDDDDD on #F0F0F0, needs 3:1; use a stronger colour"],
    "placeholder-text": ['placeholder text "TBC"; write the real text, or ask',
                         'placeholder text "Lorem ipsum, dolor"; write the real text, or ask'],
    "small-target": [
        'looks like a button (its name is "Button") but is 20 × 20 px, under 24 × 24; make it at least that big',
        'looks like a button (its component is "Chip") but is 18 × 30 px, under 24 × 24; make it at least that big'],
    "no-auto-layout": ["3 children placed by hand with no auto-layout; add auto-layout so spacing follows the content",
                       "5 children placed by hand with no auto-layout; add auto-layout so spacing follows the content"],
    "empty-frame": ["empty, shows nothing and still has Figma's default name; delete it (for a gap use the parent's spacing) or fill it"],
    "detached-instance": ["was an instance and is detached: fine as a one-off state, otherwise use the component again"],
    "no-text-style": ["has no text style or typography variables; apply one of the file's text styles"],
    "default-name": ['named "Frame 1", Figma\'s own default; name it for what it is',
                     'named "Group 7", Figma\'s own default; name it for what it is'],
    "uneven-siblings": ["is 40 px wide, its same-named siblings are 48, 40; make them equal",
                        "is 12 px high, its same-named siblings are 16; make them equal"],
    "heading-order": ["is a level 3 heading right after a level 1; do not skip a level",
                      "is a level 4 heading right after a level 2; do not skip a level"],
    "absolute-in-layout": ['positioned by hand inside the auto-layout "Row"; fine for a badge or an overlay, otherwise put it in the flow',
                           'positioned by hand inside the auto-layout "Nav, main"; fine for a badge or an overlay, otherwise put it in the flow'],
}
SUMMARIES = {
    "missing-font": "2 texts use fonts that are not installed here, so they show in stand-ins; switch them to installed fonts",
    "text-clipped": "4 texts are cut off or run out of their boxes; make room for the text or turn the clipping off",
    "content-clipped": "3 layers are cut off by frames that clip their content; move them inside, or turn the clipping off",
    "zero-size": "2 layers have no size or no visible text, so nothing shows; give them a size or delete them",
    "low-contrast": "contrast is too low on 2 texts, as low as 1.9:1; use stronger colours",
    "low-contrast-ui": "icon contrast is too low on 2 layers, as low as 1.5:1; use stronger colours",
    "placeholder-text": "2 texts look like placeholders; write the real text, or ask",
    "small-target": "2 layers look like buttons but are under 24 × 24 px; make them at least that big",
    "no-auto-layout": "2 frames have children placed by hand with no auto-layout; add auto-layout so spacing follows the content",
    "empty-frame": "2 frames are empty and still have Figma's default names; delete them or fill them",
    "detached-instance": "2 instances are detached: fine as one-off states, otherwise use the component again",
    "no-text-style": "2 texts have no text style or typography variables; apply the file's text styles",
    "default-name": "2 layers have Figma's default names; name them for what they are",
    "uneven-siblings": "2 layers differ in size from their same-named siblings; make them equal",
    "heading-order": "heading levels skip in 2 places; do not skip a level",
    "absolute-in-layout": "2 layers are positioned by hand inside auto-layout; fine for a badge or an overlay, otherwise put them in the flow",
}


def test_every_rule_has_a_summary_and_a_test_line():
    assert set(bridge_review.SUMMARIES) == set(bridge_review.TITLES) - {"unbound-color"}
    assert set(SUMMARIES) == set(bridge_review.SUMMARIES) and set(LINES) == set(SUMMARIES)


@pytest.mark.parametrize("rule", sorted(SUMMARIES))
def test_a_rule_whose_lines_differ_gets_its_summary_counted_in_layers(rule):
    lines = LINES[rule]
    if len(lines) == 1:  # the rule writes one line: two texts of it still differ in the test
        lines = [lines[0], lines[0] + " "]
    found = {"notes": [{"rule": rule, "text": t, "ids": [f"2:{i}"]} for i, t in enumerate(lines)]}
    (note,) = bridge_review._merge(found)
    assert note["text"] == SUMMARIES[rule]
    assert len(note["layers"]) == len(lines) and [d["text"] for d in note["details"]] == lines
    # one line the rule writes, the same for every layer: as it is
    (same,) = bridge_review._merge({"notes": [{"rule": rule, "text": lines[0], "count": 3, "ids": ["2:9"]}]})
    assert same["text"] == lines[0] and "details" not in same and same["more"] == 2


def test_colours_are_listed_up_to_six_then_counted():
    tail = " has no style or variable; bind it to one of the file's"

    def text(n):
        found = {"notes": [{"rule": "unbound-color", "text": f"stroke #00000{i}" + tail, "ids": [f"1:{i}"]}
                           for i in range(n)]}
        return bridge_review._merge(found)[0]["text"]
    assert text(6) == ("strokes #000000, #000001, #000002, #000003, #000004 and #000005 have no style "
                       "or variable; bind them to the file's")
    assert text(7) == "7 colours in strokes have no style or variable; bind them to the file's"
    both = {"notes": [{"rule": "unbound-color", "text": "fill #111111" + tail, "ids": ["1:1"]},
                      {"rule": "unbound-color", "text": "stroke #111111" + tail, "ids": ["1:2"]}]}
    assert bridge_review._merge(both)[0]["text"] == \
        "#111111 in fills and strokes has no style or variable; bind it to one of the file's"


def test_a_rule_without_a_summary_falls_back_to_a_count(monkeypatch):
    monkeypatch.setitem(bridge_review.TITLES, "future-rule", "Something new")
    monkeypatch.setattr(bridge_review, "RULES", bridge_review.RULES + ["future-rule"])
    found = {"notes": [{"rule": "future-rule", "text": t, "ids": [i]} for t, i in (("a", "1:1"), ("b", "1:2"))]}
    assert bridge_review._merge(found)[0]["text"] == "2 layers: something new"


def test_details_are_capped_and_the_text_fits_the_board():
    notes = [{"rule": "no-auto-layout", "text": f"{n} children placed by hand with no auto-layout",
              "ids": [f"3:{n}"]} for n in range(40)]
    (note,) = bridge_review._merge({"notes": notes})
    assert len(note["details"]) == bridge_review.DETAILS_MAX
    assert note["detailsMore"] == 40 - bridge_review.DETAILS_MAX
    assert len(note["text"]) <= bridge_review.bridge_board.TEXT_MAX
    assert len(note["layers"]) == bridge_review.LAYERS_MAX and note["more"] == 40 - bridge_review.LAYERS_MAX
    assert len(json.dumps(note)) < 4000
