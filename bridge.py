#!/usr/bin/env python3
"""Figaro bridge: HTTP -> WS -> Figma plugin -> back.

HTTP API (clients like curl / figaro CLI talk here):
    POST /exec     {"code": "...", "timeout": 60} -> {ok, result, value, logs, elapsed_ms}
                   "value_only": true leaves `result` out when it only repeats `value` as text
    GET  /status                                  -> {plugin_connected, pending}

WebSocket (the Figma plugin connects here once it's opened in Figma Desktop):
    WS   /plugin

Run:
    python bridge.py                 # default 127.0.0.1:8788
    python bridge.py --port 9000
    python bridge.py --host 0.0.0.0  # expose on LAN (not recommended)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import sys
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web, WSMsgType

# bridge_exec.py and bridge_idle.py import this module as `bridge`. Run as a
# script it is `__main__`, and a plain import would load a second copy with
# its own PLUGINS / PENDING — so both names point at this one.
if __name__ == "__main__":
    sys.modules.setdefault("bridge", sys.modules[__name__])
import bridge_board  # noqa: E402
import bridge_comments  # noqa: E402
import bridge_exec  # noqa: E402
import bridge_idle  # noqa: E402
import bridge_review  # noqa: E402
import bridge_settings  # noqa: E402
import bridge_update  # noqa: E402
import figaro_token  # noqa: E402
import result_text  # noqa: E402


PENDING: dict = {}        # rid -> {"future", "logs", "t0", "conn"}
PLUGINS: dict = {}        # conn_id -> {"ws", "fileKey", "name", "docSig"}
SLOT_PROBE_TIMEOUT = 1.0  # how long an incumbent connection has to answer a ping
LOCKS: dict = {}          # conn_id -> asyncio.Lock — one exec at a time per file
ABANDONED: dict = {}      # rid -> {"conn", "doc", "t0", "timeout", "at", "agent"} — timed out, may still be running
QUEUE: dict = {}          # conn_id -> {"waiting": [{agent, since}], "running": {agent, since} | None}
DISCONNECTED = "plugin disconnected mid-request"

# Host: values accepted in the Host header. Populated in main() from the bind
# address. Empty set means "don't check" — chosen when the user binds a
# non-loopback address on purpose (--host 0.0.0.0).
ALLOWED_HOSTS: set = set()
# The plugin's window is a page in Figma's desktop app, and its User-Agent
# names the app: "… Figma/126.9.11 Chrome/… Electron/…", FigmaBeta/ and
# FigmaGov/ in their builds, figma-linux/0.11.5 in figma-linux. A web page can
# open a socket with Origin "null" too, from a sandboxed iframe, but it can't
# change its User-Agent.
FIGMA_APP = re.compile(r"\bfigma[\w-]*/\d", re.IGNORECASE)
# The secret of the HTTP API (see _guard); main() makes it, tests set it.
TOKEN = None
PORT = 8788

# ─── version checks ───────────────────────────────────────────────────────
# A running plugin keeps the code it started with, and a running bridge keeps
# the bridge.py it started with. After a `git pull` both can be stale without
# any sign of it, so compare what is running against what is on disk.

HERE = Path(__file__).resolve().parent
PLUGIN_CODE = HERE / "plugin" / "code.js"
_VERSION_RE = re.compile(r'PLUGIN_VERSION\s*=\s*"([^"]+)"')


def _file_digest(path: Path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# The bridge runs its helper modules too — a changed bridge_exec.py is old code just the same.
_BRIDGE_CODE = [Path(__file__).resolve()] + [
    HERE / n for n in ("bridge_board.py", "bridge_comments.py", "bridge_exec.py", "bridge_idle.py", "bridge_review.py",
                       "bridge_settings.py",
                       "bridge_update.py", "figma_links.py", "figaro_token.py", "result_text.py")]
BRIDGE_DIGEST = [_file_digest(p) for p in _BRIDGE_CODE]


def expected_plugin_version():
    """PLUGIN_VERSION as written in plugin/code.js right now, or None."""
    try:
        m = _VERSION_RE.search(PLUGIN_CODE.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def plugin_outdated(info) -> bool:
    """True when this connection runs older plugin code than plugin/code.js.

    A plugin from before version reporting sends nothing — that is outdated too.
    """
    expected = expected_plugin_version()
    return expected is not None and info.get("pluginVersion") != expected


def bridge_outdated() -> bool:
    """True when bridge.py on disk differs from the one this process loaded."""
    return [_file_digest(p) for p in _BRIDGE_CODE] != BRIDGE_DIGEST  # and its helper modules


# ─── update check against GitHub releases ─────────────────────────────────
# The checks above catch code that is older than this checkout. This one asks
# whether a newer Figaro has been RELEASED — a `vX.Y.Z` tag on GitHub — so
# the plugin bar can offer an Update button, which installs that tag and
# nothing after it (bridge_update.py). Releases, not commits: a README typo
# pushed to main is not a new version. It never blocks anything and stays
# silent on any failure (a private repository answers 404 to it, too).
# Set FIGARO_NO_UPDATE_CHECK=1 to turn it off.
# FIGARO_UPDATE_BUTTON=off is different: the check still runs, and the window
# says a release is out and links to it, but it has no Update button and the
# bridge refuses update-now. Reload, which pulls nothing, keeps working.
#
# Releasing (see AGENTS.md → Releasing): bump VERSION here, move CHANGELOG's
# "Unreleased" under the new version, tag vX.Y.Z and push the tag.

VERSION = "1.9.3"
REPO = "artem-levchenko/figaro"
# The repository's address until its owner's account was renamed (November
# 2026). GitHub redirects it only while no repository takes that name again, so
# an update moves a clone of it to REPO (bridge_update.moved_origin).
MOVED_FROM = "artem-levchenko-2/figaro"
UPDATE_EVERY = 6 * 60 * 60   # seconds between checks
UPDATE: dict | None = None   # {"latest", "current", "url"} once a newer release exists
SKIPPED: str | None = None   # the release the user skipped with the window's Skip (skip_release)
CHECKED = 0.0                # when GitHub last answered the check (the window's settings show it)
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def update_allowed():
    """False with FIGARO_UPDATE_BUTTON=off (or 0, false, no): the window must not pull code."""
    return os.environ.get("FIGARO_UPDATE_BUTTON", "").strip().lower() not in ("off", "0", "false", "no")


def update_checks_on():
    """False with FIGARO_NO_UPDATE_CHECK=1 (or yes, true); 0, as anywhere, leaves the check on."""
    return os.environ.get("FIGARO_NO_UPDATE_CHECK", "").strip().lower() in ("", "0", "false", "no", "off")


def sounds_on():
    """False with FIGARO_SOUNDS=off (or 0, false, no): the window's Sounds start off (bridge_settings)."""
    return os.environ.get("FIGARO_SOUNDS", "").strip().lower() not in ("off", "0", "false", "no")


def parse_version(tag):
    """'v2.3.0' / '2.3.0' -> (2, 3, 0); anything else (pre-releases too) -> None."""
    m = _SEMVER.match(str(tag).strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newest_release(tags):
    """The highest x.y.z among GitHub tag names, as its string, or None."""
    versions = [(v, t) for t in tags if (v := parse_version(t))]
    return max(versions)[1].lstrip("v") if versions else None


def release_url(version):
    return f"https://github.com/{REPO}/releases/tag/v{version}"


async def check_for_update():
    """The newest released version on GitHub, or None if it can't be told."""
    url = f"https://api.github.com/repos/{REPO}/tags?per_page=100"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "figaro-bridge"}
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as r:
                if r.status != 200:
                    return None
                return newest_release(t.get("name") for t in await r.json())
    except Exception:
        return None


async def set_update(latest):
    """Record the newest release and tell every plugin when it changes."""
    global UPDATE
    newer = latest and parse_version(latest) and parse_version(latest) > parse_version(VERSION)
    new = ({"latest": latest, "current": VERSION, "url": release_url(latest)}
           if newer else None)
    if new == UPDATE:
        return
    UPDATE = new
    if new:
        print(f"[update] Figaro {latest} is released (this is {VERSION}) — "
              + ("Update in the plugin's window installs it" if update_allowed()
                 else "the Update button is off: update by hand"))
    for _, info in _live_plugins():
        await _send_update(info["ws"])
    bridge_board.changed()


async def _send_update(ws):
    if not offered() or not update_allowed():
        return
    try:
        await ws.send_str(json.dumps({"type": "update", **UPDATE}))
    except Exception:
        pass


# The window's Skip hides a release until a newer one: from every window, and
# from the agents' notes, which would have them tell the user about it again.
# It is kept next to the API token (~/.figaro/skipped-release), so it holds
# across restarts of the bridge and of the plugin.
def _skipped_file():
    return figaro_token.token_path(PORT).parent / "skipped-release"


def load_skipped():
    global SKIPPED
    try:
        text = _skipped_file().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        text = ""
    SKIPPED = text if parse_version(text) else None


def skip_release(version):
    """The window's Skip. Only the release on offer: False for any other."""
    global SKIPPED
    if not UPDATE or version != UPDATE["latest"]:
        return False
    SKIPPED = version
    path = _skipped_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(version + "\n", encoding="utf-8")
    except OSError as e:
        print(f"[update] can't keep the skipped release in {path} ({e}): until the bridge restarts")
    print(f"[update] Figaro {version} skipped in the plugin's window")
    bridge_board.changed()
    return True


def offered():
    """The newer release, unless the user skipped it."""
    return UPDATE if UPDATE and UPDATE["latest"] != SKIPPED else None


async def look_for_release():
    """Ask GitHub for the newest release: record it, its notes, and when it answered."""
    global CHECKED
    latest = await check_for_update()
    if latest is None:
        return
    CHECKED = time.time()
    await set_update(latest)
    if UPDATE:
        await bridge_settings.fetch_notes(UPDATE["latest"])
    bridge_board.changed()


_look = None


def look_now():
    """↻ in a window's settings: one look at a time, and none with the check off."""
    global _look
    if not update_checks_on() or (_look is not None and not _look.done()):
        return False
    _look = asyncio.get_running_loop().create_task(look_for_release())
    return True


async def _update_loop(app):
    await asyncio.sleep(5)  # let the plugins reconnect first
    while True:
        await look_for_release()
        await asyncio.sleep(UPDATE_EVERY)


async def _start_update_checks(app):
    app["update_task"] = asyncio.get_running_loop().create_task(_update_loop(app))


async def _stop_update_checks(app):
    task = app.get("update_task")
    if task:
        task.cancel()


def _doc_key(conn_id):
    """What the queue is keyed by: the document, not the connection.

    One file open in two tabs is two connections over ONE document, so a lock per
    connection would let two writers into the same file at once. The plugin's
    document id (docSig) names the document; a plugin too old to send one falls
    back to its connection.
    """
    info = PLUGINS.get(conn_id) or {}
    sig = info.get("docSig")
    return f"doc:{sig}" if sig else conn_id


def _lock_for(key):
    """One lock per document (see _doc_key).

    The Figma plugin sandbox runs a single-threaded async message handler, so two
    concurrent execs interleave at every `await` inside them — over one shared
    document and one shared undo stack. Serializing per document is what makes
    concurrent callers (orchestrator + agents) safe on the same file.
    """
    lock = LOCKS.get(key)
    if lock is None:
        lock = LOCKS[key] = asyncio.Lock()
    return lock


def _guard(request: web.Request, *, allow_null_origin: bool = False, need_token: bool = True):
    """Reject browser-driven requests. Returns an error Response, or None if OK.

    The bridge executes arbitrary JS inside the user's Figma file, so any web
    page the user happens to have open is part of the threat model — binding to
    127.0.0.1 only keeps other machines out, not other tabs.

    Two checks:
      * Origin — local clients (curl, the figaro CLI) never send this header.
        A browser always does on cross-origin requests, so its mere presence
        means the request came from a page. This blocks CSRF, including the
        "simple request" trick of posting JSON as text/plain to dodge preflight.
        The plugin's window reports "null", and so does a sandboxed iframe on
        any page: "null" passes only for a window of Figma's desktop app
        (FIGMA_APP), or a page could stand in for the plugin, read the board
        and answer the agents' scripts in its place.
      * Host — a page whose DNS is re-pointed at 127.0.0.1 (DNS rebinding)
        becomes same-origin with the bridge and could then read responses.
        Pinning Host to the loopback names we actually serve closes that.

    Then the token: every endpoint that changes a file or runs code wants the
    secret the bridge wrote to its token file (figaro_token.py). A local process
    of another user, or a page that got past the checks above, can't read it.
    The `guard` middleware below runs this before every handler.
    """
    if ALLOWED_HOSTS:
        host = (request.headers.get("Host") or "").lower()
        if host not in ALLOWED_HOSTS:
            return web.json_response(
                {"ok": False, "error": f"unexpected Host header: {host!r}"}, status=403,
            )

    origin = request.headers.get("Origin")
    if origin is not None:
        figma = FIGMA_APP.search(request.headers.get("User-Agent") or "")
        if not (allow_null_origin and origin == "null" and figma):
            return web.json_response(
                {"ok": False,
                 "error": "cross-origin requests are not allowed",
                 "hint": "the bridge only accepts local clients (curl, figaro CLI) "
                         "and the Figma plugin in Figma's desktop app"},
                status=403,
            )
    if need_token:
        sent = request.headers.get("X-Figaro-Token") or ""
        if not TOKEN or not hmac.compare_digest(sent.encode(errors="replace"), TOKEN.encode()):
            return web.json_response(
                {"ok": False,
                 "error": "missing or wrong X-Figaro-Token",
                 "hint": f"the token is in {figaro_token.token_path(PORT)}, written when "
                         "the bridge started: send its text in the X-Figaro-Token header. "
                         "figaro reads it by itself: if the caller runs in another home "
                         "folder or sandbox, set FIGARO_TOKEN_FILE to that path; if the "
                         "bridge was restarted since, run the command again"},
                status=401,
            )
    return None


@web.middleware
async def guard(request: web.Request, handler):
    """_guard before every handler, so a new route can't go without it.

    A GET only reads, so it needs no token; the plugin's socket can't send one
    either (the plugin can't read files), and its window reports Origin "null".
    """
    plugin = request.path == "/plugin"
    blocked = _guard(request, allow_null_origin=plugin,
                     need_token=not plugin and request.method not in ("GET", "HEAD"))
    if blocked is None:
        return await handler(request)
    if plugin:
        print(f"[plugin] refused connection from {request.remote} "
              f"(origin={request.headers.get('Origin')!r}, "
              f"user-agent={(request.headers.get('User-Agent') or '')[:160]!r})")
    return blocked


FROZEN_HINT = ("node.fills, node.strokes, node.effects and their paints are frozen: copy the array "
               "(JSON.parse(JSON.stringify(node.fills))), change the copy and assign it whole, "
               "or use h.solid(), h.bF(), h.bS()")
ERROR_HINTS = [
    # The errors agents hit most come first, so they win over the broader
    # matches below.
    ("took back a step that was not the script's",
     "only the user can bring that step back: tell them at once to press Cmd+Shift+Z in Figma, "
     "before anyone changes the file. Don't run undo or the script again before they have"),
    ("did not answer before --timeout ran out",
     "Figma never answered this call: the script was stopped and the file is free. A retry is "
     "unlikely to help. Import by key and figma.teamLibrary sometimes never answer at all: take "
     "components, styles and variables the file already has by id — `figaro inspect` of a layer "
     "that uses them prints it"),
    ("was still running when --timeout ran out",
     "this loop never ended: check its condition, and that what it tests changes each time round. "
     "The script was stopped and the file is free; what it changed before that stays (figaro undo "
     "takes it back)"),
    ("Could not find a component property with name",
     "property names in a set often carry an emoji and a #id (\"✒️ Label#164:0\"): use "
     "await h.variant(inst, { Label: '…', 'Right Icon': false }) — it finds the full name from "
     "the short one and lists the names there are"),
    ("could not find variable with key",
     "Figma imports a variable by key only from an enabled library that published it. Take a "
     "variable a layer in this file already uses by id: `figaro inspect` of that layer prints "
     "\"id VariableID:…\" — await h.var_(id), h.bF(node, 0, id), h.bN(node, prop, id)"),
    ("Unable to establish connection to Figma",
     "Figma says this after ~10 s for an id this file does not have: a layer from another file "
     "(check -T) or a deleted one. h.node(id) and h.resolve(id) tell you at once"),
    ("New parent is an instance",
     "layers cannot be added to an instance: detach it (inst.detachInstance()) or change its "
     "main component"),
    ("set_counterAxisAlignItems",
     "counterAxisAlignItems is MIN, MAX, CENTER or BASELINE, and has no STRETCH: to stretch a child "
     "across its parent use await h.fill(child, 'x' or 'y') (layoutSizingHorizontal/Vertical = 'FILL')"),
    ("could not be loaded",
     "this font is not installed on this computer: await h.fonts(node) shows which are missing; "
     "for Font Awesome icon fonts, await h.fa() lists the installed ones"),
    ("unloaded font",
     "load the fonts first: await h.setText(node, text), or await h.fonts(node) before the edit — "
     "both load mixed fonts too"),
    ("fills and strokes variable bindings must be set on paints directly",
     "use h.bF(node, idx, varId) to bind a fill paint to a variable"),
    ("strokes variable bindings must be set on paints directly",
     "use h.bS(node, idx, varId) to bind a stroke paint to a variable"),
    # A write that can't happen: scripts run in strict mode, so it fails.
    # QuickJS, in Figma, says "'x' is read-only" for a const that takes a new
    # value and for a frozen paint's "'color'" alike; V8 the longer form, and
    # only for a property.
    ("is read-only", "a const can't take a new value: declare it with let. If it is a property, "
                     + FROZEN_HINT),
    ("Cannot assign to read only property", FROZEN_HINT),
    ("object is not extensible", FROZEN_HINT),
    ("no setter for property",
     "this property can only be read: width and height change with node.resize(w, h) or "
     "h.fixed(node, w, h), an instance's properties with h.variant(inst, {...})"),
    ("is not defined",
     "scripts run in strict mode: declare a variable with const or let before its first use; "
     "if it is declared, check how the name is spelled"),
    ("permission not specified in manifest",
     "Figaro asks Figma for nothing but the team library: figma.currentUser, figma.activeUsers "
     "and the authors of FigJam stamps are off for scripts. Leave them out, and don't edit "
     "Figaro's manifest.json"),
    ("font has not been loaded",
     "use h.setText(node, text) or h.withFonts(root, fn) — they autoload fonts"),
    ("Cannot find font",
     "fontName may be missing or mixed — check node.fontName before loading"),
    ("appendChild",
     "create node, then parent.appendChild(node) BEFORE setting layoutMode/resize/itemSpacing/padding"),
    ("Unable to find a variant",
     "no variant matches those property values — check available: const v = await h.variantsOf(instance); return v.groups"),
    ("Invalid property name",
     "check available variants: const v = await h.variantsOf(instance); return v.groups"),
    ("Invalid value",
     "check variant values: const v = await h.variantsOf(instance); return v.groups"),
    ("setProperties",
     "if 'Unable to find variant' — check available values via h.variantsOf(instance)"),
    ("not a function",
     "the method may be renamed, exist only as an async version (getNodeByIdAsync, "
     "getMainComponentAsync), or exist only in Figma's MCP server: figma.createAutoLayout(), "
     "node.query(), node.set() and node.screenshot() are not in the Plugin API"),
]


def find_hint(error_text):
    if not error_text:
        return None
    low = error_text.lower()
    for needle, hint in ERROR_HINTS:
        if needle.lower() in low:
            return hint
    return None


async def _still_alive(info, timeout: float = SLOT_PROBE_TIMEOUT) -> bool:
    """Ping one plugin connection and wait for its pong.

    An open socket is not a live plugin: a laptop that slept, or a Figma tab that
    was closed uncleanly, leaves a socket the OS has not torn down yet. Only an
    answered ping distinguishes that from a plugin that is simply idle — which
    matters because the same file legitimately open in two tabs also produces two
    live registrations, and those must NOT evict each other.
    """
    ws = info["ws"]
    if ws.closed:
        return False
    fut = asyncio.get_event_loop().create_future()
    info["pong"] = fut
    try:
        await ws.send_str(json.dumps({"type": "ping"}))
    except Exception:
        info.pop("pong", None)
        return False
    try:
        await asyncio.wait_for(fut, timeout)
        return True
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return False
    finally:
        info.pop("pong", None)


async def _broadcast_peers():
    """Tell every connected plugin its position and the total, for the "1/2" badge.

    Only connections that have said `hello` count: one that just opened is not a
    file yet, and counting it made the badge flash "1/3" during a reconnect.
    """
    live = [(cid, i) for cid, i in _live_plugins() if i.get("hello")]
    for idx, (_, info) in enumerate(live, 1):
        try:
            await info["ws"].send_str(json.dumps(
                {"type": "peers", "index": idx, "total": len(live)}))
        except Exception:
            pass


def _text(value):
    """A field of a window's hello: a non-empty string, or None."""
    return value if isinstance(value, str) and value else None


def _in_flight(conn_id):
    """A request went to this window and it hasn't answered yet."""
    return any(e.get("conn") == conn_id for e in [*PENDING.values(), *ABANDONED.values()])


async def plugin_ws_handler(request: web.Request):
    # 64 MB: a shot of a long page at 2× is past 16 MB.
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024 * 1024)
    await ws.prepare(request)

    # Each plugin (one per open Figma file) gets its own registry slot. Identity
    # (fileKey / file name) arrives in the `hello` message a moment after connect.
    # Reconnects never lock out: a dead same-file connection is dropped at
    # `hello` time.
    conn_id = str(uuid.uuid4())
    PLUGINS[conn_id] = {"ws": ws, "fileKey": None, "name": None, "docSig": None}
    print(f"[plugin] connected {conn_id[:8]} from {request.remote}")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                print(f"[plugin] ws error: {ws.exception()}")
                break
            if msg.type != WSMsgType.TEXT:
                continue

            try:
                m = json.loads(msg.data)
            except json.JSONDecodeError:
                print(f"[plugin] bad json: {msg.data[:200]!r}")
                continue
            if not isinstance(m, dict):
                continue

            mtype = m.get("type")

            if mtype == "hello":
                # What a window says of itself. Anything but text is left out:
                # it would break /status, /targets and every route to the file.
                file_key, name, doc_sig, version = (
                    _text(m.get(k)) for k in ("fileKey", "name", "docSig", "plugin"))
                # Drop an older registration for the same file ONLY if its socket
                # is dead (plugin re-Run without a clean close, a laptop that slept).
                #
                # Evicting a LIVE one too would loop forever with the same file open
                # in two tabs/windows: each side would kick the other out, and the
                # loser would reconnect 2s later and kick back. Two live views of
                # one document are legitimate — both stay, and scripts go to the
                # first (resolve_target): a window that comes later never takes a
                # file over while the window that has it answers.
                #
                # Nor is a window dropped while a script it got hasn't ended: the
                # script may hold its thread past the ping's second, and dropping
                # it would cut the script off and let the next one run beside it
                # in the other window. A dead one goes with the heartbeat.
                for cid, info in list(PLUGINS.items()):
                    if cid == conn_id:
                        continue
                    same = (file_key and info.get("fileKey") == file_key) or (
                        name and info.get("fileKey") is None and info.get("name") == name
                    )
                    # Asked again after the ping: a script may go there meanwhile.
                    if not same or _in_flight(cid) or await _still_alive(info) or _in_flight(cid):
                        continue
                    print(f"[plugin] dropping dead connection for {name!r}")
                    try:
                        await info["ws"].close(code=1000, message=b"superseded")
                    except Exception:
                        pass
                    PLUGINS.pop(cid, None)
                PLUGINS[conn_id]["fileKey"] = file_key
                PLUGINS[conn_id]["name"] = name
                PLUGINS[conn_id]["docSig"] = doc_sig
                PLUGINS[conn_id]["hello"] = True
                PLUGINS[conn_id]["pluginVersion"] = version
                # What this build does beyond exec (undo, reload, …).
                caps = m.get("caps")
                PLUGINS[conn_id]["caps"] = (
                    [c for c in caps if isinstance(c, str)] if isinstance(caps, list) else [])
                print(f"[plugin] hello file={name!r} key={file_key} sig={doc_sig} "
                      f"plugin={version} protocol={m.get('version', '?')}")
                if plugin_outdated(PLUGINS[conn_id]):
                    # The plugin bar turns purple and asks for a re-Run.
                    try:
                        await ws.send_str(json.dumps({
                            "type": "outdated",
                            "running": version,
                            "expected": expected_plugin_version(),
                        }))
                    except Exception:
                        pass
                bridge_board.found(_doc_key(conn_id), file_key)  # its window is back
                await _send_update(ws)
                await _broadcast_peers()
                bridge_board.changed()
                continue
            if mtype == "pong":
                # Answer to a liveness probe: this slot is defended, so a new
                # connection for the same file will not take it over.
                fut = PLUGINS.get(conn_id, {}).pop("pong", None)
                if fut is not None and not fut.done():
                    fut.set_result(True)
                continue

            # A button in the window: Stop, Update, Reload — from a window that
            # has said which file it is.
            if PLUGINS[conn_id].get("hello") and await bridge_board.handle(conn_id, m):
                continue

            rid = m.get("id")
            if not isinstance(rid, str):
                continue
            # Only the window a request went to answers it.
            sent_to = (PENDING.get(rid) or ABANDONED.get(rid) or {}).get("conn")
            if sent_to is not None and sent_to != conn_id:
                continue
            if mtype in ("result", "error"):
                # The script has ended, so any interlock it holds lifts — whether
                # the reply is late (client got a 504 long ago) or lands in the same
                # tick as the timeout, while the request is still being torn down.
                # Only a final reply counts: an orphan's `log` lines mean it is
                # still running, and lifting the interlock on those let the next
                # caller write alongside it.
                orphan = ABANDONED.pop(rid, None)
                if orphan is not None:
                    late = time.time() - orphan["t0"]
                    print(f"[orphan] {rid[:8]} finished after {late:.0f}s "
                          f"({mtype}) — client already received a 504")
                    bridge_board.changed()  # the file answers again
            entry = PENDING.get(rid)
            if not entry:
                continue

            if mtype == "log":
                if isinstance(m.get("lines"), list):
                    entry["logs"].extend(str(x) for x in m["lines"])
            elif mtype in ("result", "error"):
                # Remember the script ended even if nobody is waiting any more:
                # the timeout may have cancelled the future a moment ago, and the
                # request is about to be marked abandoned (see _abandon).
                entry["finished"] = True
                if not entry["future"].done():
                    entry["future"].set_result(m)
    finally:
        key = _doc_key(conn_id)
        # A script of its still ran, or ran on past its deadline: the window may
        # have closed because Figma stopped answering. Asked before the
        # requests are failed below.
        mid = [e.get("agent") for e in PENDING.values()
               if e.get("conn") == conn_id and e.get("mtype") == "exec" and e.get("board", True)
               and not e["future"].done()]
        mid += [o.get("agent") for o in ABANDONED.values() if o.get("conn") == conn_id]
        info = PLUGINS.pop(conn_id, None) or {}
        # The lock belongs to the document: keep it while another tab on the same
        # document is still connected or someone is holding / waiting for it.
        if not any(_doc_key(c) == key for c in PLUGINS):
            q = QUEUE.get(key)
            lock = LOCKS.get(key)
            if not (q and (q["waiting"] or q["running"])) and not (lock and lock.locked()):
                LOCKS.pop(key, None)
                QUEUE.pop(key, None)
        print(f"[plugin] disconnected {conn_id[:8]}")
        # Fail in-flight requests routed to THIS connection so clients don't hang.
        for rid, entry in list(PENDING.items()):
            if entry.get("conn") == conn_id and not entry["future"].done():
                entry["future"].set_result({"id": rid, "type": "error", "text": DISCONNECTED})
        # The sandbox died with the connection, so nothing can still be mutating
        # through it — drop its interlock instead of wedging the next reconnect.
        for rid, orphan in list(ABANDONED.items()):
            if orphan.get("conn") == conn_id:
                ABANDONED.pop(rid, None)
        if mid and info.get("hello") and not any(_doc_key(c) == key for c in PLUGINS):
            bridge_board.lost(key, info, mid[0])
        await _broadcast_peers()
        bridge_board.changed()
    return ws


def _live_plugins():
    """Connected, non-closed plugin slots as a list of (conn_id, info)."""
    return [(cid, i) for cid, i in PLUGINS.items() if not i["ws"].closed]


def resolve_target(target):
    """Map a target string (connection id, file name, fileKey, or substring) to a connection.

    Returns (conn_id, info) on success, or (None, reason) where reason is a
    human-readable string explaining the miss for the error response.
    """
    live = _live_plugins()
    if not live:
        return None, "plugin not connected — open Figaro in Figma"

    if target is None or target == "":
        if len(live) == 1:
            return live[0]
        # Several tabs on ONE document are still one file — no target needed.
        if len({_doc_key(c) for c, _ in live}) == 1 and live[0][1].get("docSig"):
            return live[0]
        names = ", ".join(repr(i.get("name") or f"({c[:8]})") for c, i in live)
        return None, (f"{len(live)} files connected — specify a target. "
                      f"Connected: {names}")

    t = target.lower()
    # 0. a document id (stable: stored in the file) or a connection id (changes
    #    on every reconnect), 6+ characters — the way to pick one of two
    #    different files that share a name.
    if len(t) >= 6:
        by_doc = [(cid, i) for cid, i in live if (i.get("docSig") or "").lower().startswith(t)]
        if by_doc and len({i.get("docSig") for _, i in by_doc}) == 1:
            return by_doc[0]  # one document; its first window if open in several tabs
        by_conn = [(cid, i) for cid, i in live if cid.startswith(t)]
        if len(by_conn) == 1:
            return by_conn[0]
    # 1. exact name (case-insensitive) — must be unique, or the caller cannot know
    #    which of two same-named windows it just wrote to.
    exact = [(cid, i) for cid, i in live if i.get("name") and i["name"].lower() == t]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # Same name, same document signature = one file open in several tabs or
        # windows. Both views edit the same document, so routing to the first
        # live one is safe; the caller cannot end up in "the other copy". The
        # first, not the newest: a window that comes later must not take over
        # a file whose window still answers (see the hello in plugin_ws_handler).
        sigs = {i.get("docSig") for _, i in exact}
        if len(sigs) == 1 and None not in sigs:
            return exact[0]
        # Same name, different documents (or a plugin build too old to report a
        # signature) — genuinely ambiguous, and a write would land in a file the
        # caller did not choose.
        conns = ", ".join(c[:8] for c, _ in exact)
        return None, (f"{len(exact)} connected files are named {target!r} "
                      f"(conns: {conns}) — different documents sharing a name. "
                      f"Pick one by its document id from `figaro targets`, e.g. "
                      f"-T {exact[0][1].get('docSig') or exact[0][0][:8]}, or rename one of the files")
    # 2. exact fileKey
    for cid, i in live:
        if i.get("fileKey") == target:
            return cid, i
    # 3. unambiguous substring of the name
    subs = [(cid, i) for cid, i in live if i.get("name") and t in i["name"].lower()]
    if len(subs) == 1:
        return subs[0]

    names = ", ".join(repr(i.get("name") or f"({c[:8]})") for c, i in live)
    if len(subs) > 1:
        return None, f"target {target!r} matches multiple files: {names}"
    return None, f"no connected file matches {target!r}. Connected: {names}"


async def exec_handler(request: web.Request) -> web.Response:
    def bad(msg):
        return web.json_response({"ok": False, "error": msg}, status=400)

    try:
        body = await request.json()
    except (ValueError, LookupError, RecursionError):  # bad JSON or bytes, an unknown charset
        return bad("invalid JSON body")
    # Agents send all sorts of things; anything malformed is a 400 that says
    # what is wrong, never a 500 from inside the bridge.
    if not isinstance(body, dict):
        return bad("body must be a JSON object: {\"code\": \"...\"}")

    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        return bad("missing or empty 'code'")

    target = body.get("target")
    if target is not None and not isinstance(target, str):
        return bad("'target' must be a string: a file name or doc id")

    try:
        timeout = _seconds(body.get("timeout", 60), "timeout")
        # How long this caller is willing to wait in the file's queue before
        # giving up. Defaults to its own timeout, so the total stays bounded.
        queue_timeout = _seconds(body.get("queue_timeout", timeout), "queue_timeout")
    except ValueError as e:
        return bad(str(e))
    parallel = body.get("parallel", False) is True
    force = body.get("force", False) is True
    # figaro doctor's check of the connection: no agent's work, so the plugin's
    # window doesn't show it and a Stop doesn't refuse it
    probe = body.get("probe", False) is True
    agent = body.get("agent")
    agent = agent.strip()[:40] or None if isinstance(agent, str) else None
    try:
        opts = bridge_exec.exec_options(body)
    except ValueError as e:
        return bad(str(e))

    conn_id, info = bridge_exec._target(body)  # a Figma link works too
    if conn_id is None:
        return info
    target_ws = info["ws"]
    if opts.images and "images" not in (info.get("caps") or []):
        return bad("the plugin in this file is an older build and would drop the picture — "
                   "run `figaro reload` first")

    # Reads are safe to fan out; anything that mutates must take the file's lock.
    # So a parallel script is read-only — the plugin rolls back its changes.
    if parallel:
        updating = bridge_update.refusal()
        if updating is not None:
            return updating
        if not probe:
            held = bridge_board.refusal(_doc_key(conn_id), agent)  # the user pressed Stop for it
            if held is not None:
                return held
            bridge_board.started(_doc_key(conn_id), agent, read=True)
        return await _dispatch(target_ws, conn_id, code, timeout, force, board=not probe,
                               fields=bridge_exec.exec_fields(conn_id, agent, opts, parallel=True),
                               value_only=opts.value_only)

    # The fields are made inside the lock, where the checkpoint is decided.
    return await _queued(request, conn_id, agent, queue_timeout, lambda meta: _dispatch(
        target_ws, conn_id, code, timeout, force, meta=meta, board=not probe,
        fields=bridge_exec.exec_fields(conn_id, agent, opts), value_only=opts.value_only),
        script=not probe, read=opts.read_only)


async def _queued(request, conn_id, agent, queue_timeout, dispatch, script=True, read=False):
    """Run `dispatch(meta)` under the file's lock, waiting in its queue.

    Split out of exec_handler, so /undo and /reload wait their turn too.
    `script`: an agent's script (exec, undo), which the plugin's window shows
    and the user's Stop refuses; a /reload is none. `read`: a script that
    can't change the file, which leaves the agent's done on show.
    """
    updating = bridge_update.refusal()  # the bridge is about to restart
    if updating is not None:
        return updating
    key = _doc_key(conn_id)
    if script:
        held = bridge_board.refusal(key, agent)  # the user pressed Stop for this agent
        if held is not None:
            return held
        bridge_board.started(key, agent, read)
    lock = _lock_for(key)
    q = QUEUE.setdefault(key, {"waiting": [], "running": None})
    me = {"agent": agent, "since": time.time(), "script": script}
    q["waiting"].append(me)
    bridge_board.changed()
    try:
        got = await _acquire(lock, queue_timeout)
    finally:
        q["waiting"].remove(me)
        bridge_board.changed()
    queued_ms = int((time.time() - me["since"]) * 1000)

    if not got:
        # Busy for longer than the caller agreed to wait. Nothing was sent to
        # the plugin, so retrying later is safe — unlike after a 504.
        holder = q["running"] or {}
        who = holder.get("agent") or "another caller"
        held = int(time.time() - holder["since"]) if holder else 0
        return web.json_response({
            "ok": False,
            "error": (f"file busy: {who} has been running for {held}s and "
                      f"{len(q['waiting'])} more are waiting — gave up after "
                      f"{queue_timeout:g}s in the queue. Nothing was run; retry later "
                      f"or raise queue_timeout."),
            "busy": {"running": holder.get("agent"), "running_s": held,
                     "waiting": len(q["waiting"])},
            "queued_ms": queued_ms,
        }, status=503)

    try:
        transport = request.transport
        if transport is None or transport.is_closing():
            # The caller hung up while it waited (its own socket timeout). Running
            # the script now would apply a change nobody is waiting for — and the
            # caller, seeing a failure, may well retry it. Skip it.
            print(f"[queue] caller left after {queued_ms}ms in the queue — not running")
            return web.json_response({"ok": False, "error": "caller disconnected"},
                                     status=499)
        updating = bridge_update.refusal()  # it began while this caller waited
        if updating is not None:
            return updating
        held = bridge_board.refusal(key, agent) if script else None  # Stop while it waited
        if held is not None:
            return held
        q["running"] = {"agent": agent, "since": time.time()}
        return await dispatch({"queued_ms": queued_ms})
    finally:
        q["running"] = None
        lock.release()
        bridge_board.changed()


MAX_SECONDS = 3600  # one hour; a longer exec is a bug, not a plan


def _seconds(value, name) -> float:
    """A timeout from the request body: a number in (0, MAX_SECONDS]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{name}' must be a number of seconds")
    if not 0 < value <= MAX_SECONDS:
        raise ValueError(f"'{name}' must be between 0 and {MAX_SECONDS} seconds")
    return float(value)


async def _acquire(lock, timeout) -> bool:
    """lock.acquire() with a deadline, without ever leaking the lock.

    asyncio.wait_for(lock.acquire()) can time out in the same tick the lock is
    granted and leave it held forever on older Pythons; this cannot.
    """
    task = asyncio.ensure_future(lock.acquire())
    try:
        done, _ = await asyncio.wait({task}, timeout=max(timeout, 0))
    except asyncio.CancelledError:
        # The caller's handler was cancelled (it hung up) while queued. The inner
        # acquire would otherwise live on, take the lock later and never give it
        # back — the file would stay locked until the bridge restarts.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            lock.release()
        raise
    if done:
        return True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return False
    lock.release()  # granted while we were cancelling — hand it back
    return False


async def _send_abort(target_ws, rid):
    try:
        await target_ws.send_str(json.dumps({"id": rid, "type": "abort"}))
    except Exception:
        pass


def _abandon(rid, conn_id, timeout, target_ws):
    """Mark a request as abandoned-but-possibly-still-running.

    Nothing can kill a script already executing in the sandbox, so the next caller
    on this file is blocked (409) rather than left racing an invisible writer.
    Cancellation is requested cooperatively; scripts opt in by calling h.ck().
    """
    entry = PENDING.get(rid, {})
    if entry.get("finished"):
        # Its reply landed between the timeout firing and this call — the
        # script is over. Interlocking now would wait for a reply that already
        # came, and keep the file locked until someone POSTs /clear.
        return
    # "doc": the interlock covers the document, like the lock — the next caller
    # may be routed through another tab on the same file.
    ABANDONED[rid] = {"conn": conn_id,
                      "doc": _doc_key(conn_id),
                      "t0": entry.get("t0", time.time()),
                      "timeout": timeout,
                      "at": time.time(),
                      "agent": entry.get("agent")}
    loop = asyncio.get_running_loop()
    loop.create_task(_send_abort(target_ws, rid))
    # Still running by then, its file turns red in the windows (bridge_board.stuck).
    loop.call_later(bridge_board.STUCK_AFTER + 0.1, bridge_board.changed)


async def _dispatch(target_ws, conn_id, code, timeout, force, meta=None,
                    fields=None, mtype="exec", board=True, value_only=False) -> web.Response:
    """Send one exec to a plugin and await its reply. Caller holds the lock.

    `fields` ride along in the message (agent, readOnly, checkpoint), and
    `mtype` "undo" sends an undo the same way. `board`: False for figaro
    doctor's checks, which the plugin's window doesn't show. `value_only`
    shapes the reply only (see _reply): it never goes to the plugin.
    """
    # Per document, not per connection: a timed-out script in one tab still
    # runs in the document a caller routed through another tab would write to.
    doc = _doc_key(conn_id)
    stale = [r for r, o in ABANDONED.items() if o.get("doc", o.get("conn")) == doc]
    if stale and not force:
        info = PLUGINS.get(conn_id) or {}
        file = info.get("fileKey") or shlex.quote(info.get("name") or "<file>")
        return web.json_response({
            "ok": False,
            "error": (f"a previous script on this file timed out and may still be running "
                      f"(rid {stale[0][:8]}) — its mutations were not rolled back. Wait for "
                      f"it to finish, POST /clear to drop the interlock, or resend with "
                      f"\"force\": true."),
            "hint": (f"it may still be changing the file: `figaro wait -T {file}` returns once "
                     f"it has ended (if it doesn't, tell the user). Look at the file before you "
                     f"run anything again. Still blocked after it has ended: "
                     f"`figaro clear -T {file}`"),
            "abandoned": [r[:8] for r in stale],
        }, status=409)

    rid = str(uuid.uuid4())
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    # doc, mtype and agent: the window's Stop finds an agent's running script by them
    PENDING[rid] = {"future": fut, "logs": [], "t0": time.time(), "conn": conn_id,
                    "doc": doc, "mtype": mtype, "agent": (fields or {}).get("agent"),
                    "board": board, "value_only": value_only}

    try:
        try:
            # The timeout travels with the script so h.ck() can enforce it from
            # the plugin's own clock — see code.js: a tight loop never lets the
            # `abort` message in.
            #
            # Shielded: if the caller hangs up while this frame is being written,
            # cancelling the write midway leaves it unknown whether the plugin got
            # the script — and the lock would be released while it may be running,
            # letting the next caller in alongside it (seen on Linux, where the
            # write awaits the socket). So the send always completes, and a caller
            # cancelled here is treated like any caller that left mid-run.
            send = asyncio.ensure_future(target_ws.send_str(json.dumps(
                {"id": rid, "type": mtype, "code": code, "timeout": timeout,
                 **(fields or {})})))
            try:
                await asyncio.shield(send)
            except asyncio.CancelledError:
                try:
                    await send
                    delivered = True
                except Exception:
                    delivered = False  # the send itself failed: nothing to wait for
                if delivered:
                    _abandon(rid, conn_id, timeout, target_ws)
                raise
        except Exception as e:
            return web.json_response(
                {"ok": False, "error": f"send to plugin failed: {e}"}, status=500)

        try:
            # A moment past the deadline: the plugin ends a stuck Figma call itself
            result = await asyncio.wait_for(fut, timeout=timeout + bridge_exec.GATE_GRACE)
        except asyncio.CancelledError:
            # Client vanished mid-request (aiohttp cancels the handler). If the
            # script is still running, the file stays interlocked until it ends;
            # _abandon skips that when its reply already landed (entry
            # "finished"). Not `fut.done()`: cancelling wait_for cancels fut
            # too, so it is always done here — which released the lock while
            # the script ran on, and let the next caller in alongside it.
            _abandon(rid, conn_id, timeout, target_ws)
            raise
        except asyncio.TimeoutError:
            entry = PENDING.get(rid, {})
            _abandon(rid, conn_id, timeout, target_ws)
            bridge_board.timed_out(entry, mtype, fields, timeout)
            return web.json_response({
                "ok": False,
                "error": f"timeout after {timeout:.0f}s",
                "warning": ("the script was NOT cancelled — it may still be running and "
                            "mutating the document, and anything it already applied is "
                            "not rolled back. Re-read the nodes before trusting them."),
                "rid": rid[:8],
                "logs": list(entry.get("logs", [])),
            }, status=504)

        bridge_board.finished(PENDING[rid], mtype, fields, result)
        return _reply(PENDING[rid], result, meta)
    finally:
        # Always reap, including on client disconnect / task cancellation —
        # otherwise /status `pending` inflates permanently.
        PENDING.pop(rid, None)


def _result_text(result) -> str:
    """The human-readable `result`: sent by the plugin only when the value can't
    carry it (older plugins always send it); otherwise derived from the value,
    so the plugin ships each result once instead of twice."""
    text = result.get("text")
    if text is not None:
        return text
    return result_text.render(result.get("value"))


def _reply(entry, result, meta=None) -> web.Response:
    elapsed_ms = int((time.time() - entry["t0"]) * 1000)

    if entry.get("stopped"):
        # The user pressed Stop in the window. Whether the script noticed it in
        # time or ran to its end, its agent has to stop and ask.
        body = {"ok": False, "error": bridge_board.STOPPED, "stopped": True,
                "logs": entry["logs"], "elapsed_ms": elapsed_ms}
        status = 409
    elif result.get("type") == "error":
        error_text = result.get("text", "unknown error")
        body = {
            "ok": False,
            "error": error_text,
            "hint": find_hint(error_text),
            "stack": result.get("stack"),
            "logs": entry["logs"],
            "elapsed_ms": elapsed_ms,
        }
        status = 500
    else:
        body = {"ok": True}
        # value_only: the client makes the text from `value` (result_text.render), unless
        # the plugin sent a text of its own, which `value` can't say: that goes as it is.
        if not (entry.get("value_only") and result.get("text") is None):
            body["result"] = _result_text(result)
        body["value"] = result.get("value")
        body["logs"] = entry["logs"]
        body["elapsed_ms"] = elapsed_ms
        status = 200

    # Agents read responses, not the plugin bar — so say it here too.
    notice = _version_notice(PLUGINS.get(entry.get("conn")) or {})
    if notice:
        body["notice"] = notice
    bridge_exec.decorate(body, result, entry)  # changes, line, checkpoint, …
    if entry.get("stopped"):
        for key in ("line", "col", "source", "lib"):  # where the Stop caught it, not a bug
            body.pop(key, None)
    body.update(meta or {})
    return web.json_response(body, status=status)


def _version_notice(info):
    """One line asking for a re-Run / restart when running code is stale."""
    parts = []
    if info and plugin_outdated(info):
        # `figaro reload` swaps the code in place, no re-Run by hand.
        parts.append(f"the plugin in \"{info.get('name') or '?'}\" runs an older build "
                     f"({info.get('pluginVersion') or 'unversioned'}, current "
                     f"{expected_plugin_version()}) — update it: figaro reload "
                     f"(or re-run it in Figma: Plugins → Development → Figaro)")
    if offered():
        # Not "git pull": an agent's pull lands under other agents' running scripts
        # (with Hot reload on, Figma restarts the plugin at once). Update waits for them.
        parts.append(f"Figaro {UPDATE['latest']} is released (this is {VERSION}) — tell the user: "
                     + ("Update in the plugin's window installs it" if update_allowed()
                        else "it is updated by hand here"))
    if bridge_outdated():
        # The full path — agents call figaro from other projects' folders
        parts.append("bridge.py changed since the bridge started — restart it when no other "
                     f"agent is using it (bash {HERE / 'start-bridge.sh'}, or .\\start-bridge.ps1 -Restart)")
    return "; ".join(parts) or None


def _files_payload():
    live = _live_plugins()
    seen = {}
    out = []
    for cid, i in live:
        key = (i.get("name"), i.get("docSig"))
        entry = {"name": i.get("name"), "fileKey": i.get("fileKey"), "conn": cid[:8],
                 "doc": i.get("docSig"),
                 "plugin": i.get("pluginVersion"), "outdated": plugin_outdated(i)}
        q = QUEUE.get(_doc_key(cid)) or {}
        running = q.get("running")
        entry["queue"] = {
            "running": ({"agent": running["agent"],
                         "for_s": int(time.time() - running["since"])} if running else None),
            "waiting": [w["agent"] or "?" for w in q.get("waiting", [])],
        }
        late = bridge_board.stuck(_doc_key(cid))  # Figma isn't answering there
        if late:
            entry["stuck"] = {"agent": late["agent"], "for_s": int(time.time() - late["since"])}
        if i.get("docSig") is not None and key in seen:
            # Another tab/window on the same document. Harmless — flagged so the
            # count in `files` is not read as "two different files".
            entry["sameDocAs"] = seen[key]
        else:
            seen[key] = cid[:8]
        out.append(entry)
    return out


async def status_handler(request: web.Request) -> web.Response:
    files = _files_payload()
    return web.json_response({
        "version": VERSION,
        "plugin_connected": len(files) > 0,
        "plugin_version": expected_plugin_version(),
        "bridge_outdated": bridge_outdated(),
        "update": UPDATE,
        "skipped": SKIPPED,
        "update_button": update_allowed(),
        "sounds": bridge_settings.on("sounds"),
        "settings": bridge_settings.current(),
        "files": files,
        "pending": len(PENDING),
        "abandoned": [
            {"rid": r[:8], "conn": o["conn"][:8], "age_s": int(time.time() - o["t0"]),
             "agent": o.get("agent")}
            for r, o in ABANDONED.items()
        ],
        # files whose window closed while a script ran there (bridge_board.lost)
        "lost": [{"name": l["name"], "fileKey": l.get("fileKey"), "doc": l.get("sig"),
                  "agent": l["agent"], "ago_s": int(time.time() - l["at"])}
                 for _, l in bridge_board.lost_files({_doc_key(c) for c, _ in _live_plugins()})],
    })


async def clear_handler(request: web.Request) -> web.Response:
    """Drop the abandoned-script interlock for one file, after a 504."""
    body, error = await bridge_exec._body(request)
    if error:
        return error
    conn_id, info = bridge_exec._target(body)
    if conn_id is None:
        return info

    doc = _doc_key(conn_id)
    dropped = [r for r, o in list(ABANDONED.items()) if o.get("doc", o.get("conn")) == doc]
    for r in dropped:
        ABANDONED.pop(r, None)
    if dropped:
        bridge_board.changed()
    return web.json_response({"ok": True, "cleared": [r[:8] for r in dropped],
                              "file": info.get("name")})


async def targets_handler(request: web.Request) -> web.Response:
    return web.json_response({"files": _files_payload()})


async def root_handler(request: web.Request) -> web.Response:
    return web.json_response({
        "service": "figaro-bridge",
        "version": VERSION,
        "endpoints": {
            "POST /exec": ("{code, target?, agent?, timeout?, queue_timeout?, parallel?, force?, "
                           "read_only?, checkpoint?, quick?, libs?, images?, probe?, value_only?} -> "
                           "{ok, result, value, logs, elapsed_ms}; value_only:true leaves `result` "
                           "out when it only repeats `value` as text. Serialized per file "
                           "unless parallel:true (reads only)."),
            "GET /status": "{plugin_connected, files, pending, abandoned, lost}",
            "GET /targets": "{files: [{name, fileKey, conn}]}",
            "POST /clear": "{target} -> drop a file's abandoned-script interlock after a 504",
            "POST /undo": "{target, agent?, force?} -> revert the file's last script",
            "POST /reload": "{target} -> run plugin/code.js from disk in the open plugin",
            "POST /done": ("{target, agent?, text?} -> the agent is done in the file: the plugin's "
                           "window says so, with the note, until its next script there that "
                           "can change the file"),
            "POST /forget": ("{target?, agents: [name]} -> those agents leave the file's board in "
                             "every window, with their lines in Recent; one with a script "
                             "running stays"),
            "POST /comments": ("{target?, agent?} -> the file's open Figma comments, each with where "
                               "it is pinned; needs the designer's Figma token (403 `needs_token`)"),
            "POST /comment": ("{target?, agent?, message, node, at?} | {…, message, reply: id} | "
                              "{…, react: id, emoji?} | {…, delete: id} -> a new comment on a layer, a "
                              "reply, a reaction, or deleting one Figaro posted; with the designer's "
                              "Figma token"),
            "WS /plugin": "Figma plugin connects here (one per open file)",
        },
    })


def build_app() -> web.Application:
    load_skipped()
    bridge_settings.load()
    bridge_comments.load()
    bridge_review.load()
    app = web.Application(client_max_size=64 * 1024 * 1024,  # 16 → 64 MB, like the WebSocket
                          middlewares=[guard])
    app.router.add_get("/", root_handler)
    app.router.add_get("/status", status_handler)
    app.router.add_get("/targets", targets_handler)
    app.router.add_post("/exec", exec_handler)
    app.router.add_post("/clear", clear_handler)
    app.router.add_get("/plugin", plugin_ws_handler)
    bridge_exec.install(app)  # /undo, /reload
    bridge_board.install(app)  # /done, /forget, /notes
    bridge_comments.install(app)  # /comments
    return app


class TokenNotSaved(Exception):
    """The token file can't be written: main() says where and exits."""


def _write_token(_text=""):
    """Save TOKEN for the CLI. web.run_app calls this once the port is bound.

    Only then: a second bridge started by mistake dies on the busy port, and
    must not replace the token of the one that runs. Mode 0600 where the OS
    has modes; on Windows the file sits in the user's own profile.
    """
    path = figaro_token.token_path(PORT)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if os.name != "nt":
                os.fchmod(fd, 0o600)  # a file that was there keeps its mode: fix it before writing
            f.write(TOKEN)
    except OSError as e:
        raise TokenNotSaved(f"{path}: {e.strerror or e}") from None


def main():
    global ALLOWED_HOSTS, TOKEN, PORT

    # File names are routinely Cyrillic, CJK or emoji. With stdout redirected to
    # a log on Windows, Python falls back to the ANSI code page and a `hello`
    # print would raise UnicodeEncodeError inside the plugin handler.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Figaro bridge server")
    ap.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8788, help="bind port (default 8788)")
    # Start-bridge.sh passes 3 h, and the CLI starts the bridge again when needed.
    ap.add_argument("--idle-exit", type=bridge_idle.duration, default=0, metavar="TIME",
                    help="exit after this long with no plugin connected and no requests "
                         "(seconds, or 30m / 3h; 0 = never)")
    args = ap.parse_args()
    TOKEN = secrets.token_urlsafe(32)
    PORT = args.port
    # Update or Reload in the window starts this same command again
    bridge_update.RESTART.update(argv=sys.argv[1:], port=args.port)

    if args.host in ("127.0.0.1", "localhost", "::1"):
        ALLOWED_HOSTS = {
            f"localhost:{args.port}",
            f"127.0.0.1:{args.port}",
            f"[::1]:{args.port}",
        }
    else:
        # Binding beyond loopback is a deliberate choice, and the reachable
        # hostnames are unknowable from here — skip the Host check and say so.
        print(f"[bridge] WARNING: bound to {args.host} — Host check disabled, "
              f"anyone who can reach this port can run code in your Figma file")

    hosts = _bind_hosts(args.host, args.port)
    print(f"[bridge] listening on http://{args.host}:{args.port}"
          + (" (and [::1], so 'localhost' is fast)" if "::1" in hosts else ""))
    print(f"[bridge] plugin should connect to ws://localhost:{args.port}/plugin")
    print(f"[bridge] try: curl -X POST http://localhost:{args.port}/exec "
          f"-H 'Content-Type: application/json' "
          f"-H \"X-Figaro-Token: $(cat {figaro_token.token_path(args.port)})\" "
          f"-d '{{\"code\":\"return figma.currentPage.name\"}}'")

    loop = asyncio.new_event_loop()
    loop.set_exception_handler(_quiet_connection_resets)
    app = build_app()
    bridge_idle.install(app, args.idle_exit)
    if update_checks_on():
        app.on_startup.append(_start_update_checks)
        app.on_cleanup.append(_stop_update_checks)
    try:
        web.run_app(app, host=hosts, port=args.port, print=_write_token, loop=loop)
    except TokenNotSaved as e:
        # Without the file no command could use this bridge: better not to hold the port.
        sys.exit(f"[bridge] can't save the token to {e}. Set FIGARO_TOKEN_FILE to a file "
                 "you can write, for the bridge and the figaro command both, and start it again")


def _bind_hosts(host, port):
    """Where to listen. For the default loopback, both 127.0.0.1 and ::1.

    `localhost` resolves to ::1 first on Windows (and on many Linux setups).
    With only 127.0.0.1 bound, every client that says `localhost` — the CLI,
    curl, an agent's own script — waits ~2 s for the IPv6 attempt to be refused
    before falling back, on EVERY request. Listening on ::1 too removes that.
    Skipped quietly where IPv6 is unavailable.

    The check binds the way the server will: with SO_REUSEADDR off Windows, as
    asyncio does. Without it, after a restart in place (Update or Reload) the
    plugins' just-closed sockets, waiting in TIME_WAIT, made the check fail.
    """
    if host != "127.0.0.1":
        return [host]
    import socket
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            if os.name != "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("::1", port))
    except OSError:
        return [host]
    return [host, "::1"]


def _quiet_connection_resets(loop, context):
    """Drop the Proactor traceback Windows logs when a Figma tab closes its socket.

    asyncio's Proactor loop reports WinError 10054 from inside its own transport
    callback, after aiohttp has already handled the disconnect — so it is pure
    noise, and it filled bridge.err.log with tracebacks on every closed tab.
    """
    if isinstance(context.get("exception"), ConnectionResetError):
        return
    loop.default_exception_handler(context)


if __name__ == "__main__":
    main()
