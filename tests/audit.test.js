// h.audit on a stub of Figma: every rule with a case it flags and a case it must stay quiet about,
// the cap and the grouping, the options, and that the walk is bounded and asks h.ck().
// Rules and thresholds are in skill/figaro/references/helpers.md ("Checking your work").
//
//     node tests/audit.test.js
const fs = require("fs");
const path = require("path").join(__dirname, "..", "plugin", "code.js");
const src = fs.readFileSync(path, "utf8");

const MIXED = Symbol("mixed");
const LOCAL = { text: [], paint: [], vars: [] };  // what the stubbed file has
const STYLES = {};                                // style id -> {name}
const figma = {
  showUI() {}, ui: { onmessage: null, postMessage() {} },
  currentPage: { selection: [] },
  root: { name: "Test file", children: [{ id: "0:1" }] },
  variables: { getLocalVariablesAsync: async () => LOCAL.vars },
  mixed: MIXED,
  skipInvisibleInstanceChildren: false,
  getLocalTextStylesAsync: async () => LOCAL.text,
  getLocalPaintStylesAsync: async () => LOCAL.paint,
  getStyleByIdAsync: async (id) => STYLES[id] || null,
  on() {}, off() {},
};
const h = new Function("figma", "__html__", src + "\nreturn HELPERS;")(figma, "");

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}

// ── a tiny file of stub layers. Coordinates are absolute and the parent sits at 0,0.
let seq = 100;
const hex = (s) => h.hex(s);
const solid = (s, opacity) => ({ type: "SOLID", color: hex(s), opacity: opacity === undefined ? 1 : opacity, visible: true });
function N(type, props, kids) {
  const n = Object.assign({
    id: "t:" + (++seq), name: type.toLowerCase(), type, visible: true, x: 0, y: 0, width: 100, height: 40,
    opacity: 1, blendMode: "PASS_THROUGH", fills: [], strokes: [], strokeWeight: 0, effects: [],
  }, props);
  if (kids) { n.children = kids.slice(); n.children.forEach((c) => { c.parent = n; }); }
  if (!n.absoluteBoundingBox) n.absoluteBoundingBox = { x: n.x, y: n.y, width: n.width, height: n.height };
  return n;
}
// a frame: auto-layout, white
const F = (kids, props) => N("FRAME", Object.assign({
  layoutMode: "VERTICAL", fills: [solid("#FFFFFF")], clipsContent: false, name: "Card",
}, props), kids || []);
// a wrapper that paints nothing, so a colour rule has only what a case puts in it to judge
const W = (kids) => F(kids, { fills: [], name: "Wrapper" });
const TX = (chars, props) => {
  const t = N("TEXT", Object.assign({
    characters: chars, name: chars, fontSize: 14, fontWeight: 400, fills: [solid("#111111")], fillStyleId: "",
    textStyleId: "", hasMissingFont: false, textAutoResize: "WIDTH_AND_HEIGHT", textTruncation: "DISABLED",
    width: 80, height: 16,
  }, props));
  if (!t.absoluteRenderBounds) t.absoluteRenderBounds = Object.assign({}, t.absoluteBoundingBox);
  if (!t.getStyledTextSegments) {
    t.getStyledTextSegments = () => (t.segments || [{ characters: chars, fills: t.fills, fontSize: t.fontSize,
      fontWeight: t.fontWeight, textStyleId: t.textStyleId, fillStyleId: t.fillStyleId }]);
  }
  return t;
};
const VEC = (color, props) => N("VECTOR", Object.assign({ width: 16, height: 16, fills: [solid(color)] }, props));
const RECT = (color, props) => N("RECTANGLE", Object.assign({ fills: [solid(color)] }, props));

async function audit(root, opts) { return await h.audit(root, Object.assign({ limit: 500, group: false }, opts)); }
const only = async (rule, root, opts) => (await audit(root, Object.assign({ only: [rule] }, opts))).notes;
const ids = (notes) => notes.map((n) => n.id);

(async () => {
  // ── missing-font
  {
    const bad = TX("Hello", { hasMissingFont: true }), ok = TX("Hi");
    const r = await only("missing-font", F([bad, ok]));
    check("missing-font: flags the text with a missing font", ids(r), [bad.id]);
  }

  // ── placeholder-text
  {
    const t = [TX("TBC"), TX("Lorem ipsum dolor"), TX("Price: tbd"), TX("Text")];
    const q = [TX("Total balance"), TX("To do list"), TX("Button"), TX("Pay abc")];
    const r = await only("placeholder-text", F(t.concat(q)));
    check("placeholder-text: TBC, lorem, tbd inside a line, a bare Text", ids(r), ids(t));
    // a text override inside an instance counts; the rest of the instance is not walked
    const inner = TX("TBC"), other = TX("Pay now", { fills: [solid("#EEEEEE")] });
    const inst = N("INSTANCE", { name: "Label", children: [inner, other] });
    inst.children.forEach((c) => { c.parent = inst; });
    inst.findAllWithCriteria = () => [inner, other];
    const r2 = await only("placeholder-text", F([inst]));
    check("placeholder-text: a text inside an instance", ids(r2), [inner.id]);
    const r3 = await only("low-contrast", F([inst]));
    check("instances: only their texts are looked at, not their insides", r3.length, 0);
  }

  // ── zero-size
  {
    const flat = F([], { name: "Spacer", width: 0.2, height: 40 });
    const blank = TX(" ", { width: 0 });
    const line = N("LINE", { width: 80, height: 0 });
    const r = await only("zero-size", F([flat, blank, line, RECT("#CCCCCC")]));
    check("zero-size: a sliver and a blank text (once each), not a line", ids(r), [flat.id, blank.id]);
  }

  // ── low-contrast: WCAG AA, 4.5:1, large text 3:1
  {
    const grey = TX("light", { fills: [solid("#AAAAAA")] });                     // 2.32
    const bigGrey = TX("Big", { fills: [solid("#999999")], fontSize: 24 });      // 2.85 < 3
    const bigOk = TX("Big", { fills: [solid("#777777")], fontSize: 24 });        // 4.48 >= 3
    const boldLarge = TX("Bold", { fills: [solid("#999999")], fontSize: 19, fontWeight: 700 });  // 2.85 < 3, large
    const boldOk = TX("Bold", { fills: [solid("#777777")], fontSize: 19, fontWeight: 700 });     // large bold, 3:1
    const smallBold = TX("Small", { fills: [solid("#777777")], fontSize: 14, fontWeight: 700 }); // 4.48 < 4.5
    const pass = TX("ok", { fills: [solid("#767676")] });                        // 4.54
    const faded = TX("faded", { fills: [solid("#000000")], opacity: 0.3 });
    const solidish = TX("solid", { fills: [solid("#000000")], opacity: 0.9 });
    const lightPaint = TX("paint", { fills: [solid("#000000", 0.25)] });
    const r = await only("low-contrast", F([grey, bigGrey, bigOk, boldLarge, boldOk, smallBold, pass, faded, solidish, lightPaint]));
    check("low-contrast: below 4.5 for text, below 3 for large text, opacity counts",
          ids(r), [grey.id, bigGrey.id, boldLarge.id, smallBold.id, faded.id, lightPaint.id]);
    check("low-contrast: the note says the ratio, the colours and the need",
          r[0].text, "contrast 2.32:1 of #AAAAAA on #FFFFFF, needs 4.5:1; use a stronger colour");
    check("low-contrast: large text says so", /needs 3:1 for large text/.test(r[1].text), true);
    check("low-contrast: a ratio is rounded down, never up to the threshold",
          /^contrast 4\.4[0-9]:1/.test(r[3].text), true);
    // the edges of "large": 24 px, or 18.66 px and bold (700); #777777 is 4.48:1, so only large text passes
    const edge = (props) => TX("edge", Object.assign({ fills: [solid("#777777")] }, props));
    const e24 = edge({ fontSize: 24 }), e235 = edge({ fontSize: 23.5 }), reg20 = edge({ fontSize: 20 });
    const b1866 = edge({ fontSize: 18.66, fontWeight: 700 }), b185 = edge({ fontSize: 18.5, fontWeight: 700 });
    const b600 = edge({ fontSize: 19, fontWeight: 600 }), b700 = edge({ fontSize: 19, fontWeight: 700 });
    check("low-contrast: large is 24 px, or 18.66 px and bold 700 — not 23.5, not regular 20, not 18.5 bold, not 600",
          ids(await only("low-contrast", F([e24, e235, reg20, b1866, b185, b600, b700]))), [e235.id, reg20.id, b185.id, b600.id]);
    // opacity of a layer above counts, though it has no fill of its own
    const dim = F([TX("dim", { fills: [solid("#000000")] })], { fills: [], opacity: 0.3 });
    check("low-contrast: the opacity of a parent counts", (await only("low-contrast", F([dim]))).length, 1);
    const full = F([TX("dim", { fills: [solid("#000000")] })], { fills: [], opacity: 1 });
    check("low-contrast: a parent at full opacity does not", (await only("low-contrast", F([full]))).length, 0);
    // disabled controls are exempt (WCAG 1.4.3); a tab or item that is only not selected ("inactive") is not
    const weak = (props) => TX("x", Object.assign({ fills: [solid("#AAAAAA")] }, props));
    const under1 = async (name) => (await only("low-contrast", F([F([weak()], { name })]))).length;
    for (const name of ["Button / Disabled", "Disabled=True", "disabled=true", "Size=M, Disabled=True", "Disabled=Yes", "State=Disabled", "Size=M, State=Disabled", "Disabled"]) {
      check('low-contrast: exempt under "' + name + '"', await under1(name), 0);
    }
    for (const name of ["Disabled=False", "Size=M, Disabled=False", "Disabled=No", "State=Inactive", "Tab / Inactive", "Inactive projects", "Size=M, State=Default", "Enabled=True", "Button"]) {
      check('low-contrast: judged under "' + name + '"', await under1(name), 1);
    }
    check("low-contrast: exempt by the text's own name", (await only("low-contrast", F([weak({ name: "Label disabled" })]))).length, 0);
    check("low-contrast: not by the text's own name inactive", (await only("low-contrast", F([weak({ name: "Label inactive" })]))).length, 1);
    const withProps = async (props) => {
      const inst = N("INSTANCE", { name: "Field", componentProperties: props });
      const t = weak();
      inst.children = [t]; t.parent = inst; F([inst]);
      return (await only("low-contrast", t)).length;
    };
    check("low-contrast: exempt for an instance variant State=Disabled", await withProps({ State: { type: "VARIANT", value: "Disabled" } }), 0);
    check("low-contrast: exempt for an instance boolean Disabled#1:0 on", await withProps({ "Disabled#1:0": { type: "BOOLEAN", value: true } }), 0);
    check("low-contrast: exempt for an instance variant Disabled=True", await withProps({ Disabled: { type: "VARIANT", value: "True" } }), 0);
    check("low-contrast: judged for Disabled off, State=Inactive, State=Default", [
      await withProps({ "Disabled#1:0": { type: "BOOLEAN", value: false } }),
      await withProps({ Disabled: { type: "VARIANT", value: "False" } }),
      await withProps({ State: { type: "VARIANT", value: "Inactive" } }),
      await withProps({ State: { type: "VARIANT", value: "Default" } })], [1, 1, 1, 1]);
    // the answer for a layer is remembered for the others below the same parent: two texts, both ways
    const offTree = F([F([weak(), weak(), weak()], { name: "Button / Disabled" }), F([weak(), weak(), weak()], { name: "Button" })]);
    check("low-contrast: the remembered answer is the true one for the layers beside it (3 disabled, 3 not)",
          (await only("low-contrast", offTree, { group: false })).length, 3);
    // (and it is asked once per parent, not once per text: reads of the parent's name, two for the walk itself)
    let reads = 0;
    const counted = F([weak(), weak(), weak()], { name: "Card" });
    Object.defineProperty(counted, "name", { get() { reads++; return "Card"; } });
    await only("low-contrast", F([counted]));
    check("low-contrast: whether a parent is disabled is worked out once for all its texts", reads, 3);
    const deep = F([F([F([weak(), weak()], { name: "inner" })], { name: "Disabled=True" })]);
    check("low-contrast: a disabled layer high above exempts what is deep below, every one", (await only("low-contrast", deep)).length, 0);
    const onWord = TX("Disabled", { fills: [solid("#AAAAAA")] });  // its name is just its words
    check("low-contrast: a text that only says Disabled is still judged", (await only("low-contrast", F([onWord]))).length, 1);
    // mixed runs: one weak run is enough
    const mixed = TX("strong weak", { fills: MIXED, segments: [
      { characters: "strong ", fills: [solid("#111111")], fontSize: 14, fontWeight: 400 },
      { characters: "weak", fills: [solid("#CCCCCC")], fontSize: 14, fontWeight: 400 }] });
    check("low-contrast: a weak run in mixed text", ids(await only("low-contrast", F([mixed]))), [mixed.id]);
    // what is behind: a gradient, an image, a translucent paint, nothing at all — unknown, so quiet
    const under = (fills, props) => F([TX("x", { fills: [solid("#AAAAAA")] })], Object.assign({ fills }, props));
    const gradient = under([{ type: "GRADIENT_LINEAR", visible: true, gradientStops: [] }]);
    const image = under([{ type: "IMAGE", visible: true }]);
    const sheer = under([solid("#000000", 0.5)]);
    const nothing = under([]);
    const dimmed = under([solid("#000000")], { opacity: 0.5 });
    for (const [what, f] of [["a gradient", gradient], ["an image", image], ["a translucent paint", sheer], ["no fill up to the page", nothing], ["a faded frame", dimmed]]) {
      check("low-contrast: quiet over " + what, (await only("low-contrast", f)).length, 0);
    }
    // a frame without a fill takes the one above it
    const outer = F([F([TX("x", { fills: [solid("#AAAAAA")] })], { fills: [] })], { fills: [solid("#FFFFFF")] });
    check("low-contrast: a frame with no fill shows the one above", (await only("low-contrast", outer)).length, 1);
    // a backdrop rectangle of its own, below the text, wins over the frame
    const onDark = TX("white", { fills: [solid("#FFFFFF")], x: 10, y: 10 });
    const greyOnDark = TX("dark", { fills: [solid("#222222")], x: 10, y: 20 });
    const stack = F([RECT("#1A1A1A", { width: 200, height: 40 }), onDark, greyOnDark], { layoutMode: "NONE" });
    check("low-contrast: the rectangle behind the text is its backdrop", ids(await only("low-contrast", stack)), [greyOnDark.id]);
    const far = TX("away", { fills: [solid("#FFFFFF")], x: 300, y: 10 });
    const beside = F([RECT("#1A1A1A", { width: 100, height: 40 }), far], { layoutMode: "NONE" });
    check("low-contrast: a rectangle that does not reach the text is not its backdrop", ids(await only("low-contrast", beside)), [far.id]);
    // the root's own ancestors count: auditing a text alone
    const lone = TX("alone", { fills: [solid("#AAAAAA")] });
    F([lone], { fills: [solid("#000000")] });
    check("low-contrast: a text audited alone looks at its parents", (await only("low-contrast", lone)).length, 0);
    const hidden = TX("hidden", { fills: [solid("#EEEEEE")], visible: false });
    check("low-contrast: hidden layers are not judged", (await only("low-contrast", F([hidden]))).length, 0);
  }

  // ── low-contrast-ui: icons at 3:1, in a control
  {
    const faint = VEC("#DDDDDD"), dark = VEC("#222222");
    const r = await only("low-contrast-ui", F([faint, dark], { name: "Close Button" }));
    check("low-contrast-ui: a pale icon in a button, not a dark one", ids(r), [faint.id]);
    check("low-contrast-ui: the note", r[0].text, "icon contrast 1.35:1 of #DDDDDD on #FFFFFF, needs 3:1; use a stronger colour");
    const edge = (hex) => only("low-contrast-ui", F([VEC(hex)], { name: "Button" })).then((x) => x.length);
    check("low-contrast-ui: 2.85:1 fails, 3.54:1 passes", [await edge("#999999"), await edge("#888888")], [1, 0]);
    check("low-contrast-ui: a decorative vector outside a control is not judged",
          (await only("low-contrast-ui", F([VEC("#DDDDDD")], { name: "Illustration" }))).length, 0);
    const near = F([F([F([VEC("#DDDDDD")], { name: "wrap 2" })], { name: "wrap 1" })], { name: "Close Button" });
    check("low-contrast-ui: a control up to four levels above counts", (await only("low-contrast-ui", near)).length, 1);
    const far = F([F([F([F([F([VEC("#DDDDDD")], { name: "w4" })], { name: "w3" })], { name: "w2" })], { name: "w1" })], { name: "Close Button" });
    check("low-contrast-ui: a control five levels above does not", (await only("low-contrast-ui", far)).length, 0);
    const click = F([VEC("#DDDDDD")], { name: "Thing", reactions: [{ trigger: { type: "ON_CLICK" }, actions: [] }] });
    check("low-contrast-ui: a click in the prototype makes a control", (await only("low-contrast-ui", click)).length, 1);
    const state = (name) => only("low-contrast-ui", F([VEC("#DDDDDD")], { name })).then((r) => r.length);
    check("low-contrast-ui: nav, menu, action, chip, badge, status, indicator names make a control, words only",
          [await state("Nav item"), await state("Menu / Row"), await state("Action bar"), await state("Status"), await state("Badge"), await state("Kind=done, Indicator"),
           await state("Chip"), await state("Navigation"), await state("Dots"), await state("Illustration"), await state("Menus")], [1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0]);
    check("low-contrast-ui: a mark on a status disc in a badge (white on green, 2.9:1)", ids(await only("low-contrast-ui", F([F([VEC("#14AE5C"), VEC("#FFFFFF")], { name: "Kind=done", layoutMode: "NONE" })], { name: "Status badge" }))).length, 1);
    check("low-contrast-ui: an inactive control is exempt",
          (await only("low-contrast-ui", F([VEC("#DDDDDD")], { name: "Button disabled" }))).length, 0);
    // a mark on a disc of its own: the disc is the backdrop; the same colour as its neighbour is a drawing's part
    const disc = VEC("#0D6EFD"), mark = VEC("#FFFFFF");
    const part = VEC("#0D6EFD");
    const r2 = await only("low-contrast-ui", F([disc, mark, part], { layoutMode: "NONE", name: "Close Button" }));
    check("low-contrast-ui: a mark on its disc, and a part of the same colour, are quiet", ids(r2), []);
    const big = VEC("#EEEEEE", { width: 300, height: 300 });
    check("low-contrast-ui: only icon-sized vectors", (await only("low-contrast-ui", F([big], { name: "Button" }))).length, 0);
    const a1 = VEC("#EEEEEE"), b1 = VEC("#EEEEEE");
    check("low-contrast-ui: one note per parent", (await only("low-contrast-ui", F([a1, b1], { name: "Button" }))).length, 1);
  }

  // ── text-clipped
  {
    const ownBox = TX("long", { textAutoResize: "NONE", height: 20, absoluteRenderBounds: { x: 0, y: 0, width: 80, height: 90 } });
    const auto = TX("long", { textAutoResize: "HEIGHT", height: 90, absoluteRenderBounds: { x: 0, y: 0, width: 80, height: 88 } });
    const truncated = TX("long", { textAutoResize: "NONE", textTruncation: "ENDING", height: 20, absoluteRenderBounds: { x: 0, y: 0, width: 80, height: 20 } });
    const snug = TX("snug", { textAutoResize: "NONE", height: 16, absoluteRenderBounds: { x: 0, y: 2, width: 80, height: 14 } });
    const r = await only("text-clipped", F([ownBox, auto, truncated, snug]));
    check("text-clipped: a fixed box the text runs out of (not auto-height, truncation or a snug fit)", ids(r), [ownBox.id]);
    // the slack is 40% of the font size (5.6 px at 14): within it, glyph overhang; past it, a line gone over
    const box = (h, w) => TX("fixed", { textAutoResize: "NONE", height: 20, absoluteRenderBounds: { x: 0, y: 0, width: w || 80, height: h } });
    const within = box(25), past = box(26), tall = box(25, 80);
    check("text-clipped: 5 px over a fixed box is slack, 6 px is not", ids(await only("text-clipped", F([within, past]))), [past.id]);
    const sideIn = TX("w", { textAutoResize: "HEIGHT", absoluteRenderBounds: { x: 0, y: 0, width: 85, height: 16 } });
    const sideOut = TX("w", { textAutoResize: "HEIGHT", absoluteRenderBounds: { x: 0, y: 0, width: 86, height: 16 } });
    check("text-clipped: the same slack on the side", ids(await only("text-clipped", F([sideIn, sideOut]))), [sideOut.id]);
    // cut by a frame that clips: the box sticks out, the part that shows reaches the frame's edge
    const cut = TX("wide", { width: 209, absoluteRenderBounds: { x: 0, y: 0, width: 100, height: 16 } });
    const clipper = F([cut], { width: 100, height: 30, clipsContent: true, name: "Frame that clips" });
    const r2 = await only("text-clipped", clipper);
    check("text-clipped: cut by the frame that clips", ids(r2), [cut.id]);
    check("text-clipped: the note names the frame", /"Frame that clips"/.test(r2[0].text), true);
    const loose = TX("wide", { width: 209, absoluteRenderBounds: { x: 0, y: 0, width: 209, height: 16 } });
    check("text-clipped: quiet when the frame does not clip",
          (await only("text-clipped", F([loose], { width: 100, height: 30, clipsContent: false }))).length, 0);
    const fits = TX("fits", { width: 60, absoluteRenderBounds: { x: 4, y: 4, width: 50, height: 14 } });
    check("text-clipped: quiet when the text is inside", (await only("text-clipped", F([fits], { width: 100, height: 30, clipsContent: true }))).length, 0);
    const shortInWide = TX("short", { width: 300, absoluteRenderBounds: { x: 0, y: 0, width: 40, height: 16 } });
    check("text-clipped: quiet when only an empty part of the box is outside",
          (await only("text-clipped", F([shortInWide], { width: 100, height: 30, clipsContent: true }))).length, 0);
  }

  // ── content-clipped: a drawing, an image, an instance or a stroke cut by a frame that clips
  {
    const clip = (kids, props) => F(kids, Object.assign({ name: "Card", width: 100, height: 100, clipsContent: true, layoutMode: "NONE" }, props));
    const image = [{ type: "IMAGE", visible: true, opacity: 1, scaleMode: "FILL" }];
    const icon = VEC("#111111", { x: 90, y: 10, width: 20, height: 20 });
    const inside = VEC("#111111", { x: 10, y: 10 });
    const away = VEC("#111111", { x: 200, y: 10 });
    const r = await only("content-clipped", clip([icon, inside, away]));
    check("content-clipped: an icon half out of its card, not one inside or one wholly outside", ids(r), [icon.id]);
    check("content-clipped: the note names the frame, the side and how much",
          r[0].text, 'sticks out of the frame "Card", which clips its content, so 10 px of it is cut off on the right; move it inside, or turn the clipping off');
    const top = VEC("#111111", { x: 10, y: -6 });
    check("content-clipped: the side it is cut on", /6 px of it is cut off at the top/.test((await only("content-clipped", clip([top])))[0].text), true);
    const within = VEC("#111111", { x: 84.4 }), past = VEC("#111111", { x: 84.6, y: 30 });
    check("content-clipped: 0.5 px is an edge's anti-aliasing, 0.6 is a cut", ids(await only("content-clipped", clip([within, past]))), [past.id]);
    check("content-clipped: quiet when the frame does not clip",
          (await only("content-clipped", clip([VEC("#111111", { x: 90 })], { clipsContent: false }))).length, 0);
    const scroller = clip([VEC("#111111", { x: 90 })], { overflowDirection: "HORIZONTAL" });
    check("content-clipped: quiet in a frame that scrolls", (await only("content-clipped", scroller)).length, 0);
    // what can be cut so that it shows: drawings, instances, images, strokes; not a plain fill
    const inst = N("INSTANCE", { name: "Icon", x: 90, y: 0, width: 24, height: 24 });
    inst.findAllWithCriteria = () => [];
    const photo = RECT("#FFFFFF", { x: 70, y: 50, width: 40, height: 40, fills: image });
    const plain = RECT("#EEEEEE", { x: 70, y: 0, width: 40, height: 20 });
    check("content-clipped: an instance and an image are judged, a plain rectangle is not",
          ids(await only("content-clipped", clip([inst, photo, plain]))), [inst.id, photo.id]);
    // made to bleed: a photo cropped past both edges, a big backdrop, a fade, a glow, a mask
    const cropped = RECT("#FFFFFF", { x: -20, y: 0, width: 140, height: 60, fills: image });
    const blob = N("ELLIPSE", { x: 40, y: 40, width: 80, height: 80, fills: [solid("#2E6BFF")] });
    const fade = N("ELLIPSE", { x: 90, y: 0, width: 30, height: 30, fills: [{ type: "GRADIENT_RADIAL", visible: true, opacity: 1 }] });
    const glow = VEC("#2E6BFF", { x: 90, y: 50, effects: [{ type: "LAYER_BLUR", visible: true, radius: 20 }] });
    const mask = RECT("#000000", { x: 0, y: 0, width: 50, height: 50, isMask: true });
    const masked = VEC("#111111", { x: 90, y: 80 });
    check("content-clipped: a crop, a backdrop, a fade, a glow, a mask and what it masks are quiet",
          (await only("content-clipped", clip([cropped, blob, fade, glow, mask, masked]))).length, 0);
    // a stroke: an outside one, and a centred one wider than the tolerance, on a layer flush with the frame
    const stroked = (align, weight) => F([], { name: "Button", width: 100, height: 100, fills: [], strokes: [solid("#2E6BFF")], strokeWeight: weight, strokeAlign: align });
    const outside = stroked("OUTSIDE", 1), centre = stroked("CENTER", 2), thin = stroked("CENTER", 1), inner = stroked("INSIDE", 4);
    const s1 = await only("content-clipped", clip([outside]));
    check("content-clipped: a button's outside stroke cut by its parent", ids(s1), [outside.id]);
    check("content-clipped: the stroke's note", s1[0].text,
          'its stroke is cut off by the frame "Card", which clips its content; set the stroke inside, or make room for it');
    check("content-clipped: a centred 2 px stroke is cut, a centred 1 px or an inside one is not",
          [ids(await only("content-clipped", clip([centre]))), (await only("content-clipped", clip([thin]))).length,
           (await only("content-clipped", clip([inner]))).length], [[centre.id], 0, 0]);
    // what shows decides where Figma gives the render bounds (absoluteRenderBounds); Figma cuts them by
    // the frame that clips, so here no render box reaches past the 100 x 100 card
    const rbOf = (n, box) => Object.assign(n, { absoluteRenderBounds: box });
    const padded = rbOf(N("INSTANCE", { name: "Icon", x: 80, y: 10, width: 24, height: 24 }), { x: 82, y: 14, width: 16, height: 16 });
    padded.findAllWithCriteria = () => [];
    check("content-clipped: an icon whose padded box sticks out 4 px while its glyph is inside is quiet",
          (await only("content-clipped", clip([padded]))).length, 0);
    const halfOut = rbOf(N("INSTANCE", { name: "Icon", x: 90, y: 10, width: 24, height: 24 }), { x: 92, y: 14, width: 8, height: 16 });
    halfOut.findAllWithCriteria = () => [];
    check("content-clipped: an icon whose drawn part reaches the frame's edge is still cut",
          ids(await only("content-clipped", clip([halfOut]))), [halfOut.id]);
    const pen = (props) => rbOf(VEC("#2E6BFF", Object.assign({ x: 0, y: 50, width: 100, height: 0, fills: [], strokes: [solid("#2E6BFF")], strokeWeight: 2, strokeAlign: "CENTER" }, props)),
                                { x: 0, y: 49, width: 100, height: 2 });
    check("content-clipped: a full-width pen line with a centred 2 px stroke is quiet, its stroke stops at its ends",
          (await only("content-clipped", clip([pen()]))).length, 0);
    const flush = rbOf(stroked("OUTSIDE", 1), { x: 0, y: 0, width: 100, height: 100 });
    check("content-clipped: with render bounds, which stop at the frame's edge, a button's outside stroke is still cut",
          ids(await only("content-clipped", clip([flush]))), [flush.id]);
    const bare = VEC("#2E6BFF", { x: 0, y: 50, width: 100, height: 0, fills: [], strokes: [solid("#2E6BFF")], strokeWeight: 2, strokeAlign: "CENTER" });
    check("content-clipped: the same line is quiet when Figma gives no render bounds",
          (await only("content-clipped", clip([bare]))).length, 0);
    const round = pen({ strokeCap: "ROUND" });
    check("content-clipped: a full-width line with round caps is cut, the caps reach past its ends",
          ids(await only("content-clipped", clip([round]))), [round.id]);
    const shadowed = rbOf(F([], { name: "Chip", x: 70, y: 70, width: 20, height: 20, effects: [{ type: "DROP_SHADOW", visible: true, radius: 24, spread: 0, offset: { x: 0, y: 8 } }] }),
                          { x: 46, y: 46, width: 68, height: 76 });
    const shadowIcon = rbOf(VEC("#111111", { x: 10, y: 10, effects: [{ type: "DROP_SHADOW", visible: true, radius: 40, spread: 0, offset: { x: 0, y: 0 } }] }), { x: -30, y: -30, width: 96, height: 96 });
    check("content-clipped: a shadow reaching past the frame is no note",
          (await only("content-clipped", clip([shadowed, shadowIcon]))).length, 0);
    // rounded corners: a square logo in a 20 x 20 frame with radius 10 comes out a circle
    const ring = (kid, props) => clip([kid], Object.assign({ name: "Logo wrap", width: 20, height: 20, cornerRadius: 10 }, props));
    const logo = RECT("#FFFFFF", { name: "Logo", width: 20, height: 20, fills: image });
    const c1 = await only("content-clipped", ring(logo));
    check("content-clipped: a square logo cut into a circle by its frame", ids(c1), [logo.id]);
    check("content-clipped: the corners' note", c1[0].text,
          'is cut by the rounded corners (radius 10) of the frame "Logo wrap", which clips its content; fine for an avatar or a photo, otherwise round the layer itself or turn the clipping off');
    const logoInst = N("INSTANCE", { name: "Visa", width: 20, height: 20 });
    logoInst.findAllWithCriteria = () => [];
    check("content-clipped: an instance of a logo too", ids(await only("content-clipped", ring(logoInst))), [logoInst.id]);
    const roundLogo = RECT("#FFFFFF", { width: 20, height: 20, fills: image, cornerRadius: 10 });
    const inset = RECT("#FFFFFF", { x: 4, y: 4, width: 12, height: 12, fills: image });
    const dot = N("ELLIPSE", { width: 20, height: 20, fills: [solid("#2E6BFF")] });
    const flagIcon = VEC("#2E6BFF", { width: 20, height: 20 });
    const quiet = [];
    for (const kid of [roundLogo, inset, dot, flagIcon]) quiet.push((await only("content-clipped", ring(kid))).length);
    check("content-clipped: quiet for a layer as round as its frame, one inset from the corners, a circle, a drawing", quiet, [0, 0, 0, 0]);
    const avatar = RECT("#FFFFFF", { width: 40, height: 40, fills: image });
    check("content-clipped: quiet for an avatar or a photo, by name",
          [(await only("content-clipped", ring(avatar, { name: "Avatar", width: 40, height: 40, cornerRadius: 20 }))).length,
           (await only("content-clipped", ring(RECT("#FFFFFF", { name: "User photo", width: 20, height: 20, fills: image })))).length], [0, 0]);
    const cover = RECT("#FFFFFF", { width: 300, height: 200, fills: image });
    check("content-clipped: quiet for a card's small radius on its cover image",
          (await only("content-clipped", clip([cover], { width: 300, height: 200, cornerRadius: 12 }))).length, 0);
    // a frame above the audited layer clips it too: the Reviewer audits a changed layer on its own
    const lone = VEC("#111111", { x: 90, y: 10, width: 20, height: 20 });
    clip([F([lone], { name: "Row", fills: [], width: 100, height: 100 })]);
    check("content-clipped: a frame that clips above the root counts", ids(await only("content-clipped", lone)), [lone.id]);
    const cutText = TX("wide", { width: 209, absoluteRenderBounds: { x: 0, y: 0, width: 100, height: 16 } });
    clip([cutText], { width: 100, height: 30, name: "Frame that clips" });
    check("text-clipped: …and for a text", ids(await only("text-clipped", cutText)), [cutText.id]);
    // its place among the rules: after the text that is cut
    const both = await audit(clip([cutText, icon], { width: 100, height: 30 }), { only: ["text-clipped", "content-clipped", "zero-size"] });
    check("content-clipped: comes right after text-clipped", both.notes.map((n) => n.rule), ["text-clipped", "content-clipped"]);
  }

  // ── small-target: WCAG 2.2 SC 2.5.8, 24 x 24; a button is told by its name, its component or a click
  {
    const box = (name, w, hh, props) => F([], Object.assign({ name, width: w, height: hh }, props));
    const small = [box("Close Button", 20, 20), box("icon-button", 24, 18), box("tab", 40, 22), box("Nav link", 40, 16),
                   box("PrimaryBtn", 30, 20), box("Toggle", 30, 18)];
    const fine = [box("Badge", 16, 16), box("Table", 10, 10), box("Tabs", 20, 20), box("Button", 40, 40), box("Button big", 24, 24)];
    const r = await only("small-target", F(small.concat(fine)));
    check("small-target: button-like names under 24 px, in any case and camelCase", ids(r), ids(small));
    check("small-target: the note", r[0].text, 'looks like a button (it is named "Close Button") but is 20 × 20 px, under 24 × 24; make it at least that big');
    check("small-target: minTarget: 44 for a phone", ids(await only("small-target", F(fine), { minTarget: 44 })),
          [fine[3].id, fine[4].id]);
    const inst = N("INSTANCE", { name: "x", width: 18, height: 18 });
    inst.getMainComponentAsync = async () => ({ name: "Icon Button", parent: { type: "FRAME", name: "" } });
    const dot = N("INSTANCE", { name: "y", width: 8, height: 8 });
    dot.getMainComponentAsync = async () => ({ name: "Dot", parent: { type: "FRAME", name: "" } });
    const inSet = N("INSTANCE", { name: "z", width: 8, height: 8 });
    inSet.getMainComponentAsync = async () => ({ name: "Size=S", parent: { type: "COMPONENT_SET", name: "Close button" } });
    check("small-target: an instance by its component or its set",
          ids(await only("small-target", F([inst, dot, inSet]))), [inst.id, inSet.id]);
    const clicky = box("Thing", 20, 20, { reactions: [{ trigger: { type: "ON_CLICK" }, actions: [] }] });
    const pressy = box("Thing", 20, 20, { reactions: [{ trigger: { type: "ON_PRESS" }, actions: [] }] });
    check("small-target: a press in the prototype too", ids(await only("small-target", F([pressy]))), [pressy.id]);
    const hovery = box("Thing", 20, 20, { reactions: [{ trigger: { type: "ON_HOVER" }, actions: [] }] });
    check("small-target: a click in the prototype", ids(await only("small-target", F([clicky, hovery]))), [clicky.id]);
  }

  // ── no-auto-layout
  {
    const kids3 = () => [TX("a", { x: 10, y: 10 }), TX("b", { x: 10, y: 50 }), TX("c", { x: 10, y: 90 })];
    const hand = F(kids3(), { name: "Hand", layoutMode: "NONE", width: 300, height: 120 });
    const stacked = F([RECT("#888888", { width: 300, height: 120 }), TX("caption", { x: 10, y: 10 })], { layoutMode: "NONE", width: 300, height: 120, name: "Stacked" });
    const drawing = F([RECT("#333333", { x: 0, y: 20, width: 20, height: 40 }), RECT("#333333", { x: 30, y: 30, width: 20, height: 30 }),
                       RECT("#333333", { x: 60, y: 10, width: 20, height: 50 })], { layoutMode: "NONE", name: "Bars" });
    const single = F([TX("only")], { layoutMode: "NONE", name: "Single" });
    const laid = F(kids3(), { name: "Laid out", layoutMode: "VERTICAL" });
    const r = await only("no-auto-layout", F([hand, stacked, drawing, single, laid]));
    check("no-auto-layout: children placed by hand, not a stack, a drawing, one child or auto-layout", ids(r), [hand.id]);
    check("no-auto-layout: the note counts the children", /^3 children placed by hand/.test(r[0].text), true);
    const screen = F(kids3(), { name: "Screen", layoutMode: "NONE", width: 390, height: 844 });
    const page = N("PAGE", { name: "Page" }, [screen]);
    check("no-auto-layout: a screen on the page is placed by hand on purpose", (await only("no-auto-layout", page)).length, 0);
    const onPage = (w, hh) => { const f = F(kids3(), { name: "Mock", layoutMode: "NONE", width: w, height: hh }); N("PAGE", {}, [f]); return f; };
    const exempt = [onPage(450, 320), onPage(440, 320), onPage(320, 440), onPage(844, 390)];
    const caught = [onPage(450, 319), onPage(439, 320), onPage(300, 600)];
    check("no-auto-layout: landscape and portrait mocks are exempt", (await Promise.all(exempt.map((f) => only("no-auto-layout", f)))).map((r) => r.length), [0, 0, 0, 0]);
    check("no-auto-layout: just under the sizes they are not", (await Promise.all(caught.map((f) => only("no-auto-layout", f)))).map((r) => r.length), [1, 1, 1]);
    const card = F(kids3(), { name: "Card on page", layoutMode: "NONE", width: 300, height: 200 });
    N("PAGE", { name: "Page" }, [card]);
    check("no-auto-layout: a card on the page is not a screen", ids(await only("no-auto-layout", card)), [card.id]);
    const set = N("COMPONENT_SET", { name: "Set", layoutMode: "NONE" }, kids3());
    check("no-auto-layout: a component set lays out its variants by hand", (await only("no-auto-layout", set)).length, 0);
  }

  // ── empty-frame: nothing to see, and still named by Figma
  {
    const bare = F([], { name: "Frame", fills: [] }), bare12 = F([], { name: "Frame 12", fills: [] });
    const spacer = F([], { name: "AM/PM (empty in 24h)", fills: [] });
    const filled = F([], { name: "Frame 3" });
    const stroked = F([], { name: "Frame 4", fills: [], strokes: [solid("#CCCCCC")], strokeWeight: 1 });
    const shadow = F([], { name: "Frame 5", fills: [], effects: [{ type: "DROP_SHADOW", visible: true }] });
    const holder = F([RECT("#CCCCCC")], { name: "Frame 6", fills: [] });
    const mask = F([], { name: "Frame 7", fills: [], isMask: true });
    check("empty-frame: an empty, invisible frame that still has Figma's name; a named one is on purpose",
          ids(await only("empty-frame", F([bare, bare12, spacer, filled, stroked, shadow, holder, mask]))), [bare.id, bare12.id]);
  }

  // ── detached-instance
  {
    const det = F([RECT("#CCCCCC")], { name: "Chip", detachedInfo: { type: "local", componentId: "1:1" } });
    const plain = F([RECT("#CCCCCC")], { name: "Chip", detachedInfo: null });
    check("detached-instance: a frame that was an instance", ids(await only("detached-instance", F([det, plain]))), [det.id]);
  }

  // ── unbound-color: only where the file works with tokens
  {
    const raw = () => RECT("#FF0000");
    const styled = RECT("#FF0000", { fillStyleId: "S:1" });
    const bound = N("RECTANGLE", { fills: [Object.assign(solid("#000000"), { boundVariables: { color: { type: "VARIABLE_ALIAS", id: "V:1" } } })] });
    const grad = F([], { fills: [{ type: "GRADIENT_LINEAR", visible: true, gradientStops: [] }] });
    const stroke = F([RECT("#CCCCCC", { fillStyleId: "S:1" })], { fillStyleId: "S:1", strokes: [solid("#00AA00")], strokeWeight: 2 });
    const hiddenPaint = N("RECTANGLE", { fills: [Object.assign(solid("#FF0000"), { visible: false })] });
    const shape = VEC("#FF8800");
    const word = TX("word", { fills: [solid("#123456")] });
    const wordBound = TX("tok", { fills: [Object.assign(solid("#123456"), { boundVariables: { color: { type: "VARIABLE_ALIAS", id: "V:1" } } })] });
    const tree = () => W([raw(), styled, bound, grad, stroke, hiddenPaint, shape, word, wordBound]);
    // the file has no tokens, and nothing in the layers uses one: nothing to bind to
    LOCAL.paint = []; LOCAL.vars = [];
    const lonely = W([raw(), word]);
    check("unbound-color: quiet where nothing in the file is tokenised", (await only("unbound-color", lonely)).length, 0);
    // a style or variable used in the layers shows the file works with them
    const used = W([raw(), RECT("#00FF00", { fillStyleId: "S:2" })]);
    check("unbound-color: a style used in the layers turns it on", (await only("unbound-color", used)).length, 1);
    LOCAL.paint = [{ id: "S:9" }];
    const t = tree();
    const r = await only("unbound-color", t);
    const kidsOf = t.children;
    check("unbound-color: raw fills and strokes, not a style, a variable, a gradient, a hidden paint or a drawing",
          ids(r), [kidsOf[0].id, kidsOf[4].id, kidsOf[7].id]);
    // (the frame "stroke" has a style fill but a raw stroke)
    check("unbound-color: the note names the colour",
          r[0].text, "fill #FF0000 has no style or variable; bind it to one of the file's");
    check("unbound-color: a raw stroke is named too", /^stroke #00AA00 /.test(r[1].text), true);
    LOCAL.paint = []; LOCAL.vars = [{ id: "V:1" }];
    check("unbound-color: a colour variable in the file turns it on", (await only("unbound-color", W([raw()]))).length, 1);
    LOCAL.vars = [];
  }

  // ── no-text-style: only where the file has text styles
  {
    LOCAL.text = [];
    const plain = TX("plain"), styled = TX("styled", { textStyleId: "S:5" });
    check("no-text-style: quiet when the file has no styles and nothing is styled",
          (await only("no-text-style", F([plain]))).length, 0);
    check("no-text-style: a styled text in the layers shows the file uses them",
          ids(await only("no-text-style", F([plain, styled]))), [plain.id]);
    const typed = TX("typed", { boundVariables: { fontSize: { type: "VARIABLE_ALIAS", id: "V:2" } } });
    const family = TX("family", { boundVariables: { fontFamily: { type: "VARIABLE_ALIAS", id: "V:3" } } });
    const color = TX("colour only", { boundVariables: { fills: [{ type: "VARIABLE_ALIAS", id: "V:4" }] } });
    LOCAL.text = [{ id: "S:9" }];
    check("no-text-style: typography variables count as a style, a colour variable does not",
          ids(await only("no-text-style", F([typed, family, color]))), [color.id]);
    LOCAL.text = [];
    check("no-text-style: a text with typography variables shows the file works with tokens",
          ids(await only("no-text-style", F([TX("plain"), typed]))).length, 1);
    LOCAL.text = [{ id: "S:9" }];
    check("no-text-style: the file has styles", ids(await only("no-text-style", F([plain, styled]))), [plain.id]);
    const half = TX("half styled", { textStyleId: MIXED, segments: [
      { characters: "half ", textStyleId: "S:5", fills: [solid("#111111")], fontSize: 14, fontWeight: 400 },
      { characters: "styled", textStyleId: "", fills: [solid("#111111")], fontSize: 14, fontWeight: 400 }] });
    check("no-text-style: a run of mixed text without one", ids(await only("no-text-style", F([half]))), [half.id]);
    LOCAL.text = [];
  }

  // ── default-name
  {
    const names = ["Frame 12", "Frame", "Auto layout", "Card"];
    const frames = names.map((name) => F([], { name }));
    const grp = N("GROUP", { name: "Group 3" }, [RECT("#CCCCCC")]);
    const rect = RECT("#CCCCCC", { name: "Rectangle 5" });
    const r = await only("default-name", F(frames.concat([grp, rect])));
    check("default-name: Figma's own names of containers, not a rectangle or a real name",
          ids(r), [frames[0].id, frames[1].id, frames[2].id, grp.id]);
  }

  // ── uneven-siblings
  {
    const card = (w, props) => F([], Object.assign({ name: "Card", width: w, height: 40, layoutSizingHorizontal: "FIXED", layoutSizingVertical: "FIXED" }, props));
    const a = [card(300), card(300), card(296)];
    const equal = [card(200), card(200), card(200)];
    const far = [card(300), card(150)];
    const hug = [card(300, { layoutSizingHorizontal: "HUG" }), card(296, { layoutSizingHorizontal: "HUG" })];
    const tall = [card(100, { height: 40 }), card(100, { height: 44 })];
    const r = await only("uneven-siblings", F([F(a, { layoutMode: "HORIZONTAL", name: "A" }), F(equal, { layoutMode: "HORIZONTAL", name: "B" }),
      F(far, { layoutMode: "HORIZONTAL", name: "C" }), F(hug, { layoutMode: "HORIZONTAL", name: "D" }), F(tall, { layoutMode: "HORIZONTAL", name: "E" })]));
    check("uneven-siblings: same-named fixed siblings a few px apart, not equal, far apart or hugging", ids(r), [a[2].id, tall[1].id]);
    const pair = async (a, b) => {
      const kids = [card(a), card(b)];
      return ids(await only("uneven-siblings", F([F(kids, { layoutMode: "HORIZONTAL" })]))).length;
    };
    check("uneven-siblings: 8 px apart is a slip, 9 is a decision, 0.6 is a slip, 0.5 is rounding",
          [await pair(300, 292), await pair(300, 291), await pair(300, 299.4), await pair(300, 299.5)], [1, 0, 1, 0]);
    const near = await h.audit(F([F([card(300), card(299.7)], { layoutMode: "HORIZONTAL" })]), { only: ["uneven-siblings"] });
    check("uneven-siblings: 0.3 px apart is rounding: no note and no failure", [near.total, near.errors], [0, undefined]);
    check("uneven-siblings: the note", r[0].text, "is 296 px wide, its same-named siblings are 300, 296; make them equal");
  }

  // ── heading-order: by name or by the text style's name, top to bottom, within each screen
  {
    STYLES["S:h2"] = { name: "Heading/H2" };
    const h1 = TX("Title", { name: "H1", y: 0 }), h2 = TX("Sub", { textStyleId: "S:h2", name: "x", y: 40 });
    const h4 = TX("Deep", { name: "H4", y: 80 }), h3 = TX("Fine", { name: "H3", y: 120 });
    const up = TX("Again", { name: "H2", y: 160 });
    const r = await only("heading-order", F([up, h3, h4, h2, h1], { layoutMode: "NONE" }));
    check("heading-order: a level skipped going down the page (read in position, not tree order)", ids(r), [h4.id]);
    check("heading-order: the note", r[0].text, "is a level 4 heading right after a level 2; do not skip a level");
    const fine = F([TX("a", { name: "H1", y: 0 }), TX("b", { name: "H2", y: 10 }), TX("c", { name: "H3", y: 20 }), TX("d", { name: "H2", y: 30 })], { layoutMode: "NONE" });
    check("heading-order: going back up is fine", (await only("heading-order", fine)).length, 0);
    check("heading-order: a lone heading is fine", (await only("heading-order", F([TX("a", { name: "H3" })]))).length, 0);
    // a text is a heading by its whole name: "H2 2024 results" is a line of text, "H2 / Section" is not H2
    const t1 = TX("Title", { name: "H1", y: 0 });
    const words = TX("H2 2024 results", { y: 10 });           // named by its own words
    const sect = TX("Section", { name: "H4 Section", y: 20 });
    const same = TX("H4", { y: 30 });                         // a text that says H4, named by it
    check("heading-order: only a name that is exactly H1-H6 (or Heading N) makes a heading",
          (await only("heading-order", F([t1, words, sect, same], { layoutMode: "NONE" }))).length, 0);
    const wordy = TX("Section", { name: "Heading 4", y: 20 });
    check("heading-order: Heading 4 is a name too", ids(await only("heading-order", F([t1, wordy], { layoutMode: "NONE" }))), [wordy.id]);
    // screens side by side are judged one by one
    const A = F([TX("a1", { name: "H1", y: 0 }), TX("a2", { name: "H2", y: 100 })], { name: "A", layoutMode: "NONE" });
    const B = F([TX("b3", { name: "H3", y: 50 })], { name: "B", layoutMode: "NONE" });
    const C1 = TX("c1", { name: "H1", y: 0 }), C4 = TX("c4", { name: "H4", y: 10 });
    const C = F([C1, C4], { name: "C", layoutMode: "NONE" });
    const board = N("PAGE", { name: "Page" }, [A, B, C]);
    check("heading-order: screens on a page are judged apart (B starting at H3 is fine; C skips)", ids(await only("heading-order", board)), [C4.id]);
    const sectioned = N("SECTION", { name: "Sec" }, [F([TX("s1", { name: "H1", y: 0 })], { name: "S1" }), F([TX("s3", { name: "H3", y: 5 })], { name: "S2" })]);
    check("heading-order: the frames of a section are screens too", (await only("heading-order", sectioned)).length, 0);
  }

  // ── absolute-in-layout
  {
    const ab = F([], { name: "Badge", layoutPositioning: "ABSOLUTE" });
    const ring = RECT("#FF0000", { layoutPositioning: "ABSOLUTE" });
    const inflow = TX("flow");
    const r = await only("absolute-in-layout", F([inflow, ab, ring], { name: "List" }));
    check("absolute-in-layout: content placed by hand in the flow, not a shape or an in-flow layer", ids(r), [ab.id]);
    check("absolute-in-layout: not inside a frame without auto-layout",
          (await only("absolute-in-layout", F([ab], { layoutMode: "NONE" }))).length, 0);
  }

  // ── the options, the answer's form, the cap
  {
    const tree = () => F([TX("TBC", { fills: [solid("#AAAAAA")], name: "a" }), F([], { name: "Frame 3", fills: [] }), F([], { name: "Close Button", width: 20, height: 20 })]);
    const t = tree();
    const all = await audit(t);
    check("audit: sorted by importance, then in tree order",
          all.notes.map((n) => n.rule), ["low-contrast", "placeholder-text", "small-target", "empty-frame", "default-name"]);
    check("audit: counts per rule and the layers it looked at", [all.counts, all.visited, all.total, all.more],
          [{ "low-contrast": 1, "placeholder-text": 1, "small-target": 1, "empty-frame": 1, "default-name": 1 }, 4, 5, 0]);
    check("audit: a note has the rule, the layer's id and name and one line",
          Object.keys(all.notes[0]), ["rule", "id", "name", "text"]);
    const skipped = await audit(t, { skip: ["low-contrast", "empty-frame"] });
    check("audit: skip", skipped.notes.map((n) => n.rule), ["placeholder-text", "small-target", "default-name"]);
    const some = await audit(t, { only: ["default-name", "small-target"] });
    check("audit: only", some.notes.map((n) => n.rule), ["small-target", "default-name"]);
    let why = "";
    try { await audit(t, { only: ["nope"] }); } catch (e) { why = e.message; }
    check("audit: an unknown rule fails and lists the rules", /^h\.audit: no rule "nope"; there are: missing-font, text-clipped, content-clipped, zero-size,/.test(why), true);
    why = "";
    try { await h.audit({ type: "DOCUMENT" }); } catch (e) { why = e.message; }
    check("audit: the document is refused", /not the document/.test(why), true);
    const linked = await h.audit(t, { limit: 1, links: true, group: false });
    check("audit: links: true adds the link", [linked.notes.length, "url" in linked.notes[0]], [1, true]);
  }
  {
    // the cap: 120 default-named frames and 30 placeholders
    const many = [];
    for (let i = 0; i < 120; i++) many.push(F([], { name: "Frame " + i, fills: [solid("#EEEEEE")] }));
    for (let i = 0; i < 30; i++) many.push(TX("TBC"));
    const big = F(many, { name: "Big" });
    const r = await h.audit(big, { group: false });
    check("cap: the first 50 notes, the loudest first, and a count of the rest", [r.notes.length, r.total, r.more], [50, 150, 100]);
    check("cap: placeholders come before default names", [r.notes[0].rule, r.notes[29].rule, r.notes[30].rule],
          ["placeholder-text", "placeholder-text", "default-name"]);
    const small = await h.audit(big, { group: false, limit: 5 });
    check("cap: limit", [small.notes.length, small.more], [5, 145]);
    // layers with the same note come as one
    const grouped = await h.audit(big);
    check("group: the thirty placeholders are one note with their ids",
          [grouped.notes[0].rule, grouped.notes[0].count, grouped.notes[0].ids.length], ["placeholder-text", 30, 30]);
    check("group: 120 names are 120 notes, the cap holds, `more` counts layers",
          [grouped.notes.length, grouped.total, grouped.more], [50, 150, 150 - 30 - 49]);
    const ten = [];
    for (let i = 0; i < 80; i++) ten.push(TX("TBC"));
    const g2 = await h.audit(F(ten), {});
    check("group: the ids listed stop at 30", [g2.notes.length, g2.notes[0].count, g2.notes[0].ids.length], [1, 80, 30]);
  }

  // ── the cap is by rank: a flood of a quiet rule never pushes out a louder one
  {
    LOCAL.text = [{ id: "S:9" }]; LOCAL.paint = [{ id: "S:9" }];
    const texts = [];
    for (let i = 0; i < 2100; i++) texts.push(TX("t" + i, { fillStyleId: "S:1" }));
    const rects = [];
    for (let i = 0; i < 5; i++) rects.push(RECT("#FF00" + (10 + i)));
    const r = await h.audit(W(texts.concat(rects)), { group: false });
    check("cap by rank: 5 raw colours come first, then no-text-style fills the rest",
          [r.notes.slice(0, 5).map((n) => n.rule), r.notes[5].rule, r.notes.length], [Array(5).fill("unbound-color"), "no-text-style", 50]);
    check("cap by rank: total and more count every layer", [r.total, r.more, r.counts["unbound-color"], r.counts["no-text-style"]], [2105, 2055, 5, 2100]);
    // a rule never holds more than `limit` groups while walking (memory stays small on a big page)
    const stats = {};
    await h.audit(W(texts.concat(rects)), { group: false, limit: 7, stats });
    check("cap by rank: no rule holds more than limit groups in memory", stats.held, 7);
    const stats2 = {};
    await h.audit(W(texts.concat(rects)), { group: false, stats: stats2 });
    check("cap by rank: and by default 50", stats2.held, 50);
    const g = await h.audit(W(texts.concat(rects)));
    check("cap by rank: grouped, 5 colours and one group of 2100 texts", [g.notes.length, g.notes[5].count, g.more], [6, 2100, 0]);
    LOCAL.text = []; LOCAL.paint = [];
  }

  // ── a call to Figma that fails does not fail the audit
  {
    const was = [figma.getLocalTextStylesAsync, figma.getLocalPaintStylesAsync, figma.variables.getLocalVariablesAsync];
    figma.getLocalTextStylesAsync = async () => { throw new Error("did not answer"); };
    figma.getLocalPaintStylesAsync = async () => { throw new Error("did not answer"); };
    figma.variables.getLocalVariablesAsync = async () => { throw new Error("did not answer"); };
    let why = null, a = null, b = null;
    try {
      a = await h.audit(F([TX("plain")]), { only: ["no-text-style"] });
      b = await h.audit(W([RECT("#FF0000")]), { only: ["unbound-color"] });
    } catch (e) { why = e.message; }
    figma.getLocalTextStylesAsync = was[0]; figma.getLocalPaintStylesAsync = was[1]; figma.variables.getLocalVariablesAsync = was[2];
    check("audit: a rejected call for the file's styles or variables is no failure, and no tokens means no notes",
          [why, a && a.total, b && b.total], [null, 0, 0]);
  }

  // ── a layer that breaks the walk is counted, not hidden
  {
    const broken = TX("x");
    Object.defineProperty(broken, "characters", { get() { throw new Error("gone"); } });
    const r = await h.audit(F([broken, TX("TBC")]), { only: ["placeholder-text"] });
    check("errors: one layer failed; the rest is still judged and the count is in the answer", [r.total, r.errors], [1, 1]);
    check("errors: a clean walk has no errors field", "errors" in (await h.audit(F([TX("fine")]))), false);
  }

  // ── a walk that is bounded and asks h.ck()
  {
    const row = [];
    for (let i = 0; i < 200; i++) row.push(F([], { name: "Row " + i, fills: [solid("#EEEEEE")] }));
    const page = N("PAGE", { name: "Page" }, [F(row, { name: "List" })]);
    let calls = 0;
    const walked = await h.audit.call({ ck() { calls++; return true; } }, page, { only: ["default-name"] });
    check("walk: asks h.ck() as it goes", calls >= 6, true);
    check("walk: visits every layer of a page", walked.visited, 202);
    let why = "";
    try { await h.audit.call({ ck() { throw new Error("aborted: past this exec's timeout"); } }, page); } catch (e) { why = e.message; }
    check("walk: stops when h.ck() throws", /^aborted/.test(why), true);
    const cut = await h.audit(page, { maxNodes: 50 });
    check("walk: a node budget ends it with a partial answer", [cut.visited, /^stopped after 50 layers/.test(cut.partial)], [51, true]);
    const now = Date.now;
    let clock = now();
    Date.now = () => (clock += 500);
    const slow = await h.audit(page, { maxMs: 2000 });
    Date.now = now;
    check("walk: a time budget ends it too", /^stopped after 2 s/.test(slow.partial), true);
    clock = now();
    Date.now = () => (clock += 500);
    const quick = await h.audit(page, { maxMs: 300 });
    Date.now = now;
    check("walk: under a second is told in tenths", /^stopped after 0\.3 s/.test(quick.partial), true);
    // the walk gives the plugin a breath (setTimeout 0) when it has run 40 ms without one
    const realTimeout = globalThis.setTimeout;
    let breaths = 0;
    globalThis.setTimeout = (fn, ms) => { if (ms === 0) breaths++; return realTimeout(fn, 0); };
    clock = now();
    Date.now = () => (clock += 100);
    await h.audit(page, { only: ["default-name"] });
    Date.now = now;
    globalThis.setTimeout = realTimeout;
    check("walk: yields to the plugin while it runs", breaths >= 1, true);
    // depth does not matter: an iterative walk has no stack to run out of
    let deep = F([TX("TBC")], { name: "deepest" });
    for (let i = 0; i < 3000; i++) deep = F([deep], { name: "level " + i });
    check("walk: 3000 levels deep, no recursion", (await h.audit(deep, { only: ["placeholder-text"] })).total, 1);
    // a hidden subtree is skipped whole
    const secret = F([TX("TBC")], { visible: false });
    check("walk: hidden layers and what is under them are skipped", (await h.audit(F([secret]), { only: ["placeholder-text"] })).total, 0);
    figma.skipInvisibleInstanceChildren = false;
    await h.audit(page, { only: ["default-name"] });
    check("walk: leaves skipInvisibleInstanceChildren as it was", figma.skipInvisibleInstanceChildren, false);
    // a page given by name, like the other helpers take
    figma.currentPage = page;
    check("audit: \"page\" is the current page", (await h.audit("page", { only: ["default-name"] })).visited, 202);
  }

  console.log(failed ? `\n${failed} FAILED` : "\nall audit checks passed");
  process.exit(failed ? 1 : 0);
})();
