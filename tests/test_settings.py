"""The plugin window's settings: the switches, what is new in a release, and ↻
that asks GitHub again. What the window does with them is tests/ui.test.js.

    pytest tests/test_settings.py
"""
import asyncio
import json
from pathlib import Path

import bridge
import bridge_board
import bridge_settings
import figaro_token
from test_board import CAPS, NEWER, boards, quick_update, settle  # noqa: F401 (a fixture)
from test_bridge import make_client, run
from test_exec import FakePlugin, post


def kept():
    return figaro_token.token_path(bridge.PORT).parent / "settings.json"


def switch(p, name, on):
    return p.ws.send_str(json.dumps({"type": "set", "name": name, "on": on}))


# ─── the switches ─────────────────────────────────────────────────────────

def test_a_switch_in_one_window_switches_it_in_all_and_holds_across_restarts():
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="A", key="KEY1", caps=CAPS) as a, \
                FakePlugin(c, name="B", key="KEY2", caps=CAPS) as b:
            assert boards(b)[-1]["settings"] == {"sounds": True, "others": True, "recent": True, "reviewer": True, "versions": True, "sign": True}
            await switch(a, "others", False)
            await settle()
            for p in (a, b):
                assert boards(p)[-1]["settings"] == {"sounds": True, "others": False, "recent": True, "reviewer": True, "versions": True, "sign": True}
            assert json.loads(kept().read_text(encoding="utf-8")) == {"others": False}
            assert [f.name for f in kept().parent.glob("settings.json*")] == ["settings.json"]  # no .part left
            status = await (await c.get("/status")).json()
            assert status["settings"]["others"] is False
        await c.close()

        bridge_settings.SETTINGS.clear()  # the bridge starts again: the file keeps the switch
        c = await make_client()
        assert bridge_settings.current()["others"] is False
        await c.close()
    run(go())


def test_the_reviewer_is_on_until_switched_and_the_switch_reaches_every_window_and_the_exec():
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="A", key="KEY1", caps=CAPS) as a, \
                FakePlugin(c, name="B", key="KEY2", caps=CAPS) as b:
            assert boards(a)[-1]["settings"]["reviewer"] is True  # an old settings.json has no such field
            await switch(a, "reviewer", False)
            await settle()
            for p in (a, b):
                assert boards(p)[-1]["settings"]["reviewer"] is False
            assert json.loads(kept().read_text(encoding="utf-8")) == {"reviewer": False}
            status = await (await c.get("/status")).json()
            assert status["settings"]["reviewer"] is False
            await switch(b, "reviewer", True)
            await settle()
            assert boards(a)[-1]["settings"]["reviewer"] is True
        await c.close()

        kept().write_text('{"others": false}', encoding="utf-8")  # a file from before the switch
        bridge_settings.load()
        assert bridge_settings.current()["reviewer"] is True
    run(go())


def test_recent_changes_are_on_until_switched_and_the_switch_reaches_every_window():
    async def go():
        c = await make_client()
        async with FakePlugin(c, name="A", key="KEY1", caps=CAPS) as a, \
                FakePlugin(c, name="B", key="KEY2", caps=CAPS) as b:
            assert boards(a)[-1]["settings"]["recent"] is True  # an old settings.json has no such field
            await switch(a, "recent", False)
            await settle()
            for p in (a, b):
                assert boards(p)[-1]["settings"]["recent"] is False
            assert json.loads(kept().read_text(encoding="utf-8")) == {"recent": False}
            status = await (await c.get("/status")).json()
            assert status["settings"]["recent"] is False
            await switch(b, "recent", True)
            await settle()
            assert boards(a)[-1]["settings"]["recent"] is True
        await c.close()

        kept().write_text('{"others": false}', encoding="utf-8")  # a file from before the switch
        bridge_settings.load()
        assert bridge_settings.current()["recent"] is True
        kept().write_text('{"recent": false}', encoding="utf-8")
        bridge_settings.load()
        assert bridge_settings.current()["recent"] is False
    run(go())


def test_sounds_follow_figaro_sounds_until_switched(monkeypatch):
    monkeypatch.setenv("FIGARO_SOUNDS", "off")
    board = bridge_board.payload()
    assert board["settings"]["sounds"] is False and board["sounds"] is False  # "sounds": a window before 1.5.0
    assert bridge_settings.switch("sounds", True)
    board = bridge_board.payload()
    assert board["settings"]["sounds"] is True and board["sounds"] is True


def test_a_switch_it_doesnt_know_changes_nothing():
    async def go():
        c = await make_client()
        async with FakePlugin(c, caps=CAPS) as p:
            for name, on in (("tone", False), ("sounds", "off"), ("sounds", 0), (None, False)):
                await switch(p, name, on)
            await settle()
            assert boards(p)[-1]["settings"] == {"sounds": True, "others": True, "recent": True, "reviewer": True, "versions": True, "sign": True}
            assert not kept().exists()
        await c.close()
    run(go())


def test_a_settings_file_it_cant_read_leaves_them_as_they_start(monkeypatch):
    monkeypatch.setenv("FIGARO_SOUNDS", "off")
    kept().write_text('{"others": "no", "versions": false, "tone": false', encoding="utf-8")
    bridge_settings.load()
    assert bridge_settings.current() == {"sounds": False, "others": True, "recent": True, "reviewer": True, "versions": True, "sign": True}
    kept().write_text('{"others": "no", "versions": false, "tone": false}', encoding="utf-8")
    bridge_settings.load()
    assert bridge_settings.current() == {"sounds": False, "others": True, "recent": True, "reviewer": True, "versions": False, "sign": True}


def test_with_versions_off_agents_save_none_unless_they_name_one():
    async def go():
        c = await make_client()
        async with FakePlugin(c) as p:
            bridge_settings.switch("versions", False)
            await post(c)
            await post(c, checkpoint="before the grid")
            bridge_settings.switch("versions", True)
            await post(c)
            off, named, on = p.of("exec")
            assert "checkpoint" not in off
            assert named["checkpoint"] == "Figaro · before the grid"
            # The label saved the file's version just now: the next one is due in an hour.
            assert "checkpoint" not in on
        await c.close()
    run(go())


def test_versions_back_on_save_one_before_the_next_change():
    async def go():
        c = await make_client()
        async with FakePlugin(c) as p:
            bridge_settings.switch("versions", False)
            await post(c)
            bridge_settings.switch("versions", True)
            await post(c)
            off, on = p.of("exec")
            assert "checkpoint" not in off
            assert on["checkpoint"] == "Figaro · before changes by an agent"
        await c.close()
    run(go())


# ─── what is new ──────────────────────────────────────────────────────────

CHANGELOG = """# Changelog

## [Unreleased]

- Something not out yet.

## [2.0.0] — 2026-11-01

### Added

- **Settings in the plugin's window:** three switches, and what is new.
  Wrapped onto a second line.
  - a nested point, left out
- `figaro shot --absolute` and `h.shot(node, {absolute: true})` export a layer at its own size. More.
- [The README](README.md) has a picture (#4)
- A very long note that goes on and on, far past what two lines of a window three hundred and twenty pixels wide can show
- Four
- Five
- Six, one too many

## [1.9.0] — 2026-10-20

- Older.
"""


def test_a_releases_notes_are_its_first_clauses_a_line_each():
    assert bridge_settings.changelog_notes(CHANGELOG, "2.0.0") == [
        "Settings in the plugin's window",
        "figaro shot --absolute and h.shot(node, {absolute: true}) export a layer at its own size",
        "The README has a picture",
        "A very long note that goes on and on, far past what two lines of a window three hundred and twenty pixels…",
        "Four",
    ]
    assert bridge_settings.changelog_notes(CHANGELOG, "1.9.0") == ["Older"]
    assert bridge_settings.changelog_notes(CHANGELOG, "3.0.0") == []
    assert all(len(n) <= bridge_settings.NOTE_LEN for n in bridge_settings.changelog_notes(CHANGELOG, "2.0.0"))


def test_the_window_gets_this_versions_notes_then_the_newest_releases(tmp_path, monkeypatch):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG.replace("2.0.0", bridge.VERSION), encoding="utf-8")
    monkeypatch.setattr(bridge, "HERE", tmp_path)
    notes = bridge_board.payload()["notes"]
    assert notes["version"] == bridge.VERSION and notes["items"][0] == "Settings in the plugin's window"
    bridge.UPDATE = {"latest": NEWER, "current": bridge.VERSION, "url": bridge.release_url(NEWER)}
    assert bridge_board.payload()["notes"]["version"] == bridge.VERSION  # until the release's notes come
    bridge_settings.NOTES[NEWER] = ["Faster"]
    assert bridge_board.payload()["notes"] == {"version": NEWER, "items": ["Faster"]}


def test_no_changelog_no_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "HERE", tmp_path)
    assert bridge_board.payload()["notes"] is None


def test_the_folder_is_shown_from_home(monkeypatch):
    monkeypatch.setattr(bridge, "HERE", Path.home() / "figaro")
    assert bridge_board.payload()["folder"] == str(Path("~") / "figaro")
    monkeypatch.setattr(bridge, "HERE", Path.home().parent / "figaro")
    assert bridge_board.payload()["folder"] == str(Path.home().parent / "figaro")


# ─── the version: ↻ and Update ────────────────────────────────────────────

def test_check_now_asks_github_once_and_tells_every_window(monkeypatch):
    monkeypatch.setenv("FIGARO_NO_UPDATE_CHECK", "0")
    asked = []

    async def check():
        asked.append(1)
        await asyncio.sleep(0.1)
        return NEWER

    async def fetch(version):
        bridge_settings.NOTES[version] = ["Faster"]

    monkeypatch.setattr(bridge, "check_for_update", check)
    monkeypatch.setattr(bridge_settings, "fetch_notes", fetch)

    async def go():
        c = await make_client()
        async with FakePlugin(c, name="A", key="KEY1", caps=CAPS) as a, \
                FakePlugin(c, name="B", key="KEY2", caps=CAPS) as b:
            board = boards(b)[-1]
            assert (board["checks"], board["checked"], board["newest"]) == (True, None, None)
            for _ in range(2):  # a second press while it asks starts nothing
                await a.ws.send_str(json.dumps({"type": "check-update"}))
            await asyncio.sleep(0.3)
            assert asked == [1]
            board = boards(b)[-1]
            assert board["checked"] and abs(board["checked"] - board["now"]) < 5
            assert board["newest"] == {"latest": NEWER, "url": bridge.release_url(NEWER), "button": True}
            assert board["update"]["latest"] == NEWER
            assert board["notes"] == {"version": NEWER, "items": ["Faster"]}
        await c.close()
    run(go())


def test_a_check_github_doesnt_answer_changes_nothing(monkeypatch):
    monkeypatch.setenv("FIGARO_NO_UPDATE_CHECK", "0")

    async def check():
        return None

    monkeypatch.setattr(bridge, "check_for_update", check)

    async def go():
        await bridge.look_for_release()
        board = bridge_board.payload()
        assert (board["checked"], board["newest"]) == (None, None)
    run(go())


def test_check_now_with_the_check_off_asks_nothing(monkeypatch):
    asked = []

    async def check():
        asked.append(1)
        return NEWER

    monkeypatch.setattr(bridge, "check_for_update", check)

    async def go():
        c = await make_client()
        async with FakePlugin(c, caps=CAPS) as p:  # conftest: FIGARO_NO_UPDATE_CHECK=1
            assert boards(p)[-1]["checks"] is False
            await p.ws.send_str(json.dumps({"type": "check-update"}))
            await settle()
            assert asked == [] and boards(p)[-1]["checked"] is None
        await c.close()
    run(go())


def test_the_settings_offer_a_skipped_release_and_update_installs_it(quick_update):
    async def go():
        c = await make_client()
        async with FakePlugin(c, caps=CAPS) as p:
            assert bridge.skip_release(NEWER)
            await settle()
            board = boards(p)[-1]
            assert board["update"] is None and board["skipped"] == NEWER
            assert board["newest"]["latest"] == NEWER  # the settings still show it
            await p.ws.send_str(json.dumps({"type": "update-now"}))
            await asyncio.sleep(0.4)
            assert quick_update[:1] == ["pull"]
        await c.close()
    run(go())


def test_newest_says_when_the_update_button_is_off(monkeypatch):
    monkeypatch.setenv("FIGARO_UPDATE_BUTTON", "off")
    bridge.UPDATE = {"latest": NEWER, "current": bridge.VERSION, "url": bridge.release_url(NEWER)}
    assert bridge_board.payload()["newest"]["button"] is False
