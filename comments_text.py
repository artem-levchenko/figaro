"""`figaro comments` and `figaro comment`: the scripts that find what each pin is on and
the layer a new comment goes on, and the text they print.

The bridge lists a file's open comments with the node each is pinned to and the
point on it (bridge_comments). Only the plugin knows the layers, so one quick
read-only script asks it for every pin at once: the layer, its page, what lies
under the point, and when nothing does, the nearest layers. The text puts a
link to each layer beside its id, for the agent's report.
"""
from __future__ import annotations

import json
from datetime import datetime

import figma_links

NEAREST = 3     # layers named when nothing lies under a pin on the canvas
PIN_IN = 24     # px: a new comment's pin goes into its layer half this from the corner, at most
KIDS = 12       # children of a frame named when nothing deeper lies under the pin
SNIPPET = 80    # characters of a text layer's text

# The body of an async function in the plugin, with `figma` and `h`; it only reads.
_SCRIPT = """
const pins = %s;
const SNIP = %d, NEAR = %d, KIDS = %d;
const box = (n) => n.absoluteBoundingBox || null;
const inside = (b, x, y) => !!b && x >= b.x && x <= b.x + b.width && y >= b.y && y <= b.y + b.height;
const gap = (b, x, y) => !b ? Infinity : Math.round(Math.hypot(Math.max(b.x - x, 0, x - (b.x + b.width)), Math.max(b.y - y, 0, y - (b.y + b.height))));
const brief = (n) => {
  const o = { id: n.id, name: n.name, type: n.type };
  if (n.type === 'TEXT') o.text = String(n.characters).slice(0, SNIP);
  return o;
};
const pageOf = (n) => { let p = n; while (p && p.type !== 'PAGE') p = p.parent; return p; };
const out = [];
for (const pin of pins) {
  const r = { id: pin.id };
  out.push(r);
  if (!pin.node) continue;
  let n = null;
  try { n = await h.node(pin.node); } catch (e) { n = null; }
  if (!n) { r.gone = true; continue; }
  r.node = brief(n);
  const page = pageOf(n);
  if (page) r.page = { id: page.id, name: page.name };
  if (pin.x === null || pin.y === null) continue;
  let x = pin.x, y = pin.y;
  if (n.type !== 'PAGE' && n.absoluteTransform) {
    const t = n.absoluteTransform;
    x = t[0][0] * pin.x + t[0][1] * pin.y + t[0][2];
    y = t[1][0] * pin.x + t[1][1] * pin.y + t[1][2];
  }
  r.at = { x: Math.round(x), y: Math.round(y) };
  const path = [];
  let cur = n;
  for (let i = 0; i < 80; i++) {
    const kids = 'children' in cur ? cur.children : [];
    let hit = null;
    for (let k = kids.length - 1; k >= 0; k--) {
      if (kids[k].visible !== false && inside(box(kids[k]), x, y)) { hit = kids[k]; break; }
    }
    if (!hit) break;
    path.push(hit);
    cur = hit;
  }
  r.under = path.map(brief);
  if (!path.length && 'children' in n) {
    if (n.type === 'PAGE') {
      r.nearest = n.children.map((k) => Object.assign(brief(k), { away: gap(box(k), x, y) }))
        .filter((k) => k.away !== Infinity).sort((a, b) => a.away - b.away).slice(0, NEAR);
    } else {
      r.kids = n.children.length;
      r.children = n.children.slice(0, KIDS).map(brief);
    }
  }
}
return out;
"""


def pin_code(threads):
    """The script for the pins of these threads; None when none is on a layer."""
    pins = [{"id": t["id"], "node": t["pin"].get("node"), "x": t["pin"].get("x"), "y": t["pin"].get("y")}
            for t in threads if t.get("pin")]
    if not any(p["node"] for p in pins):
        return None
    return _SCRIPT % (json.dumps(pins), SNIPPET, NEAREST, KIDS)


def when(iso):
    """'2026-10-26T15:12:19.827Z' -> '2026-10-26 15:12 UTC'; what it can't read, as it is."""
    try:
        return datetime.strptime(str(iso)[:16], "%Y-%m-%dT%H:%M").strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(iso or "")


def _indent(text, pad):
    lines = str(text).splitlines() or [""]
    return "\n".join(pad + line for line in lines)


def _layer(item, key, name, link=True):
    text = f'{item["type"]} "{item["name"]}" {item["id"]}'
    url = figma_links.node_url(key, name, item["id"]) if link else None
    return f"{text}  {url}" if url else text


def describe(thread, found, key, name):
    """The lines that say where one comment is pinned. `found` is the plugin's
    answer for its pin, or None when the plugin couldn't be asked."""
    pin = thread.get("pin") or {}
    node, x, y = pin.get("node"), pin.get("x"), pin.get("y")
    if not node:
        where = f"canvas point ({_n(x)}, {_n(y)})" if x is not None and y is not None else "no layer"
        return [f"pinned to no layer, at {where}: the page isn't known"]
    link = figma_links.node_url(key, name, node)
    if found is None:
        return [f"pinned to {node}" + (f" at ({_n(x)}, {_n(y)}) in it" if x is not None else "")
                + (f"  {link}" if link else "") + " (no layer names: Figma isn't answering for this file)"]
    if found.get("gone"):
        return [f"pinned to {node}, which is not in the file any more (deleted)"]
    if not found.get("node"):
        return [f"pinned to {node}" + (f"  {link}" if link else "")]
    n, page, at = found["node"], found.get("page"), found.get("at")
    lines = []
    if n["type"] == "PAGE":
        lines.append(f'on the page "{n["name"]}" ({n["id"]}) at canvas ({at["x"]}, {at["y"]})'
                     if at else f'on the page "{n["name"]}" ({n["id"]})')
        under = found.get("under") or []
        if under:
            lines.extend(_under(under, key, name))
        else:
            lines.append("no layer is under the pin: it is on the page's empty canvas")
            near = found.get("nearest") or []
            if near:
                lines.append("nearest layers: " + "; ".join(
                    f'{_layer(i, key, name)} ({i["away"]} px away)' for i in near))
            else:
                lines.append("the page has no layers near it")
        return lines
    where = f"pinned to {_layer(n, key, name)}"
    if page:
        where += f' (page "{page["name"]}")'
    lines.append(where + (f", {_n(x)} px right and {_n(y)} px down from its corner" if x is not None else ""))
    under = found.get("under") or []
    if under:
        lines.extend(_under(under, key, name))
        if n["type"] in ("FRAME", "COMPONENT", "INSTANCE", "SECTION", "GROUP", "COMPONENT_SET"):
            lines.append("it is the layer under the pin, not necessarily the one the comment is about: "
                         "look at its neighbours too")
    elif found.get("children"):
        lines.append("no child lies exactly under the pin; its children"
                     + (f" (first {len(found['children'])} of {found['kids']})"
                        if found.get("kids", 0) > len(found["children"]) else "") + ":")
        lines.extend("    " + _layer(i, key, name, link=False) + _text(i) for i in found["children"])
    return lines


def _under(chain, key, name):
    last = chain[-1]
    path = " > ".join(i["name"] for i in chain)
    return [f"deepest layer under the pin: {_layer(last, key, name)}{_text(last)}",
            f"  path below the pinned layer: {path}"]


def _text(item):
    return f'  text: "{item["text"]}"' if item.get("text") else ""


def _n(v):
    return str(int(round(v))) if isinstance(v, (int, float)) else "?"


def render(resp, answers):
    """The reply's lines. `answers` is {comment id: the plugin's answer} or None."""
    key, name = resp.get("fileKey"), resp.get("file") or "Untitled"
    threads = resp.get("comments") or []
    skipped = resp.get("skipped") or 0
    head = (f'{len(threads)} open comment{"" if len(threads) == 1 else "s"} in "{name}"'
            if threads else f'no open comments in "{name}"')
    if skipped:
        head += f" ({skipped} resolved, left out)"
    cut = resp.get("cut") or 0
    if cut:
        head += f" ({cut} more open, left out)"
    out = [head]
    for t in threads:
        out.append("")
        out.append(f'#{t["id"]}  {t["author"]}  {when(t.get("at"))}')
        out.append(_indent(t["message"], "  | "))
        for line in describe(t, None if answers is None else answers.get(t["id"], {}), key, name):
            out.append("  " + line)
        for r in t.get("replies") or []:
            out.append(f'  reply, {r["author"]}, {when(r.get("at"))}:')
            out.append(_indent(r["message"], "    | "))
    if threads:
        out.append("")
        out.append("A comment is often pinned to a frame, not to the layer it talks about: look among "
                   "the frame's children (figaro tree <id>, figaro shot <id>).")
    return out


# ─── figaro comment ───────────────────────────────────────────────────────

# The body of an async function in the plugin: the layer a new comment goes on. It only
# reads. Figma takes a pin on any id, one the file doesn't have too, so this checks it first.
_LAYER = """
const want = %s;
const pageOf = (n) => { let p = n; while (p && p.type !== 'PAGE') p = p.parent; return p; };
let n = null;
if (want === 'sel') n = figma.currentPage.selection[0] || null;
else { try { n = await h.node(want); } catch (e) { n = null; } }
if (!n) return { gone: true };
if (n.type === 'PAGE' || n.type === 'DOCUMENT') return { isPage: true, id: n.id, name: n.name };
const page = pageOf(n);
return { id: n.id, name: n.name, type: n.type, w: n.width, h: n.height,
         page: page ? { id: page.id, name: page.name } : null };
"""


def layer_code(node):
    """The script that finds the layer `node` names ("12:34" or "sel")."""
    return _LAYER % json.dumps(node)


def pin_at(layer, at=None):
    """Where in the layer the pin goes: `at`, else a little in from its top left corner."""
    if at:
        return {"x": at[0], "y": at[1]}
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0  # noqa: E731
    return {"x": round(min(num(layer.get("w")), PIN_IN) / 2), "y": round(min(num(layer.get("h")), PIN_IN) / 2)}


def posted(resp, layer=None):
    """The lines that say what `figaro comment` did."""
    key, name = resp.get("fileKey"), resp.get("file") or "Untitled"
    act, cid = resp.get("action"), resp.get("id")
    if act == "post":
        where = _layer(layer, key, name) if layer else "the layer"
        if layer and layer.get("page"):
            where += f' (page "{layer["page"]["name"]}")'
        out = [f"posted comment #{cid} on {where}"]
    elif act == "reply":
        out = [f"replied in the thread of #{resp.get('parent')}: #{cid}"]
    elif act == "react":
        return [f"reacted {resp.get('emoji')} to #{cid}"]
    else:
        return [f"deleted comment #{cid}"]
    out.append(_indent(resp.get("message") or "", "  | "))
    return out
