// Exercise the pure helpers from plugin/code.js against a stubbed Figma.
//
// The parts worth pinning down are the ones with no Figma in them: the hex
// conversion, and the order h.frame() applies auto-layout properties in —
// getting that order wrong makes Figma silently ignore the settings.
//
//     node tests/helpers.test.js
const fs = require("fs");
const path = require("path").join(__dirname, "..", "plugin", "code.js");
const src = fs.readFileSync(path, "utf8");

const order = [];   // records the sequence of property writes on a frame

function makeFrame() {
  const f = {
    _children: [],
    width: 100, height: 100,
    appendChild(c) { order.push("appendChild"); this._children.push(c); },
    resize(w, h) { order.push("resize"); this.width = w; this.height = h; },
  };
  return new Proxy(f, {
    set(t, k, v) {
      if (typeof v !== "function") order.push(String(k));
      t[k] = v;
      return true;
    },
  });
}

const figma = {
  showUI() {},
  ui: { onmessage: null, postMessage() {} },
  createFrame: makeFrame,
  currentPage: { selection: [] },
  // code.js posts the file's identity as soon as it loads.
  root: { name: "Test file", children: [{ id: "0:1" }] },
  variables: {},
  on() {}, off() {},  // code.js listens for documentchange
};

// code.js is written for the plugin sandbox: evaluate it with our stub in scope
// and hand back the HELPERS object it builds.
const { HELPERS: h, setPrint } = new Function("figma", "__html__", src +
  "\nreturn { HELPERS, setPrint: (p) => { CURRENT_PRINT = p || (() => {}); } };")(figma, "");

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}

// hex — the /255 conversion that gets hand-rolled wrong
check("hex #ffffff", h.hex("#ffffff"), { r: 1, g: 1, b: 1 });
check("hex 000000 without #", h.hex("000000"), { r: 0, g: 0, b: 0 });
check("hex shorthand #f00", h.hex("#f00"), { r: 1, g: 0, b: 0 });
check("hex #808080", h.hex("#808080"), { r: 128 / 255, g: 128 / 255, b: 128 / 255 });

let threw = false;
try { h.hex("#12"); } catch (e) { threw = /expected #RGB/.test(e.message); }
check("hex rejects garbage", threw, true);

// solid
check("solid paint", h.solid("#ff0000"),
      [{ type: "SOLID", color: { r: 1, g: 0, b: 0 } }]);
check("solid with opacity", h.solid("#ff0000", 0.5),
      [{ type: "SOLID", color: { r: 1, g: 0, b: 0 }, opacity: 0.5 }]);

// frame — the whole point is the order Figma demands
order.length = 0;
const parent = makeFrame();
const f = h.frame(parent, {
  name: "Card", layout: "V", spacing: 16, padding: [24, 12], radius: 8,
  fill: "#ffffff", align: { primary: "CENTER", counter: "MIN" },
});
const idx = (k) => order.indexOf(k);
check("appendChild before layoutMode", idx("appendChild") < idx("layoutMode"), true);
check("layoutMode before sizing", idx("layoutMode") < idx("primaryAxisSizingMode"), true);
check("sizing before itemSpacing", idx("primaryAxisSizingMode") < idx("itemSpacing"), true);
check("hugs both axes when unsized", [f.primaryAxisSizingMode, f.counterAxisSizingMode],
      ["AUTO", "AUTO"]);
check("padding [v,h] expands", [f.paddingTop, f.paddingRight, f.paddingBottom, f.paddingLeft],
      [24, 12, 24, 12]);
check("layout shorthand V", f.layoutMode, "VERTICAL");
check("fill from hex", f.fills, [{ type: "SOLID", color: { r: 1, g: 1, b: 1 } }]);
check("align applied", [f.primaryAxisAlignItems, f.counterAxisAlignItems], ["CENTER", "MIN"]);

// an axis pinned to a number is FIXED, the other hugs
order.length = 0;
const fixed = h.frame(parent, { layout: "H", w: 320 });
check("pinned width is FIXED", fixed.primaryAxisSizingMode, "FIXED");
check("pinned width still hugs height", fixed.counterAxisSizingMode, "AUTO");
check("resize after layoutMode", order.indexOf("layoutMode") < order.indexOf("resize"), true);
check("sizing after resize", order.indexOf("resize") < order.indexOf("primaryAxisSizingMode"), true);
// resize() fixes only an axis whose size changes, and a new frame is 100 × 100
const tall = h.frame(parent, { layout: "V", w: 200, h: 100 });
check("h: 100, the size it had, is FIXED too",
      [tall.primaryAxisSizingMode, tall.counterAxisSizingMode], ["FIXED", "FIXED"]);
const free = h.frame(parent, { layout: "V", w: 200, hug: false });
check("hug: false leaves the free axis alone",
      [free.primaryAxisSizingMode, free.counterAxisSizingMode], [undefined, "FIXED"]);

// cloneNext — next to the original, right after it among its siblings
{
  const layer = (id, x) => ({
    id, type: "FRAME", x, y: 0, width: 100, height: 50, parent: null,
    clone() { return layer(id + "c", 0); },
  });
  const row = { type: "FRAME", children: [] };
  row.insertChild = (i, c) => { row.children.splice(i, 0, c); c.parent = row; };
  const a = layer("a", 0), b = layer("b", 200);
  row.children.push(a, b);
  a.parent = b.parent = row;
  const c = h.cloneNext(a, { direction: "RIGHT", gap: 20 });
  check("cloneNext: right after the original", row.children.map((n) => n.id), ["a", "ac", "b"]);
  check("cloneNext: direction in any case", [c.x, c.y], [120, 0]);
  let why = "";
  try { h.cloneNext(a, { direction: "sideways" }); } catch (e) { why = e.message; }
  check("cloneNext: an unknown direction fails", /right, left, down or up/.test(why), true);
  const page = { type: "PAGE", parent: { type: "DOCUMENT" }, clone() { throw new Error("cloned a page"); } };
  why = "";
  try { h.cloneNext(page); } catch (e) { why = e.message; }
  check("cloneNext: a page is refused before it is copied", /not a page/.test(why), true);
}

// numeric padding
const p = h.frame(parent, { layout: "V", padding: 20 });
check("numeric padding", [p.paddingTop, p.paddingRight], [20, 20]);
const sides = (f) => [f.paddingTop, f.paddingRight, f.paddingBottom, f.paddingLeft];
check("padding [t, r, b, l] as in CSS", sides(h.frame(parent, { layout: "V", padding: [1, 2, 3, 4] })), [1, 2, 3, 4]);
check("padding [t, h, b] as in CSS", sides(h.frame(parent, { layout: "V", padding: [1, 2, 3] })), [1, 2, 3, 2]);
check("padding as text of a number", sides(h.frame(parent, { layout: "V", padding: "12" })), [12, 12, 12, 12]);
check("padding [] is none", sides(h.frame(parent, { layout: "V", padding: [] })), [0, 0, 0, 0]);

// sel
figma.currentPage.selection = [
  { id: "1:2", name: "Btn", type: "FRAME", width: 100, height: 40 },
];
check("sel maps selection", h.sel(), [
  { id: "1:2", name: "Btn", type: "FRAME", w: 100, h: 40 },
]);

// annotations — only annotated layers, visible through the parents, the frame they sit in
(async () => {
  const mk = (id, name, extra, kids) => {
    const n = Object.assign({ id, name, type: "FRAME", visible: true, children: kids || [] }, extra);
    n.children.forEach((c) => { c.parent = n; });
    return n;
  };
  // What Figma gives back: label and labelMarkdown both, or only properties.
  const note = mk("3:1", "Note", { type: "TEXT", annotations: [
    { label: "A", labelMarkdown: "A" }, { label: "B text", labelMarkdown: "**B** text" }] });
  const inHidden = mk("4:1", "Deep", { annotations: [{ label: "C", labelMarkdown: "C" }] });
  const hiddenBox = mk("4:0", "Box", { visible: false }, [inHidden]);
  const plain = mk("5:1", "Plain", {});
  const pinned = mk("6:1", "Pinned", { annotations: [{ properties: [{ type: "width" }, { type: "fills" }] }] });
  const card = mk("2:1", "Card", {}, [note, hiddenBox, plain, pinned]);
  const section = mk("1:5", "Sec", { type: "SECTION" }, [card]);
  const deep = mk("8:1", "D", { annotations: [{ label: "D", labelMarkdown: "D" }] });
  const innerCard = mk("7:1", "Inner card", {}, [deep]);
  const outer = mk("1:6", "Outer", { type: "SECTION" }, [mk("1:7", "Inner", { type: "SECTION" }, [innerCard])]);
  const page = mk("0:1", "Page", { type: "PAGE" }, [section, outer]);
  figma.currentPage = page; page.selection = [];
  const r = await h.annotations();
  check("annotations: only annotated layers, the page by default", r.map((x) => x.node),
        ["3:1", "4:1", "6:1", "8:1"]);
  check("annotations: the plain labels", r[0].labels, ["A", "B text"]);
  check("annotations: visible through a hidden parent", r.map((x) => x.visible), [true, false, true, true]);
  check("annotations: the frame inside sections, nested ones too", r.map((x) => x.frame),
        ["Card", "Card", "Card", "Inner card"]);
  check("annotations: pinned properties, and no label for them", [r[2].labels, r[2].properties],
        [[], ["width", "fills"]]);
  check("annotations: no properties field without pinned ones", "properties" in r[0], false);
  check("annotations: scoped to a node", (await h.annotations(hiddenBox)).map((x) => x.node), ["4:1"]);
  page.selection = [innerCard, card, note];  // the note is inside the card: listed once
  check("annotations: every selected layer, each annotation once",
        (await h.annotations()).map((x) => x.node), ["8:1", "3:1", "4:1", "6:1"]);
  check("annotations: \"sel\" is the selection too", (await h.annotations("sel")).length, 4);
  page.selection = [];
  let why = null;
  try { await h.annotations("sel"); } catch (e) { why = e.message; }
  check("annotations: \"sel\" with nothing selected", why, "nothing selected in Figma");
  // A long walk breathes and asks the script's h.ck() whether to stop.
  const now = Date.now;
  let clock = now();
  Date.now = () => (clock += 100);
  why = null;
  try {
    await h.annotations.call({ ck() { throw new Error("stopped"); } }, page);
  } catch (e) { why = e.message; }
  Date.now = now;
  check("annotations: stops when h.ck() says so", why, "stopped");

  // h.bF and h.bS say when binding changed the paint's opacity (#11). On the layer, a paint bound
  // to a colour variable takes the colour's alpha: a fill at 10% bound to an opaque colour is at
  // 100%. The stub's layer does that on assignment, as Figma does.
  const vars = {
    "VariableID:1:1": { id: "VariableID:1:1", name: "brand", a: 1 },
    "VariableID:1:2": { id: "VariableID:1:2", name: "scrim", a: 0.5 },
  };
  figma.variables.getVariableByIdAsync = async (id) => vars[id] || null;
  figma.variables.setBoundVariableForPaint = (paint, field, v) => Object.assign({}, paint,
    { boundVariables: { [field]: { type: "VARIABLE_ALIAS", id: v.id } } });
  const resolve = (paints) => paints.map((p) => p.boundVariables
    ? Object.assign({}, p, { opacity: vars[p.boundVariables.color.id].a }) : p);
  const layer = (name, fills, strokes) => {
    const n = { name, _f: fills, _s: strokes || [] };
    Object.defineProperty(n, "fills", { get() { return this._f; }, set(v) { this._f = resolve(v); } });
    Object.defineProperty(n, "strokes", { get() { return this._s; }, set(v) { this._s = resolve(v); } });
    return n;
  };
  const solid = (opacity) => Object.assign({ type: "SOLID", color: { r: 0, g: 0, b: 0 } },
    opacity === undefined ? {} : { opacity });
  const printed = [];
  const run = (fn) => (async () => { setPrint((line) => printed.push(line)); try { await fn(); } finally { setPrint(null); } })();
  const box = layer("Box", [solid(0.1), solid(1)], [solid(0.25)]);
  await run(async () => {
    await h.bF(box, 0, "VariableID:1:1");
    await h.bF(box, 1, "VariableID:1:1");
    await h.bS(box, 0, "VariableID:1:2");
  });
  check("bF/bS: the paint is bound, at the colour's alpha", [box.fills.map((p) => p.opacity), box.strokes[0].opacity,
    box.fills[0].boundVariables.color.id], [[1, 1], 0.5, "VariableID:1:1"]);
  check("…and a changed opacity is told, once per paint that changed", printed, [
    "⚠ h.bF: the fill of \"Box\" was at 10%, now 100%: a fill bound to a colour variable takes the colour's alpha. " +
      "For 10%, bind a variable whose colour has that alpha (the layer's opacity would fade all of the layer, " +
      "not only its fill)",
    "⚠ h.bS: the stroke of \"Box\" was at 25%, now 50%: a stroke bound to a colour variable takes the colour's " +
      "alpha. For 25%, bind a variable whose colour has that alpha (the layer's opacity would fade all of the " +
      "layer, not only its stroke)",
  ]);
  console.log(failed ? `\n${failed} FAILED` : "\nall helper checks passed");
  process.exit(failed ? 1 : 0);
})();
