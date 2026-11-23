"""The bridge's side of exec: change reports, read-only, checkpoints, undo.

    POST /exec    {..., read_only?, checkpoint?, quick?, libs?, images?, value_only?, stream?}
                  -> {..., changes?, line?, source?, lib?, lib_missing?, rolled_back?, checkpoint?}
                  stream: once the script prints, NDJSON — {"logs": [...]} lines, then the
                  reply with its "status" (bridge.Live)
    POST /undo    {target?, agent?, force?}      -> revert the file's last script
    POST /reload  {target?}                      -> run plugin/code.js from disk in
                                                    the open plugin, no re-Run needed

The plugin does the work (plugin/code.js): it reports what each
script changed, rolls back a read-only script that changed something, makes
each script one Cmd+Z step and saves checkpoints into the file's version
history. This module decides when a checkpoint is due, sends an agent's
library only to a plugin that has not got it yet, and carries the fields.
A target may be a Figma link (figma_links).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import NamedTuple

from aiohttp import web

import bridge
import bridge_settings
import figma_links

# A checkpoint — a named version in the file's history — before the first
# writing script in a file, then at most once an hour. Everyone with access to
# the file sees its title in File → Version history. "Save versions as agents
# work" off in the window's settings stops these; a label still saves one.
CHECKPOINT_EVERY = 60 * 60
STATE_DIR = Path(os.environ.get("FIGARO_STATE_DIR") or Path.home() / ".cache" / "figaro")
CHECKPOINTS_FILE = STATE_DIR / "checkpoints.json"
CHECKPOINTS: dict = {}  # document id -> unix time of the last checkpoint we made there
_loaded = False

RELOAD_WAIT = 15.0  # seconds for the reloaded plugin to connect again


# ─── /exec ────────────────────────────────────────────────────────────────

class Options(NamedTuple):
    read_only: bool
    checkpoint: str | bool | None  # a label forces one, False skips it, None: hourly rule
    quick: bool                    # the CLI's own readers: no change report, undo step or code reading
    libs: list                     # [{"name", "code"}] — the agent's --lib files
    images: tuple = ()             # [{"name", "data"}] — files for `figaro image`, `exec --image`
    value_only: bool = False       # no `result` in the reply when `value` says it: the client
                                   # renders the text (result_text.render); the plugin never sees this
    stream: bool = False           # print() lines go to the caller as they come (bridge.Live);
                                   # the plugin never sees this either


IMAGES_MAX = 20            # files in one request
IMAGES_SIZE = 40 * 1024 * 1024  # base64 characters together: ~30 MB of files, well inside the
                                # 64 MB the bridge's body and WebSocket take
LIBS_MAX = 50              # per plugin connection; plugin/code.js keeps as many
LIBS_SIZE = 4 * 1024 * 1024


def exec_options(body):
    """Options from an /exec body; ValueError if malformed.

    A quick script is read-only too, but the plugin does not watch what it
    changes — it is for code that cannot change anything (`inspect`, `shot`, …).
    """
    read_only = body.get("read_only", body.get("readOnly", False))
    if not isinstance(read_only, bool):
        raise ValueError("'read_only' must be true or false")
    checkpoint = body.get("checkpoint")
    if checkpoint is True or not isinstance(checkpoint, (str, bool, type(None))):
        raise ValueError("'checkpoint' must be a label (string) or false")
    if isinstance(checkpoint, str):
        checkpoint = checkpoint.strip()[:100] or None
    quick = body.get("quick", False)
    if not isinstance(quick, bool):
        raise ValueError("'quick' must be true or false")
    value_only = body.get("value_only", False)
    if not isinstance(value_only, bool):
        raise ValueError("'value_only' must be true or false")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise ValueError("'stream' must be true or false")
    return Options(read_only or quick, checkpoint, quick, _libs(body.get("libs")),
                   _images(body.get("images")), value_only, stream)


def _libs(value):
    if value is None:
        return []
    if not isinstance(value, list) or not all(
            isinstance(x, dict) and isinstance(x.get("code"), str) for x in value):
        raise ValueError("'libs' must be a list of {name, code}")
    if sum(len(x["code"]) for x in value) > LIBS_SIZE:
        raise ValueError(f"'libs' are over {LIBS_SIZE // (1024 * 1024)} MB together")
    return [{"name": str(x.get("name") or f"lib{i + 1}")[:80], "code": x["code"]}
            for i, x in enumerate(value)]


def _images(value):
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
            isinstance(x, dict) and isinstance(x.get("name"), str) and x["name"]
            and isinstance(x.get("data"), str) for x in value):
        raise ValueError("'images' must be a list of {name, data}: base64 of a file")
    if len(value) > IMAGES_MAX:
        raise ValueError(f"'images' has more than {IMAGES_MAX} files")
    if sum(len(x["data"]) for x in value) > IMAGES_SIZE:
        raise ValueError(f"'images' are over {IMAGES_SIZE * 3 // 4 // (1024 * 1024)} MB together")
    return tuple({"name": x["name"][:80], "data": x["data"]} for x in value)


def exec_fields(conn_id, agent, opts, parallel=False):
    """The extra fields of the exec message. Called when the script is about to be
    sent — inside the file's lock — so queued writers don't all decide that a
    checkpoint is due. A parallel script is read-only."""
    read_only = opts.read_only or parallel
    fields = {"readOnly": read_only}
    if opts.quick:
        fields["quick"] = True
    if agent:
        fields["agent"] = agent
    title = None if opts.quick else _checkpoint_title(conn_id, agent, read_only, opts.checkpoint)
    if title:
        fields["checkpoint"] = title
    if opts.libs:
        fields["libs"] = _lib_fields(conn_id, opts.libs)
    if opts.images:
        fields["images"] = list(opts.images)
    return fields


def _lib_fields(conn_id, libs):
    """[{hash, name, code?}]: a library's code goes to a plugin only until it
    has it. The plugin keeps the last LIBS_MAX in the same order, so both sides
    forget the same one; if they ever disagree, it asks for a retry
    (`libMissing`, see decorate)."""
    info = bridge.PLUGINS.get(conn_id)
    sent = info.setdefault("sent_libs", OrderedDict()) if info is not None else OrderedDict()
    out = []
    for lib in libs:
        digest = hashlib.sha256(lib["code"].encode("utf-8")).hexdigest()[:16]
        item = {"hash": digest, "name": lib["name"]}
        if digest in sent:
            sent.move_to_end(digest)
        else:
            item["code"] = lib["code"]
            sent[digest] = True
            while len(sent) > LIBS_MAX:
                sent.popitem(last=False)
        out.append(item)
    return out


# The plugin ends a script stuck in a Figma call at the script's own deadline
# (makeGate in plugin/code.js) and replies ~0.2 s after it. Waiting that much
# longer gets the caller its error — which call hung — instead of a 504, and
# the file is not interlocked.
GATE_GRACE = 1.0


def resolve_target(target):
    """bridge.resolve_target, for a Figma link too. When the file is not
    connected, the reason says how to connect it."""
    link = figma_links.parse(target)
    conn_id, info = bridge.resolve_target(link.key if link else target)
    if conn_id is None and target:
        hint = figma_links.open_hint(target)
        if hint:
            info = f"{info} — {hint}"
    return conn_id, info


def _checkpoint_title(conn_id, agent, read_only, requested):
    if requested is False:
        return None
    doc = _doc(conn_id)
    if isinstance(requested, str):
        _remember(doc, time.time())
        return f"Figaro · {requested}"
    if read_only or not bridge_settings.on("versions"):
        return None
    last = _checkpoints().get(doc)
    if last is not None and time.time() - last < CHECKPOINT_EVERY:
        return None
    _remember(doc, time.time())
    return f"Figaro · before changes by {agent or 'an agent'}"


def _doc(conn_id):
    """The document a checkpoint belongs to: its file key when the plugin knows
    it (that id outlives the plugin run), else the bridge's document id."""
    info = bridge.PLUGINS.get(conn_id) or {}
    return f"file:{info['fileKey']}" if info.get("fileKey") else bridge._doc_key(conn_id)


def _checkpoints():
    global _loaded
    if not _loaded:
        _loaded = True
        try:
            CHECKPOINTS.update(json.loads(CHECKPOINTS_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return CHECKPOINTS


def _remember(doc, when):
    """Record a checkpoint at once — the next queued writer must see it — and
    keep the file-key ones across bridge restarts. `when=None` forgets it, so
    the next writer tries again."""
    checkpoints = _checkpoints()
    if when is None:
        checkpoints.pop(doc, None)
    else:
        checkpoints[doc] = when
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CHECKPOINTS_FILE.write_text(
            json.dumps({k: v for k, v in checkpoints.items() if k.startswith("file:")}),
            encoding="utf-8")
    except OSError:
        pass


def decorate(body, result, entry):
    """Copy what the plugin reported about the script into the HTTP reply."""
    for key in ("changes", "line", "col", "source", "lib", "checkpoint"):
        if result.get(key) is not None:
            body[key] = result[key]
    if result.get("rolledBack"):
        body["rolled_back"] = True
    notice = result.get("notice")
    if notice:
        body["notice"] = f"{body['notice']}; {notice}" if body.get("notice") else notice
    info = bridge.PLUGINS.get(entry.get("conn")) or {}
    checkpoint = result.get("checkpoint")
    if isinstance(checkpoint, dict) and checkpoint.get("error") and entry.get("conn"):
        _remember(_doc(entry["conn"]), None)
    for digest in result.get("libMissing") or []:
        (info.get("sent_libs") or {}).pop(digest, None)
        body["lib_missing"] = True  # the script did not run: a retry sends the code and is safe
    _link_items(body.get("changes"), info)
    _shot_text(body)


def _link_items(changes, info):
    """A link to each new or changed layer, for reports to people."""
    if not isinstance(changes, dict) or not info.get("fileKey"):
        return
    top = [dict(t, op="+") for t in changes.get("top") or [] if isinstance(t, dict)]
    if top:
        changes["top"] = top
    for item in (changes.get("items") or []) + top:
        if (isinstance(item, dict) and item.get("op") in ("+", "~")
                and item.get("type") not in ("STYLE", "DOCUMENT")):
            url = figma_links.node_url(info["fileKey"], info.get("name"), item.get("id"))
            if url:
                item["url"] = url


def _shot_text(body):
    """`result` is the value as text; for a picture that would be the whole
    base64 a second time."""
    value = body.get("value")
    shots = value if isinstance(value, list) else [value]
    if shots and all(isinstance(s, dict) and s.get("kind") == "shot" for s in shots):
        body["result"] = "; ".join(
            f"shot {s.get('id')} {s.get('format')} {s.get('size')} B" for s in shots)


# ─── /undo ────────────────────────────────────────────────────────────────

async def undo_handler(request: web.Request) -> web.Response:
    body, error = await _body(request)
    if error:
        return error
    agent = body.get("agent")
    agent = agent.strip()[:40] or None if isinstance(agent, str) else None
    force = body.get("force", False) is True

    conn_id, info = _target(body)
    if conn_id is None:
        return info
    ws = info["ws"]
    fields = {"agent": agent, "force": force}
    return await bridge._queued(
        request, conn_id, agent, 30.0,
        lambda meta: bridge._dispatch(ws, conn_id, "", 30.0, False, meta=meta,
                                      fields=fields, mtype="undo"))


# ─── /reload ──────────────────────────────────────────────────────────────

async def reload_handler(request: web.Request) -> web.Response:
    """Run plugin/code.js and ui.html from disk inside the open plugin.

    The new code replaces the old one as a re-Run would: its window takes the
    old one's place and connects again.
    """
    body, error = await _body(request)
    if error:
        return error
    conn_id, info = _target(body)
    if conn_id is None:
        return info
    try:
        message = reload_message()
    except OSError as e:
        return web.json_response({"ok": False, "error": f"cannot read plugin/: {e}"}, status=500)

    async def run(meta):
        t0 = time.time()
        before = set(bridge.PLUGINS)
        reply = await _send(info, conn_id, message, RELOAD_WAIT)
        if conn_id in bridge.PLUGINS:
            # The window was not replaced: the old code answered or went quiet.
            why = reply.get("text") if reply.get("type") == "error" else "it kept the old code"
            return web.json_response({
                "ok": False,
                "error": (f"the plugin in \"{info.get('name')}\" did not reload: {why} — re-run "
                          f"it in Figma: Plugins → Development → Figaro"),
            }, status=504 if reply.get("timeout") else 500)
        expected = bridge.expected_plugin_version()
        new = await _reconnected(info, before, expected, time.time() + RELOAD_WAIT)
        if new is None:
            return web.json_response({
                "ok": False,
                "error": (f"the plugin in \"{info.get('name')}\" did not come back with build "
                          f"{expected} — re-run it in Figma: Plugins → Development → Figaro"),
            }, status=504)
        return web.json_response({"ok": True, "file": new.get("name"), "plugin": expected,
                                  "elapsed_ms": int((time.time() - t0) * 1000), **meta})

    return await bridge._queued(request, conn_id, None, 30.0, run, script=False)


def reload_message():
    """plugin/code.js and ui.html from disk, for a running plugin to run in its
    own place. OSError when they can't be read."""
    return {"type": "reload",
            "code": bridge.PLUGIN_CODE.read_text(encoding="utf-8"),
            "html": (bridge.PLUGIN_CODE.parent / "ui.html").read_text(encoding="utf-8")}


async def _send(info, conn_id, message, timeout):
    """Send one message to a plugin and wait for its reply, or for its socket
    to close (bridge.plugin_ws_handler fails in-flight requests then)."""
    rid = str(uuid.uuid4())
    fut = asyncio.get_running_loop().create_future()
    bridge.PENDING[rid] = {"future": fut, "logs": [], "t0": time.time(), "conn": conn_id}
    try:
        await info["ws"].send_str(json.dumps({"id": rid, **message}))
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        return {"type": "error", "text": f"no answer in {timeout:g}s", "timeout": True}
    except Exception as e:  # the socket went away while sending
        return {"type": "error", "text": f"plugin disconnected: {e}"}
    finally:
        bridge.PENDING.pop(rid, None)


async def _reconnected(old, before, expected, deadline):
    """The window that took `old`'s place: a connection that came after the
    reload, for the same file, running the build on disk."""
    while time.time() < deadline:
        for cid, i in bridge._live_plugins():
            if (cid not in before and i.get("hello") and _same_file(old, i)
                    and i.get("pluginVersion") == expected):
                return i
        await asyncio.sleep(0.1)
    return None


def _same_file(old, new):
    if old.get("fileKey"):
        return new.get("fileKey") == old["fileKey"]
    # Without a key the document id is per run, so the name has to do.
    return bool(old.get("name")) and new.get("name") == old.get("name")


# ─── shared ───────────────────────────────────────────────────────────────

async def _body(request):
    try:
        body = await request.json()
    except (ValueError, LookupError, RecursionError):  # bad JSON or bytes, an unknown charset
        body = {}
    if not isinstance(body, dict):
        return None, web.json_response({"ok": False, "error": "body must be a JSON object"},
                                       status=400)
    if body.get("target") is not None and not isinstance(body.get("target"), str):
        return None, web.json_response(
            {"ok": False, "error": "'target' must be a string: a file name or doc id"}, status=400)
    return body, None


def _target(body):
    conn_id, info = resolve_target(body.get("target"))
    if conn_id is None:
        status = 503 if not bridge._live_plugins() else 409
        return None, web.json_response({"ok": False, "error": info}, status=status)
    return conn_id, info


def install(app: web.Application):
    app.router.add_post("/undo", undo_handler)
    app.router.add_post("/reload", reload_handler)
