"""The Reviewer: a built-in agent that checks what other agents changed.

It is the last row of every file's Agents in the window. When an agent runs
`figaro done`, the bridge checks the layers that agent changed since its last
check with `h.audit` (a read-only script in the file's queue, no agent's work)
and shows the notes to the designer, not to the agent. The designer ticks the
notes the agent should fix and sends them; the agent waits for that answer
with `figaro notes --wait`, up to EXPIRE seconds.

    POST /notes   {target?, agent?, wait?}  -> the designer's answer, once

The file's switch is kept per file key in `reviewer.json` next to the token
({"<fileKey>": true}); off in every file until switched. The global switch
(bridge_settings `reviewer`) off: no row, no check, no `reviewer` on the board.

From the window: `reviewer` {doc, on}, `review-answer` {doc, send: [note ids]}
and the Stop of the row (`stop` with agent "reviewer"; bridge_board.handle).

A note the designer marks as on purpose (`review-intentional` {doc, note, at, layers?}) leaves the
notes, and its rule is not raised again on its layers in that file; with `layers`, a list of the
note's layer ids, only those layers are marked and the note keeps the rest (an older window sends
none: the whole note). The marks are kept per file key in `reviewer-intentional.json`
({"<fileKey>": {"<rule>": [layer ids]}}), and `review-unmark` {doc} clears the file's. The board's
`intentional` counts the file's marked layers, once each; the agent's answer lists what was marked
in its round (`marked`).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time

from aiohttp import web

import bridge
import bridge_board
import bridge_exec
import bridge_settings
import figaro_token
import figma_links

AGENT = "reviewer"       # the agent's name on the board, and the check's own
EXPIRE = 600             # the agent stops waiting for the designer's answer after ten minutes
HOLD = 55                # seconds one /notes call holds: under the CLI's own socket limit
CHECK_TIMEOUT = 15.0     # the check's script
CHECK_QUEUE = 30.0       # how long it waits in the file's queue
CHECK_MS = 10000         # time h.audit gets for all the layers together
ROOTS_MAX = 25           # top-most layers audited in one check
TRACK_MAX = 500          # changed layers remembered per agent
NOTES_MAX = 30           # notes kept from one check
LAYERS_MAX = 5           # layers named per note
MARKS_MAX = 2000         # layers marked on purpose kept per file; the oldest go first

# One per h.audit rule, in its order of importance.
TITLES = {
    "missing-font": "Font is missing",
    "text-clipped": "Text is cut off",
    "content-clipped": "Content is cut off",
    "zero-size": "Layer has no size",
    "low-contrast": "Text contrast is too low",
    "low-contrast-ui": "Icon or control contrast is too low",
    "placeholder-text": "Placeholder text left in",
    "small-target": "Click target is too small",
    "no-auto-layout": "Children placed by hand",
    "empty-frame": "Empty frame",
    "detached-instance": "Detached instance",
    "unbound-color": "Colour isn't a token",
    "no-text-style": "Text has no style",
    "default-name": "Layer has a default name",
    "uneven-siblings": "Siblings are uneven",
    "heading-order": "Heading levels skip",
    "absolute-in-layout": "Layer ignores auto-layout",
}
RULES = list(TITLES)

ON: dict = {}        # file key (else doc key) -> the file's switch
STATE: dict = {}     # doc key -> the Reviewer's row: see _row
TRACK: dict = {}     # doc key -> {agent: {layer id: None}}: changed since its last check
ASKING: dict = {}    # doc key -> notes waiting for the designer: {agent, notes, at, expired, layers}
ANSWERS: dict = {}   # doc key -> {agent: the designer's answer, until the agent reads it}
LOCKS: dict = {}     # doc key -> a check at a time per file
MARKED: dict = {}    # file key (else doc key) -> {rule: {layer id: None}}: on purpose, not raised again


def reset():
    for registry in (ON, STATE, TRACK, ASKING, ANSWERS, LOCKS, MARKED):
        registry.clear()


# ─── the file's switch ────────────────────────────────────────────────────

def _path():
    return figaro_token.token_path(bridge.PORT).parent / "reviewer.json"


def _marks_path():
    return _path().with_name("reviewer-intentional.json")


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load():
    ON.clear()
    ON.update({k: v for k, v in _read(_path()).items() if isinstance(k, str) and isinstance(v, bool)})
    MARKED.clear()
    for key, rules in _read(_marks_path()).items():
        if not (isinstance(key, str) and isinstance(rules, dict)):
            continue
        kept = {r: dict.fromkeys(i for i in ids if isinstance(i, str))
                for r, ids in rules.items() if isinstance(r, str) and isinstance(ids, list)}
        kept = {r: ids for r, ids in kept.items() if ids}
        if kept:
            MARKED[key] = kept


def _write(path, data, what):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")  # a write cut short leaves the old file whole
        part.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(part, path)
    except OSError as e:
        print(f"[reviewer] can't keep {what} in {path} ({e}): until the bridge restarts")


def _save():
    _write(_path(), {k: v for k, v in ON.items() if v}, "the switches")


def _save_marks():
    _write(_marks_path(), {k: {r: list(ids) for r, ids in rules.items()} for k, rules in MARKED.items()},
           "the notes marked on purpose")


def _info(doc):
    for cid, info in bridge._live_plugins():
        if bridge._doc_key(cid) == doc:
            return info
    return {}


def _key(doc, info=None):
    """What the file's switch is kept under: its file key, which outlives the plugin run."""
    info = info if info is not None else _info(doc)
    return info.get("fileKey") or doc


def enabled(doc, info=None):
    """The Reviewer is on for this file: the global switch and the file's."""
    return bridge_settings.on("reviewer") and ON.get(_key(doc, info), False)


def switch(doc, on):
    if not isinstance(on, bool):
        return False
    ON[_key(doc)] = on
    _save()
    if not on:
        _withdraw(doc)
        STATE.pop(doc, None)
    print(f"[reviewer] turned {'on' if on else 'off'} in {doc}")
    bridge_board.changed()
    return True


# ─── what agents changed ──────────────────────────────────────────────────

def track(doc, agent, changes):
    """A script of `agent` changed layers: remember them for the next check."""
    if not isinstance(changes, dict):
        return
    ids = []
    for t in changes.get("top") or []:
        if isinstance(t, dict) and isinstance(t.get("id"), str):
            ids.append(t["id"])
    for i in changes.get("items") or []:
        if (isinstance(i, dict) and i.get("op") in ("+", "~") and isinstance(i.get("id"), str)
                and i.get("type") not in ("STYLE", "DOCUMENT")):
            ids.append(i["id"])
    if not ids:
        return
    mine = TRACK.setdefault(doc, {}).setdefault(agent, {})
    for i in ids:
        mine.pop(i, None)  # the newest at the end
        mine[i] = None
    while len(mine) > TRACK_MAX:
        mine.pop(next(iter(mine)))


# ─── the board ────────────────────────────────────────────────────────────

def _row(doc):
    return STATE.setdefault(doc, {"state": "waiting", "agent": None, "layers": 0, "since": None,
                                  "at": time.time(), "sent": 0, "left": 0})


def _set(doc, state, **fields):
    row = _row(doc)
    row.update(state=state, at=time.time(), since=None, **fields)
    return row


def board_part(doc, info):
    """The file's `reviewer` for the board; None with the global switch off, so a
    window shows no row."""
    if not bridge_settings.on("reviewer"):
        return None
    on = ON.get(_key(doc, info), False)
    out = {"on": on, "state": "off", "agent": None, "layers": 0, "since": None, "at": None,
           "notes": [], "expired": False, "sent": 0, "left": 0,
           "intentional": len({i for ids in MARKED.get(_key(doc, info), {}).values() for i in ids})}
    if not on:
        return out
    row = STATE.get(doc)
    if row is None:
        return dict(out, state="waiting")
    out.update(state=row["state"], agent=row["agent"], layers=row["layers"], since=row["since"],
               at=row["at"], sent=row["sent"], left=row["left"])
    asking = ASKING.get(doc)
    if row["state"] == "notes" and asking:
        out["notes"] = asking["notes"]
        out["expired"] = asking["expired"]
    return out


def _recent(doc, event):
    bridge_board._add(bridge_board._file(doc), dict(event, at=time.time(), kind="ok"))


# ─── the check ────────────────────────────────────────────────────────────

def _script(ids):
    return _SCRIPT.replace("__IDS__", json.dumps(ids)).replace("__MS__", str(CHECK_MS)) \
                  .replace("__ROOTS__", str(ROOTS_MAX))


# The top-most changed layers that still exist, audited; each note carries the
# first layers it names, with their names and types.
_SCRIPT = """
const ids = __IDS__;
const want = new Set(ids);
const nodes = [];
for (const id of ids) {
  h.ck();
  let n = null;
  try { n = await h.node(id); } catch (e) { n = null; }
  let gone = !n;
  try { if (n && (n.removed || n.type === "DOCUMENT" || n.type === "PAGE")) gone = true; } catch (e) { gone = true; }
  if (!gone) nodes.push(n);
}
const top = nodes.filter((n) => {
  let p = null;
  try { p = n.parent; } catch (e) { p = null; }
  while (p) {
    if (want.has(p.id)) return false;
    try { p = p.parent; } catch (e) { p = null; }
  }
  return true;
});
const roots = top.slice(0, __ROOTS__);
const per = Math.max(1500, Math.floor(__MS__ / Math.max(1, roots.length)));
const notes = [], info = {};
let partial = null, errors = 0;
for (const r of roots) {
  h.ck();
  let res = null;
  try { res = await h.audit(r, { maxMs: per, links: false }); } catch (e) { errors++; continue; }
  if (res.partial) partial = res.partial;
  errors += res.errors || 0;
  for (const n of res.notes) {
    const all = n.ids || [n.id];
    notes.push({ rule: n.rule, text: n.text, count: n.count || 1, ids: all });
    for (const id of all.slice(0, 5)) {
      if (info[id]) continue;
      try { const x = await h.node(id); info[id] = { name: x.name, type: x.type }; } catch (e) { /* gone */ }
    }
  }
}
return { layers: nodes.length, roots: roots.length, skipped: top.length - roots.length,
         notes, info, partial, errors };
"""


DETAILS_MAX = 10       # original lines kept per note, for the agent
VALUES_MAX = 6         # distinct colours a summary lists
_PAINT = re.compile(r"\b(fill|stroke) (#[0-9A-Fa-f]{3,8})\b")
_RATIO = re.compile(r"contrast (\d+(?:\.\d+)?):1")
_UNDER = re.compile(r"\bunder (\d+ × \d+)\b")


def _list(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _lowest(texts):
    """", as low as 1.9:1" from the ratios the lines name; nothing when they name none."""
    ratios = [float(m.group(1)) for t in texts for m in [_RATIO.search(t)] if m]
    return f", as low as {min(ratios):g}:1" if ratios else ""


def _button_size(texts):
    sizes = {m.group(1) for t in texts for m in [_UNDER.search(t)] if m}
    return f"under {next(iter(sizes))} px" if len(sizes) == 1 else "too small to hit"


# One line per rule for when its layers' lines differ; every value in it is a count or a
# single token the lines name, never a piece cut out of them. n: layers.
SUMMARIES = {
    "missing-font": lambda n, t: f"{n} texts use fonts that are not installed here, so they show "
                                 f"in stand-ins; switch them to installed fonts",
    "text-clipped": lambda n, t: f"{n} texts are cut off or run out of their boxes; make room for "
                                 f"the text or turn the clipping off",
    "content-clipped": lambda n, t: f"{n} layers are cut off by frames that clip their content; "
                                    f"move them inside, or turn the clipping off",
    "zero-size": lambda n, t: f"{n} layers have no size or no visible text, so nothing shows; "
                              f"give them a size or delete them",
    "low-contrast": lambda n, t: f"contrast is too low on {n} texts{_lowest(t)}; use stronger colours",
    "low-contrast-ui": lambda n, t: f"icon contrast is too low on {n} layers{_lowest(t)}; "
                                    f"use stronger colours",
    "placeholder-text": lambda n, t: f"{n} texts look like placeholders; write the real text, or ask",
    "small-target": lambda n, t: f"{n} layers look like buttons but are {_button_size(t)}; "
                                 f"make them at least that big",
    "no-auto-layout": lambda n, t: f"{n} frames have children placed by hand with no auto-layout; "
                                   f"add auto-layout so spacing follows the content",
    "empty-frame": lambda n, t: f"{n} frames are empty and still have Figma's default names; "
                                f"delete them or fill them",
    "detached-instance": lambda n, t: f"{n} instances are detached: fine as one-off states, "
                                      f"otherwise use the component again",
    "no-text-style": lambda n, t: f"{n} texts have no text style or typography variables; "
                                  f"apply the file's text styles",
    "default-name": lambda n, t: f"{n} layers have Figma's default names; name them for what "
                                 f"they are",
    "uneven-siblings": lambda n, t: f"{n} layers differ in size from their same-named siblings; "
                                    f"make them equal",
    "heading-order": lambda n, t: f"heading levels skip in {n} places; do not skip a level",
    "absolute-in-layout": lambda n, t: f"{n} layers are positioned by hand inside auto-layout; "
                                       f"fine for a badge or an overlay, otherwise put them "
                                       f"in the flow",
}


def _colours(texts):
    """unbound-color: the hexes, when they are few enough to list, else how many."""
    paints = [m for t in texts for m in _PAINT.findall(t)]
    if not paints:
        return None
    kinds = list(dict.fromkeys(k for k, _ in paints))
    where = {"fill": "fills", "stroke": "strokes"}.get(kinds[0]) if len(kinds) == 1 \
        else "fills and strokes"
    values = list(dict.fromkeys(v for _, v in paints))
    if len(values) == 1:
        return f"{values[0]} in {where} has no style or variable; bind it to one of the file's"
    if len(values) <= VALUES_MAX:
        return f"{where} {_list(values)} have no style or variable; bind them to the file's"
    return f"{len(values)} colours in {where} have no style or variable; bind them to the file's"


def _summary(rule, texts, layers):
    """One line for the window from the texts of one rule, `layers` counted: the text itself
    when they are all the same, else the rule's summary."""
    if len(texts) == 1:
        return texts[0]
    if rule == "unbound-color":
        line = _colours(texts)
        if line:
            return line
    n = max(layers, len(texts))
    if rule in SUMMARIES:
        return SUMMARIES[rule](n, texts)
    return f"{n} layers: {TITLES[rule].lower()}"


def _named(meta):
    return {"name": str(meta.get("name") or "Untitled")[:60], "type": str(meta.get("type") or "FRAME")}


def _merge(found):
    """One note per rule, in the order of the rules; ids n1, n2… Its text sums up the texts of
    the rule's layers (`_summary`); `details`, when they differ, keep each original line with
    its layers: [{text, layers: [ids]}], at most DETAILS_MAX, `detailsMore` the lines left out,
    and `names` the name and type of each of those layers the check could read: {id: {name, type}}.
    The ids stay plain in `details`, as the 1.8.0 window reads them."""
    groups = {}
    for n in found.get("notes") or []:
        if not (isinstance(n, dict) and n.get("rule") in TITLES and isinstance(n.get("text"), str)):
            continue
        g = groups.setdefault(n["rule"], {})
        t = g.setdefault(n["text"], {"count": 0, "ids": []})
        t["count"] += int(n.get("count") or 1)
        t["ids"] += [i for i in n.get("ids") or [] if isinstance(i, str) and i not in t["ids"]]
    info = found.get("info") if isinstance(found.get("info"), dict) else {}
    notes = []
    for rule in sorted(groups, key=RULES.index)[:NOTES_MAX]:
        texts = groups[rule]
        ids = list(dict.fromkeys(i for t in texts.values() for i in t["ids"]))
        # layers counted, not occurrences: ids a text did not list are added as they were counted
        total = len(ids) + sum(max(0, t["count"] - len(t["ids"])) for t in texts.values())
        layers = []
        for i in ids[:LAYERS_MAX]:
            meta = info.get(i) if isinstance(info.get(i), dict) else {}
            layers.append(dict(id=i, **_named(meta)))
        note = {"id": f"n{len(notes) + 1}", "rule": rule, "title": TITLES[rule], "_ids": ids,
                "text": _summary(rule, list(texts), total)[:bridge_board.TEXT_MAX], "layers": layers,
                "more": max(0, total - len(layers))}
        if len(texts) > 1:
            note["details"] = [{"text": t[:bridge_board.TEXT_MAX], "layers": v["ids"][:LAYERS_MAX]}
                               for t, v in list(texts.items())[:DETAILS_MAX]]
            if len(texts) > DETAILS_MAX:
                note["detailsMore"] = len(texts) - DETAILS_MAX
            named = dict.fromkeys(i for d in note["details"] for i in d["layers"])
            note["names"] = {i: _named(info[i]) for i in named if isinstance(info.get(i), dict)}
        notes.append(note)
    return notes


def _unmarked(found, marked):
    """The check's notes without the layers the designer marked on purpose for their rule. A note
    left with none of its layers goes; its count drops by the layers taken out."""
    if not marked:
        return found
    notes = []
    for n in found.get("notes") or []:
        if not isinstance(n, dict):
            continue
        ids = [i for i in n.get("ids") or [] if isinstance(i, str)]
        mine = marked.get(n.get("rule")) or {}
        left = [i for i in ids if i not in mine]
        if len(left) == len(ids):
            notes.append(n)
        elif left:
            count = int(n.get("count") or 1)
            notes.append(dict(n, ids=left, count=max(len(left), count - (len(ids) - len(left)))))
    return dict(found, notes=notes)


def _public(notes):
    """The notes as the board and the agent get them: without each one's full list of ids."""
    return [{k: v for k, v in n.items() if k != "_ids"} for n in notes]


async def _check(request, conn_id, doc, ids):
    """Run the check in the file's queue. (found, why): the script's value, or None and the
    reason it gave no answer."""
    row = STATE.get(doc)

    async def dispatch(meta):
        if row is None or row.get("cancel"):
            return web.json_response({"ok": False, "error": "stopped", "stopped": True})
        ws = (bridge.PLUGINS.get(conn_id) or {}).get("ws")
        if ws is None:
            return web.json_response({"ok": False, "error": "the file's window closed"})
        return await bridge._dispatch(
            ws, conn_id, _script(ids), CHECK_TIMEOUT, False, meta=meta,
            board=False, fields={"readOnly": True, "quick": True, "agent": AGENT})

    resp = await bridge._queued(request, conn_id, AGENT, CHECK_QUEUE, dispatch, script=False,
                                read=True)
    try:
        body = json.loads(resp.text)
    except (TypeError, ValueError):
        return None, "no answer"
    if resp.status == 200 and body.get("ok") and isinstance(body.get("value"), dict):
        return body["value"], None
    return None, "stopped" if body.get("stopped") else str(body.get("error") or "no answer")[:120]


def _n(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


async def on_done(request, conn_id, info, doc, agent):
    """`figaro done`: check what the agent changed, if the Reviewer is on in this file.
    The answer's `review`, or None when there is nothing to say."""
    mine = TRACK.get(doc, {}).pop(agent, None)
    if not enabled(doc, info) or not mine:
        return None
    ids = list(mine)
    try:
        return await _on_done(request, conn_id, doc, agent, ids)
    except asyncio.CancelledError:
        # The caller left (a disconnect, its own timeout): nothing was reviewed, so the layers
        # wait for the next done and a row that says "checking" doesn't stay so.
        _restore(doc, agent, ids)
        raise


def _restore(doc, agent, ids):
    mine = TRACK.setdefault(doc, {}).setdefault(agent, {})
    for i in ids:
        mine.setdefault(i, None)
    row = STATE.get(doc)
    if row and row["state"] == "checking" and row["agent"] == agent:
        _set(doc, "waiting", agent=agent, layers=len(ids))
        bridge_board.changed()


async def _on_done(request, conn_id, doc, agent, ids):
    lock = LOCKS.setdefault(doc, asyncio.Lock())
    async with lock:
        now = time.time()
        _withdraw(doc, agent)
        ANSWERS.get(doc, {}).pop(agent, None)
        row = _set(doc, "checking", agent=agent, layers=len(ids))
        row.update(since=now, cancel=False)
        bridge_board.changed()
        found, why = await _check(request, conn_id, doc, ids)
        row = STATE.get(doc)
        if row is None or row.get("cancel") or row.get("state") != "checking":
            return {"state": "none", "layers": len(ids), "notes": 0,
                    "text": "The designer stopped the reviewer's check, so nothing was reviewed."}
        if found is None:
            _set(doc, "waiting", agent=agent, layers=len(ids))
            for i in ids:
                TRACK.setdefault(doc, {}).setdefault(agent, {}).setdefault(i, None)
            bridge_board.changed()
            return {"state": "none", "layers": len(ids), "notes": 0,
                    "text": f"The reviewer couldn't check your layers this time ({why}). "
                            f"Carry on; run figaro done again to have them checked."}
        layers = int(found.get("layers") or 0)
        notes = _merge(_unmarked(found, MARKED.get(_key(doc))))
        if not layers:
            _set(doc, "waiting", agent=agent, layers=0)
            bridge_board.changed()
            return None
        if not notes:
            _set(doc, "good", agent=agent, layers=layers)
            _recent(doc, {"agent": AGENT, "summary": f"Checked {_n(layers, 'layer')}",
                          "note": "all good"})
            bridge_board.changed()
            return {"state": "good", "layers": layers, "notes": 0,
                    "text": f"Done. The reviewer checked the {_n(layers, 'layer')} you changed: "
                            f"all good."}
        at = time.time()
        ASKING[doc] = {"agent": agent, "notes": _public(notes), "at": at, "expired": False,
                       "ids": {n["id"]: n["_ids"] for n in notes}, "marked": [], "found": found}
        _set(doc, "notes", agent=agent, layers=layers)
        STATE[doc]["at"] = at  # the window sends it back with its answer: which notes it answers
        _recent(doc, {"agent": AGENT, "summary": f"Checked {_n(layers, 'layer')}",
                      "note": _n(len(notes), "note")})
        _expire_later(doc, at)
        bridge_board.changed()
        return {"state": "notes", "layers": layers, "notes": len(notes),
                "text": (f"Done. The reviewer is on in this file: it checked the "
                         f"{_n(layers, 'layer')} you changed and shows its notes to the "
                         f"designer, not to you.\nWait for their answer: figaro notes -A {shlex.quote(agent)} "
                         f"--wait   (up to {EXPIRE // 60} min)")}


def _withdraw(doc, rechecking=None):
    """The notes waiting for the designer go: the file's switch went off, or a newer check
    replaced them. Their agent is told when it asks, unless it is the one being checked again."""
    asking = ASKING.pop(doc, None)
    if asking and asking["agent"] != rechecking:
        ANSWERS.setdefault(doc, {})[asking["agent"]] = {"kind": "withdrawn"}


def _expire_later(doc, at):
    try:
        asyncio.get_running_loop().call_later(EXPIRE + 0.1, _expire, doc, at)
    except RuntimeError:
        pass


def _expire(doc, at):
    asking = ASKING.get(doc)
    if asking and asking["at"] == at and not asking["expired"]:
        asking["expired"] = True
        bridge_board.changed()


def forget_agent(doc, agent):
    """The window forgot an agent: its notes waiting for the designer and its layers go."""
    TRACK.get(doc, {}).pop(agent, None)
    ANSWERS.get(doc, {}).pop(agent, None)
    asking = ASKING.get(doc)
    if asking and asking["agent"] == agent:
        ASKING.pop(doc, None)
        row = STATE.get(doc)
        if row and row["state"] == "notes":
            _set(doc, "waiting", agent=agent, layers=0)


def stop(doc):
    """Stop in the Reviewer's row while it checks: the check ends, the row waits again."""
    row = STATE.get(doc)
    if not row or row.get("state") != "checking":
        return False
    row["cancel"] = True
    for rid, entry in list(bridge.PENDING.items()):
        if (entry.get("doc") == doc and entry.get("agent") == AGENT and entry.get("mtype") == "exec"
                and not entry.get("finished") and not entry.get("stopped")):
            entry["stopped"] = True
            info = bridge.PLUGINS.get(entry.get("conn")) or {}
            if info.get("ws") is not None:
                asyncio.get_running_loop().create_task(bridge._send_abort(info["ws"], rid))
    _set(doc, "waiting")
    row = STATE[doc]
    row["cancel"] = True
    print(f"[reviewer] the user stopped the check in {doc}")
    bridge_board.changed()
    return True


# ─── the window's messages ────────────────────────────────────────────────

def answer(doc, send, at=None):
    """The designer's answer: the ids of the notes the agent should fix, [] for all fine.
    `at`, when the window sends it, is the `at` of the notes it answers: an answer to notes
    that were replaced meanwhile is ignored (an older window sends none)."""
    asking = ASKING.get(doc)
    if not asking or not isinstance(send, list):
        return False
    if at is not None and not (isinstance(at, (int, float)) and not isinstance(at, bool)
                               and abs(at - asking["at"]) < 0.001):
        return False
    ASKING.pop(doc, None)
    chosen = {s for s in send if isinstance(s, str)}
    notes = asking["notes"]
    sent = [n for n in notes if n["id"] in chosen]
    left = len(notes) - len(sent)
    agent = asking["agent"]
    marked = asking.get("marked") or []
    # A note that lost some of its layers to a mark is still one of the notes: count a marked rule
    # only when none of its note is left.
    total = len(notes) + len({m["rule"] for m in marked} - {n["rule"] for n in notes})
    if sent:
        ANSWERS.setdefault(doc, {})[agent] = {"kind": "sent", "notes": sent, "total": total,
                                              "left": left, "marked": marked}
        _recent(doc, {"agent": "you", "summary": f"Sent {_n(len(sent), 'note')} to {agent}",
                      "note": f"{left} left as is" if left else None})
    else:
        ANSWERS.setdefault(doc, {})[agent] = {"kind": "fine", "total": total, "marked": marked}
        _recent(doc, {"agent": "you", "summary": f"All fine, nothing sent to {agent}"})
    row = _row(doc)
    row.update(state="sent", at=time.time(), since=None, sent=len(sent), left=left)
    bridge_board.changed()
    return True


def _refresh(doc, asking):
    """The waiting notes again from the check's own answer, less what is marked now: a note that
    lost some of its layers keeps its id and its ticks, and its text, count and lines follow the layers
    left. (No `info` for a layer the first check did not name, so the chips are the ones it did.)"""
    fresh = _merge(_unmarked(asking["found"], MARKED.get(_key(doc))))
    old = {n["rule"]: n for n in asking["notes"]}
    notes, ids = [], {}
    for n in fresh:
        prev = old.get(n["rule"])
        if prev is None:
            continue
        shown = [l for l in prev["layers"] if l["id"] in n["_ids"]] or n["layers"]
        total = len(n["layers"]) + n["more"]
        ids[prev["id"]] = n["_ids"]
        notes.append(_public([dict(n, id=prev["id"], layers=shown, more=max(0, total - len(shown)))])[0])
    asking["notes"], asking["ids"] = notes, ids


def intentional(doc, note, at=None, layers=None):
    """The designer marks a note waiting for them as on purpose: its rule is not raised again on
    its layers in this file, and the note leaves the ones the agent waits on. With `layers`, only
    those of the note's layers are marked and the note keeps the rest; marking the last ones is
    marking the note. With none of the notes left, that is the answer: all fine."""
    asking = ASKING.get(doc)
    if not asking or not isinstance(note, str):
        return False
    if at is not None and not (isinstance(at, (int, float)) and not isinstance(at, bool)
                               and abs(at - asking["at"]) < 0.001):
        return False
    hit = next((n for n in asking["notes"] if n["id"] == note), None)
    if hit is None:
        return False
    mine_all = asking.get("ids", {}).get(note) or [l["id"] for l in hit["layers"]]
    if layers is None:
        chosen = list(mine_all)
    elif isinstance(layers, list):
        chosen = [i for i in dict.fromkeys(layers) if isinstance(i, str) and i in mine_all]
    else:
        chosen = []
    if not chosen:
        return False
    rules = MARKED.setdefault(_key(doc), {})
    mine = rules.setdefault(hit["rule"], {})
    for i in chosen:
        mine.pop(i, None)  # the newest at the end
        mine[i] = None
    while sum(len(ids) for ids in rules.values()) > MARKS_MAX:
        oldest = next(r for r in rules if rules[r])
        rules[oldest].pop(next(iter(rules[oldest])))
        if not rules[oldest]:
            rules.pop(oldest)
    _save_marks()
    whole = layers is None
    if whole:
        taken = hit["layers"]
        more = hit.get("more", 0)
    else:
        named = {l["id"]: l for l in hit["layers"]}
        taken = [named.get(i) or dict(id=i, **_named({})) for i in chosen]
        more = 0
    if whole:
        asking["notes"] = [n for n in asking["notes"] if n["id"] != note]
    else:
        _refresh(doc, asking)
    asking.setdefault("marked", []).append(
        {"rule": hit["rule"], "title": hit["title"], "text": hit["text"], "layers": taken, "more": more})
    _recent(doc, {"agent": "you", "summary": f"Marked on purpose: {hit['title']}"})
    print(f"[reviewer] {hit['rule']} marked on purpose in {doc}")
    if not asking["notes"]:
        return answer(doc, [], asking["at"])
    bridge_board.changed()
    return True


def unmark(doc):
    """Clear the file's notes marked on purpose: the reviewer raises them again."""
    if MARKED.pop(_key(doc), None) is None:
        return False
    _save_marks()
    print(f"[reviewer] the notes marked on purpose in {doc} were cleared")
    bridge_board.changed()
    return True


def window_message(m):
    """`reviewer`, `review-answer`, `review-intentional` and `review-unmark` from a window.
    True if `m` was one."""
    mtype = m.get("type")
    doc = m.get("doc") if isinstance(m.get("doc"), str) else None
    if mtype not in ("reviewer", "review-answer", "review-intentional", "review-unmark"):
        return False
    if doc:
        if mtype == "reviewer":
            switch(doc, m.get("on"))
        elif mtype == "review-answer":
            answer(doc, m.get("send"), m.get("at"))
        elif mtype == "review-intentional":
            intentional(doc, m.get("note"), m.get("at"), m.get("layers"))
        else:
            unmark(doc)
    return True


# ─── POST /notes ──────────────────────────────────────────────────────────

def _result(doc, agent, info):
    """What the agent asks for, now: (state, extra fields). The answer is consumed."""
    got = ANSWERS.get(doc, {}).pop(agent, None)
    if got:
        if got["kind"] == "sent":
            notes = [dict(n, layers=[dict(l, url=figma_links.node_url(info.get("fileKey"),
                                                                      info.get("name"), l["id"]))
                                     for l in n["layers"]]) for n in got["notes"]]
            for n in notes:  # a link for every id a detail line names
                ids = {i for d in n.get("details") or [] for i in d.get("layers") or []}
                if ids:
                    n["links"] = {i: figma_links.node_url(info.get("fileKey"), info.get("name"), i) for i in ids}
            extra = {"notes": notes, "total": got["total"], "left": got["left"]}
        else:
            extra = {}
        if got.get("marked"):
            extra["marked"] = got["marked"]
        return got["kind"], extra
    asking = ASKING.get(doc)
    if asking and asking["agent"] == agent:
        if asking["expired"] or time.time() - asking["at"] >= EXPIRE:
            if not asking["expired"]:
                asking["expired"] = True
                bridge_board.changed()
            return "expired", {}
        return "pending", {}
    return "none", {}


async def notes_handler(request: web.Request) -> web.Response:
    """The designer's answer to the reviewer's notes for this agent, once. With `wait`, one
    call holds up to HOLD seconds while the designer has not answered; the CLI asks again."""
    body, error = await bridge_exec._body(request)
    if error:
        return error
    agent = body.get("agent")
    agent = agent.strip()[:40] or None if isinstance(agent, str) else None
    conn_id, info = bridge_exec._target(body)
    if conn_id is None:
        return info
    doc = bridge._doc_key(conn_id)
    agent = bridge_board._who(agent)
    end = time.monotonic() + (HOLD if body.get("wait") is True else 0)
    while True:
        asking = ASKING.get(doc)
        waiting = asking is not None and asking["agent"] == agent and not asking["expired"] \
            and time.time() - asking["at"] < EXPIRE
        if not waiting or time.monotonic() >= end:
            break
        await asyncio.sleep(0.2)
    state, extra = _result(doc, agent, info)
    return web.json_response(dict({"ok": True, "state": state, "agent": agent,
                                   "file": info.get("name")}, **extra))


def install(app: web.Application):
    app.router.add_post("/notes", notes_handler)
