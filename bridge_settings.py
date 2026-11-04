"""The plugin window's settings, and what they show about the version.

The switches are the bridge's, for every window: one switched in a window
changes them in all of them. They are kept next to the API token
(~/.figaro/settings.json), so they hold across restarts; until one is switched,
it follows the variable the bridge started with (FIGARO_SOUNDS=off).

    sounds    the window's sounds (never the keep-awake tone)
    others    the islands of other files where agents work; off: this file, and
              those where Figma stopped answering
    recent    each island's list of what agents changed lately; off: only the
              agents (a window before the switch ignores it)
    reviewer  the Reviewer agent (bridge_review); off: no row on any island,
              no check on `figaro done` in any file; each file's own switch is
              kept apart, in reviewer.json
    versions  a checkpoint before an agent first changes a file, then hourly
              (bridge_exec); an agent's --checkpoint "label" still saves one
    sign      a comment an agent posts in Figma (bridge_comments) starts with
              "[AI agent]": Figma takes it under the designer's account

Under them the version: what is new in it (the newest release's notes from
GitHub, else this version's from CHANGELOG.md, a line each), where Figaro runs,
and when the bridge last asked GitHub for a release (bridge.CHECKED; ↻ in the
window asks again, bridge.look_now).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import aiohttp

import bridge
import bridge_board
import figaro_token

NAMES = ("sounds", "others", "recent", "reviewer", "versions", "sign")
SETTINGS: dict = {}   # what was switched in a window: name -> bool
NOTES: dict = {}      # version -> what is new in it, a line each
NOTES_MAX = 5
NOTE_LEN = 110        # characters: two lines of the 320 px window


def _file():
    return figaro_token.token_path(bridge.PORT).parent / "settings.json"


def load():
    SETTINGS.clear()
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict):
        SETTINGS.update({k: v for k, v in data.items() if k in NAMES and isinstance(v, bool)})


def on(name):
    if name in SETTINGS:
        return SETTINGS[name]
    return bridge.sounds_on() if name == "sounds" else True


def current():
    return {name: on(name) for name in NAMES}


def switch(name, value):
    """A switch in a window. False for a name or a value it doesn't know."""
    if name not in NAMES or not isinstance(value, bool):
        return False
    SETTINGS[name] = value
    path = _file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")  # a write cut short leaves the old file whole
        part.write_text(json.dumps(SETTINGS, indent=2) + "\n", encoding="utf-8")
        os.replace(part, path)
    except OSError as e:
        print(f"[settings] can't keep them in {path} ({e}): until the bridge restarts")
    print(f"[settings] {name} turned {'on' if value else 'off'} in the plugin's window")
    bridge_board.changed()
    return True


def folder():
    """Where Figaro runs, with ~ for the home folder."""
    here, home = str(bridge.HERE), str(Path.home())
    return "~" + here[len(home):] if here == home or here.startswith(home + os.sep) else here


# ─── what is new ──────────────────────────────────────────────────────────

_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_ISSUE = re.compile(r"\s*\(#\d+\)")
_CODE = re.compile(r"`[^`]*`")
_CLAUSE_END = re.compile(r"[.:;](?=\s|$)")


def _bullets(lines):
    """The top-level items of a Markdown list, each with its wrapped lines."""
    items = []
    for line in lines:
        if line.startswith("- "):
            items.append(line[2:].strip())
        elif items and line.startswith("  ") and not line.lstrip().startswith("- ") and line.strip():
            items[-1] += " " + line.strip()
    return items


def _gist(item):
    """A note's first clause in plain words: what the window has room for."""
    text = _ISSUE.sub("", _LINK.sub(r"\1", item)).replace("**", "")
    # where the clause ends, never inside `code`
    end = _CLAUSE_END.search(_CODE.sub(lambda m: "x" * len(m.group()), text))
    text = (text[:end.start()] if end else text).replace("`", "").strip()
    if len(text) > NOTE_LEN:
        text = text[:NOTE_LEN - 1].rsplit(" ", 1)[0].rstrip(",") + "…"
    return text


def notes_of(lines):
    return [g for g in (_gist(i) for i in _bullets(lines)) if g][:NOTES_MAX]


def changelog_notes(text, version):
    """The notes under '## [version]' in a changelog."""
    section, inside = [], False
    for line in text.splitlines():
        if line.startswith("## ["):
            if inside:
                break
            inside = line.startswith(f"## [{version}]")
        elif inside:
            section.append(line)
    return notes_of(section)


async def fetch_notes(version):
    """The notes of a release on GitHub, once; nothing when it can't be told."""
    if NOTES.get(version):
        return
    url = f"https://api.github.com/repos/{bridge.REPO}/releases/tags/v{version}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "figaro-bridge"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url, headers=headers) as r:
                if r.status != 200:
                    return
                body = (await r.json()).get("body") or ""
    except Exception:
        return
    items = notes_of(str(body).splitlines())
    if items:
        NOTES[version] = items
        bridge_board.changed()


def notes():
    """{version, items}: the newest release's, once fetched, else this version's."""
    newest = bridge.UPDATE and bridge.UPDATE["latest"]
    if newest and NOTES.get(newest):
        return {"version": newest, "items": NOTES[newest]}
    version = bridge.VERSION
    if version not in NOTES:
        try:
            NOTES[version] = changelog_notes((bridge.HERE / "CHANGELOG.md").read_text(encoding="utf-8"), version)
        except (OSError, UnicodeDecodeError):
            NOTES[version] = []
    return {"version": version, "items": NOTES[version]} if NOTES[version] else None


def board():
    """This module's part of the board."""
    newest = bridge.UPDATE
    return {
        "sounds": on("sounds"),          # what a window before 1.5.0 reads
        "settings": current(),
        "notes": notes(),
        "folder": folder(),
        # the newest release, skipped or not, for the settings
        "newest": ({"latest": newest["latest"], "url": newest["url"], "button": bridge.update_allowed()}
                   if newest else None),
        "checks": bridge.update_checks_on(),
        "checked": bridge.CHECKED or None,
    }
