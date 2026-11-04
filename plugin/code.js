// The window: one island per file (ui.html). It grows and shrinks with them —
// the UI measures itself and asks for its height (`resize`, fitWindow below) —
// and stays on the canvas: with no room under it, it moves up. Minimized, it
// is narrower (it asks for its width too).
const UI_WIDTH = 320;
figma.showUI(__html__, { width: UI_WIDTH, height: 70, title: "Figaro Relay", themeColors: true });

// Build id of this plugin code. The bridge compares it with plugin/code.js on
// disk and asks for a re-Run when they differ, because a running plugin keeps
// the code it started with. Bump it on every change to plugin/ —
// tests/test_plugin_version.py fails until you do.
const PLUGIN_VERSION = "2026-11-04.1";

// What this build can do beyond a plain exec, so the bridge knows which
// requests it may send (an older build gets `figaro reload` first).
const CAPS = ["changes", "readOnly", "undo", "reload", "checkpoint",
                 "quick", "libs", "links", "inspect", "shot", "gate", "board", "images"];

let keptView = null;  // the window's view (below)

// Tell the UI which file we're in, so it can register this connection with the
// bridge by name (figma.root.name). The bridge routes --target by that name.
function postIdentity() {
  figma.ui.postMessage({
    type: "identity",
    fileKey: fileKey(),
    name: figma.root.name,
    docSig: docSignature(),
    pluginVersion: PLUGIN_VERSION,
    caps: CAPS,
    view: keptView,  // null until it is read
  });
}

// The bridge needs an id that is the same for one file open in two windows and
// different for two files that share a name, to route a target safely.
// `figma.root.id` is "0:0" in every file, and page ids are not enough either:
// every new file starts with a single page "0:1".
//
// The id is the file key. "enablePrivatePluginApi" in manifest.json gives a
// development plugin `figma.fileKey`, and reading it writes nothing: storing an
// id in the document's plugin data would edit every file the plugin is run in,
// shared libraries included. Without a key the id lives only as long as this
// run, and still nothing is written.
const RUN_ID = "r" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);

function docSignature() {
  return fileKey() || RUN_ID;
}
postIdentity();

// The window's view: minimized in a file, it opens minimized there the next
// time. Kept on this computer (clientStorage), never in the file (keptView, above).
const viewKey = () => "view:" + fileKey();
const viewRead = (async () => {
  try {
    const v = fileKey() && figma.clientStorage ? await figma.clientStorage.getAsync(viewKey()) : null;
    keptView = v === "mini" || v === "nano" ? v : "files";
  } catch (e) {
    keptView = "files";
  }
  postIdentity();
})();
async function keepView(view) {
  if (view !== "files" && view !== "mini" && view !== "nano") return;
  keptView = view;
  try {
    if (fileKey() && figma.clientStorage) await figma.clientStorage.setAsync(viewKey(), view);
  } catch (e) {}
}

function safeStringify(value) {
  if (value === undefined) return null;
  try { return JSON.parse(JSON.stringify(value)); } catch (e) {
    try { return String(value); } catch (e2) { return null; }
  }
}

// The `result` message for a finished exec. It carries the value once: the
// bridge derives the human-readable `result` text from it. Sending both, as
// before, doubled every payload and pretty-printed objects a third time — a
// 50k-item array took ~0.6 s to come back. `text` is only sent when the value
// can't express it: no return value (logs or "Done"), or NaN / BigInt / etc.
function resultMessage(id, result, logs) {
  const t = typeof result;
  if (result === undefined) {
    return { type: "result", id, value: null, text: asText(result, logs) };
  }
  if (t === "string" || t === "boolean" || (t === "number" && isFinite(result))) {
    return { type: "result", id, value: result };
  }
  if (t === "object") {  // objects, arrays, null
    // Serialized exactly once, here. The UI splices this string into the
    // WebSocket frame as is, so the object is never cloned across postMessage
    // or stringified again (that was 3 passes over a big result).
    try {
      const json = JSON.stringify(result);
      if (json !== undefined) return { type: "result", id, valueJson: json };
    } catch (e) { /* cyclic, BigInt inside… — fall through */ }
    return { type: "result", id, value: safeStringify(result) };
  }
  return { type: "result", id, value: safeStringify(result), text: String(result) };
}

function asText(value, logs) {
  try {
    if (value !== undefined) {
      return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
    }
  } catch (e) { /* unserializable — fall back to logs */ }
  return logs.length > 0 ? logs.join("\n") : "Done";
}

// ─── helpers exposed as `h.*` to every exec ──────────────────────────────

// Set for the duration of one exec so helpers can surface warnings through the
// same `print()` the user's code gets. No-op outside an exec.
let CURRENT_PRINT = () => {};

// The `figma` of the script whose h called a helper: its calls end at the
// script's deadline (scriptFigma), so an import by key that Figma never
// answers can't hold the file past --timeout. Without that h, the plain one.
const SCRIPT_FIGMA = Symbol("figma");
function figmaOf(h) {
  return (h && h[SCRIPT_FIGMA]) || figma;
}

// The files that came with the script (`exec --image NAME=FILE`, `figaro
// image`): name -> base64, so h.image(node, "name") finds its bytes.
const SCRIPT_IMAGES = Symbol("images");

function scriptImages(list) {
  const files = new Map();
  if (Array.isArray(list)) {
    for (const f of list) if (f && typeof f.name === "string" && typeof f.data === "string") files.set(f.name, f.data);
  }
  return files;
}

// The script's deadline or the bridge's abort, not an answer from Figma.
function stopped(e) {
  return !!e && typeof e.message === "string" && e.message.indexOf("aborted: ") === 0;
}

async function resolveVar(varOrId, fig) {
  if (varOrId == null) return null;
  if (typeof varOrId !== "string") return varOrId;
  fig = fig || figma;

  // Local variables are addressed as "VariableID:1:23"; anything else is a
  // library key, which has to be imported rather than looked up.
  if (varOrId.indexOf("VariableID:") === 0) {
    return await fig.variables.getVariableByIdAsync(varOrId);
  }
  // A library key is not a well-formed id, and getVariableByIdAsync rejects
  // those by throwing rather than returning null — so this has to be guarded,
  // otherwise the import below is unreachable for the very case it exists for.
  let local = null;
  try {
    local = await fig.variables.getVariableByIdAsync(varOrId);
  } catch (e) {
    if (stopped(e)) throw e;
    local = null;
  }
  if (local) return local;

  try {
    return await fig.variables.importVariableByKeyAsync(varOrId);
  } catch (e) {
    if (stopped(e)) throw e;  // the deadline, not a key Figma refused
    return null;
  }
}

// "#1a2b3c" / "1a2b3c" / "#f00" -> {r,g,b} in Figma's 0..1 range.
function hexToRgb(value) {
  let s = String(value).trim().replace(/^#/, "");
  if (s.length === 3) s = s[0] + s[0] + s[1] + s[1] + s[2] + s[2];
  if (!/^[0-9a-fA-F]{6}$/.test(s)) {
    throw new Error("h.hex: expected #RGB or #RRGGBB, got " + JSON.stringify(value));
  }
  const n = parseInt(s, 16);
  return {
    r: ((n >> 16) & 255) / 255,
    g: ((n >> 8) & 255) / 255,
    b: (n & 255) / 255,
  };
}

// Padding as a number, or as in CSS: [all], [vertical, horizontal], [top,
// horizontal, bottom], [top, right, bottom, left]; or {top, right, bottom, left}.
function paddingOf(p) {
  if (p == null) return null;
  const n = (v) => Number(v) || 0;
  if (typeof p !== "object") return { top: n(p), right: n(p), bottom: n(p), left: n(p) };
  if (Array.isArray(p)) {
    const [top = 0, right = top, bottom = top, left = right] = p.map(n);
    return { top, right, bottom, left };
  }
  return { top: n(p.top), right: n(p.right), bottom: n(p.bottom), left: n(p.left) };
}

// Copy a paint array before mutating it — node.fills/strokes are frozen.
function copyPaints(node, prop, who) {
  const paints = node[prop];
  if (typeof paints === "symbol") {
    throw new Error(
      who + ": '" + node.name + "' has mixed " + prop +
      "; set the paint per-range, or unify " + prop + " on the node first"
    );
  }
  if (!Array.isArray(paints)) {
    throw new Error(who + ": '" + node.name + "' has no " + prop);
  }
  return JSON.parse(JSON.stringify(paints));
}

// A key Figma would not import is the usual dead end — library variables
// the file already uses can still be bound by id.
function noVar(who, varOrId) {
  const id = String(varOrId);
  return new Error(who + ": no variable " + id + (id.indexOf("VariableID:") === 0 ? " in this file"
    : ": it is not an id, and Figma did not import it by key (the library is not enabled for this file, " +
      "or the variable is not published). Take a variable a layer in this file already uses by id: " +
      "figaro inspect of that layer prints \"id VariableID:…\""));
}

const HELPERS = {
  // Bind fill paint at index to a variable (id or instance)
  async bF(node, idx, varOrId) {
    const v = await resolveVar(varOrId, figmaOf(this));
    if (!v) throw noVar("h.bF", varOrId);
    const f = copyPaints(node, "fills", "h.bF");
    if (!f[idx]) throw new Error("h.bF: '" + node.name + "' has no fill at index " + idx);
    f[idx] = figma.variables.setBoundVariableForPaint(f[idx], "color", v);
    node.fills = f;
    return v;
  },

  // Bind stroke paint at index
  async bS(node, idx, varOrId) {
    const v = await resolveVar(varOrId, figmaOf(this));
    if (!v) throw noVar("h.bS", varOrId);
    const s = copyPaints(node, "strokes", "h.bS");
    if (!s[idx]) throw new Error("h.bS: '" + node.name + "' has no stroke at index " + idx);
    s[idx] = figma.variables.setBoundVariableForPaint(s[idx], "color", v);
    node.strokes = s;
    return v;
  },

  // Bind numeric property (radii, padding, sizes, itemSpacing, etc.)
  async bN(node, prop, varOrId) {
    const v = await resolveVar(varOrId, figmaOf(this));
    if (!v) throw noVar("h.bN", varOrId);
    node.setBoundVariable(prop, v);
    return v;
  },

  // First descendant by exact name
  findByName(root, name) {
    return root.findOne((n) => n.name === name);
  },

  // All descendants by exact name
  findAllByName(root, name) {
    return root.findAll((n) => n.name === name);
  },

  // Dump subtree as indented text
  dumpTree(node, opts) {
    opts = opts || {};
    const maxDepth = opts.maxDepth == null ? 99 : opts.maxDepth;
    const showSize = opts.showSize !== false;
    const showText = opts.showText !== false;
    const showLayout = opts.showLayout === true;
    const lines = [];
    const walk = (n, d) => {
      if (d > maxDepth) return;
      const pad = "  ".repeat(d);
      let line = pad + n.name + " [" + n.type + "] " + n.id;
      if (showSize && n.width !== undefined) {
        line += " " + Math.round(n.width) + "×" + Math.round(n.height);
      }
      if (showLayout && n.layoutMode && n.layoutMode !== "NONE") {
        line += " {" + n.layoutMode[0] +
          " gap:" + n.itemSpacing +
          " pad:" + n.paddingTop + "," + n.paddingRight + "," + n.paddingBottom + "," + n.paddingLeft +
          " " + n.primaryAxisSizingMode + "/" + n.counterAxisSizingMode + "}";
      }
      if (showText && n.type === "TEXT") line += " " + JSON.stringify(n.characters);  // a line break as \n
      lines.push(line);
      if (n.children) for (const c of n.children) walk(c, d + 1);
    };
    walk(node, 0);
    return lines.join("\n");
  },

  // Clone node and place it next to the original: in an auto-layout frame,
  // right after it.
  cloneNext(node, opts) {
    opts = opts || {};
    const direction = String(opts.direction || "right").toLowerCase();
    if (!["right", "left", "down", "up"].includes(direction)) {
      throw new Error(`h.cloneNext: direction is right, left, down or up, not "${opts.direction}"`);
    }
    // A page's clone is a whole new page, made before x and y fail on it.
    if (!node.parent || node.type === "PAGE") {
      throw new Error(`h.cloneNext copies a layer, not a ${node.type.toLowerCase()}: name a layer on the page`);
    }
    const gap = opts.gap == null ? 100 : opts.gap;
    const c = node.clone();
    node.parent.insertChild(node.parent.children.indexOf(node) + 1, c);
    if (direction === "right") { c.x = node.x + node.width + gap; c.y = node.y; }
    else if (direction === "left")  { c.x = node.x - node.width - gap; c.y = node.y; }
    else if (direction === "down")  { c.x = node.x; c.y = node.y + node.height + gap; }
    else if (direction === "up")    { c.x = node.x; c.y = node.y - node.height - gap; }
    if (opts.name) c.name = opts.name;
    return c;
  },

  // Available variants for an instance's component
  async variantsOf(instance) {
    const main = await instance.getMainComponentAsync();
    if (!main) return null;
    const set = main.parent && main.parent.type === "COMPONENT_SET" ? main.parent : null;
    return set
      ? { current: main.name, groups: set.variantGroupProperties, all: set.children.map(c => c.name) }
      : { current: main.name, groups: null, all: null };
  },

  // Hex string -> {r,g,b}. Hand-rolling this is where the missing /255 lives.
  hex(value) { return hexToRgb(value); },

  // Ready-to-assign paint array: node.fills = h.solid("#1a2b3c")
  solid(value, opacity) {
    const paint = { type: "SOLID", color: hexToRgb(value) };
    if (opacity != null) paint.opacity = opacity;
    return [paint];
  },

  // Create a frame with auto-layout applied in the order Figma demands:
  // into the tree -> layoutMode -> size -> sizing mode -> spacing/padding.
  // Getting that order wrong silently drops the settings.
  frame(parent, opts) {
    opts = opts || {};
    const f = figma.createFrame();
    if (parent) parent.appendChild(f);

    if (opts.name) f.name = opts.name;

    if (opts.layout) {
      const l = String(opts.layout).toUpperCase();
      f.layoutMode = l === "V" ? "VERTICAL" : l === "H" ? "HORIZONTAL" : l;
    }

    if (opts.w != null || opts.h != null) {
      f.resize(opts.w == null ? f.width : opts.w, opts.h == null ? f.height : opts.h);
    }

    if (f.layoutMode && f.layoutMode !== "NONE") {
      // An axis pinned to a number is FIXED: resize() fixes only an axis whose
      // size changes, and a new frame is 100 × 100, so h: 100 kept hugging.
      // Hug by default on the others.
      const horizontalIsPrimary = f.layoutMode === "HORIZONTAL";
      const primaryFixed = horizontalIsPrimary ? opts.w != null : opts.h != null;
      const counterFixed = horizontalIsPrimary ? opts.h != null : opts.w != null;
      if (primaryFixed) f.primaryAxisSizingMode = "FIXED";
      else if (opts.hug !== false) f.primaryAxisSizingMode = "AUTO";
      if (counterFixed) f.counterAxisSizingMode = "FIXED";
      else if (opts.hug !== false) f.counterAxisSizingMode = "AUTO";
      if (opts.spacing != null) f.itemSpacing = opts.spacing;
      if (opts.align) {
        if (opts.align.primary) f.primaryAxisAlignItems = opts.align.primary;
        if (opts.align.counter) f.counterAxisAlignItems = opts.align.counter;
      }
      const pad = paddingOf(opts.padding);
      if (pad) {
        f.paddingTop = pad.top; f.paddingRight = pad.right;
        f.paddingBottom = pad.bottom; f.paddingLeft = pad.left;
      }
    }

    if (opts.fill != null) f.fills = opts.fill === false ? [] : HELPERS.solid(opts.fill);
    if (opts.radius != null) f.cornerRadius = opts.radius;
    return f;
  },

  // Accept "page" / "sel" alongside a real node id, so callers can say
  // "the thing I'm looking at" without first hunting for its id.
  async resolve(idOrAlias) {
    if (idOrAlias === "page") return figma.currentPage;
    if (idOrAlias === "sel") {
      const s = figma.currentPage.selection;
      if (!s.length) throw new Error("nothing selected in Figma");
      return s[0];
    }
    return await HELPERS.node(idOrAlias);  // fails at once for a missing id
  },

  // Quick async accessors
  // An id this file does not have fails at once. Figma itself waits ~10 s
  // on another file's id or a deleted node, then throws an error with no text.
  // A Figma link or a link-form id ("1-11") works too.
  // The sync lookup answers first: getNodeByIdAsync has hung for a layer
  // inside an instance (I2:1068;2:1041) that the sync lookup found.
  async node(id) {
    id = linkNodeId(id);
    const found = nodeHere(id);
    if (found === null) {
      throw new Error("no node " + id + " in \"" + figma.root.name +
        "\": it is from another file or was deleted — check -T and the id");
    }
    return found || await figma.getNodeByIdAsync(id);
  },
  async var_(idOrKey) { return await resolveVar(idOrKey, figmaOf(this)); },
  async importComp(key) { return await figmaOf(this).importComponentByKeyAsync(key); },
  async importVar(key)  { return await figmaOf(this).variables.importVariableByKeyAsync(key); },
};

// ──────────────────────────────────────────────────────────────────────────

// Ids the bridge gave up waiting for. A running script cannot be killed, so
// cancellation is cooperative: long loops call h.ck() and bail out.
const ABORTED = new Set();
// Bounded — a long session used to keep every aborted id forever.
const ABORTED_MAX = 100;
const ABORTED_TEXT = "aborted: past this exec's timeout — the bridge stopped waiting";

function markAborted(id) {
  ABORTED.add(id);
  if (ABORTED.size > ABORTED_MAX) ABORTED.delete(ABORTED.values().next().value);
}

// What ends a Figma call that never settles. The call a script awaits
// through its `figma` also races this gate, which fails at the deadline or on
// the bridge's `abort`. figma.teamLibrary has gone minutes without an answer,
// and the file stayed blocked (409) until the plugin restarted.
const GATES = new Map();  // exec id -> fail()

function makeGate(id, deadline) {
  let fail;
  const lost = new Promise((_, reject) => { fail = reject; });
  lost.catch(() => {});  // a script that awaited nothing never looks at it
  const timer = deadline === Infinity ? null : setTimeout(fail, Math.max(0, deadline - Date.now()));
  GATES.set(id, fail);
  return {
    race(p, name) {
      // made here, so its stack has the script's line that awaited the call
      const hung = new Error("aborted: figma." + name + "() did not answer before --timeout ran out — " +
        "the script was stopped");
      return Promise.race([p, lost.then(null, () => { throw hung; })]);
    },
    close() { if (timer !== null) clearTimeout(timer); GATES.delete(id); },
  };
}

// ─── the window's height and place ───────────────────────────────────────
// ui.html asks for the height of all it shows (`height`). The window takes it
// and stays where the user put it (`spot`) while it has room under it; with no
// room, it moves up instead, just enough and at most to the canvas's top, and
// as it gets shorter it comes back down, never past the spot. What the user
// left below the canvas's bottom edge (`hidden`: a window dragged partly off
// it) stays out of sight there, and no more of it goes there. Only a window
// taller than the whole canvas stops short of its height, and ui.html scrolls
// its islands; the sandbox tells ui.html how tall the window may get (`room`).
// Figma has no event for a moved window or a resized canvas, so the window
// asks again every second: one that isn't where the sandbox put it was moved
// by the user, and where it is now is its spot.
const FIGMA_HEADER = 40;  // Figma's header (the plugin's name, the close button) with the line under it: the
                          // window's position is its top edge, and the islands start 40 px lower (checked live)
const CANVAS_GAP = 8;     // the window keeps this far from the canvas's top and bottom edges
const HEIGHT_MAX = 2000;
const HEIGHT_MIN = 30;    // on a canvas too short for more: an island's row
const WIDTH_MIN = 70;     // Figma's narrowest plugin window
const placed = { height: 0, width: UI_WIDTH, spot: null, hidden: 0, pos: null, room: 0 };

// Where the canvas's top left corner is in window space. getPosition gives the
// window's corner in window space in whole pixels and on the canvas in whole
// canvas units (checked live): at 280 % one unit is 2.8 px, so what one look
// says can be off by more than a pixel either way, and by how much depends on
// where the window is. Each look narrows the range the corner can be in (`lo`
// to `hi`); one that falls outside it means Figma's layout changed (full screen,
// a panel shown or hidden), and the range starts over from it. A window this
// code has just moved is a sharper look (moveTo).
const canvasAt = { x: null, y: null };
function narrow(axis, at, slack) {
  const r = canvasAt[axis];
  if (r && at + slack >= r.lo && at - slack <= r.hi) {
    r.lo = Math.max(r.lo, at - slack);
    r.hi = Math.min(r.hi, at + slack);
  } else {
    canvasAt[axis] = { lo: at - slack, hi: at + slack };
  }
  return canvasAt[axis];
}

// The canvas's corner and its top and bottom edges in window space, the
// edges less the gap; the viewport, and where the window is. Whole pixels:
// Figma's numbers come out a hair off as well (991.9999999999998 for 992).
// `off`: how far from where it is asked Figma may put the window, as the
// canvas's corner is known no better. null when Figma can't tell.
function canvasEdges() {
  try {
    const p = figma.ui.getPosition();
    const zoom = figma.viewport.zoom;
    const b = figma.viewport.bounds;
    if (!p || !p.windowSpace || !p.canvasSpace || !b || !(zoom > 0)) return null;
    // windowSpace and canvasSpace are one point, the window's corner: from it, where the canvas starts
    const slack = zoom / 2 + 1;  // half a canvas unit, and a pixel
    const rx = narrow("x", p.windowSpace.x - (p.canvasSpace.x - b.x) * zoom, slack);
    const ry = narrow("y", p.windowSpace.y - (p.canvasSpace.y - b.y) * zoom, slack);
    const left = Math.round((rx.lo + rx.hi) / 2);
    const top = Math.round((ry.lo + ry.hi) / 2);
    return { at: p, zoom, view: b, left, origin: top, off: Math.max(rx.hi - rx.lo, ry.hi - ry.lo) / 2 + 1,
             top: top + CANVAS_GAP, bottom: top + Math.round(b.height * zoom) - CANVAS_GAP };
  } catch (e) {
    return null;
  }
}

function fitWindow(ask) {
  const want = Math.min(Math.round(Number(ask.height)), HEIGHT_MAX);
  if (!(want > 0)) return;
  const width = Math.max(WIDTH_MIN, Math.min(UI_WIDTH, Math.round(Number(ask.width)) || UI_WIDTH));
  const edges = canvasEdges();
  let height = want;
  if (edges && typeof figma.ui.reposition === "function") height = placeWindow(edges, want, width, !!ask.corner);
  else if (edges) {  // a Figma that can't move it: the window ends at the canvas's bottom
    height = Math.max(HEIGHT_MIN, Math.min(want, Math.floor(edges.bottom - edges.at.windowSpace.y - FIGMA_HEADER)));
  }
  if (height !== placed.height || width !== placed.width) {
    placed.height = height;
    placed.width = width;
    figma.ui.resize(width, height);
  }
}

// Puts the window where `want` px of islands fit, and gives the height they get there.
// `corner`: the window glides from one size to another about its bottom right corner.
function placeWindow(edges, want, width, corner) {
  // Where the window is: where it was put, unless it is further from there than
  // Figma may put it (`edges.off`), when the user moved it.
  const seen = edges.at.windowSpace;
  const ours = placed.pos && Math.abs(seen.x - placed.pos.x) <= edges.off && Math.abs(seen.y - placed.pos.y) <= edges.off;
  let here = ours ? placed.pos : seen;
  if (!ours) {  // the user moved it, or this is the first ask
    placed.spot = { x: here.x, y: here.y };
    placed.hidden = Math.max(0, Math.round(here.y + FIGMA_HEADER + (placed.height || want) - edges.bottom));
  }
  if (width !== placed.width || corner) {
    // Minimized or restored: the window keeps its bottom right corner, so the
    // button that did it stays under the pointer, over the layers too. Only
    // Figma's own window stops it, at its left edge (window space starts there).
    const bottom = Math.min(here.y + FIGMA_HEADER + (placed.height || want), edges.bottom);
    placed.spot = { x: Math.max(0, here.x + placed.width - width),
                    y: Math.max(Math.min(here.y, edges.top), bottom - FIGMA_HEADER - want) };
    placed.hidden = 0;
  }
  const spot = placed.spot;
  const place = (e) => {
    const floor = e.bottom + placed.hidden;  // where the window may end
    const top = Math.min(spot.y, e.top);     // and how high it may go
    return { x: spot.x, y: Math.max(top, Math.min(spot.y, Math.floor(floor - FIGMA_HEADER - want))), floor, top };
  };
  let to = place(edges);
  if ((to.y !== here.y || to.x !== here.x) && edges.off > 2) {
    // The canvas's corner is known only roughly: the window goes there as well
    // as can be told, which says where the canvas is, and its place is worked
    // out again. All in one go, so Figma shows only where it ends.
    moveTo(edges, to.x, to.y, 1);
    edges = canvasEdges() || edges;
    here = edges.at.windowSpace;
    to = place(edges);
  }
  if (to.y !== here.y || to.x !== here.x) moveTo(edges, to.x, to.y, 2);
  placed.pos = { x: to.x, y: to.y };
  const room = Math.max(HEIGHT_MIN, Math.floor(to.floor - to.top - FIGMA_HEADER));
  if (room !== placed.room) {
    placed.room = room;
    figma.ui.postMessage({ type: "room", height: room });
  }
  return Math.max(HEIGHT_MIN, Math.min(want, Math.floor(to.floor - to.y - FIGMA_HEADER)));
}

// Puts the window's corner at x, y in window space. reposition takes the
// canvas's coordinates, and getPosition sees the window there at once (checked
// live): its corner in whole pixels, with the canvas point it was given, says
// where the canvas starts to within a pixel, and a second try puts the window
// on the very pixel asked for.
function moveTo(edges, x, y, tries) {
  const b = edges.view, zoom = edges.zoom;
  let left = edges.left, top = edges.origin;
  for (let i = 0; i < tries; i++) {
    const cx = b.x + (x - left) / zoom, cy = b.y + (y - top) / zoom;
    figma.ui.reposition(cx, cy);
    const p = figma.ui.getPosition();
    if (!p || !p.windowSpace) return;
    // further off than the canvas's corner can be: Figma put it elsewhere, nothing to learn
    if (Math.abs(p.windowSpace.x - x) > edges.off + 1 || Math.abs(p.windowSpace.y - y) > edges.off + 1) return;
    const rx = narrow("x", p.windowSpace.x - (cx - b.x) * zoom, 0.75);
    const ry = narrow("y", p.windowSpace.y - (cy - b.y) * zoom, 0.75);
    if (p.windowSpace.x === x && p.windowSpace.y === y) return;
    left = Math.round((rx.lo + rx.hi) / 2);
    top = Math.round((ry.lo + ry.hi) / 2);
  }
}

figma.ui.onmessage = async (msg) => {
  if (msg.type === "need-identity") { postIdentity(); return; }
  if (msg.type === "open-url") {
    // Only the project's GitHub pages (a release, the notes, a new issue) and Figma's help (the
    // token's article). A bridge from before the repository moved sends its old address, which
    // GitHub only redirects: the window opens the new one.
    const url = typeof msg.url === "string" ? msg.url.replace(
      /^https:\/\/github\.com\/artem-levchenko-2\/figaro\//, "https://github.com/artem-levchenko/figaro/") : "";
    if (url.startsWith("https://github.com/artem-levchenko/figaro/") || url.startsWith("https://help.figma.com/")) {
      figma.openExternal(url);
    }
    return;
  }
  if (msg.type === "resize") { fitWindow(msg); return; }
  if (msg.type === "view") { await keepView(msg.view); return; }
  if (msg.type === "select") { await selectLayer(msg.id, msg.ids); return; }
  if (msg.type === "abort") {
    if (msg.id) markAborted(msg.id);
    const fail = GATES.get(msg.id);  // a script stuck in a Figma call ends now
    if (fail) fail();
    return;
  }
  // Undo the last script and hot reload — see below.
  if (msg.type === "undo") { await answerUndo(msg); return; }
  if (msg.type === "reload") { reloadPlugin(msg); return; }
  if (msg.type !== "exec") return;
  const { id, code } = msg;
  // A script that may write starts only once what Figma updated after a read
  // before it has been reported (afterRead), or FLUSH_MS after the last read;
  // its time (--timeout) starts then, and the bridge allows a second more for
  // the reply.
  const writes = msg.quick === true ? null : writesOf(code, msg.libs);
  const budget = typeof msg.timeout === "number" ? msg.timeout * 1000 : Infinity;
  let shared = false;
  if (writes && writes.any && AFTER_READ.timer !== null) {
    const reads = AFTER_READ.ended;
    await afterRead(id, budget);
    if (ABORTED.has(id)) {  // the bridge's abort (Stop) came while it waited: the script never starts
      ABORTED.delete(id);
      figma.ui.postMessage({ type: "error", id, text: ABORTED_TEXT, stack: null });
      return;
    }
    // A --parallel read ended while it waited, or reads kept the window open
    // past AFTER_READ_MAX: what Figma reports next may be their doing, so its
    // report is shared, as with scripts that ran at the same time.
    shared = AFTER_READ.ended !== reads || AFTER_READ.timer !== null;
  }
  // When the bridge gives up on this run. Checked by h.ck() against the
  // sandbox's own clock, because the bridge's `abort` message can't get in
  // while a loop only awaits Figma APIs: those promises settle as microtasks,
  // so the plugin never returns to its message queue until the loop ends.
  const deadline = Date.now() + budget;

  const logs = [];
  // print() lines go to the bridge in batches, not one message per line: each
  // message crosses two hops (sandbox -> UI -> WebSocket), and 20k separate
  // lines took ~0.5 s. They are still streamed (flushed every 200 lines or
  // 100 ms), so a script that times out still returns the logs it got to.
  let unsent = [];
  let flushTimer = null;
  const flushLogs = () => {
    if (flushTimer !== null) { clearTimeout(flushTimer); flushTimer = null; }
    if (unsent.length) {
      figma.ui.postMessage({ type: "log", id, lines: unsent });
      unsent = [];
    }
  };
  const print = (...args) => {
    const text = args.map(printable).join(" ");
    logs.push(text);
    unsent.push(text);
    if (unsent.length >= 200) flushLogs();
    else if (flushTimer === null) flushTimer = setTimeout(flushLogs, 100);
  };

  // Per-exec helper view: h.ck() throws once the bridge has given up on this
  // run (its timeout passed, or it sent `abort`), so chunked sweeps stop
  // instead of mutating under the next caller.
  const h = Object.create(HELPERS);
  h.aborted = () => ABORTED.has(id) || Date.now() > deadline;
  h.ck = () => {
    if (h.aborted()) throw new Error(ABORTED_TEXT);
    return true;
  };

  // Figma's reports about this run's edits are collected into `run`. A
  // writing script becomes one Cmd+Z step; a read-only one is rolled back if
  // it changed anything. A checkpoint is a version in the file's history. A
  // script whose code only reads answers the moment it ends: it has no edits
  // of its own to wait for, and what Figma reports meanwhile is not its doing.
  // A script that may write waits for Figma's report (~150 ms). A quick run —
  // the CLI's own readers, which change nothing — skips all that.
  const run = writes ? startRun(id, { agent: msg.agent, readOnly: msg.readOnly === true, writes, shared }) : null;
  if (run) figma.commitUndo();  // whatever came before is not this run's step
  const checkpoint = run && msg.checkpoint ? await saveCheckpoint(msg.checkpoint) : null;

  CURRENT_PRINT = print;
  let ok = false;
  let result, error, shifts = null;
  const libErrors = { missing: [] };
  const gate = makeGate(id, deadline);
  try {
    const fig = scriptFigma(h.aborted, print, gate);
    h[SCRIPT_FIGMA] = fig;  // a helper's import by key ends at the deadline too
    h[SCRIPT_IMAGES] = scriptImages(msg.images);
    const guard = (name) => loopGuard(id, deadline, name);
    const lib = loadLibs(msg.libs, fig, print, h, libErrors, guard);  // --lib files
    const script = compileScript(code);
    shifts = script.shifts;
    result = await script.fn(fig, print, h, lib, guard(null));
    ok = true;
  } catch (e) {
    error = e;
  } finally {
    CURRENT_PRINT = () => {};
    gate.close();
  }
  ABORTED.delete(id);
  const outcome = run ? await finishRun(run) : { fields: {}, error: null };
  if (libErrors.missing.length) outcome.fields.libMissing = libErrors.missing;
  if (checkpoint) outcome.fields.checkpoint = checkpoint;
  flushLogs();
  if (ok && !outcome.error) {
    figma.ui.postMessage(Object.assign(resultMessage(id, result, logs), outcome.fields));
  } else {
    const reply = ok ? { type: "error", id, text: outcome.error, stack: null }
                     : errorMessage(id, error, code, shifts);
    if (!ok && outcome.error) reply.text += " — " + outcome.error;
    figma.ui.postMessage(Object.assign(reply, outcome.fields));
  }
};

// ─── exec core ───────────────────────────────────────────────────────────
// What Figma reports about the document while each script runs: so a reply can
// say what the script changed, a read-only script can be rolled back and
// `undo` can tell whether anything else changed since. Figma sends
// `documentchange` in a batch ~100 ms after an edit — but in a background
// window Chromium slows that to about once a minute, which is why ui.html keeps
// the window awake while agents work. A script that may write waits for its
// batch (`settle`); one whose code only reads does not (`finishRun`), and the
// next one that may write waits out what Figma reports after a read
// (`afterRead`).

const FLUSH_MS = 150;    // a batch comes ~100 ms after an edit while Figma is awake
const ITEMS_MAX = 30;    // nodes listed by name in a reply
const HISTORY_MAX = 50;
const OUTSIDE_MAX = 200;

const RUNS = new Map();  // id -> run: scripts and undos in flight, until Figma has reported them
const HISTORY = [];      // runs that changed the file, newest last — what `undo` walks back
const OUTSIDE = { local: 0, remote: 0, recent: [] };  // changes made while no run was open

// Variable edits never reach documentchange, and neither does removing a
// component or a component set. A script that may make them
// is kept in HISTORY even when Figma reported nothing, so `undo` stops there
// instead of reverting the wrong step.
const VARIABLE_WRITES = /\b(setValueForMode|createVariable|createVariableCollection|addMode|removeMode|renameMode|setVariableCodeSyntax|removeVariableCodeSyntax)\b|\.\s*remove\s*(?:\?\.\s*)?\(\s*\)/;

// Figma reports every local change made while a script runs without saying
// who made it: Figma's own updates (in a large file that was just opened) and
// the user's edits come as the script's. A script whose code only reads cannot
// have made them, so they count as made in Figma itself: -R rolls nothing back
// for them — the undo would take back someone else's step, and the plugin has
// no redo — and `undo` neither takes them for a script's step nor reverts past
// them. The code is read as text: it may write when it assigns to anything but
// a slot of its own objects and arrays (out.count, rows[i] — not rows[i].name),
// calls a method that writes, an h.* helper that writes or a figma method
// beyond those that only read, passes figma or h on, or runs --lib files.
// Importing by key reads: it brings a library's component, style or variable
// in for the script to use and changes no layer. Counted as a write, it let -R
// roll back changes Figma made meanwhile as the script's.
// Read wrong one way, a script keeps the rollback; the other way, a change it
// did make stays, reported — so whatever is unclear counts as a write.
// A script that only reads does not wait for Figma's report either: it answers
// when it ends, and what Figma updates after that is counted as made in Figma
// itself (OUTSIDE), never as the next script's change (afterRead).
const FIGMA_READS = /^(?:get\w*|load\w*|import\w*ByKeyAsync|listAvailableFontsAsync|notify|on|once|off|base64Encode|base64Decode|setCurrentPageAsync|saveVersionHistoryAsync|commitUndo)$/;
const HELPER_READS = /^(?:node|resolve|sel|find|findByName|findAllByName|dumpTree|link|inspect|shot|hex|solid|fonts|withFonts|fa|variantsOf|var_|importComp|importVar|annotations|audit|ck|aborted)$/;
// Methods that change the file, called or bound on anything; a call by a
// computed name; code that runs code.
const WRITE_CALLS = /\.\s*(?:set(?!CurrentPageAsync\b|Timeout\b|Interval\b)[A-Z]\w*|create[A-Z]\w*|(?:add|edit|delete|remove|reset|clear)[A-Z]\w*|remove|appendChild|insertChild|resize|resizeWithoutConstraints|rescale|clone|detachInstance|swapComponent|outlineStroke|insertCharacters|(?:lock|unlock)AspectRatio|group|ungroup|flatten|union|subtract|intersect|exclude|combineAsVariants|moveLocal\w*|triggerUndo|renameMode)\s*(?:\?\.\s*)?(?:\(|\.\s*(?:call|apply|bind)\b)|\]\s*\(|\b(?:eval|Function|import)\s*\(|(?:^|[;{}\n])\s*with\s*\(|\bReflect\s*\.|\bObject\s*\.\s*(?:defineProperty|defineProperties|setPrototypeOf)\b/;
const ASSIGN_OP = "(?:(?:[-+*/%&|^]|\\*\\*|<<|>>>?|&&|\\|\\||\\?\\?)?=(?![=>])|\\+\\+|--)";
// Not the file: the page on show, the selection, the view, a speed-up for reads.
const NOT_THE_FILE = /^figma\.(?:currentPage|skipInvisibleInstanceChildren|viewport\..+)$|\.selection$/;
const OWN_CALL = /^\s*(?:new\s+(?:Map|Set|WeakMap|WeakSet|Array|Object)\b|(?:Object\s*\.\s*(?:create|fromEntries)|Array\s*\.\s*from|JSON\s*\.\s*parse|structuredClone)(?=\s*\())/;
const KEYWORD_BEFORE = /(?:^|[^\w$])(?:const|let|var|return|typeof|void|delete|await|yield|in|of|new|case|throw|else|do)$/;
const MEMBER = /[\w$)\]]\s*(?:\??\.\s*[A-Za-z_$]|\[)/;  // x.y or x[k] inside a pattern

// What a script's code may do to the file, read once: `variables` (edits that
// Figma doesn't report) and `any` (it may write at all). An undo has no code,
// and does write.
function writesOf(code, libs) {
  if (code === undefined) return { variables: false, any: true };
  const variables = VARIABLE_WRITES.test(codeOnly(code));
  return { variables, any: variables || codeMayWrite(code, libs) };
}

function codeMayWrite(code, libs) {
  if (Array.isArray(libs) && libs.length) return true;
  const src = codeOnly(code);
  if (VARIABLE_WRITES.test(src) || WRITE_CALLS.test(src)) return true;
  let m;
  const figmaCall = /\bfigma\s*\??\.\s*([A-Za-z_$][\w$]*)\s*(?:\?\.\s*)?\(/g;
  while ((m = figmaCall.exec(src))) if (!FIGMA_READS.test(m[1])) return true;
  const helperCall = /\bh\s*\??\.\s*([A-Za-z_$][\w$]*)\s*(?:\?\.\s*)?\(/g;
  while ((m = helperCall.exec(src))) if (!HELPER_READS.test(m[1])) return true;
  const { own, bound } = bindings(src);
  for (const name of ["figma", "h"]) {  // passed on: what is called on it under another name is unknown
    const passed = new RegExp("(?:^|[^\\w$.])" + name + "[ \\t]*(?=[\\n,;)\\]}]|$)");
    if (!bound.has(name) && passed.test(src)) return true;
  }
  const assign = /\bObject\s*\.\s*assign\s*\(\s*(?:([A-Za-z_$][\w$]*)\s*(?=[,)])|([{[]))?/g;
  while ((m = assign.exec(src))) if (!m[2] && !(m[1] && own.has(m[1]))) return true;
  const write = new RegExp("(\\.\\s*[A-Za-z_$][\\w$]*|\\])\\s*" + ASSIGN_OP, "g");
  while ((m = write.exec(src))) {
    const end = m.index + m[1].length;
    const target = assignTarget(src, end);
    if (target === null) {  // [a, b] = …: sets a property only if it names one
      if (MEMBER.test(src.slice(groupStart(src, end - 1), end))) return true;
      continue;
    }
    if (!ownSlot(target, own) && !NOT_THE_FILE.test(target.replace(/\s+/g, ""))) return true;
  }
  const objectPattern = /\}\s*=(?![=>])/g;  // ({ a: n.name } = …)
  while ((m = objectPattern.exec(src))) {
    const start = groupStart(src, m.index);
    if (start < 0) return true;
    const declared = /(?:^|[^\w$])(?:const|let|var)\s*$/.test(src.slice(Math.max(0, start - 8), start));
    if (!declared && MEMBER.test(src.slice(start, m.index + 1))) return true;
  }
  const before = /(?:\+\+|--)\s*([A-Za-z_$][\w$]*)(?=\s*(?:\??\.|\[))/g;  // ++x.y
  while ((m = before.exec(src))) {
    let k = m.index;
    while (k > 0 && /[ \t]/.test(src[k - 1])) k--;
    if (/[\w$)\]]/.test(src[k - 1] || "")) continue;  // x++ on the same line: it belongs to x
    const start = m.index + m[0].length - m[1].length;
    if (!ownSlot(src.slice(start, chainEnd(src, start + m[1].length)), own)) return true;
  }
  return false;
}

// The code without its comments, strings and regular expressions, which say
// nothing about what it does: a comment becomes a space, the others "". A
// template keeps the code of its ${…} parts, in brackets: that code runs.
function codeOnly(code) {
  const s = String(code || "");
  const parts = [];
  let tail = "";  // the end of the code so far, spaces squeezed: what a slash comes after
  const add = (text) => {
    parts.push(text);
    tail = (tail + text.slice(-64)).replace(/\s+/g, " ").slice(-24);
  };
  let from = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c !== "/" && c !== '"' && c !== "'" && c !== "`") continue;
    const d = s[i + 1];
    let end, text = '""';
    if (c === "/" && d === "/") {
      end = s.indexOf("\n", i);
      if (end < 0) end = s.length;
      text = " ";
    } else if (c === "/" && d === "*") {
      end = s.indexOf("*/", i + 2);
      end = end < 0 ? s.length : end + 2;
      text = " ";
    } else if (c === "`") {
      const t = templateAt(s, i);
      end = t.end;
      if (t.code.length) text = "(" + t.code.map(codeOnly).join(", ") + ")";
    } else if (c !== "/" || regexHere(tail + s.slice(Math.max(from, i - 64), i))) {
      end = literalEnd(s, i);
    } else {
      end = -1;  // a division
    }
    if (end < 0) continue;
    add(s.slice(from, i));
    add(text);
    from = end;
    i = end - 1;
  }
  add(s.slice(from));
  return parts.join("");
}

// codeOnly that keeps every character in its place, for finding code by its
// index: a comment turns into spaces, a string, a regular expression and a
// template's text into "_", and a template's ${…} code stays code. A newline
// stays a newline. It tells a regular expression from a division as codeOnly
// does, by the same text before it.
function codeMask(code) {
  const s = String(code || "");
  const out = s.split("");
  let tail = "";  // as in codeOnly
  const add = (text) => { tail = (tail + text.slice(-64)).replace(/\s+/g, " ").slice(-24); };
  let from = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c !== "/" && c !== '"' && c !== "'" && c !== "`") continue;
    const d = s[i + 1];
    let end, text = '""', fill = "_", t = null;
    if (c === "/" && d === "/") {
      end = s.indexOf("\n", i);
      if (end < 0) end = s.length;
      text = fill = " ";
    } else if (c === "/" && d === "*") {
      end = s.indexOf("*/", i + 2);
      end = end < 0 ? s.length : end + 2;
      text = fill = " ";
    } else if (c === "`") {
      t = templateAt(s, i);
      end = t.end;
      if (t.code.length) text = "(" + t.code.map(codeOnly).join(", ") + ")";
    } else if (c !== "/" || regexHere(tail + s.slice(Math.max(from, i - 64), i))) {
      end = literalEnd(s, i);
    } else {
      end = -1;  // a division
    }
    if (end < 0) continue;
    add(s.slice(from, i));
    add(text);
    for (let k = i; k < end; k++) if (out[k] !== "\n") out[k] = fill;
    if (t) {
      t.code.forEach((part, n) => {
        const inner = codeMask(part);
        for (let k = 0; k < inner.length; k++) out[t.at[n] + k] = inner[k];
      });
    }
    from = end;
    i = end - 1;
  }
  return out.join("");
}

// A slash after an operator, a bracket that opens, a comma or a keyword like
// `return` starts a regular expression; after a name, ) or ] it divides.
function regexHere(before) {
  let k = before.length;
  while (k > 0 && /\s/.test(before[k - 1])) k--;
  const t = before.slice(Math.max(0, k - 12), k);
  if (/(?:\+\+|--)$/.test(t)) return false;
  return k === 0 || /[(,=:[!&|?{};+\-*%<>~^]$/.test(t) ||
    /(?:^|[^\w$.])(?:return|typeof|case|do|else|in|of|new|delete|void|throw|instanceof|yield|await)$/.test(t);
}

// The template that starts at `i`: the index just past it (-1 if it doesn't
// end), and the code of its ${…} parts and where each starts.
function templateAt(s, i) {
  const code = [], at = [];
  for (let j = i + 1; j < s.length; j++) {
    const c = s[j];
    if (c === "\\") { j++; continue; }
    if (c === "`") return { end: j + 1, code, at };
    if (c !== "$" || s[j + 1] !== "{") continue;
    let k = j + 2;
    for (let depth = 1; depth; k++) {
      if (k >= s.length) return { end: -1, code, at };
      const d = s[k];
      if (d === "{") depth++;
      else if (d === "}") depth--;
      else if (d === '"' || d === "'" || d === "`") {
        const e = d === "`" ? templateAt(s, k).end : literalEnd(s, k);
        if (e < 0) return { end: -1, code, at };
        k = e - 1;
      }
    }
    code.push(s.slice(j + 2, k - 1));
    at.push(j + 2);
    j = k - 1;
  }
  return { end: -1, code, at };
}

// The end of the string, template or regular expression that starts at `i`,
// or -1 if it doesn't end (on its own line, but for a template).
function literalEnd(s, i) {
  const q = s[i];
  let inClass = false;
  for (let j = i + 1; j < s.length; j++) {
    const c = s[j];
    if (c === "\\") { j++; continue; }
    if (c === "\n" && q !== "`") return -1;
    if (q !== "/") {
      if (c === q) return j + 1;
    } else if (c === "[") {
      inClass = true;
    } else if (c === "]") {
      inClass = false;
    } else if (c === "/" && !inClass) {
      j++;
      while (j < s.length && /[A-Za-z]/.test(s[j])) j++;  // flags
      return j;
    }
  }
  return -1;
}

// The bracket that closes the one at `open`: the index just past it, or -1.
function groupEnd(src, open) {
  const o = src[open], c = o === "{" ? "}" : o === "[" ? "]" : ")";
  for (let i = open, depth = 0; i < src.length; i++) {
    if (src[i] === o) depth++;
    else if (src[i] === c && --depth === 0) return i + 1;
  }
  return -1;
}

// The bracket that opens the one at `close`: its index, or -1.
function groupStart(src, close) {
  const c = src[close], o = c === "}" ? "{" : c === "]" ? "[" : "(";
  for (let i = close, depth = 0; i >= 0; i--) {
    if (src[i] === c) depth++;
    else if (src[i] === o && --depth === 0) return i;
  }
  return -1;
}

// The end of the member chain from `i` on: .name, [k], (args).
function chainEnd(src, i) {
  for (;;) {
    const m = /^\s*(?:\??\.\s*[A-Za-z_$][\w$]*|[[(])/.exec(src.slice(i, i + 256));
    if (!m) return i;
    if (!/[[(]$/.test(m[0])) {
      i += m[0].length;
      continue;
    }
    const end = groupEnd(src, i + m[0].length - 1);
    if (end < 0) return i;
    i = end;
  }
}

// The target of an assignment that ends at `end`, from its first name on
// ("out.items[0].name"); "" when it starts with an expression
// ((await h.node(id)).name); null for a pattern ([a, b] = …).
function assignTarget(src, end) {
  let i = end;
  for (let first = true; ; first = false) {
    while (i > 0 && /\s/.test(src[i - 1])) i--;
    const c = src[i - 1];
    if (c === "]" || c === ")") {
      i = groupStart(src, i - 1);
      if (i < 0) return "";
      let k = i;
      while (k > 0 && /\s/.test(src[k - 1])) k--;
      if (/[\w$)\]]/.test(src[k - 1] || "") && !KEYWORD_BEFORE.test(src.slice(Math.max(0, k - 8), k))) {
        i = k;
        continue;  // x[k], f(a): the chain goes on
      }
      return first && c === "]" ? null : "";
    }
    if (!/[\w$]/.test(c || "")) return "";
    let j = i;
    while (j > 0 && /[\w$]/.test(src[j - 1])) j--;
    let k = j;
    while (k > 0 && /\s/.test(src[k - 1])) k--;
    if (src[k - 1] !== ".") return src.slice(j, end);
    i = src[k - 2] === "?" ? k - 2 : k - 1;
  }
}

// out.count, counts[n.type]: a slot of the script's own object. Not
// rows[0].name: what an array or a map of its own holds may be a layer.
function ownSlot(target, own) {
  const base = (/^[A-Za-z_$][\w$]*/.exec(target) || [""])[0];
  if (!own.has(base)) return false;
  const rest = target.slice(base.length).trim();
  if (/^\.\s*[A-Za-z_$][\w$]*$/.test(rest)) return true;
  return rest[0] === "[" && groupEnd(rest, 0) === rest.length;
}

// Whether the value at `i` is the script's own and nothing more: an object or
// array literal, a new Map, Set…, or a copy (JSON.parse, Array.from…) — not
// [node][0], nor Array.from(nodes)[0].
function ownValueAt(src, i) {
  let j;
  const literal = /^\s*[{[]/.exec(src.slice(i, i + 64));
  if (literal) {
    j = groupEnd(src, i + literal[0].length - 1);
  } else {
    const call = OWN_CALL.exec(src.slice(i, i + 64));
    if (!call) return false;
    j = i + call[0].length;
    const args = /^\s*\(/.exec(src.slice(j, j + 16));
    if (args) j = groupEnd(src, j + args[0].length - 1);
  }
  if (j < 0) return false;
  // then the statement ends, or a line that doesn't go on with the value
  const rest = /^[ \t]*(\n\s*)?([\s\S]?)/.exec(src.slice(j, j + 64));
  if (!rest[2] || /[;,)\]}]/.test(rest[2])) return true;
  return !!rest[1] && !/[.([?+\-*/%&|^<>=!`]/.test(rest[2]);
}

// The names the script binds (`bound`), and those of them that only ever
// hold objects and arrays of its own (`own`).
function bindings(src) {
  const own = new Set(), other = new Set();
  let m;
  const decl = /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(=(?![=>]))?/g;
  while ((m = decl.exec(src))) (m[2] && ownValueAt(src, decl.lastIndex) ? own : other).add(m[1]);
  const pattern = /\b(?:const|let|var)\s*([{[])/g;  // const { width: w, height: h } = …
  while ((m = pattern.exec(src))) {
    const open = m.index + m[0].length - 1;
    const end = groupEnd(src, open);
    const text = src.slice(open, end < 0 ? src.length : end).replace(/[A-Za-z_$][\w$]*\s*:/g, " ");
    for (const name of text.match(/[A-Za-z_$][\w$]*/g) || []) other.add(name);
  }
  const params = /\(([^()]*)\)\s*=>|\bfunction\b[^(]*\(([^()]*)\)|([A-Za-z_$][\w$]*)\s*=>|\bcatch\s*\(([^()]*)\)/g;
  while ((m = params.exec(src))) {
    for (const name of (m[1] || m[2] || m[3] || m[4] || "").match(/[A-Za-z_$][\w$]*/g) || []) other.add(name);
  }
  const reset = new RegExp("([A-Za-z_$][\\w$]*)\\s*(" + ASSIGN_OP + ")", "g");  // x = …, x += …
  while ((m = reset.exec(src))) {
    if (/[\w$]/.test(src[m.index - 1] || "")) continue;
    let k = m.index;
    while (k > 0 && /\s/.test(src[k - 1])) k--;
    if (/[\w$.]/.test(src[k - 1] || "")) continue;  // a property, or a declaration
    if (m[2] !== "=" || !ownValueAt(src, reset.lastIndex)) other.add(m[1]);
  }
  for (const name of other) own.delete(name);
  return { own, bound: new Set(Array.from(own).concat(Array.from(other))) };
}

function startRun(id, opts) {
  // `writes` is what writesOf read in the code alone ("addMode" in a comment
  // or a string writes nothing); an undo has no code.
  const writes = opts.writes || writesOf(undefined);
  const run = {
    id, agent: opts.agent || null, readOnly: !!opts.readOnly, quiet: false,
    mayWriteVariables: writes.variables,
    mayWrite: writes.any,
    created: new Set(), createdNodes: new Map(), deleted: new Set(), changed: new Set(),
    styles: 0, props: {}, items: new Map(), itemsTotal: 0, remote: 0,
    overlap: RUNS.size > 0 || opts.shared === true, settled: null, late: false,
  };
  for (const other of RUNS.values()) other.overlap = true;
  RUNS.set(id, run);
  return run;
}

function onDocumentChange(event) {
  // A change no open run can have made — none is open, or their code only
  // reads — was made in Figma itself: by Figma, or by someone in the file.
  let scripted = false;
  for (const run of RUNS.values()) if (run.mayWrite) scripted = true;
  if (!scripted) {
    const now = Date.now();
    for (const c of event.documentChanges) {
      if (c.origin === "REMOTE") OUTSIDE.remote++; else OUTSIDE.local++;
      OUTSIDE.recent.push({ at: now, origin: c.origin, type: c.type, id: c.id, name: changeName(c) });
    }
    if (OUTSIDE.recent.length > OUTSIDE_MAX) OUTSIDE.recent.splice(0, OUTSIDE.recent.length - OUTSIDE_MAX);
  }
  // Runs that overlap (parallel reads) each get every change: nobody can tell
  // whose it was, and such a run is marked `overlap`.
  for (const run of RUNS.values()) {
    if (!run.quiet) for (const c of event.documentChanges) record(run, c);
    else if (run.seen) {  // revertSteps: what its undo changed — not what other people did meanwhile
      for (const c of event.documentChanges) if (c.origin !== "REMOTE") run.seen.add(c.id);
    }
    if (run.settled) run.settled();
  }
  readWindowClosed();  // Figma has reported what it did after a read
}

function changeName(c) {
  try {
    if (c.node && !c.node.removed) return c.node.name;
    if (c.style) return c.style.name;
  } catch (e) { /* the node went away meanwhile */ }
  return undefined;
}

function record(run, c) {
  if (c.origin === "REMOTE") { run.remote++; return; }
  run.touched = true;  // Figma saw an edit, so the run has its own Cmd+Z step
  const t = c.type;
  if (t === "CREATE") {
    run.created.add(c.id);
    if (c.node) run.createdNodes.set(c.id, c.node);  // to drop it if it is gone at the end
    note(run, c, "+");
  } else if (t === "DELETE") {
    if (dropCreated(run, c.id)) return;  // made and dropped here
    run.deleted.add(c.id);
    note(run, c, "-");
  } else if (t === "PROPERTY_CHANGE") {
    if (run.created.has(c.id)) return;  // a new node's first properties are part of creating it
    run.changed.add(c.id);
    for (const p of c.properties || []) run.props[p] = (run.props[p] || 0) + 1;
    note(run, c, "~", c.properties);
  } else if (t.indexOf("STYLE_") === 0) {
    run.styles++;
    note(run, c, t === "STYLE_CREATE" ? "+" : t === "STYLE_DELETE" ? "-" : "~", c.properties, true);
  }
}

function note(run, c, op, props, style) {
  const item = run.items.get(c.id);
  if (item) {
    if (props) item.props = Array.from(new Set(item.props.concat(props)));
    return;
  }
  run.itemsTotal++;
  if (run.items.size >= ITEMS_MAX) return;
  run.items.set(c.id, {
    id: c.id, op, name: changeName(c),
    type: style ? "STYLE" : (c.node && c.node.type) || undefined,
    props: props ? props.slice() : [],
  });
}

// A layer the script made and then removed is no change. Figma reports the
// removal of the topmost one only, so a layer that went with its new parent is
// found by its `removed` flag at the end.
function dropCreated(run, id) {
  if (!run.created.delete(id)) return false;
  run.createdNodes.delete(id);
  run.items.delete(id);
  run.itemsTotal--;
  return true;
}

function dropRemovedCreated(run) {
  for (const [id, node] of Array.from(run.createdNodes)) {
    let gone = false;
    try { gone = node.removed === true; } catch (e) { gone = true; }
    if (gone) dropCreated(run, id);
  }
}

function ownCount(run) {
  return run.created.size + run.deleted.size + run.changed.size + run.styles;
}

// What changed while a script whose code only reads ran is not its doing: it
// is told in a notice and no longer counted as the script's.
function disown(run) {
  const n = ownCount(run);
  if (!n) return null;
  const names = Array.from(new Set(Array.from(run.items.values()).map((i) => i.name).filter(Boolean)));
  run.created.clear(); run.createdNodes.clear(); run.deleted.clear(); run.changed.clear();
  run.styles = 0; run.props = {}; run.items.clear(); run.itemsTotal = 0; run.touched = false;
  const shown = names.slice(0, 3).join(", ") + (n > Math.min(names.length, 3) ? ", …" : "");
  const list = names.length ? " (" + shown + ")" : "";
  return (run.readOnly ? "read-only: " : "") + n + " changed in the file while the script ran" + list +
    " — not by the script: its code only reads, so Figma itself" + (run.overlap ? ", another script" : "") +
    " or someone in the file made them" + (run.readOnly ? "; nothing rolled back" : "");
}

function summarize(run) {
  const own = ownCount(run);
  if (!own && !run.remote && !run.late) return null;
  const props = Object.keys(run.props).sort((a, b) => run.props[b] - run.props[a]);
  const out = {
    created: run.created.size, deleted: run.deleted.size, changed: run.changed.size,
    styles: run.styles,
    props: props.slice(0, 12).reduce((o, p) => { o[p] = run.props[p]; return o; }, {}),
    items: Array.from(run.items.values()),
  };
  if (run.itemsTotal > out.items.length) out.more = run.itemsTotal - out.items.length;
  const top = topCreated(run);
  if (top.list.length) out.top = top.list;
  if (top.more) out.topMore = top.more;
  if (run.remote) out.remote = run.remote;
  if (run.late) out.late = true;
  if (run.overlap) out.shared = true;
  return out;
}

// The new layers that are not inside other new ones — what a report links
// to. A component set comes last of all it holds, so the first new layer is
// usually a text deep inside it.
const TOP_MAX = 5;

function topCreated(run) {
  const list = [];
  let more = 0;
  for (const id of run.created) {
    const node = run.createdNodes.get(id);
    let parent = null;
    try { parent = node ? node.parent : null; } catch (e) { parent = null; }
    if (parent && run.created.has(parent.id)) continue;
    if (list.length >= TOP_MAX) { more++; continue; }
    let name, type;
    try { name = node ? node.name : undefined; type = node ? node.type : undefined; } catch (e) { /* gone */ }
    list.push({ id, name, type });
  }
  return { list, more };
}

// Wait for Figma's report on this run: the first batch after the script ended
// holds every edit it made, and no batch within FLUSH_MS means there were none.
// A timer that fires far too late means Figma was throttled after all, and the
// report may still be on its way. Only a run that may write waits: a script
// whose code only reads has no edits to be reported.
function settle(run) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    let timer = null;
    const done = () => { clearTimeout(timer); run.settled = null; resolve(); };
    run.settled = done;
    timer = setTimeout(() => {
      if (Date.now() - t0 > FLUSH_MS * 4) run.late = true;
      done();
    }, FLUSH_MS);
  });
}

// What Figma updates by itself after a script that reads — in a large file,
// hundreds of instances on a page the script loaded for the first time — is
// reported in a batch a moment after the script's answer. That script answers
// without waiting for it, so the report is still on its way when the next
// script starts; one that may write would take it for its own changes: in its
// report, its -R rollback, its step for `undo`, the Reviewer's list. So the
// read opens a window (`readEnded`) that closes with the first batch after it,
// or FLUSH_MS after the last read, and a script that may write starts after it
// (`afterRead`). Reads never wait for it, nor for each other. A batch that
// comes after the answer belongs to no run: it counts as made in Figma itself
// (OUTSIDE).
const AFTER_READ = { timer: null, waiting: new Set(), ended: 0 };  // open while `timer` is set
// --parallel reads, one after another, keep the window open: a script that
// may write waits no longer than this for them (the bridge allows it a second).
const AFTER_READ_MAX = 3 * FLUSH_MS;

function readEnded() {
  AFTER_READ.ended++;  // reads so far: a writer that waits sees whether one ended meanwhile
  if (AFTER_READ.timer !== null) clearTimeout(AFTER_READ.timer);
  AFTER_READ.timer = setTimeout(readWindowClosed, FLUSH_MS);
}

function readWindowClosed() {
  if (AFTER_READ.timer === null) return;
  clearTimeout(AFTER_READ.timer);
  AFTER_READ.timer = null;
  const waiting = Array.from(AFTER_READ.waiting);
  AFTER_READ.waiting.clear();
  for (const wake of waiting) wake();
}

// Wait while the window after a read is open — at most AFTER_READ_MAX, and not
// past the script's time (`budget`, ms); the bridge's abort ends the wait at once.
function afterRead(id, budget) {
  return new Promise((resolve) => {
    const wake = () => {
      clearTimeout(cap);
      AFTER_READ.waiting.delete(wake);
      if (GATES.get(id) === wake) GATES.delete(id);
      resolve();
    };
    const cap = setTimeout(wake, Math.min(AFTER_READ_MAX, budget));
    AFTER_READ.waiting.add(wake);
    GATES.set(id, wake);  // `abort` calls it, as it does a script's gate
  });
}

async function finishRun(run) {
  if (!run.readOnly) figma.commitUndo();  // the whole script is one Cmd+Z step
  if (run.mayWrite) await settle(run);
  else readEnded();
  dropRemovedCreated(run);
  const out = { fields: {}, error: null };
  const elsewhere = run.mayWrite ? null : disown(run);
  if (elsewhere) out.fields.notice = elsewhere;
  const own = ownCount(run);
  const changes = summarize(run);
  if (changes) out.fields.changes = changes;
  if (run.readOnly && own === 0 && !run.touched && run.mayWriteVariables) {
    out.fields.notice = "read-only: the code may have changed variables or removed components (remove()) — " +
      "Figma does not report that, so -R cannot roll it back; check the file";
  }
  if (run.readOnly && own > 0) {
    if (run.overlap) {
      out.fields.notice = "read-only: " + own + " changed in the file during the script — other " +
        "scripts ran at the same time, so whose is unknown; nothing rolled back";
    } else {
      // Commit first: an undo with edits still pending also reverts the step
      // before them.
      figma.commitUndo();
      const trace = traceOf(run);
      run.quiet = true;  // the reversal is not news
      const back = await revertSteps(run, trace);
      if (back.foreign) {
        out.error = "read-only: " + own + " changed in the file during the script, and the undo meant to " +
          "roll that back " + FOREIGN_UNDO;
      } else {
        out.fields.rolledBack = true;
        out.error = "read-only: the script changed " + own + " — rolled back" + leftNote(back, trace);
      }
    }
  }
  RUNS.delete(run.id);
  if (!run.readOnly && (own > 0 || run.touched || run.mayWriteVariables)) {
    HISTORY.push({
      id: run.id, agent: run.agent, at: Date.now(), own, touched: !!run.touched,
      brief: changes ? briefChanges(changes) : run.touched ? "nothing: layers made and removed" : "no visible changes",
      outsideAtEnd: OUTSIDE.local, trace: traceOf(run),
    });
    if (HISTORY.length > HISTORY_MAX) HISTORY.shift();
  }
  return out;
}

function briefChanges(c) {
  const parts = [];
  if (c.created) parts.push("+" + c.created);
  if (c.deleted) parts.push("−" + c.deleted);
  if (c.changed) parts.push("~" + c.changed);
  if (c.styles) parts.push("styles " + c.styles);
  return parts.join(" ") || "nothing";
}

function agoText(at) {
  const s = Math.round((Date.now() - at) / 1000);
  return s < 60 ? s + " s ago" : Math.round(s / 60) + " min ago";
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ─── the script ──────────────────────────────────────────────────────────

// A script runs on Figma's main thread, so a loop that never ends would
// freeze Figma, and the bridge's abort couldn't get in. Every `while`,
// `do … while` and `for (;;)` of a script or a --lib file asks a guard each
// time round: it counts down, and every so often looks at the clock and
// throws once the script's --timeout has run out or the bridge has given up
// on it, naming the loop's line. for … of, for … in and for await go over
// what they are given and stay as they are. The loop heads are found on
// codeMask, so strings, comments and templates are left alone, and the guard
// adds no line, so error lines don't move; the columns it shifts are put back
// (unshift). Should the guards break the code (an object's method named
// `while`, say), it runs as it is.
const GUARD_N = "$figaroN" + RUN_ID;  // names a script can't know beforehand
const GUARD_G = "$figaroG" + RUN_ID;
const GUARD_STEP_MAX = 1024;          // rounds between two looks at the clock, at most

// The code with a guard in the head of each loop, and per line the columns it
// moves: {code, shifts}, shifts null if there is no loop to guard.
function guardLoops(code) {
  const src = String(code);
  const mask = codeMask(src);
  const breaks = [];  // where each line ends
  for (let k = src.indexOf("\n"); k !== -1; k = src.indexOf("\n", k + 1)) breaks.push(k);
  const lineAt = (at) => {
    let lo = 0, hi = breaks.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (breaks[mid] < at) lo = mid + 1;
      else hi = mid;
    }
    return lo + 1;
  };
  // --N gets down to 0 every `step` rounds, and G(line) gives the next step,
  // never 0. When G throws into a try of the script's around an inner loop, N
  // stays at 0 and goes below it, and every round after asks G again.
  const guard = (at) => "(--" + GUARD_N + " > 0 || (" + GUARD_N + " = " + GUARD_G + "(" + lineAt(at) + ")))";
  const ins = [];  // [index, text]
  const head = /(^|[^\w$#.\u0080-\uffff])(while|for)\s*(await\b)?\s*\(/g;  // not in a name (éwhile)
  let m;
  while ((m = head.exec(mask))) {
    const open = m.index + m[0].length - 1;
    head.lastIndex = open + 1;
    if (m[3]) continue;  // for await
    // x.while(…) is a call; get/set/static/async/* while(…) {…} a method
    let k = m.index + m[1].length;
    while (k > 0 && /\s/.test(mask[k - 1])) k--;
    if (mask[k - 1] === "." || mask[k - 1] === "*") continue;
    let w = k;
    while (w > 0 && /[\w$]/.test(mask[w - 1])) w--;
    if (/^(?:get|set|static|async)$/.test(mask.slice(w, k))) continue;
    const close = groupEnd(mask, open);
    if (close < 0) continue;
    if (m[2] === "while") {
      if (!mask.slice(open + 1, close - 1).trim()) continue;
      ins.push([open + 1, guard(open + 1) + " && ("], [close - 1, ")"]);
      continue;
    }
    // for (a; b; c): the two semicolons outside any brackets
    const semi = [];
    for (let j = open + 1, depth = 0; j < close - 1; j++) {
      const c = mask[j];
      if (c === "(" || c === "[" || c === "{") depth++;
      else if (c === ")" || c === "]" || c === "}") depth--;
      else if (c === ";" && depth === 0) semi.push(j);
    }
    if (semi.length !== 2) continue;  // for … of, for … in
    const [a, b] = semi;
    if (!mask.slice(a + 1, b).trim()) ins.push([a + 1, guard(a + 1)]);
    else ins.push([a + 1, guard(a + 1) + " && ("], [b, ")"]);
  }
  if (!ins.length) return { code: src, shifts: null };
  ins.sort((x, y) => x[0] - y[0]);
  const shifts = {};  // line -> [[column in the code as it was, length], …]
  let out = "", from = 0;
  for (const [at, text] of ins) {
    const line = lineAt(at);
    out += src.slice(from, at) + text;
    from = at;
    (shifts[line] = shifts[line] || []).push([line === 1 ? at : at - breaks[line - 2] - 1, text.length]);
  }
  return { code: out + src.slice(from), shifts };
}

// A column in a stack trace (1-based) of the guarded code, in the code as it
// was. A column inside a guard is the start of the loop's condition.
function unshift(shifts, line, col) {
  const moved = shifts && shifts[line];
  if (!moved) return col;
  const at = col - 1;
  let by = 0;
  for (const [c, len] of moved) {
    if (at < c + by) break;
    if (at < c + by + len) return c + 1;
    by += len;
  }
  return at - by + 1;
}

// The guard of one script's loops (`lib`: of one --lib file in it). It looks
// at the clock about every 10 ms of looping: a fast loop goes more rounds
// between two looks, a slow one fewer.
function loopGuard(id, deadline, lib) {
  let step = 1, last = Date.now();
  return (line) => {
    const now = Date.now();
    if (now > deadline || ABORTED.has(id)) {
      const loop = "the loop on line " + line + (lib ? " of " + lib : "");
      throw new Error(now > deadline
        ? "aborted: " + loop + " was still running when --timeout ran out — the script was stopped"
        : "aborted: " + loop + " was still running when the bridge stopped the script");
    }
    const spent = now - last;
    last = now;
    if (spent < 2) step = Math.min(step * 2, GUARD_STEP_MAX);
    else if (spent > 20) step = Math.max(1, Math.floor(step * 10 / spent));
    return step;
  };
}

// new Function(...params, body(code)) for the guarded code, or for the code as
// it is if the guards break it. {fn, shifts}
function compileGuarded(params, body, code) {
  const guarded = guardLoops(code);
  if (guarded.shifts) {
    try {
      return { fn: new Function(...params, body(guarded.code)), shifts: guarded.shifts };
    } catch (e) {
      // compiled as it is below: it has the error to report, if any
    }
  }
  return { fn: new Function(...params, body(code)), shifts: null };
}

// The script's line 1 starts on its own line, so a trailing `// comment` no
// longer swallows the closing `})();`. `lib` is what the --lib files define.
// Strict mode, on the first line so the lines don't move: a write that can't
// happen (node.fills[0].color = …, a frozen array) fails instead of doing
// nothing, silently. The loops' guard is the last parameter.
const SCRIPT_PARAMS = ["figma", "print", "h", "lib", GUARD_G];

function compileScript(code) {
  return compileGuarded(SCRIPT_PARAMS, (text) =>
    '"use strict"; let ' + GUARD_N + " = 1; return (async () => {\n" + text + "\n})();", code);
}

// A script line in a stack trace: "<input>:L:C" in Figma's sandbox (QuickJS),
// "<anonymous>:L:C" under Node. Where line 1 lands is measured once, for
// whichever engine runs us.
const STACK_POS = /<(?:input|anonymous)>:(\d+):(\d+)/g;
const SCRIPT_LINE_OFFSET = (() => {
  try {
    new Function(...SCRIPT_PARAMS, '"use strict"; return (() => {\nthrow new Error(\'probe\')\n})();')();
  } catch (e) {
    STACK_POS.lastIndex = 0;
    const m = STACK_POS.exec(String(e && e.stack));
    STACK_POS.lastIndex = 0;
    if (m) return Number(m[1]) - 1;
  }
  return null;
})();

// Each script position in the stack, as the script's own line and column.
function scriptPositions(stack, lineCount, shifts) {
  const out = [];
  if (SCRIPT_LINE_OFFSET === null || !stack) return out;
  STACK_POS.lastIndex = 0;
  let m;
  while ((m = STACK_POS.exec(String(stack)))) {
    const line = Number(m[1]) - SCRIPT_LINE_OFFSET;
    if (line >= 1 && line <= lineCount) out.push({ at: m[0], line, col: unshift(shifts, line, Number(m[2])) });
  }
  STACK_POS.lastIndex = 0;
  return out;
}

// What print() writes for a value: an object as JSON, an error as its name
// and text (JSON makes it "{}"), and what JSON can't hold, an object that
// leads back to itself or a BigInt, as well as it can, never failing the script.
function printable(a) {
  const isError = (v) => v instanceof Error || Object.prototype.toString.call(v) === "[object Error]";
  if (isError(a) || a === null || typeof a !== "object") return String(a);
  const plain = (v) => (typeof v === "bigint" || isError(v) ? String(v) : v);
  try {
    return JSON.stringify(a, (key, v) => plain(v), 2);
  } catch (e) {
    const seen = new Set();
    try {
      return JSON.stringify(a, (key, v) => {
        v = plain(v);
        if (v && typeof v === "object") {
          if (seen.has(v)) return "[Circular]";
          seen.add(v);
        }
        return v;
      }, 2);
    } catch (e2) {
      return String(a);
    }
  }
}

function errorText(e) {
  if (e && e.message) return String(e.message);
  if (e && typeof e === "object") {
    return "an error with no text — that is how Figma ends getNodeByIdAsync for an id from another " +
      "file or a deleted node (after ~10 s)";
  }
  return String(e);
}

function errorMessage(id, e, code, shifts) {
  const lines = String(code).split("\n");
  const stack = (e && e.stack) || null;
  const where = scriptPositions(stack, lines.length, shifts);
  const inLib = libPositions(stack);  // where in a --lib file it failed, if it did there
  const msg = { type: "error", id, text: errorText(e), stack: ownFrames(stack, where.concat(inLib)) };
  if (where.length) {
    msg.line = where[0].line;
    msg.col = where[0].col;
    msg.source = lines[where[0].line - 1].trim().slice(0, 200);
  }
  if (inLib.length) {
    const first = inLib[0];
    msg.lib = { name: first.name, line: first.line, source: first.source };
  }
  return msg;
}

// The stack as the script's author reads it: the frames of the script
// ("script:L:C") and of its --lib files ("name.js:L:C"), and no others — not
// the plugin's own (the loop guard that threw, the code that ran the script,
// a deadline's timer) nor Figma's; null when none of them is the script's.
// `own`: those positions, as scriptPositions and libPositions found them, in
// the order of the stack.
function ownFrames(stack, own) {
  if (!own.length) return null;
  const kept = [];
  for (const line of String(stack).split("\n")) {
    STACK_POS.lastIndex = 0;
    const m = STACK_POS.exec(line);
    STACK_POS.lastIndex = 0;
    const w = m && own.find((o) => o.at === m[0] && !o.used);
    if (w) {
      w.used = true;
      kept.push(line.replace(m[0], (w.name || "script") + ":" + w.line + ":" + w.col));
    } else if (!m && !/^\s*at\s/.test(line)) {
      kept.push(line);  // not a frame: V8 starts with the error's text
    }
  }
  for (const o of own) delete o.used;
  return kept.join("\n");
}

// Fonts loaded in this run of the plugin. Figma keeps a loaded font loaded, and
// in a background window every real loadFontAsync in a loop can stall up to a
// minute (the 7th call once took 49 s), so a known font returns at once.
const FONTS = new Map();  // "family|style" -> promise of its load

function loadFont(font) {
  if (!font || typeof font !== "object") return figma.loadFontAsync(font);
  const key = font.family + "|" + font.style;
  let p = FONTS.get(key);
  if (!p) {
    p = figma.loadFontAsync(font);
    FONTS.set(key, p);
    p.catch(() => FONTS.delete(key));
  }
  return p;
}

// The node, null when this file has no such id, or undefined when there is no
// sync lookup (dynamic-page) and only Figma's async one can answer.
function nodeHere(id) {
  try {
    return figma.getNodeById(String(id));
  } catch (e) {
    return undefined;
  }
}

// A view of `target` for scripts. `figma` itself is frozen, so a Proxy over it
// cannot change a thing (QuickJS: "proxy: inconsistent get"); this one forwards
// to it from an empty object. Every *Async call goes through `guard`: it checks
// the deadline first, so a loop over Figma calls ends once the bridge gave up,
// even without h.ck(), and a call that never settles ends at the deadline.
function forward(target, overrides, guard, path, guarded) {
  const bound = new Map();
  return new Proxy({}, {
    get(_, key) {
      if (Object.prototype.hasOwnProperty.call(overrides, key)) return overrides[key];
      const value = target[key];
      if (typeof value !== "function") return value;
      let fn = bound.get(key);
      if (!fn) {
        fn = typeof key === "string" && /Async$/.test(key)
          ? function () { const args = arguments; return guard(() => value.apply(target, args), path + key); }
          : value.bind(target);
        bound.set(key, fn);
      }
      return fn;
    },
    set(_, key, value) {
      if (guarded && guarded.indexOf(key) !== -1) {
        throw new Error("figma." + path + String(key) + " keeps the plugin connected to figaro — leave it alone");
      }
      target[key] = value;
      return true;
    },
    has(_, key) { return key in target; },
  });
}

// What a script gets as `figma`.
function scriptFigma(isAborted, print, gate) {
  const guard = (start, name) => {
    if (isAborted()) throw new Error(ABORTED_TEXT);
    return gate ? gate.race(start(), name) : start();
  };
  const cut = (name) => () => {
    throw new Error("figma." + name + "() is turned off: the plugin keeps the connection to figaro");
  };
  let library = null;
  return forward(figma, {
    getNodeByIdAsync(id) {
      return guard(() => {
        const found = nodeHere(id);  // as in h.node
        if (found === null) {
          print("figma.getNodeByIdAsync(" + JSON.stringify(id) + "): \"" + figma.root.name +
            "\" has no such node — it is from another file or was deleted");
          return Promise.resolve(null);
        }
        return found ? Promise.resolve(found) : figma.getNodeByIdAsync(id);
      }, "getNodeByIdAsync");
    },
    loadFontAsync(font) { return guard(() => loadFont(font), "loadFontAsync"); },
    closePlugin: cut("closePlugin"),
    showUI: cut("showUI"),
    ui: forward(figma.ui, { close: cut("ui.close") }, guard, "ui.", ["onmessage"]),
    variables: forward(figma.variables, {}, guard, "variables."),
    // Lazy — reading figma.teamLibrary needs the manifest's permission
    get teamLibrary() { return library || (library = forward(figma.teamLibrary, {}, guard, "teamLibrary.")); },
  }, guard, "");
}

async function saveCheckpoint(title) {
  const t0 = Date.now();
  try {
    const v = await figma.saveVersionHistoryAsync(String(title).slice(0, 140));
    return { id: v && v.id, title: String(title), ms: Date.now() - t0 };
  } catch (e) {
    return { error: errorText(e), title: String(title) };
  }
}

// ─── undo ────────────────────────────────────────────────────────────────
// Each writing script is one step in Figma's undo stack (commitUndo above), so
// triggerUndo reverts the newest one — whoever made it. It is safe only when
// that step is the script's: it changed something Figma reported, and nothing
// else changed after it. An undo with nothing to revert would revert the step
// before instead.

async function answerUndo(msg) {
  try {
    const value = await undoLast(msg);
    figma.ui.postMessage({ type: "result", id: msg.id, value });
  } catch (e) {
    figma.ui.postMessage({ type: "error", id: msg.id, text: errorText(e), stack: null });
  }
}

async function undoLast(msg) {
  await wait(FLUSH_MS);  // edits made just before this request are still on their way
  const top = HISTORY[HISTORY.length - 1];
  if (!top) {
    throw new Error("undo: nothing to undo — scripts in this run of the plugin changed nothing, " +
      "or they are undone already");
  }
  const who = top.agent || "a script without -A";
  const when = agoText(top.at);
  if (top.own === 0 && !top.touched) {
    throw new Error("undo: Figma showed no changes for the last script (" + who + ", " + when + "), " +
      "but it may have changed variables or removed components — Figma does not report that. " +
      "Undo it by hand: Cmd+Z in Figma");
  }
  const after = OUTSIDE.local - top.outsideAtEnd;
  if (after > 0) {
    const names = OUTSIDE.recent.slice(-after).map((c) => c.name).filter(Boolean);
    const list = Array.from(new Set(names)).slice(0, 5).join(", ");
    throw new Error("undo: after the script of " + who + " (" + when + ") " + after + " more changed in " +
      "the file" + (list ? " (" + list + ")" : "") + " — in Figma itself, not by a script. Undo would " +
      "revert that instead, so nothing is done. Undo by hand: Cmd+Z in Figma, or a version in " +
      "File → Version history");
  }
  if (msg.agent && top.agent && msg.agent !== top.agent && !msg.force) {
    throw new Error("undo: the last changes are by " + top.agent + ", not " + msg.agent +
      ". To undo them anyway, run it again with --force");
  }
  const run = startRun(msg.id, { agent: msg.agent });
  run.quiet = true;  // the reversal is not news
  const trace = top.trace || traceOf(run);
  const back = await revertSteps(run, trace);
  RUNS.delete(run.id);
  HISTORY.pop();
  if (back.foreign) throw new Error("undo: Figma's undo " + FOREIGN_UNDO);
  const value = { undone: { agent: top.agent, ago: when, changes: top.brief }, left: HISTORY.length };
  if (back.steps > 1) value.steps = back.steps;
  const note = leftNote(back, trace);
  if (note) value.warning = note.replace(/^; /, "");
  return value;
}

// Figma closes an undo step of its own inside some calls — each
// addComponentProperty does — so one script can be several
// Cmd+Z steps. Reverting goes back step by step while the script's traces are
// still there: a layer it made exists, or one it removed is missing. Only its
// own steps can hold those, and each step reverted must touch its layers, so
// it does not go past the step the script started from. A first undo that
// touched none of its layers took back someone else's step: what Figma
// reported was not on the undo stack as the script's. The Plugin API has no
// redo, so the reply says how to bring that step back.
const UNDO_STEPS_MAX = 20;
const TRACE_MAX = 500;
const FOREIGN_UNDO = "took back a step that was not the script's (it touched none of its layers) — " +
  "press Cmd+Shift+Z in Figma at once to redo that step, before anything else changes the file";

function traceOf(run) {
  return {
    created: Array.from(run.created).slice(0, TRACE_MAX),
    deleted: Array.from(run.deleted).slice(0, TRACE_MAX),
    changed: Array.from(run.changed).slice(0, TRACE_MAX),
    // properties of components that were there before: no layer to check them by
    props: (run.props.componentPropertyDefinitions || 0) > 0,
    styles: run.styles > 0,  // their undo touches styles, not layers
  };
}

function exists(id) {
  try {
    const n = figma.getNodeById(id);
    return n !== null && n.removed !== true;
  } catch (e) {
    return null;  // no sync lookup (dynamic-page): no evidence either way
  }
}

function traceLeft(trace) {
  let left = 0;
  for (const id of trace.created) if (exists(id) === true) left++;
  for (const id of trace.deleted) if (exists(id) === false) left++;
  return left;
}

async function revertSteps(run, trace) {
  const mine = new Set(trace.created.concat(trace.deleted, trace.changed));
  let steps = 0;
  for (;;) {
    run.seen = new Set();
    figma.triggerUndo();
    steps++;
    await settle(run);
    const left = traceLeft(trace);
    const touched = Array.from(run.seen).some((id) => mine.has(id));
    if (steps === 1 && !touched && run.seen.size > 0 && mine.size > 0 && !trace.styles) {
      return { steps, left, foreign: true };
    }
    if (left === 0 || steps >= UNDO_STEPS_MAX || !touched) return { steps, left };
  }
}

function leftNote(back, trace) {
  if (back.left > 0) {
    return "; not everything was undone: " + back.left + " layers of this script are left — look at " +
      "the file and remove the rest by hand (Cmd+Z in Figma, or a version in File → Version history)";
  }
  if (trace.props) {
    return "; the script changed properties of existing components, and Figma splits such changes " +
      "into several Cmd+Z steps — check with inspect that all of it is undone";
  }
  return "";
}

// ─── links ───────────────────────────────────────────────────────────────
// A Figma link names a file by its key and a layer by node-id ("1-11" there,
// "1:11" here). h.node and h.resolve take a link — or a bare "1-11" — and
// refuse one into another file; h.link makes one for a report. figma_links.py
// does the same on the bridge's side.

const LINK = /^(?:https?:\/\/)?(?:[\w-]+\.)?figma\.com\/(?:design|file|proto|board|slides|deck|make|site)\/([A-Za-z0-9]{10,})(?:\/branch\/([A-Za-z0-9]{10,}))?[^?#]*(?:\?([^#]*))?/i;
const LINK_NODE = /^I?\d+[:-]\d+(?:;\d+[:-]\d+)*$/;

function decoded(s) {
  try { return decodeURIComponent(s); } catch (e) { return s; }
}

// "1-11", "I1-2;3-4", "1%3A11" → "1:11", "I1:2;3:4"; null if not a node id.
function urlNodeId(value) {
  const s = decoded(String(value).trim());
  return LINK_NODE.test(s) ? s.replace(/(\d+)-(\d+)/g, "$1:$2") : null;
}

function parseLink(text) {
  const m = LINK.exec(String(text).trim());
  if (!m) return null;
  const q = /(?:^|&)node-id=([^&]*)/.exec(m[3] || "");
  return { key: m[2] || m[1], node: q ? urlNodeId(q[1]) : null };
}

function fileKey() {
  try { return figma.fileKey || null; } catch (e) { return null; }
}

// A node id from an id, a link-form id or a link into this file.
function linkNodeId(value) {
  if (typeof value !== "string") return value;
  const link = parseLink(value);
  if (!link) return urlNodeId(value) || value;
  const here = fileKey();
  if (here && link.key !== here) {
    throw new Error("the link points to another file (" + link.key + "), but the script runs in \"" +
      figma.root.name + "\" — run it with -T <link>");
  }
  if (!link.node) throw new Error("the link has no node-id: " + value);
  return link.node;
}

function nodeLink(id) {
  const key = fileKey();
  if (!key || !id) return undefined;
  const slug = String(figma.root.name || "").replace(/[^A-Za-z0-9]+/g, "-") || "Untitled";
  return "https://www.figma.com/design/" + key + "/" + slug + "?node-id=" +
    encodeURIComponent(String(id).replace(/:/g, "-"));
}

// ─── inspect ─────────────────────────────────────────────────────────────
// What a design-to-code worker needs from a subtree, as data: layout, sizes,
// paddings, radii, paints, effects, text with its styled segments, and the
// names AND keys of the components, styles and variables it uses — a key is
// what import*ByKeyAsync takes. Hidden layers are counted, not listed. Scripts
// read these keys from `figaro inspect --json`, so keep them stable;
// inspect_text.py prints the same data as text.

const SEGMENTS_MAX = 60;

async function inspectTree(root, opts, ctx) {
  opts = opts || {};
  const maxDepth = opts.depth == null ? 8 : Number(opts.depth);
  const maxNodes = opts.maxNodes == null ? 2000 : Number(opts.maxNodes);
  const withHidden = opts.hidden === true;
  const out = {
    file: figma.root.name, fileKey: fileKey() || undefined, url: nodeLink(root.id),
    node: null, nodes: 0, components: {}, styles: {}, variables: {}, collections: {},
  };
  const varNames = new Map();
  const styleNames = new Map();
  const collections = new Map();
  const r = (v) => Math.round(v * 100) / 100;
  const hex = (c) => "#" + [c.r, c.g, c.b].map((x) => Math.round(x * 255).toString(16).padStart(2, "0")).join("");
  const alpha = (c) => (c.a !== undefined && c.a !== 1 ? "/" + r(c.a) : "");
  const safe = (fn) => { try { return fn(); } catch (e) { return undefined; } };
  const mixed = (v) => v === figma.mixed;

  const collection = async (id) => {
    if (!collections.has(id)) {
      let c = null;
      try { c = await figma.variables.getVariableCollectionByIdAsync(id); } catch (e) { /* gone */ }
      if (c && !out.collections[c.name]) {
        out.collections[c.name] = { key: c.key, remote: c.remote, modes: c.modes.map((m) => m.name) };
      }
      collections.set(id, c);
    }
    return collections.get(id);
  };
  const variable = async (alias) => {
    const id = alias && alias.id;
    if (!id) return undefined;
    if (!varNames.has(id)) {
      let label;
      try {
        const v = await figma.variables.getVariableByIdAsync(id);
        if (v) {
          const c = await collection(v.variableCollectionId);
          label = v.name;
          const same = out.variables[label];
          if (same && same.key !== v.key) label = v.name + " (" + (c ? c.name : "?") + ")";
          out.variables[label] = { id: v.id, key: v.key, type: v.resolvedType,
                                   collection: c ? c.name : undefined, remote: v.remote };
        }
      } catch (e) { /* a library that is gone */ }
      varNames.set(id, label);
    }
    return varNames.get(id);
  };
  const style = async (id) => {
    if (typeof id !== "string" || !id) return undefined;
    if (!styleNames.has(id)) {
      let label;
      try {
        const st = await figma.getStyleByIdAsync(id);
        if (st) {
          label = st.name;
          out.styles[label] = { id: st.id, key: st.key, type: st.type, remote: st.remote };
        }
      } catch (e) { /* gone */ }
      styleNames.set(id, label);
    }
    return styleNames.get(id);
  };
  const paint = async (p) => {
    if (!p || p.visible === false) return null;
    const o = { type: p.type };
    if (p.type === "SOLID") o.color = hex(p.color);
    if (p.opacity !== undefined && p.opacity !== 1) o.opacity = r(p.opacity);
    if (p.type.indexOf("GRADIENT") === 0) {
      o.stops = p.gradientStops.map((s) => hex(s.color) + "@" + r(s.position) + alpha(s.color));
    }
    if (p.type === "IMAGE") o.scaleMode = p.scaleMode;
    if (p.boundVariables && p.boundVariables.color) o.var = await variable(p.boundVariables.color);
    return o;
  };
  const paints = async (list) => {
    if (!Array.isArray(list)) return mixed(list) ? "mixed" : undefined;
    const got = [];
    for (const p of list) { const x = await paint(p); if (x) got.push(x); }
    return got.length ? got : undefined;
  };
  const NOT_VARS = { fills: 1, strokes: 1, effects: 1, layoutGrids: 1, componentProperties: 1, textRangeFills: 1 };
  const boundVars = async (n) => {
    const bv = safe(() => n.boundVariables) || {};
    const o = {};
    for (const k of Object.keys(bv)) {
      if (NOT_VARS[k]) continue;
      const v = bv[k];
      const name = await variable(Array.isArray(v) ? v[0] : v);
      if (name) o[k] = name;
    }
    return Object.keys(o).length ? o : undefined;
  };
  const modes = async (n) => {
    const em = safe(() => n.explicitVariableModes);
    if (!em) return undefined;
    const o = {};
    for (const cid of Object.keys(em)) {
      const c = await collection(cid);
      if (!c) continue;
      const m = c.modes.filter((x) => x.modeId === em[cid])[0];
      o[c.name] = m ? m.name : em[cid];
    }
    return Object.keys(o).length ? o : undefined;
  };
  const swapName = (id) => {
    const c = safe(() => figma.getNodeById(id));
    return c ? c.name : id;
  };
  const component = async (n) => {
    let mc = null;
    try { mc = await n.getMainComponentAsync(); } catch (e) { /* its library is gone */ }
    if (!mc) return undefined;
    const set = mc.parent && mc.parent.type === "COMPONENT_SET" ? mc.parent : null;
    const label = set ? set.name + " / " + mc.name : mc.name;
    if (!out.components[label]) {
      out.components[label] = { id: mc.id, key: mc.key, remote: mc.remote };
      if (set) Object.assign(out.components[label], { set: set.name, setKey: set.key });
    }
    return label;
  };
  const effects = async (list) => {
    if (!Array.isArray(list) || !list.length) return undefined;
    const got = [];
    for (const e of list) {
      if (e.visible === false) continue;
      const x = { type: e.type };
      if (e.radius) x.radius = e.radius;
      if (e.offset) x.offset = [e.offset.x, e.offset.y];
      if (e.spread) x.spread = e.spread;
      if (e.color) x.color = hex(e.color) + alpha(e.color);
      if (e.boundVariables) {
        const v = {};
        for (const k of Object.keys(e.boundVariables)) {
          const name = await variable(e.boundVariables[k]);
          if (name) v[k] = name;
        }
        if (Object.keys(v).length) x.vars = v;
      }
      got.push(x);
    }
    return got.length ? got : undefined;
  };
  const text = async (n, o) => {
    const chars = n.characters;
    o.text = chars.length > 400 ? chars.slice(0, 400) + "…" : chars;
    const st = await style(n.textStyleId);
    if (st) o.textStyle = st;
    const lh = n.lineHeight, ls = n.letterSpacing;
    o.font = {
      family: mixed(n.fontName) ? "mixed" : n.fontName.family + " " + n.fontName.style,
      size: mixed(n.fontSize) ? "mixed" : n.fontSize,
      lineHeight: mixed(lh) ? "mixed" : lh.unit === "AUTO" ? "auto" : r(lh.value) + (lh.unit === "PERCENT" ? "%" : "px"),
      letterSpacing: mixed(ls) ? "mixed" : r(ls.value) + (ls.unit === "PERCENT" ? "%" : "px"),
      align: n.textAlignHorizontal,
    };
    if (n.textCase && n.textCase !== "ORIGINAL" && !mixed(n.textCase)) o.font.case = n.textCase;
    if (n.textAutoResize) o.autoResize = n.textAutoResize;
    if (n.hasMissingFont) o.missingFont = true;
    const link = safe(() => n.hyperlink);
    if (link && !mixed(link)) o.link = link.type === "URL" ? link.value : "node:" + link.value;
    const segs = safe(() => n.getStyledTextSegments(["fontName", "fontSize", "textDecoration",
      "textCase", "fills", "textStyleId", "hyperlink", "listOptions"]));
    if (!segs || segs.length < 2) return;
    o.segments = [];
    for (const s of segs.slice(0, SEGMENTS_MAX)) {
      const x = { text: s.characters.length > 120 ? s.characters.slice(0, 120) + "…" : s.characters,
                  font: s.fontName.family + " " + s.fontName.style, size: s.fontSize };
      const sst = await style(s.textStyleId);
      if (sst) x.style = sst;
      const f = await paints(s.fills);
      if (f) x.fills = f;
      if (s.textDecoration && s.textDecoration !== "NONE") x.decoration = s.textDecoration;
      if (s.textCase && s.textCase !== "ORIGINAL") x.case = s.textCase;
      if (s.hyperlink) x.link = s.hyperlink.type === "URL" ? s.hyperlink.value : "node:" + s.hyperlink.value;
      if (s.listOptions && s.listOptions.type && s.listOptions.type !== "NONE") x.list = s.listOptions.type;
      o.segments.push(x);
    }
    if (segs.length > SEGMENTS_MAX) o.segmentsCut = segs.length - SEGMENTS_MAX;
  };

  const walk = async (n, level) => {
    if (ctx && typeof ctx.aborted === "function" && ctx.aborted()) throw new Error(ABORTED_TEXT);
    out.nodes++;
    const o = { id: n.id, name: n.name, type: n.type };
    if ("width" in n) { o.w = r(n.width); o.h = r(n.height); }
    if (level > 0 && "x" in n) { o.x = r(n.x); o.y = r(n.y); }
    if (n.visible === false) o.visible = false;
    if (n.layoutMode && n.layoutMode !== "NONE") {
      const l = { mode: n.layoutMode };
      if (n.layoutMode === "GRID") {
        Object.assign(l, { rows: n.gridRowCount, cols: n.gridColumnCount,
                           rowGap: n.gridRowGap, colGap: n.gridColumnGap });
      } else {
        Object.assign(l, { gap: n.itemSpacing, main: n.primaryAxisAlignItems,
                           cross: n.counterAxisAlignItems });
        if (n.layoutWrap === "WRAP") { l.wrap = true; l.crossGap = n.counterAxisSpacing; }
      }
      l.pad = [n.paddingTop, n.paddingRight, n.paddingBottom, n.paddingLeft];
      o.layout = l;
    }
    const sh = safe(() => n.layoutSizingHorizontal), sv = safe(() => n.layoutSizingVertical);
    if (sh && sv) o.sizing = sh + "/" + sv;
    if (n.layoutPositioning === "ABSOLUTE") o.absolute = true;
    if ("minWidth" in n) {
      const mm = [n.minWidth, n.maxWidth, n.minHeight, n.maxHeight];
      if (mm.some((x) => x != null)) o.minmax = mm;
    }
    if ("cornerRadius" in n) {
      if (mixed(n.cornerRadius)) {
        o.radius = [n.topLeftRadius, n.topRightRadius, n.bottomRightRadius, n.bottomLeftRadius];
      } else if (n.cornerRadius) o.radius = n.cornerRadius;
    }
    if (n.clipsContent) o.clip = true;
    if ("opacity" in n && n.opacity !== 1) o.opacity = r(n.opacity);
    const fills = await paints(safe(() => n.fills));
    if (fills) o.fills = fills;
    const strokes = await paints(safe(() => n.strokes));
    if (strokes) {
      o.strokes = strokes;
      o.strokeWeight = mixed(n.strokeWeight)
        ? [n.strokeTopWeight, n.strokeRightWeight, n.strokeBottomWeight, n.strokeLeftWeight] : n.strokeWeight;
      o.strokeAlign = n.strokeAlign;
    }
    const fx = await effects(safe(() => n.effects));
    if (fx) o.effects = fx;
    const styles = {};
    for (const pair of [["fill", "fillStyleId"], ["stroke", "strokeStyleId"],
                        ["effect", "effectStyleId"], ["grid", "gridStyleId"]]) {
      const name = await style(safe(() => n[pair[1]]));
      if (name) styles[pair[0]] = name;
    }
    if (Object.keys(styles).length) o.styles = styles;
    const bound = await boundVars(n);
    if (bound) o.vars = bound;
    const md = await modes(n);
    if (md) o.modes = md;
    if (n.type === "TEXT") await text(n, o);
    // Which component property drives this layer — "characters" ← "Label#12:0"
    const refs = safe(() => n.componentPropertyReferences);
    if (refs && Object.keys(refs).length) o.refs = Object.assign({}, refs);
    if (n.type === "INSTANCE") {
      const c = await component(n);
      if (c) o.component = c;
      const props = safe(() => n.componentProperties) || {};
      const p = {};
      for (const k of Object.keys(props)) {
        p[k] = props[k].type === "INSTANCE_SWAP" ? swapName(props[k].value) : props[k].value;
      }
      if (Object.keys(p).length) o.props = p;
    }
    if (n.type === "COMPONENT" || n.type === "COMPONENT_SET") {
      o.key = n.key;
      if (n.remote) o.remote = true;
      const defs = safe(() => n.componentPropertyDefinitions);  // a variant has none: its set does
      if (defs && Object.keys(defs).length) {
        o.propDefs = {};
        for (const k of Object.keys(defs)) {
          const d = defs[k];
          const x = { type: d.type,
                      default: d.type === "INSTANCE_SWAP" ? swapName(d.defaultValue) : d.defaultValue };
          if (d.variantOptions) x.options = d.variantOptions;
          if (d.preferredValues && d.preferredValues.length) x.preferred = d.preferredValues.map((v) => v.key);
          o.propDefs[k] = x;
        }
      }
      if (n.description) o.description = n.description.slice(0, 300);
    }
    if ("children" in n && n.children.length) {
      if (level >= maxDepth) o.childrenCut = n.children.length;
      else {
        const kids = [];
        let hidden = 0, cut = 0;
        for (const c of n.children) {
          if (c.visible === false && !withHidden) { hidden++; continue; }
          if (out.nodes >= maxNodes) { cut++; continue; }
          kids.push(await walk(c, level + 1));
        }
        if (kids.length) o.children = kids;
        if (hidden) o.hiddenKids = hidden;
        if (cut) { o.childrenCut = cut; out.cut = true; }
      }
    }
    return o;
  };
  out.node = await walk(root, 0);
  return out;
}

// ─── shot ────────────────────────────────────────────────────────────────
// A picture of a layer for the agent to look at: the CLI saves it into a file
// and cuts a tall one into parts (cli_extras.py), because a model sees an image
// shrunk to about 1.15 megapixels. Small layers come at 2×, wide ones at most
// SHOT_MAX_W pixels wide. Exporting changes nothing in the file.

const SHOT_MAX_W = 1600;

function shotScale(node) {
  const w = Math.max(1, node.width || 1);
  if (w * 2 <= SHOT_MAX_W) return 2;
  if (w <= SHOT_MAX_W) return 1;
  return Math.floor((SHOT_MAX_W / w) * 100) / 100;
}

async function shotOf(node, opts) {
  if (!node || typeof node.exportAsync !== "function") {
    throw new Error("h.shot: " + (node ? node.type + " \"" + node.name + "\"" : "no node") +
      " — it cannot be exported");
  }
  opts = opts || {};
  const format = String(opts.format || "PNG").toUpperCase().replace("JPEG", "JPG");
  let settings;
  let scale;
  if (format === "SVG") {
    settings = { format: "SVG", svgOutlineText: opts.outlineText !== false, svgIdAttribute: opts.ids === true };
  } else if (format === "PDF") {
    settings = { format: "PDF" };
  } else if (format === "PNG" || format === "JPG") {
    if (opts.width) {
      settings = { format, constraint: { type: "WIDTH", value: Number(opts.width) } };
    } else {
      scale = opts.scale ? Number(opts.scale) : shotScale(node);
      settings = { format, constraint: { type: "SCALE", value: scale } };
    }
  } else {
    throw new Error("h.shot: the format is png, jpg, svg or pdf, not " + JSON.stringify(opts.format));
  }
  // A layer whose children or effects stick out of it exports larger than the layer;
  // absolute bounds give exactly its own size (for a picture reused inside a mockup).
  if (opts.absolute) settings.useAbsoluteBounds = true;
  const bytes = await node.exportAsync(settings);
  return {
    kind: "shot", id: node.id, name: node.name, type: node.type,
    file: figma.root.name, fileKey: fileKey() || undefined, url: nodeLink(node.id),
    format, scale, width: opts.width ? Number(opts.width) : undefined,
    w: Math.round(node.width), h: Math.round(node.height),
    size: bytes.length, data: figma.base64Encode(bytes),
  };
}

// ─── images ──────────────────────────────────────────────────────────────

const IMAGE_MODES = { fill: "FILL", fit: "FIT", crop: "CROP", tile: "TILE" };
const IMAGE_MAX_PX = 4096;  // Figma: "a maximum size of 4096 pixels in width and height"

function imageFill(node, source, mode, h) {
  if (!node || typeof node !== "object") throw new Error("h.image: needs a layer as its first argument");
  const how = IMAGE_MODES[String(mode == null ? "fill" : mode).toLowerCase()];
  if (!how) {
    throw new Error("h.image: the mode is fill, fit, crop or tile, not " + JSON.stringify(mode));
  }
  if (!("fills" in node)) {
    throw new Error("h.image: " + nodeLabel(node) + " has no fills, so it can't hold a picture — " +
      "put the picture on a frame, a rectangle or a shape inside it");
  }
  let bytes = source;
  if (typeof source === "string") {
    const files = (h && h[SCRIPT_IMAGES]) || new Map();
    if (!files.has(source)) {
      throw new Error("h.image: no file named " + JSON.stringify(source) + " came with the script" +
        (files.size ? " (it has: " + Array.from(files.keys()).join(", ") + ")" : "") +
        " — figaro exec --image " + source + "=FILE; if the CLI did send one, the bridge is older than " +
        "the CLI and dropped it: restart the bridge (bash start-bridge.sh, or Update in the window)");
    }
    bytes = figma.base64Decode(files.get(source));
  }
  if (!(bytes instanceof Uint8Array)) {
    throw new Error("h.image: the picture is the bytes of a PNG, JPEG or GIF (a Uint8Array), or the " +
      "name of a file that came with the script");
  }
  let image;
  try {
    image = figma.createImage(bytes);
  } catch (e) {
    throw new Error("h.image: Figma refused the picture: " + errorText(e) +
      " (it takes PNG, JPEG and GIF up to " + IMAGE_MAX_PX + " px wide and high)");
  }
  const paint = { type: "IMAGE", scaleMode: how, imageHash: image.hash };
  if (how === "CROP") paint.imageTransform = [[1, 0, 0], [0, 1, 0]];  // the whole picture, until it is moved
  if (how === "TILE") paint.scalingFactor = 1;                        // at its own size
  node.fills = [paint];
  return { id: node.id, name: node.name, mode: how.toLowerCase(), hash: image.hash };
}

// ─── fonts ───────────────────────────────────────────────────────────────
// A text with several fonts has `figma.mixed` for fontName, so its fonts are
// read per range. loadFonts loads them all at once and reports the ones this
// computer has not got instead of failing on the first; for Font Awesome icon
// fonts it names the families that are installed, since the fix for a missing
// one is to change the font and keep the glyph name.

function fontLabel(f) {
  return f.family + " " + f.style;
}

function fontsIn(node) {
  const texts = node.type === "TEXT" ? [node]
    : typeof node.findAllWithCriteria === "function" ? node.findAllWithCriteria({ types: ["TEXT"] })
    : typeof node.findAll === "function" ? node.findAll((n) => n.type === "TEXT") : [];
  const seen = new Map();
  for (const t of texts) {
    const list = t.fontName === figma.mixed ? t.getRangeAllFontNames(0, t.characters.length) : [t.fontName];
    for (const f of list) seen.set(fontLabel(f), f);
  }
  return Array.from(seen.values());
}

async function loadFonts(target) {
  const list = Array.isArray(target) ? target
    : target && typeof target === "object" && "family" in target ? [target]
    : target && typeof target === "object" ? fontsIn(target) : [];
  const settled = await Promise.all(list.map((f) => loadFont(f).then(() => null, () => f)));
  const missing = settled.filter(Boolean);
  const out = { loaded: list.length - missing.length, missing: missing.map(fontLabel) };
  if (missing.length) {
    const fa = missing.some((f) => /font awesome/i.test(f.family)) ? await faAdvice() : "";
    out.note = "fonts not installed on this computer: " + out.missing.join(", ") + fa;
    CURRENT_PRINT(out.note);
  }
  return out;
}

let FA_FONTS = null;  // promise of {family: [styles]} for the installed Font Awesome fonts

function faFamilies() {
  if (!FA_FONTS) {
    FA_FONTS = figma.listAvailableFontsAsync().then((all) => {
      const fam = {};
      for (const f of all) {
        const n = f.fontName;
        if (/font awesome/i.test(n.family)) (fam[n.family] = fam[n.family] || []).push(n.style);
      }
      return fam;
    });
    FA_FONTS.catch(() => { FA_FONTS = null; });
  }
  return FA_FONTS;
}

async function faInfo() {
  const families = await faFamilies();
  const names = Object.keys(families);
  const version = (n) => Number((/\d+/.exec(n) || [0])[0]);
  const pro = names.filter((n) => /^Font Awesome \d+ Pro$/i.test(n)).sort((a, b) => version(b) - version(a));
  const kit = names.filter((n) => /^Font Awesome Kit\b/i.test(n) && !/duotone/i.test(n));
  return { pro: pro[0] || null, kit: kit[0] || null, families };
}

async function faAdvice() {
  try {
    const fa = await faInfo();
    const parts = [];
    if (fa.pro) parts.push(fa.pro + " (" + fa.families[fa.pro].join(", ") + ")");
    if (fa.kit) parts.push(fa.kit + " — custom icons");
    return parts.length ? "; Font Awesome installed here: " + parts.join("; ") +
      " — change the font, keep the glyph name (h.fa())" : "; no Font Awesome installed here";
  } catch (e) {
    return "";
  }
}

// ─── auto-layout ─────────────────────────────────────────────────────────
// Sizing in the order Figma needs, so nothing is silently undone: FILL only on
// a child of an auto-layout frame, HUG only on an auto-layout frame or a text,
// and a new size keeps the other axis as it was — resize() makes both FIXED.
// A text's sizing needs its fonts, so these load them.

const SIZING_AXES = { x: ["layoutSizingHorizontal"], y: ["layoutSizingVertical"],
                      xy: ["layoutSizingHorizontal", "layoutSizingVertical"] };

function sizingProps(axis, fallback, who) {
  const a = String(axis == null ? fallback : axis).toLowerCase();
  const alias = { both: "xy", h: "x", horizontal: "x", width: "x", v: "y", vertical: "y", height: "y" };
  const props = SIZING_AXES[alias[a] || a];
  if (!props) throw new Error(who + ": the axis is 'x', 'y' or 'xy', not " + JSON.stringify(axis));
  return props;
}

function isAutoLayout(n) {
  return !!n && typeof n.layoutMode === "string" && n.layoutMode !== "NONE";
}

function nodeLabel(n) {
  return n ? n.type + " \"" + n.name + "\"" : "nothing";
}

async function textReady(node, who) {
  if (!node || node.type !== "TEXT") return;
  const got = await loadFonts(node);
  if (got.missing.length) throw new Error(who + ": \"" + node.name + "\" — " + got.note);
}

// ─── agent libraries (--lib) ─────────────────────────────────────────────
// `figaro exec --lib my.js` sends a file's code with its hash the first time
// and only the hash after that (bridge_exec.py). The code is compiled once and run
// again for every script, so its functions get that script's `figma`, `print`
// and `h`. Top-level functions, classes and consts become `lib.<name>`, and so
// does whatever `module.exports` holds; a later lib sees the earlier ones as
// `lib`. A lib's lines sit at slot × LIB_STRIDE in a stack trace — that is how
// an error inside one names its file and line. A lib runs in strict mode, as a
// script does: the directive goes on the first of the empty lines before it, so
// no line of the lib moves. Its loops are guarded as a script's are, by the
// guard of the script that runs it.

const LIBS = new Map();  // hash -> {name, base, lines, shifts, factory | error}, oldest first
const LIBS_MAX = 50;      // as LIBS_MAX in bridge_exec.py: both forget the same one
const LIB_STRIDE = 10000;
const LIB_DECL = /^(?:async\s+)?(?:function\s*\*?\s*|class\s+|(?:const|let|var)\s+)([A-Za-z_$][\w$]*)/gm;

function compileLib(item) {
  const src = String(item.code)
    .replace(/^export\s+(?:default\s+)?(?=(?:async\s+)?function|class\s|const\s|let\s|var\s)/gm, "");
  const names = [];
  let m;
  LIB_DECL.lastIndex = 0;
  while ((m = LIB_DECL.exec(src))) if (names.indexOf(m[1]) === -1) names.push(m[1]);
  LIB_DECL.lastIndex = 0;
  // The lowest slot no kept lib uses, so two libs never share their lines.
  const used = new Set(Array.from(LIBS.values(), (e) => e.base));
  let base = LIB_STRIDE;
  while (used.has(base)) base += LIB_STRIDE;
  const tail = "\n;return Object.assign({}, module.exports, {" + names.map((n) =>
    JSON.stringify(n) + ": typeof " + n + " === 'undefined' ? undefined : " + n).join(", ") + "});";
  const entry = { name: String(item.name || "lib"), base, lines: src.split("\n"), shifts: null };
  try {
    const compiled = compileGuarded(["figma", "print", "h", "lib", "module", "exports", GUARD_G], (text) =>
      '"use strict"; let ' + GUARD_N + " = 1;" + "\n".repeat(base) + text + tail, src);
    entry.factory = compiled.fn;
    entry.shifts = compiled.shifts;
  } catch (e) {
    entry.error = e;
  }
  return entry;
}

function loadLibs(items, fig, print, h, errors, guard) {
  const lib = {};
  if (!Array.isArray(items)) return lib;
  for (const item of items) {
    let entry = LIBS.get(item.hash);
    if (entry) {
      LIBS.delete(item.hash);  // newest last, as the bridge counts
      LIBS.set(item.hash, entry);
    } else if (typeof item.code === "string") {
      entry = compileLib(item);
      LIBS.set(item.hash, entry);
      while (LIBS.size > LIBS_MAX) LIBS.delete(LIBS.keys().next().value);
    } else {
      errors.missing.push(item.hash);
      throw new Error("lib " + item.name + " is not loaded in the plugin (was it restarted?) — " +
        "run the call again, the bridge sends its code");
    }
    if (entry.error) throw entry.error;
    const module = { exports: {} };
    Object.assign(lib, entry.factory(fig, print, h, lib, module, module.exports, guard(entry.name)));
  }
  return lib;
}

// A --lib file's own line for each of its positions in a stack trace.
function libPositions(stack) {
  const out = [];
  if (SCRIPT_LINE_OFFSET === null || !stack) return out;
  STACK_POS.lastIndex = 0;
  let m;
  while ((m = STACK_POS.exec(String(stack)))) {
    const bodyLine = Number(m[1]) - SCRIPT_LINE_OFFSET + 1;
    for (const e of LIBS.values()) {
      const line = bodyLine - e.base;
      if (line >= 1 && line <= e.lines.length) {
        out.push({ at: m[0], name: e.name, line, col: unshift(e.shifts, line, Number(m[2])),
                   source: e.lines[line - 1].trim().slice(0, 200) });
        break;
      }
    }
  }
  STACK_POS.lastIndex = 0;
  return out;
}

// ─── more helpers ────────────────────────────────────────────────────────
// h.withFonts and h.setText take mixed fonts, and a missing font doesn't stop
// them; h.sel gives a link to each layer.

Object.assign(HELPERS, {
  // A link to a layer, for reports to people: h.link(node) or h.link("1:11").
  link(nodeOrId) {
    const id = typeof nodeOrId === "string" ? linkNodeId(nodeOrId) : nodeOrId && nodeOrId.id;
    return nodeLink(id) || null;
  },

  // What the user has selected right now, with a link to each layer.
  sel() {
    return figma.currentPage.selection.map((n) => ({
      id: n.id, name: n.name, type: n.type,
      w: n.width, h: n.height,
      chars: n.type === "TEXT" ? n.characters : undefined,
      url: nodeLink(n.id),
    }));
  },

  // Everything about a subtree, with the keys of what it uses: {depth, hidden, maxNodes}.
  async inspect(node, opts) {
    const n = typeof node === "string" ? await HELPERS.resolve(node) : node;
    if (!n) throw new Error("h.inspect: no node");
    return await inspectTree(n, opts, this);
  },

  // A picture: {format: png|jpg|svg|pdf, scale, width, absolute}; base64 in `data`.
  async shot(node, opts) {
    const n = typeof node === "string" ? await HELPERS.resolve(node) : node;
    return await shotOf(n, opts);
  },

  // Fill a layer with a picture, instead of the fills it has: `source` is the
  // bytes of a PNG, JPEG or GIF (a Uint8Array) or the name of a file that came
  // with the script (`exec --image NAME=FILE`); `mode` is fill (the default),
  // fit, crop or tile. Returns {id, name, mode, hash}.
  image(node, source, mode) {
    return imageFill(node, source, mode, this);
  },

  // Load every font of a subtree (mixed ranges too), a FontName or a list of
  // them; never throws for a missing one — {loaded, missing, note?}.
  async fonts(target) {
    return await loadFonts(target);
  },

  // The installed Font Awesome: {pro, kit, families}.
  async fa() {
    return await faInfo();
  },

  async withFonts(rootNode, asyncFn) {
    await loadFonts(rootNode);  // a missing font is reported, the rest still load
    return await asyncFn();
  },

  // Mixed-font texts too: the new text takes the first character's style.
  async setText(node, text) {
    if (!node || node.type !== "TEXT") throw new Error("h.setText: needs a TEXT, not " + nodeLabel(node));
    await textReady(node, "h.setText");
    node.characters = String(text);
  },

  // FILL along an axis ("x" by default, "y", "xy") — inside an auto-layout parent.
  async fill(node, axis) {
    const props = sizingProps(axis, "x", "h.fill");
    if (!isAutoLayout(node.parent)) {
      throw new Error("h.fill: only a child of an auto-layout frame can FILL, and the parent of \"" +
        node.name + "\" is " + nodeLabel(node.parent) + ": first parent.appendChild(node), and give the " +
        "parent a layoutMode");
    }
    if (node.layoutPositioning === "ABSOLUTE") {
      throw new Error("h.fill: \"" + node.name + "\" is absolute — it cannot FILL");
    }
    await textReady(node, "h.fill");
    for (const p of props) {
      node[p] = "FILL";
      if (node.parent[p] === "HUG") {
        CURRENT_PRINT("h.fill: the parent \"" + node.parent.name + "\" HUGs on this axis — a FILL child " +
          "in it collapses; make the parent FIXED or FILL");
      }
    }
    return node;
  },

  // HUG ("xy" by default) — an auto-layout frame or a text.
  async hug(node, axis) {
    const props = sizingProps(axis, "xy", "h.hug");
    if (!isAutoLayout(node) && node.type !== "TEXT") {
      throw new Error("h.hug: only an auto-layout frame or a text can HUG, and this is " + nodeLabel(node) +
        (node.type === "FRAME" ? " without a layoutMode" : ""));
    }
    await textReady(node, "h.hug");
    for (const p of props) node[p] = "HUG";
    return node;
  },

  // A fixed size on the axes given; null keeps that axis's sizing (HUG, FILL).
  async fixed(node, width, height) {
    if (!node || typeof node.resize !== "function") throw new Error("h.fixed: " + nodeLabel(node) + " has no size");
    await textReady(node, "h.fixed");
    const read = (p) => { try { return node[p]; } catch (e) { return undefined; } };
    const keep = {};
    if (width == null) keep.layoutSizingHorizontal = read("layoutSizingHorizontal");
    if (height == null) keep.layoutSizingVertical = read("layoutSizingVertical");
    node.resize(width == null ? node.width : Number(width), height == null ? node.height : Number(height));
    for (const p of Object.keys(keep)) if (keep[p] && keep[p] !== "FIXED") node[p] = keep[p];
    return node;
  },

  // Wrapping text: the parent's width in auto-layout, or `width` px.
  async wrapText(node, width) {
    if (!node || node.type !== "TEXT") throw new Error("h.wrapText: needs a TEXT, not " + nodeLabel(node));
    await textReady(node, "h.wrapText");
    if (width == null) {
      if (!isAutoLayout(node.parent)) {
        throw new Error("h.wrapText: \"" + node.name + "\" is not in auto-layout — give a width: " +
          "h.wrapText(node, 320)");
      }
      node.textAutoResize = "HEIGHT";
      node.layoutSizingHorizontal = "FILL";
    } else {
      node.resize(Number(width), Math.max(1, node.height));
      node.textAutoResize = "HEIGHT";
    }
    if (node.width < 2) {
      CURRENT_PRINT("h.wrapText: \"" + node.name + "\" is " + node.width + " px wide — the text became a " +
        "column; check the parent's width");
    }
    return node;
  },

  // The CLI's `find`: {name, nameHas, type, text, textHas, hidden}. Layers
  // hidden inside instances are skipped while it searches (several times
  // faster) unless `hidden`; with a type it uses findAllWithCriteria.
  find(root, q) {
    q = q || {};
    if (!root || typeof root.findAll !== "function") throw new Error("h.find: " + nodeLabel(root) + " has no children");
    const type = q.type ? String(q.type).toUpperCase() : q.text != null || q.textHas != null ? "TEXT" : null;
    const text = (n) => (typeof n.characters === "string" ? n.characters : null);
    const match = (n) => (q.name == null || n.name === q.name)
      && (q.nameHas == null || n.name.indexOf(q.nameHas) !== -1)
      && (q.text == null || text(n) === q.text)
      && (q.textHas == null || (text(n) !== null && text(n).indexOf(q.textHas) !== -1));
    // Set either way: a script before may have left it on.
    const was = figma.skipInvisibleInstanceChildren;
    figma.skipInvisibleInstanceChildren = !q.hidden;
    try {
      const found = type && typeof root.findAllWithCriteria === "function"
        ? root.findAllWithCriteria({ types: [type] }).filter(match)
        : root.findAll((n) => (!type || n.type === type) && match(n));
      return found.map((n) => ({
        id: n.id, name: n.name, type: n.type, w: n.width, h: n.height,
        chars: n.type === "TEXT" ? n.characters : undefined,
      }));
    } finally {
      figma.skipInvisibleInstanceChildren = was;
    }
  },

  // Dev Mode annotations under a node, link, id or "page"; by default, or with
  // "sel", under every selected layer, else the page. One record per annotated
  // layer: {frame, node, name, visible, labels, properties?}. `frame` is the
  // top-level frame holding it (inside sections, the frame in them); `visible` is
  // false when the layer or any parent is hidden, as Dev Mode shows nothing for
  // those; `labels` are the texts, `properties` the pinned ones ("width"...).
  async annotations(scope) {
    const sel = figma.currentPage.selection;
    let roots;
    if (scope == null || scope === "sel") {
      if (scope === "sel" && !sel.length) throw new Error("nothing selected in Figma");
      roots = sel.length ? sel.slice().reverse() : [figma.currentPage];
    } else {
      roots = [typeof scope === "string" ? await HELPERS.resolve(scope) : scope];
    }
    // The script's own h: a long walk stops at the deadline or on Stop.
    const ck = this && typeof this.ck === "function" ? this.ck : null;
    const out = [];
    const listed = new Set();  // a selected layer and its parent, both selected
    const stack = roots;
    let breath = Date.now();
    while (stack.length) {
      const n = stack.pop();
      // A big page takes a while to walk: let the plugin take messages now and then.
      if (Date.now() - breath > 50) {
        await wait(0);
        if (ck) ck();
        breath = Date.now();
      }
      // Each read of annotations or children builds a new array: read them once.
      const notes = n.annotations;
      if (notes && notes.length && !listed.has(n.id)) {
        listed.add(n.id);
        const chain = [];
        for (let p = n; p && p.type !== "PAGE"; p = p.parent) chain.push(p);
        let top = chain.length - 1;
        while (top > 0 && chain[top].type === "SECTION") top--;
        const rec = {
          frame: chain[top].name, node: n.id, name: n.name,
          visible: chain.every((p) => p.visible !== false), labels: [],
        };
        const pinned = [];
        for (const a of notes) {
          const text = a.label || a.labelMarkdown;
          if (text) rec.labels.push(text);
          if (a.properties) for (const prop of a.properties) pinned.push(prop.type);
        }
        if (pinned.length) rec.properties = pinned;
        out.push(rec);
      }
      const kids = n.children;
      if (kids) for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i]);
    }
    return out;
  },
});

// ─── h.audit: a check of one's own work ─────────────────────────────────
// h.audit(root, opts) walks a layer (or a page) and returns notes: what looks
// wrong, on which layer, and what to do. It only hints. The walk is iterative
// and bounded (layers, time), asks the script's h.ck() as it goes and gives
// the plugin a breath now and then: it must never freeze Figma on a big page
// (a recursive walk would: guardLoops doesn't see recursion). The block
// between the two markers reads `figma` only through `env`, so a --lib file can
// carry it as it is, to check a branch live before the plugin runs it.
// audit:begin
// In order of importance: the first rule is the loudest.
const AUDIT_RULES = [
  "missing-font", "text-clipped", "zero-size", "low-contrast", "low-contrast-ui", "placeholder-text",
  "small-target", "no-auto-layout", "empty-frame", "detached-instance", "unbound-color",
  "no-text-style", "default-name", "uneven-siblings", "heading-order", "absolute-in-layout",
];
const AUDIT_SHOWN = 50;     // notes in the answer
const AUDIT_IDS = 30;      // layer ids listed in a note that covers several
const AUDIT_NODES = 8000;   // layers visited before the walk stops (partial)
const AUDIT_MS = 8000;      // time before the walk stops (partial)
const AUDIT_TARGET = 24;    // WCAG 2.2, SC 2.5.8 (AA): a target of at least 24 x 24 px
const AUDIT_BUTTON = new Set(["button", "btn", "cta", "chip", "tab", "toggle", "checkbox", "radio", "switch", "link"]);
const AUDIT_COLORED = new Set(["FRAME", "COMPONENT", "RECTANGLE", "ELLIPSE", "TEXT"]);  // where a raw colour is a note
const AUDIT_BEHIND = new Set(["FRAME", "COMPONENT", "INSTANCE", "RECTANGLE", "ELLIPSE", "SECTION", "VECTOR", "BOOLEAN_OPERATION", "STAR", "POLYGON"]);  // can be a backdrop
const AUDIT_SHAPES = new Set(["VECTOR", "BOOLEAN_OPERATION", "ELLIPSE", "STAR", "POLYGON", "LINE", "RECTANGLE"]);
const AUDIT_ICON = new Set(["VECTOR", "BOOLEAN_OPERATION", "STAR", "POLYGON"]);
const AUDIT_HOLDS = new Set(["FRAME", "COMPONENT", "COMPONENT_SET", "GROUP", "SECTION", "PAGE"]);
const AUDIT_DEFAULT_NAME = /^(?:Frame|Group|Component|Section|Auto layout)(?: \d+)?$/;
const AUDIT_PLACEHOLDER = [
  /\b(?:tbc|tbd)\b/i,
  /lorem ipsum|dolor sit amet/i,
  /^\s*(?:todo|xxx+|placeholder|text|your text(?: here)?|text here|\?{2,}|\.{3}|…)\s*$/i,
];
const AUDIT_HEADING_NAME = /^\s*(?:h|heading\s*)([1-6])\s*$/i;  // the whole name: "H2 2024 results" is a text, not a heading
const AUDIT_CONTROL = new Set(Array.from(AUDIT_BUTTON).concat(["nav", "menu", "action", "actions", "badge", "status", "indicator"]));  // where an icon is a UI graphic (WCAG 1.4.11)
const AUDIT_TYPE_VARS = ["fontSize", "fontFamily", "fontStyle", "fontWeight", "lineHeight", "letterSpacing"];
const AUDIT_HEADING_STYLE = /(?:^|[\s/_-])(?:h|heading\s?)([1-6])(?:$|[\s/_-])/i;

// Is a property "Disabled" on: true or yes. Only disabled controls are exempt from contrast (WCAG 1.4.3);
// a tab that is just not selected ("inactive") is not.
function auditOnValue(prop, value) {
  if (auditWords(prop).indexOf("disabled") === -1) return false;
  return value === true || (typeof value === "string" && /^\s*(?:true|yes|on)\s*$/i.test(value));
}

// A layer name that says it is disabled: "Button / Disabled", or a variant name, "Disabled=True" or
// "State=Disabled" among "Prop=Value, …" (where "Disabled=False" is the ordinary one).
function auditNamedOff(name) {
  name = String(name);
  if (name.indexOf("=") === -1) return auditWords(name).indexOf("disabled") !== -1;
  return name.split(",").some((part) => {
    const at = part.indexOf("=");
    if (at === -1) return false;
    const value = part.slice(at + 1);
    return auditOnValue(part.slice(0, at), value) || value.trim().toLowerCase() === "disabled";
  });
}

function auditSafe(fn, otherwise) {
  try { return fn(); } catch (e) { return otherwise; }
}

async function auditTry(fn, otherwise) {
  try { return await fn(); } catch (e) { return otherwise; }
}

// "stopped after 0.3 s", "stopped after 8 s"
function auditSecs(ms) {
  return ms % 1000 === 0 ? String(ms / 1000) : String(Math.round(ms / 100) / 10);
}

function auditClicks(n) {
  const list = auditSafe(() => n.reactions, null);
  return Array.isArray(list) && list.some((r) => r && r.trigger && (r.trigger.type === "ON_CLICK" || r.trigger.type === "ON_PRESS"));
}

function auditHex(c) {
  const p = (v) => Math.round(Math.max(0, Math.min(1, v)) * 255).toString(16).padStart(2, "0");
  return ("#" + p(c.r) + p(c.g) + p(c.b)).toUpperCase();
}

// WCAG 2: relative luminance, and the contrast ratio of two colours (1 to 21).
function auditLum(c) {
  const f = (v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
}

function auditRatio(a, b) {
  const x = auditLum(a), y = auditLum(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

function auditBlend(fg, a, bg) {
  return { r: fg.r * a + bg.r * (1 - a), g: fg.g * a + bg.g * (1 - a), b: fg.b * a + bg.b * (1 - a) };
}

function auditWords(name) {
  return String(name).replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
}

function auditShort(s, n) {
  s = String(s).replace(/\s+/g, " ");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function auditNum(v) {
  return Math.round(v * 10) / 10;
}

// A ratio as text, rounded down: 2.996 is not "3", which would pass.
function auditShy(v) {
  return String(Math.floor(v * 100) / 100);
}

// The paints that show: visible and not fully transparent.
function auditShown(paints) {
  return Array.isArray(paints) ? paints.filter((p) => p && p.visible !== false && (p.opacity === undefined || p.opacity > 0)) : [];
}

// What a layer paints without a style or a variable: {raw: "#RRGGBB" | null, tokened}.
function auditPaintState(paints, styleId, mixed) {
  if (styleId && styleId !== mixed) return { raw: null, tokened: true };
  let raw = null, tokened = false;
  for (const p of auditShown(paints)) {
    if (p.type !== "SOLID") continue;
    if (p.boundVariables && p.boundVariables.color) tokened = true;
    else if (raw === null) raw = auditHex(p.color);
  }
  return { raw, tokened };
}

function auditFillState(n, mixed) {
  if (n.fills === mixed || n.fillStyleId === mixed) {  // a text with several colours or styles
    let raw = null, tokened = false;
    const segs = auditSafe(() => n.getStyledTextSegments(["fills", "fillStyleId"]), []);
    for (const s of segs.slice(0, 20)) {
      const r = auditPaintState(s.fills, s.fillStyleId, mixed);
      if (r.tokened) tokened = true;
      if (raw === null) raw = r.raw;
    }
    return { raw, tokened };
  }
  return auditPaintState(n.fills, n.fillStyleId, mixed);
}

// What a layer's own fill does for what is above it: undefined — nothing, so the
// layer behind or the parent shows through; null — something that isn't one
// colour (an image, a gradient, a translucent paint, a blend mode); or the colour.
function auditBackdropOf(n) {
  const shown = auditShown(n.fills);
  if (!shown.length) return undefined;
  const top = shown[shown.length - 1];
  const plain = n.opacity === undefined || n.opacity === 1;
  const blend = n.blendMode === undefined || n.blendMode === "NORMAL" || n.blendMode === "PASS_THROUGH";
  if (top.type === "SOLID" && (top.opacity === undefined || top.opacity === 1) && plain && blend) return top.color;
  return null;
}

async function auditRun(root, opts, env) {
  opts = opts || {};
  const fig = env.fig, ck = env.ck || null;
  if (typeof root === "string") root = await env.resolve(root);
  if (!root || typeof root.type !== "string" || root.type === "DOCUMENT") {
    throw new Error("h.audit: give it a layer or a page, not " + (root && root.type === "DOCUMENT" ? "the document" : String(root)));
  }
  const known = new Set(AUDIT_RULES);
  for (const key of ["only", "skip"]) {
    if (opts[key] == null) continue;
    if (!Array.isArray(opts[key])) throw new Error("h.audit: " + key + " is a list of rule ids");
    for (const id of opts[key]) {
      if (!known.has(id)) throw new Error('h.audit: no rule "' + id + '"; there are: ' + AUDIT_RULES.join(", "));
    }
  }
  const on = (id) => (!opts.only || opts.only.indexOf(id) !== -1) && !(opts.skip && opts.skip.indexOf(id) !== -1);
  const shownMax = Math.max(1, Math.floor(opts.limit || AUDIT_SHOWN));
  const maxNodes = opts.maxNodes || AUDIT_NODES;
  const maxMs = opts.maxMs || AUDIT_MS;
  const minTarget = typeof opts.minTarget === "number" ? opts.minTarget : AUDIT_TARGET;
  const mixed = fig.mixed;

  // Notes are grouped as they arrive (the same rule and text, or each layer with group: false). A rule
  // keeps only its first `limit` groups: the answer is the top `limit` by rank, so no more can show, and
  // memory stays small on a page of thousands. Every layer is still counted.
  const counts = {}, held = new Map();
  let seq = 0, errors = 0;
  const add = (rule, n, text) => {
    if (!on(rule)) return;
    counts[rule] = (counts[rule] || 0) + 1;
    let m = held.get(rule);
    if (!m) { m = new Map(); held.set(rule, m); }
    const key = opts.group === false ? String(seq) : text;
    const g = m.get(key);
    if (g) { g.count++; if (g.ids.length < AUDIT_IDS) g.ids.push(n.id); }
    else if (m.size < shownMax) m.set(key, { rule, id: n.id, name: auditShort(n.name || n.type, 60), text, count: 1, ids: [n.id], seq });
    seq++;
  };

  if (root.type === "PAGE" && typeof root.loadAsync === "function") await root.loadAsync();
  const was = fig.skipInvisibleInstanceChildren;
  fig.skipInvisibleInstanceChildren = true;
  const started = Date.now();
  let breath = started, visited = 0, partial = null;

  const kidsOf = new Map();  // a layer's children, read once: every read builds an array
  const kids = (n) => {
    let k = kidsOf.get(n.id);
    if (!k) { k = n.children ? n.children.slice() : []; kidsOf.set(n.id, k); }
    return k;
  };
  const backdrops = new Map();
  const backdrop = (n) => {
    if (!backdrops.has(n.id)) backdrops.set(n.id, auditBackdropOf(n));
    return backdrops.get(n.id);
  };
  const styleNames = new Map();
  const styleName = async (id) => {
    if (!styleNames.has(id)) {
      let s = null;
      try { s = await fig.getStyleByIdAsync(id); } catch (e) { s = null; }
      styleNames.set(id, s ? String(s.name) : "");
    }
    return styleNames.get(id);
  };

  // A stack entry: the layer, its place among its parent's children (-1: not
  // looked up yet), its parent's entry (undefined: not built yet; above the
  // root they are built when asked), the opacity of the layers above it, whether
  // one of them blends, the frames above it that clip, and whether the parent
  // is an auto-layout.
  const parentOf = (e) => {
    if (e.up === undefined) {
      const p = e.n.parent;
      e.up = p && p.type !== "PAGE" && p.type !== "DOCUMENT" ? { n: p, at: -1, up: undefined } : null;
    }
    return e.up;
  };
  const indexOf = (e) => {
    if (e.at < 0) {
      const p = e.n.parent;
      e.at = p ? kids(p).findIndex((k) => k.id === e.n.id) : 0;
    }
    return e.at;
  };
  // The colour behind a layer: going up, at each level first the layers below it
  // that cover its middle (a backdrop rectangle of its own), then the parent's
  // fill. {c, near}: the colour, and whether a layer beside it gave it (not an
  // ancestor). null: unknown (an image, a gradient, a translucent paint, or
  // nothing at all up to the page), so there is no note about it.
  const behind = (e) => {
    const b = e.n.absoluteBoundingBox;
    const cx = b ? b.x + b.width / 2 : null, cy = b ? b.y + b.height / 2 : null;
    for (let cur = e, depth = 0; depth < 80; depth++) {
      const up = parentOf(cur);
      if (!up) return null;
      if (cx !== null) {
        const sibs = kids(up.n);
        const at = indexOf(cur);
        for (let j = at - 1; j >= 0 && j >= at - 40; j--) {
          const s = sibs[j];
          if (s.visible === false || !AUDIT_BEHIND.has(s.type)) continue;
          const sb = s.absoluteBoundingBox;
          if (!sb || cx < sb.x || cx > sb.x + sb.width || cy < sb.y || cy > sb.y + sb.height) continue;
          const d = backdrop(s);
          if (d !== undefined) return d && { c: d, near: true };
        }
      }
      const d = backdrop(up.n);
      if (d !== undefined) return d && { c: d, near: false };
      cur = up;
    }
    return null;
  };

  // Inactive things are left out of contrast: a layer named for it, or an instance of a variant that
  // says so, on the layer or any layer above it (also above the root).
  const offMemo = new Map();
  const looksOff = (n) => {
    if (!(n.type === "TEXT" && n.name === n.characters) && auditNamedOff(n.name)) return true;
    if (n.type === "INSTANCE") {
      const props = auditSafe(() => n.componentProperties, null);
      if (props) for (const k of Object.keys(props)) {
        const v = props[k] && props[k].value;
        if (auditOnValue(k.replace(/#\d+:\d+$/, ""), v) || (typeof v === "string" && v.trim().toLowerCase() === "disabled")) return true;
      }
    }
    return false;
  };
  const off = (e) => {
    const chain = [];
    let res = false;
    for (let c = e; c; c = parentOf(c)) {
      if (offMemo.has(c.n.id)) { res = offMemo.get(c.n.id); break; }
      chain.push(c.n.id);
      if (looksOff(c.n)) { res = true; break; }
    }
    for (const id of chain) offMemo.set(id, res);
    return res;
  };
  // A control: a name with a button-like or state word (AUDIT_CONTROL) or a click in the prototype, on a
  // layer within four levels above.
  const inControl = (e) => {
    let c = parentOf(e);
    for (let d = 0; c && d < 4; d++, c = parentOf(c)) {
      if (auditWords(c.n.name).some((w) => AUDIT_CONTROL.has(w)) || auditClicks(c.n)) return true;
    }
    return false;
  };

  const headings = [];
  const unstyled = [], raws = [];
  let styled = false, tokened = false;
  const iconSeen = new Set();

  // What is wrong with a text wherever it is, in an instance too.
  const textBasics = (n) => {
    const chars = typeof n.characters === "string" ? n.characters : "";
    if (n.hasMissingFont) add("missing-font", n, "uses a font that is not installed here, so it shows in a stand-in; switch it to an installed font");
    for (const re of AUDIT_PLACEHOLDER) {
      if (re.test(chars)) { add("placeholder-text", n, 'placeholder text "' + auditShort(chars, 40) + '"; write the real text, or ask'); break; }
    }
  };

  const textBody = async (e, n) => {
    const chars = n.characters;
    // Cut: by a frame that clips, or past its own fixed box.
    const rb = n.absoluteRenderBounds, bb = n.absoluteBoundingBox;
    if (rb && bb && n.textTruncation !== "ENDING") {
      const size = typeof n.fontSize === "number" ? n.fontSize : 16;
      const slack = Math.max(2, size * 0.4);
      const auto = n.textAutoResize;
      let cut = null;
      if (auto === "NONE" && rb.height > bb.height + slack) cut = "runs out of its own box (" + auditNum(bb.height) + " px high, the text needs " + auditNum(rb.height) + "); make the box taller or the text shorter";
      else if (auto !== "WIDTH_AND_HEIGHT" && rb.width > bb.width + slack) cut = "runs out of its own box on the side; make the box wider or the text shorter";
      // Figma's render bounds are the part that shows, already cut by the frame: the
      // text's box sticks out of the frame and its drawn part reaches the frame's edge.
      for (let c = e.clip; !cut && c; c = c.up) {
        const o = c.box;
        const right = bb.x + bb.width > o.x + o.width + 2 && rb.x + rb.width >= o.x + o.width - 1;
        const left = bb.x < o.x - 2 && rb.x <= o.x + 1;
        const bottom = bb.y + bb.height > o.y + o.height + 2 && rb.y + rb.height >= o.y + o.height - 1;
        const top = bb.y < o.y - 2 && rb.y <= o.y + 1;
        if (right || left || bottom || top) {
          cut = 'is cut off by the frame "' + auditShort(c.name, 30) + '", which clips its content; make room for the text or turn the clipping off';
        }
      }
      if (cut) add("text-clipped", n, cut);
    }
    // Contrast, per run of one colour and size.
    if (on("low-contrast")) {
      const back = behind(e);
      if (back) {
        const bg = back.c;
        const one = n.fills !== mixed && n.fontSize !== mixed && n.fontWeight !== mixed;
        const segs = one ? [{ characters: chars, fills: n.fills, fontSize: n.fontSize, fontWeight: n.fontWeight }]
          : auditSafe(() => n.getStyledTextSegments(["fills", "fontSize", "fontWeight"]), []).slice(0, 20);
        let worst = null;
        for (const s of segs) {
          if (!s.characters.trim()) continue;
          const paints = auditShown(s.fills);
          const top = paints[paints.length - 1];
          if (!top || top.type !== "SOLID") continue;
          const a = (top.opacity === undefined ? 1 : top.opacity) * (n.opacity === undefined ? 1 : n.opacity) * e.alpha;
          const fg = auditBlend(top.color, a, bg);
          const ratio = auditRatio(fg, bg);
          const large = s.fontSize >= 24 || (s.fontSize >= 18.66 && s.fontWeight >= 700);
          const need = large ? 3 : 4.5;
          if (ratio < need && (!worst || ratio < worst.ratio)) worst = { ratio, need, fg, large };
        }
        if (worst && !off(e)) {
          add("low-contrast", n, "contrast " + auditShy(worst.ratio) + ":1 of " + auditHex(worst.fg) + " on " + auditHex(bg) + ", needs " +
            worst.need + ":1" + (worst.large ? " for large text" : "") + "; use a stronger colour");
        }
      }
    }
    // The text style: only counts where the file uses styles at all (resolved below).
    // A text driven by typography variables is styled too.
    const sid = n.textStyleId;
    const vars = n.boundVariables;
    const typed = !!vars && AUDIT_TYPE_VARS.some((k) => vars[k]);
    if (sid && sid !== mixed) styled = true;
    else if (typed) styled = true;
    else if (sid === mixed) {
      const segs = auditSafe(() => n.getStyledTextSegments(["textStyleId"]), []);
      if (segs.some((s) => s.textStyleId)) styled = true;
      if (segs.some((s) => !s.textStyleId)) unstyled.push(n);
    } else unstyled.push(n);
    // A heading's level, from its text style or its name.
    let level = null;
    const named = n.name.trim() === chars.trim() ? null : AUDIT_HEADING_NAME.exec(n.name);
    if (named) level = +named[1];
    else if (sid && sid !== mixed) {
      const m = AUDIT_HEADING_STYLE.exec(await styleName(sid));
      if (m) level = +m[1];
    }
    if (level !== null && bb) headings.push({ n, level, y: bb.y, x: bb.x, screen: e.screen });
  };

  const small = async (n) => {
    if (!on("small-target") || (n.width >= minTarget && n.height >= minTarget)) return;
    let why = null;
    if (auditClicks(n)) {
      why = "it has a click in the prototype";
    } else if (auditWords(n.name).some((x) => AUDIT_BUTTON.has(x))) {
      why = 'it is named "' + auditShort(n.name, 30) + '"';
    } else if (n.type === "INSTANCE") {
      let main = null;
      try { main = await n.getMainComponentAsync(); } catch (e) { main = null; }
      const names = main ? [main.name, main.parent && main.parent.type === "COMPONENT_SET" ? main.parent.name : ""] : [];
      if (names.some((s) => auditWords(s).some((x) => AUDIT_BUTTON.has(x)))) why = 'its component is "' + auditShort(names[1] || names[0], 30) + '"';
    }
    if (why) {
      add("small-target", n, "looks like a button (" + why + ") but is " + auditNum(n.width) + " × " + auditNum(n.height) +
        " px, under " + minTarget + " × " + minTarget + "; make it at least that big");
    }
  };

  // What is wrong with a frame in its own right, and with the way its children sit.
  const frameBody = (e, n, list) => {
    const type = n.type;
    if (n.detachedInfo) add("detached-instance", n, "was an instance and is detached: fine as a one-off state, otherwise use the component again");
    const flow = list.filter((k) => k.visible !== false && k.layoutPositioning !== "ABSOLUTE");
    if (!list.length) {
      const bare = !auditShown(n.fills).length && !(auditShown(n.strokes).length && n.strokeWeight !== 0) &&
        !(Array.isArray(n.effects) && n.effects.some((x) => x && x.visible !== false)) && !n.isMask;
      // Only one still named by Figma: an empty frame with a name is a spacer or a spec on purpose.
      if (bare && AUDIT_DEFAULT_NAME.test(n.name) && !(n.width < 1 || n.height < 1)) {
        add("empty-frame", n, "empty, shows nothing and still has Figma's default name; delete it (for a gap use the parent's spacing) or fill it");
      }
      return;
    }
    if (type === "COMPONENT_SET") return;
    if (n.layoutMode === "NONE") {
      const onPage = n.parent && (n.parent.type === "PAGE" || n.parent.type === "SECTION");
      // a screen, a device mock or a board, in either orientation: placed by hand on purpose
      const screen = onPage && Math.min(n.width, n.height) >= 320 && Math.max(n.width, n.height) >= 440;
      const drawing = flow.every((k) => AUDIT_SHAPES.has(k.type));
      if (flow.length >= 2 && !screen && !drawing) {
        let overlaps = false;
        const some = flow.slice(0, 16);
        for (let i = 0; i < some.length && !overlaps; i++) {
          for (let j = i + 1; j < some.length && !overlaps; j++) {
            const a = some[i], b = some[j];
            const dx = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
            const dy = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
            const least = Math.min(a.width * a.height, b.width * b.height);
            if (dx > 0 && dy > 0 && least > 0 && dx * dy > 0.25 * least) overlaps = true;  // a stack, not a row
          }
        }
        if (!overlaps) add("no-auto-layout", n, flow.length + " children placed by hand with no auto-layout; add auto-layout so spacing follows the content");
      }
      return;
    }
    // Same kind of siblings of a fixed size that are a few px apart.
    const groups = new Map();
    for (const k of flow) {
      const key = k.type + "|" + String(k.name).replace(/[\s_-]*\d+$/, "").toLowerCase();
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(k);
    }
    for (const g of groups.values()) {
      if (g.length < 2) continue;
      for (const axis of ["Horizontal", "Vertical"]) {
        const fixed = g.filter((k) => k["layoutSizing" + axis] === "FIXED");
        if (fixed.length < 2) continue;
        const of = (k) => auditNum(axis === "Horizontal" ? k.width : k.height);
        const sizes = fixed.map(of);
        const spread = Math.max.apply(null, sizes) - Math.min.apply(null, sizes);
        if (spread <= 0.5 || spread > 8) continue;  // equal, or too different to be a slip
        const tally = new Map();
        for (const s of sizes) tally.set(s, (tally.get(s) || 0) + 1);
        let mode = sizes[0];
        for (const [s, c] of tally) if (c > tally.get(mode)) mode = s;
        const odd = fixed.find((k) => Math.abs(of(k) - mode) > 0.5);
        add("uneven-siblings", odd, "is " + of(odd) + " px " + (axis === "Horizontal" ? "wide" : "high") + ", its same-named siblings are " +
          auditShort(Array.from(new Set(sizes)).join(", "), 40) + "; make them equal");
      }
    }
  };

  try {
    const stack = [{ n: root, at: -1, up: undefined, alpha: 1, clip: null, inLayout: false, screen: root.id }];
    // One layer: its own notes, then its children onto the stack.
    const visit = async (e) => {
      const n = e.n;
      if (n.visible === false) return;
      const type = n.type;
      const w = n.width, h = n.height;
      let hollow = false;
      if (type === "TEXT" || type === "FRAME" || type === "COMPONENT" || type === "INSTANCE" || type === "RECTANGLE" || type === "ELLIPSE") {
        if (w < 1 || h < 1) { hollow = true; add("zero-size", n, "is " + auditNum(w) + " × " + auditNum(h) + " px, so nothing shows; give it a size or delete it"); }
      }
      if (!hollow && type === "TEXT" && typeof n.characters === "string" && !n.characters.trim()) {
        hollow = true;
        add("zero-size", n, "text with no visible characters; write the text or delete the layer");
      }
      if (AUDIT_DEFAULT_NAME.test(n.name) && (type === "FRAME" || type === "GROUP" || type === "COMPONENT" || type === "SECTION")) {
        add("default-name", n, 'named "' + n.name + '", Figma\'s own default; name it for what it is');
      }
      // A dot, a ring or a backdrop shape is made to sit over its neighbours; content is not.
      if (e.inLayout && n.layoutPositioning === "ABSOLUTE" && !AUDIT_SHAPES.has(type)) {
        const pe = parentOf(e);
        add("absolute-in-layout", n, 'positioned by hand inside the auto-layout "' + auditShort(pe ? pe.n.name : "", 30) +
          '"; fine for a badge or an overlay, otherwise put it in the flow');
      }

      if (type === "TEXT") {
        textBasics(n);
        if (!hollow) await textBody(e, n);
        const f = auditFillState(n, mixed);
        if (f.tokened) tokened = true;
        if (f.raw) raws.push({ n, fill: f.raw, stroke: null });
        return;
      }

      if (AUDIT_COLORED.has(type)) {
        const f = auditFillState(n, mixed);
        const s = n.strokeWeight === 0 ? { raw: null, tokened: false } : auditPaintState(n.strokes, n.strokeStyleId, mixed);
        if (f.tokened || s.tokened) tokened = true;
        if (f.raw || s.raw) raws.push({ n, fill: f.raw, stroke: s.raw });
      }

      if (type === "INSTANCE") {
        await small(n);
        // What is inside belongs to the component; only the texts people fill in are looked at.
        const texts = auditSafe(() => n.findAllWithCriteria({ types: ["TEXT"] }), []);
        for (const t of texts.slice(0, 200)) if (t.visible !== false) textBasics(t);
        visited += Math.min(texts.length, 200);
        return;
      }

      if (AUDIT_ICON.has(type) && on("low-contrast-ui") && w <= 64 && h <= 64) {
        const key = n.parent ? n.parent.id : "";
        const back = iconSeen.has(key) ? null : behind(e);
        const bg = back && back.c;
        const paints = auditShown(n.fills).length ? auditShown(n.fills) : n.strokeWeight === 0 ? [] : auditShown(n.strokes);
        const top = paints[paints.length - 1];
        if (bg && top && top.type === "SOLID") {
          const a = (top.opacity === undefined ? 1 : top.opacity) * (n.opacity === undefined ? 1 : n.opacity) * e.alpha;
          const fg = auditBlend(top.color, a, bg);
          const ratio = auditRatio(fg, bg);
          // The same colour as a layer beside it: parts of one drawing, not an invisible icon.
          // Only a graphic that is part of a control (WCAG 1.4.11); not one that is inactive.
          if (ratio < 3 && !(back.near && ratio < 1.15) && inControl(e) && !off(e)) {
            iconSeen.add(key);
            add("low-contrast-ui", n, "icon contrast " + auditShy(ratio) + ":1 of " + auditHex(fg) + " on " + auditHex(bg) + ", needs 3:1; use a stronger colour");
          }
        }
        return;
      }

      if (!AUDIT_HOLDS.has(type)) return;
      const list = kids(n);
      if (type === "FRAME" || type === "COMPONENT" || type === "COMPONENT_SET") {
        if (type !== "COMPONENT_SET") await small(n);
        frameBody(e, n, list);
      }
      let clip = e.clip;
      if ((type === "FRAME" || type === "COMPONENT") && n.clipsContent === true && n.absoluteBoundingBox) {
        clip = { box: n.absoluteBoundingBox, name: n.name, up: e.clip };
      }
      const alpha = e.alpha * (typeof n.opacity === "number" ? n.opacity : 1);
      const lay = (type === "FRAME" || type === "COMPONENT") && n.layoutMode !== "NONE";
      const onBoard = type === "PAGE" || type === "SECTION";  // each frame on a page or in a section is a screen
      for (let i = list.length - 1; i >= 0; i--) stack.push({ n: list[i], at: i, up: e, alpha, clip, inLayout: lay, screen: onBoard ? list[i].id : e.screen });
    };
    while (stack.length) {
      const e = stack.pop();
      if (++visited > maxNodes) { partial = "stopped after " + maxNodes + " layers"; break; }
      if ((visited & 31) === 0) {
        if (ck) ck();
        const t = Date.now();
        if (t - started > maxMs) { partial = "stopped after " + auditSecs(maxMs) + " s"; break; }
        if (t - breath > 40) { await env.wait(0); breath = Date.now(); }
      }
      try { await visit(e); } catch (err) { errors++; }  // a layer removed while the walk ran; counted, not hidden
    }

    // Judged on the whole: a style or a variable counts as missing only where the
    // file works with them (its own styles or variables, or ones used in what was walked).
    if (unstyled.length && on("no-text-style")) {
      let any = styled;
      if (!any) {
        const list = await auditTry(() => fig.getLocalTextStylesAsync(), []);
        any = list.length > 0;
      }
      if (any) for (const n of unstyled) add("no-text-style", n, "has no text style or typography variables; apply one of the file's text styles");
    }
    if (raws.length && on("unbound-color")) {
      let any = tokened;
      if (!any) {
        const paintStyles = await auditTry(() => fig.getLocalPaintStylesAsync(), []);
        any = paintStyles.length > 0;
      }
      if (!any && fig.variables) {
        const colors = await auditTry(() => fig.variables.getLocalVariablesAsync("COLOR"), []);
        any = colors.length > 0;
      }
      if (any) {
        for (const r of raws) {
          const parts = [];
          if (r.fill) parts.push("fill " + r.fill);
          if (r.stroke) parts.push("stroke " + r.stroke);
          add("unbound-color", r.n, parts.join(" and ") + " has no style or variable; bind it to one of the file's");
        }
      }
    }
    // Headings, top to bottom within each screen: one that jumps down more than a level.
    if (headings.length > 1 && on("heading-order")) {
      const screens = new Map();
      for (const x of headings) {
        if (!screens.has(x.screen)) screens.set(x.screen, []);
        screens.get(x.screen).push(x);
      }
      for (const list of screens.values()) {
        list.sort((a, b) => a.y - b.y || a.x - b.x);
        for (let i = 1; i < list.length; i++) {
          const prev = list[i - 1].level, cur = list[i].level;
          if (cur > prev + 1) add("heading-order", list[i].n, "is a level " + cur + " heading right after a level " + prev + "; do not skip a level");
        }
      }
    }
  } finally {
    fig.skipInvisibleInstanceChildren = was;
  }

  if (ck) ck();  // past the deadline, the answers of the calls above were thrown away
  const rank = (id) => AUDIT_RULES.indexOf(id);
  const total = Object.keys(counts).reduce((t, k) => t + counts[k], 0);
  const all = [];
  for (const m of held.values()) for (const g of m.values()) all.push(g);
  all.sort((a, b) => rank(a.rule) - rank(b.rule) || a.seq - b.seq);
  let shown = 0;
  const out = [];
  for (const g of all.slice(0, shownMax)) {
    const o = { rule: g.rule, id: g.id, name: g.name, text: g.text };
    if (g.count > 1) { o.count = g.count; o.ids = g.ids; }
    if (opts.links && env.link) o.url = env.link(g.id);
    out.push(o);
    shown += g.count;
  }
  const byRule = {};
  for (const id of AUDIT_RULES) if (counts[id]) byRule[id] = counts[id];
  const res = { notes: out, total, more: total - shown, counts: byRule, visited };
  if (partial) res.partial = partial + "; audit a smaller layer for the rest";
  if (errors) res.errors = errors;
  // opts.stats, for tests: the most groups any rule held while walking (never more than limit)
  if (opts.stats) {
    opts.stats.held = 0;
    for (const m of held.values()) opts.stats.held = Math.max(opts.stats.held, m.size);
  }
  return res;
}
// audit:end

Object.assign(HELPERS, {
  // h.audit(layer | page | id, {only, skip, limit, minTarget, links}): what looks wrong in the layers
  // below, with what to do about it; see references/helpers.md.
  async audit(root, opts) {
    const h = this;
    return await auditRun(root, opts, {
      fig: figmaOf(h),
      ck: h && typeof h.ck === "function" ? h.ck : null,
      wait,
      resolve: (x) => HELPERS.resolve(x),
      link: (id) => HELPERS.link(id),
    });
  },
});

// ─── instance properties by their short names ───────────────────────────
// Figma wants a TEXT, BOOLEAN or INSTANCE_SWAP property by its full name
// ("✒️ Label#164:0") and a BOOLEAN as a boolean; the CLI's `variant` sends
// strings. h.variant takes "Label", "label" or "Label#164:0" for it, and
// "true"/"false".

const PROP_ID = /#\d+:\d+$/;
const PROP_DECOR = /^[^A-Za-z0-9\u0400-\u04FF]+/;  // an emoji and spaces in front: "✒️ Label"

function propBase(name) {
  return name.replace(PROP_ID, "").trim().toLowerCase();
}

function propKey(defs, name, value) {
  if (Object.prototype.hasOwnProperty.call(defs, name)) return name;
  const all = Object.keys(defs);
  // "Label#164:0" without the emoji in front: the #id alone is unique.
  const id = String(name).trim().match(PROP_ID);
  const byId = id ? all.filter((k) => k.endsWith(id[0])) : [];
  if (byId.length === 1) return byId[0];
  const want = propBase(String(name).trim());
  // The same name without its #id, then without what decorates it.
  let hits = all.filter((k) => propBase(k) === want);
  if (!hits.length) hits = all.filter((k) => propBase(k).replace(PROP_DECOR, "") === want.replace(PROP_DECOR, ""));
  if (hits.length > 1) {
    const flag = typeof value === "boolean" || /^(true|false)$/i.test(String(value));
    const swap = !flag && LINK_NODE.test(String(value).trim());
    const kind = (k) => defs[k].type;
    const narrowed = hits.filter((k) => flag ? kind(k) === "BOOLEAN"
      : swap ? kind(k) === "INSTANCE_SWAP" : kind(k) !== "BOOLEAN" && kind(k) !== "INSTANCE_SWAP");
    if (narrowed.length === 1) hits = narrowed;
  }
  const list = (keys) => keys.map((k) => k + " (" + defs[k].type + ")").join(", ");
  if (hits.length === 1) return hits[0];
  if (hits.length > 1) {
    throw new Error("h.variant: \"" + name + "\" matches several properties: " + list(hits) + "; give the full name");
  }
  throw new Error("h.variant: no property \"" + name + "\"; there are: " + list(all));
}

function propValue(def, key, value) {
  if (def.type === "BOOLEAN") {
    if (typeof value === "boolean") return value;
    if (/^(true|false)$/i.test(String(value).trim())) return /^true$/i.test(String(value).trim());
    throw new Error("h.variant: \"" + key + "\" is a BOOLEAN, so true or false, not " + JSON.stringify(value));
  }
  if (def.type === "INSTANCE_SWAP") return linkNodeId(String(value).trim());  // a component id or its link
  return value;
}

Object.assign(HELPERS, {
  async variant(instance, props) {
    if (!instance || instance.type !== "INSTANCE") throw new Error("h.variant: needs an INSTANCE, not " + nodeLabel(instance));
    const defs = instance.componentProperties;
    const out = {};
    for (const name of Object.keys(props || {})) {
      const key = propKey(defs, name, props[name]);
      out[key] = propValue(defs[key], key, props[name]);
    }
    instance.setProperties(out);
    return instance;
  },
});

// ─── the window's Recent list ────────────────────────────────────────────
// A click on a layer there selects it and brings it into view, on its own
// page. The user asked for it, so their selection and camera are ours to move.

async function selectLayer(id, ids) {
  // Several layers (the window's Show all): those on the first one's page are selected together.
  const list = Array.isArray(ids) && ids.length ? ids.map(String).slice(0, 200) : [id];
  const found = [];
  for (const one of list) {
    let node = null;
    try {
      node = nodeHere(one);
      if (node === undefined) node = await figma.getNodeByIdAsync(String(one));
    } catch (e) { node = null; }
    let page = node;
    try {
      while (page && page.type !== "PAGE") page = page.parent;
    } catch (e) { page = null; }
    found.push({ id: one, node: node && page && node !== page ? node : null, page });
  }
  const first = found.find((x) => x.node);
  const nodes = first ? found.filter((x) => x.node && x.page === first.page) : [];
  if (nodes.length) {
    try {
      if (figma.currentPage !== first.page) await figma.setCurrentPageAsync(first.page);
      // Figma keeps only the ancestor of a frame and its own child: select the specific layers
      const pick = nodes.map((x) => x.node);
      const parentsOf = new Set();
      for (const n of pick) for (let q = n.parent; q; q = q.parent) parentsOf.add(q);
      const picked = pick.filter((n) => !parentsOf.has(n));
      figma.currentPage.selection = picked;
      figma.viewport.scrollAndZoomIntoView(picked);
    } catch (e) { /* removed meanwhile, or locked away in an instance */ }
  }
  for (const x of found) figma.ui.postMessage({ type: "selected", id: x.id, ok: !!x.node });
}

// ─── hot reload ──────────────────────────────────────────────────────────
// `figaro reload` sends plugin/code.js and ui.html from disk; running them
// here replaces this code inside the running plugin, as a re-Run in Figma
// would. The new UI reconnects to the bridge on its own.

function reloadPlugin(msg) {
  try {
    new Function("figma", "__html__", String(msg.code))(figma, String(msg.html));
  } catch (e) {
    figma.ui.postMessage({ type: "error", id: msg.id, text: "reload: " + errorText(e), stack: null });
  }
}

// A previous copy of this code (before a hot reload) is still listening; it
// must stop, or every change would be counted twice.
if (typeof globalThis.__figaroDispose === "function") {
  try { globalThis.__figaroDispose(); } catch (e) { /* it was half-started anyway */ }
}
figma.on("documentchange", onDocumentChange);
globalThis.__figaroDispose = () => figma.off("documentchange", onDocumentChange);
