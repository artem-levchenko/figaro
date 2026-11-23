// The plugin's helpers in plugin/code.js against a stub of Figma:
// links in place of ids, quick reads, --lib, h.inspect, h.shot, fonts and
// Font Awesome, the auto-layout helpers and h.find.
//
//     node tests/commands.test.js
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "plugin", "code.js"), "utf8");

const KEY = "AbCdEfGhIjKlMnOpQrStUv";
const MIXED = Symbol("mixed");
const solid = (r, g, b, extra) => Object.assign({ type: "SOLID", visible: true, opacity: 1, color: { r, g, b } }, extra);

// A layer with what Figma's nodes do that the helpers rely on: resize() makes
// both axes FIXED, findAll skips invisible layers inside instances when
// figma.skipInvisibleInstanceChildren is on.
function makeFigma(opts) {
  opts = opts || {};
  const nodes = new Map();
  const loads = [];
  const missingFonts = new Set(opts.missingFonts || []);
  const figma = {
    mixed: MIXED,
    root: { name: opts.fileName || "Sandbox 5$", children: [] },
    fileKey: KEY,
    skipInvisibleInstanceChildren: false,
    showUI() {},
    ui: { onmessage: null, posted: [], postMessage(m) { this.posted.push(m); } },
    currentPage: { selection: [] },
    on() {}, off() {},
    commitUndo() { figma.commits = (figma.commits || 0) + 1; },
    triggerUndo() {},
    getNodeById(id) { return nodes.get(String(id)) || null; },
    async getNodeByIdAsync(id) { return nodes.get(String(id)) || null; },
    async getStyleByIdAsync(id) { return (opts.styles || {})[id] || null; },
    variables: {
      async getVariableByIdAsync(id) { return (opts.variables || {})[id] || null; },
      async getVariableCollectionByIdAsync(id) { return (opts.collections || {})[id] || null; },
    },
    async loadFontAsync(f) {
      loads.push(f.family + " " + f.style);
      if (missingFonts.has(f.family)) throw new Error("The font \"" + f.family + "\" could not be loaded");
    },
    async listAvailableFontsAsync() {
      return ["Inter|Regular", "Inter|Bold", "Font Awesome 7 Pro|Light", "Font Awesome 7 Pro|Solid",
              "Font Awesome 6 Free|Solid", "Font Awesome Kit a1b2c3d4e5|Regular",
              "Font Awesome Kit Duotone a1b2c3d4e5|Regular"]
        .map((s) => ({ fontName: { family: s.split("|")[0], style: s.split("|")[1] } }));
    },
    base64Encode(bytes) { return Buffer.from(bytes).toString("base64"); },
  };
  const proto = {
    remove() { this.removed = true; if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); },
    resize(w, h) {
      this.width = w; this.height = h;
      if ("layoutSizingHorizontal" in this) { this.layoutSizingHorizontal = "FIXED"; this.layoutSizingVertical = "FIXED"; }
    },
    findAll(fn) { return descendants(this).filter((n) => !fn || fn(n)); },
    findAllWithCriteria(q) { figma.criteria = (figma.criteria || 0) + 1; return descendants(this).filter((n) => q.types.indexOf(n.type) !== -1); },
    async exportAsync(settings) { this.exported = settings; return new Uint8Array([1, 2, 3]); },
  };
  const inInstance = (n) => { for (let p = n.parent; p; p = p.parent) if (p.type === "INSTANCE") return true; return false; };
  function descendants(n) {
    const out = [];
    for (const c of n.children || []) {
      if (figma.skipInvisibleInstanceChildren && c.visible === false && inInstance(c)) continue;
      out.push(c, ...descendants(c));
    }
    return out;
  }
  function build(spec, parent) {
    const n = Object.assign(Object.create(proto), { visible: true, removed: false }, spec, { parent: parent || null });
    if (spec.children) n.children = spec.children.map((c) => build(c, n));
    nodes.set(n.id, n);
    return n;
  }
  const env = { figma, nodes, loads, build };
  env.api = new Function("figma", "__html__", src + "\nreturn { HELPERS, LIBS };")(figma, "");
  return env;
}

let seq = 0;
async function exec(env, code, extra) {
  const id = "e" + (++seq);
  const posted = env.figma.ui.posted;
  await env.figma.ui.onmessage(Object.assign({ type: "exec", id, code, timeout: 10, quick: true }, extra));
  const reply = posted.find((m) => m.id === id && (m.type === "result" || m.type === "error"));
  const logs = [].concat(...posted.filter((m) => m.id === id && m.type === "log").map((m) => m.lines));
  const value = typeof reply.valueJson === "string" ? JSON.parse(reply.valueJson) : reply.value;
  return Object.assign({}, reply, { value, logs });
}

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}

// A card as the UI kit has them: auto-layout frame, a two-style title, a
// button instance from a library set, a hidden badge.
function card(env) {
  const set = env.build({ id: "50:1", name: "Button", type: "COMPONENT_SET", key: "setkey", remote: true,
    componentPropertyDefinitions: {
      Size: { type: "VARIANT", defaultValue: "M", variantOptions: ["S", "M", "L"] },
      "Icon#2:0": { type: "INSTANCE_SWAP", defaultValue: "9:9", preferredValues: [{ type: "COMPONENT", key: "pk1" }] },
    },
    children: [{ id: "50:2", name: "Size=M", type: "COMPONENT", key: "compkey", remote: true,
      children: [{ id: "50:3", name: "Icon", type: "FRAME", componentPropertyReferences: { visible: "Show#1:0" } }] }] });
  env.build({ id: "9:9", name: "Star", type: "COMPONENT", key: "starkey" });
  return env.build({
    id: "1:1", name: "Card", type: "FRAME", width: 320, height: 200, x: 10, y: 20,
    layoutMode: "VERTICAL", itemSpacing: 12, primaryAxisAlignItems: "MIN", counterAxisAlignItems: "CENTER",
    layoutWrap: "NO_WRAP", paddingTop: 24, paddingRight: 16, paddingBottom: 24, paddingLeft: 16,
    layoutSizingHorizontal: "FIXED", layoutSizingVertical: "HUG", layoutPositioning: "AUTO",
    cornerRadius: 12, clipsContent: true, opacity: 1, fillStyleId: "S:fill",
    fills: [solid(1, 1, 1, { boundVariables: { color: { type: "VARIABLE_ALIAS", id: "V:1" } } })],
    strokes: [], effects: [{ type: "DROP_SHADOW", visible: true, radius: 8, offset: { x: 0, y: 4 }, spread: 0,
                             color: { r: 0, g: 0, b: 0, a: 0.25 } }],
    boundVariables: { itemSpacing: { type: "VARIABLE_ALIAS", id: "V:2" } },
    explicitVariableModes: { "C:1": "m2" },
    children: [
      { id: "1:2", name: "Title", type: "TEXT", width: 288, height: 24, x: 16, y: 24,
        layoutSizingHorizontal: "FILL", layoutSizingVertical: "HUG", layoutPositioning: "AUTO",
        characters: "Hello world", fontName: MIXED, fontSize: 20, textStyleId: "S:text",
        lineHeight: { unit: "PIXELS", value: 24 }, letterSpacing: { unit: "PIXELS", value: 0 },
        textAlignHorizontal: "LEFT", textCase: "ORIGINAL", textAutoResize: "HEIGHT", hasMissingFont: false,
        hyperlink: null, fills: [solid(0, 0, 0)],
        getRangeAllFontNames() { return [{ family: "Inter", style: "Semi Bold" }, { family: "Inter", style: "Bold" }]; },
        getStyledTextSegments() {
          return [
            { characters: "Hello ", start: 0, end: 6, fontName: { family: "Inter", style: "Semi Bold" }, fontSize: 20,
              textDecoration: "NONE", textCase: "ORIGINAL", fills: [solid(0, 0, 0)], textStyleId: "S:text",
              hyperlink: null, listOptions: { type: "NONE" } },
            { characters: "world", start: 6, end: 11, fontName: { family: "Inter", style: "Bold" }, fontSize: 20,
              textDecoration: "UNDERLINE", textCase: "ORIGINAL", fills: [solid(1, 0, 0)], textStyleId: "",
              hyperlink: { type: "URL", value: "https://example.com" }, listOptions: { type: "NONE" } },
          ];
        } },
      { id: "1:3", name: "Button", type: "INSTANCE", width: 120, height: 40, x: 16, y: 60,
        layoutSizingHorizontal: "HUG", layoutSizingVertical: "HUG", layoutPositioning: "AUTO",
        async getMainComponentAsync() { return set.children[0]; },
        componentProperties: { "Label#1:0": { type: "TEXT", value: "Go" }, "Icon#2:0": { type: "INSTANCE_SWAP", value: "9:9" } },
        children: [
          { id: "I1:3;5:1", name: "Label", type: "TEXT", visible: true, characters: "Go",
            fontName: { family: "Inter", style: "Regular" }, fontSize: 14, textStyleId: "",
            lineHeight: { unit: "AUTO" }, letterSpacing: { unit: "PERCENT", value: 0 },
            textAlignHorizontal: "CENTER", textCase: "ORIGINAL", textAutoResize: "WIDTH_AND_HEIGHT",
            hasMissingFont: false, hyperlink: null,
            getStyledTextSegments() { return [{ characters: "Go", fontName: this.fontName, fontSize: 14 }]; } },
          { id: "I1:3;5:2", name: "Spinner", type: "FRAME", visible: false },
        ] },
      { id: "1:4", name: "Badge", type: "RECTANGLE", visible: false, width: 8, height: 8 },
    ],
  });
}

const kit = () => ({
  styles: { "S:fill": { id: "S:fill", name: "Surface/Card", key: "fillkey", type: "PAINT", remote: true },
            "S:text": { id: "S:text", name: "Heading/H3", key: "textkey", type: "TEXT", remote: true } },
  variables: { "V:1": { id: "V:1", name: "surface/card", key: "v1key", resolvedType: "COLOR", variableCollectionId: "C:1", remote: false },
               "V:2": { id: "V:2", name: "space/md", key: "v2key", resolvedType: "FLOAT", variableCollectionId: "C:1", remote: false } },
  collections: { "C:1": { name: "Tokens", key: "ckey", remote: false,
                          modes: [{ modeId: "m1", name: "Light" }, { modeId: "m2", name: "Dark" }] } },
});

(async () => {
  // ─── links ──────────────────────────────────────────────────────────────
  {
    const env = makeFigma();
    card(env);
    const url = "https://www.figma.com/design/" + KEY + "/Sandbox-5-?node-id=1-2&t=abc";
    let r = await exec(env, "return (await h.node(" + JSON.stringify(url) + ")).name");
    check("h.node takes a link into this file", r.value, "Title");
    r = await exec(env, "return (await h.resolve('1-3')).name");
    check("h.resolve takes a link-form id", r.value, "Button");
    r = await exec(env, "return (await h.node('I1-3;5-1')).name");
    check("an instance sublayer id in link form", r.value, "Label");
    r = await exec(env, "return await h.node('https://www.figma.com/design/QqQqQqQqQqQqQqQqQqQqQq/X?node-id=1-2')");
    check("a link into another file is refused", [r.type, /another file \(QqQqQqQqQqQqQqQqQqQqQq\).*-T/.test(r.text)], ["error", true]);
    r = await exec(env, "return await h.node('https://www.figma.com/design/" + KEY + "/X')");
    check("a link without node-id says so", [r.type, /has no node-id/.test(r.text)], ["error", true]);
    r = await exec(env, "return [h.link('1:2'), h.link(await h.node('I1:3;5:1'))]");
    check("h.link", r.value, ["https://www.figma.com/design/" + KEY + "/Sandbox-5-?node-id=1-2",
                              "https://www.figma.com/design/" + KEY + "/Sandbox-5-?node-id=I1-3%3B5-1"]);
    env.figma.currentPage.selection = [env.nodes.get("1:2")];
    r = await exec(env, "return h.sel()[0].url");
    check("h.sel gives each layer's link", r.value, "https://www.figma.com/design/" + KEY + "/Sandbox-5-?node-id=1-2");
  }

  // ─── quick reads ────────────────────────────────────────────────────────
  {
    const env = makeFigma();
    card(env);
    let t0 = Date.now();
    let r = await exec(env, "return 1");
    const quickMs = Date.now() - t0;
    check("a quick read: no undo step, no change report", [r.value, env.figma.commits || 0, "changes" in r], [1, 0, false]);
    t0 = Date.now();
    r = await exec(env, "return 1", { quick: false });
    const readMs = Date.now() - t0;
    await new Promise((resolve) => setTimeout(resolve, 200));  // the window after that read
    t0 = Date.now();
    r = await exec(env, 'const n = await h.node("1:2"); if (n.name === "nope") n.name = "x"; return 1;', { quick: false });
    const fullMs = Date.now() - t0;
    check("a normal exec that may write waits for Figma's report; a quick one, or one that only reads, does not",
      [quickMs < 60, readMs < 60, fullMs >= 140, env.figma.commits >= 4], [true, true, true, true]);
  }

  // ─── --lib ──────────────────────────────────────────────────────────────
  {
    const env = makeFigma();
    card(env);
    const grid = { hash: "h1", name: "grid.js",
      code: "export function col(n) { return n * 72; }\nconst GAP = 24;\nmodule.exports.extra = 5;\n" +
            "async function title() { print('reading'); return (await h.node('1:2')).name + ' in ' + figma.root.name; }" };
    let r = await exec(env, "return [lib.col(2), lib.GAP, lib.extra, await lib.title()]", { libs: [grid] });
    check("top-level functions, consts and module.exports become lib.*", r.value, [144, 24, 5, "Title in Sandbox 5$"]);
    check("a lib gets the script's print", r.logs, ["reading"]);
    r = await exec(env, "return lib.col(1)", { libs: [{ hash: "h1", name: "grid.js" }] });
    check("then the hash is enough", r.value, 72);
    const twice = { hash: "h2", name: "twice.js", code: "const twice = (n) => lib.col(n) * 2;" };
    r = await exec(env, "return lib.twice(1)", { libs: [{ hash: "h1", name: "grid.js" }, twice] });
    check("a later lib sees the earlier ones", r.value, 144);
    r = await exec(env, "return 1", { libs: [{ hash: "gone", name: "old.js" }] });
    check("a hash the plugin lost: error and libMissing", [r.type, r.libMissing, /old\.js/.test(r.text)], ["error", ["gone"], true]);
    const broken = { hash: "h3", name: "broken.js",
      code: "function ok() { return 1; }\n\nfunction boom(n) {\n  return n.width.height;\n}" };
    r = await exec(env, "const a = 1;\nreturn lib.boom({});", { libs: [broken] });
    check("an error inside a lib names its file and line", [r.type, r.lib], ["error", { name: "broken.js", line: 4, source: "return n.width.height;" }]);
    check("the script line that called it", [r.line, r.source], [2, "return lib.boom({});"]);
    check("the stack names the lib", /broken\.js:4:\d+/.test(r.stack), true);
    check("…then the script's line, and no other frame",
      r.stack.split("\n").filter((l) => /^\s*at\s/.test(l)).map((l) => (/(broken\.js|script):(\d+):\d+/.exec(l) || [l]).slice(1).join(":")),
      ["broken.js:4", "script:2"]);
    r = await exec(env, "return 1", { libs: [{ hash: "h4", name: "syntax.js", code: "function (" }] });
    check("a lib that does not compile", r.type, "error");
    check("…with no stack: none of its frames is the script's or a lib's", r.stack, null);
    for (let i = 0; i < 52; i++) {
      await exec(env, "return 1", { libs: [{ hash: "n" + i, name: "n.js", code: "const N = " + i }] });
      if (i >= 40) await exec(env, "return 1", { libs: [{ hash: "h1", name: "grid.js" }] });  // keep grid.js in use
    }
    check("the plugin keeps the last 50", [env.api.LIBS.size, env.api.LIBS.has("n0"), env.api.LIBS.has("n51"),
                                           env.api.LIBS.has("h1")], [50, false, true, true]);
    const bases = Array.from(env.api.LIBS.values(), (e) => e.base);
    check("no two kept libs share their lines", new Set(bases).size, bases.length);
    r = await exec(env, "return lib.boom({})", { libs: [broken] });
    check("…so an error still names the right lib", [r.lib && r.lib.name, r.lib && r.lib.line], ["broken.js", 4]);
    const sloppy = { hash: "h5", name: "sloppy.js", code: "function leak() {\n  leaked = 1;\n}" };
    r = await exec(env, "return lib.leak()", { libs: [sloppy] });
    check("a lib runs in strict mode, as a script does, and its lines stay",
      [r.type, /leaked is not defined/.test(r.text), r.lib && r.lib.line], ["error", true, 2]);
  }

  // ─── inspect ────────────────────────────────────────────────────────────
  {
    const env = makeFigma(kit());
    card(env);
    let r = await exec(env, "return await h.inspect('1:1')");
    const v = r.value;
    check("inspect: file, link, count (the hidden badge is not walked)",
      [v.file, v.fileKey, v.url, v.nodes], ["Sandbox 5$", KEY, "https://www.figma.com/design/" + KEY + "/Sandbox-5-?node-id=1-1", 4]);
    const root = v.node;
    check("layout and sizing", [root.layout, root.sizing, root.radius, root.clip],
      [{ mode: "VERTICAL", gap: 12, main: "MIN", cross: "CENTER", pad: [24, 16, 24, 16] }, "FIXED/HUG", 12, true]);
    check("the root has no position", ["x" in root, root.children[0].x], [false, 16]);
    check("fill with its style and variable", [root.fills, root.styles],
      [[{ type: "SOLID", color: "#ffffff", var: "surface/card" }], { fill: "Surface/Card" }]);
    check("shadow", root.effects, [{ type: "DROP_SHADOW", radius: 8, offset: [0, 4], color: "#000000/0.25" }]);
    check("bound variables and modes", [root.vars, root.modes], [{ itemSpacing: "space/md" }, { Tokens: "Dark" }]);
    check("hidden children counted", root.hiddenKids, 1);
    const title = root.children[0];
    check("text: font, style, auto-resize", [title.text, title.font.family, title.font.lineHeight, title.textStyle, title.autoResize],
      ["Hello world", "mixed", "24px", "Heading/H3", "HEIGHT"]);
    check("text segments", title.segments, [
      { text: "Hello ", font: "Inter Semi Bold", size: 20, style: "Heading/H3", fills: [{ type: "SOLID", color: "#000000" }] },
      { text: "world", font: "Inter Bold", size: 20, fills: [{ type: "SOLID", color: "#ff0000" }], decoration: "UNDERLINE",
        link: "https://example.com" }]);
    const button = root.children[1];
    check("instance: component and props (a swap by name)", [button.component, button.props],
      ["Button / Size=M", { "Label#1:0": "Go", "Icon#2:0": "Star" }]);
    check("invisible layers inside the instance are counted too", [button.children.length, button.hiddenKids], [1, 1]);
    // Ids too — what the file already uses goes by id, import by key may not answer
    check("keys: components", v.components, { "Button / Size=M": { id: "50:2", key: "compkey", remote: true, set: "Button", setKey: "setkey" } });
    check("keys: styles", v.styles, { "Surface/Card": { id: "S:fill", key: "fillkey", type: "PAINT", remote: true },
                                      "Heading/H3": { id: "S:text", key: "textkey", type: "TEXT", remote: true } });
    check("keys: variables", v.variables, { "surface/card": { id: "V:1", key: "v1key", type: "COLOR", collection: "Tokens", remote: false },
                                            "space/md": { id: "V:2", key: "v2key", type: "FLOAT", collection: "Tokens", remote: false } });
    check("keys: collections", v.collections, { Tokens: { key: "ckey", remote: false, modes: ["Light", "Dark"] } });

    r = await exec(env, "return await h.inspect('50:1')");
    check("a component set: key and property definitions", [r.value.node.key, r.value.node.remote, r.value.node.propDefs],
      ["setkey", true, { Size: { type: "VARIANT", default: "M", options: ["S", "M", "L"] },
                         "Icon#2:0": { type: "INSTANCE_SWAP", default: "Star", preferred: ["pk1"] } }]);
    check("a layer says which property drives it", r.value.node.children[0].children[0].refs, { visible: "Show#1:0" });
    r = await exec(env, "return await h.inspect('1:1', {depth: 0})");
    check("depth 0: the children are only counted", [r.value.nodes, r.value.node.childrenCut, "children" in r.value.node], [1, 3, false]);
    r = await exec(env, "return await h.inspect('1:1', {hidden: true})");
    check("hidden: true walks hidden layers too", [r.value.nodes, r.value.node.children.length], [6, 3]);
    r = await exec(env, "return await h.inspect('1:1', {maxNodes: 2})");
    check("maxNodes cuts the walk and says so", [r.value.nodes, r.value.cut, r.value.node.childrenCut], [2, true, 1]);
    r = await exec(env, "return await h.inspect.call({aborted: () => true}, '1:1')");
    check("an inspect past its timeout stops", [r.type, /aborted/.test(r.text)], ["error", true]);
  }

  // ─── shot ───────────────────────────────────────────────────────────────
  {
    const env = makeFigma();
    card(env);
    const wide = env.build({ id: "2:1", name: "Page", type: "FRAME", width: 3200, height: 9000 });
    const mid = env.build({ id: "2:2", name: "Hero", type: "FRAME", width: 1000, height: 600 });
    let r = await exec(env, "return [await h.shot('1:1'), await h.shot('2:2'), await h.shot('2:1')]");
    check("default scale: 2×, 1×, at most 1600 px wide", r.value.map((s) => s.scale), [2, 1, 0.5]);
    check("the picture as base64 with its link", [r.value[0].kind, r.value[0].data, r.value[0].size, r.value[0].url, r.value[0].w],
      ["shot", "AQID", 3, "https://www.figma.com/design/" + KEY + "/Sandbox-5-?node-id=1-1", 320]);
    check("export settings", [wide.exported, mid.exported],
      [{ format: "PNG", constraint: { type: "SCALE", value: 0.5 } }, { format: "PNG", constraint: { type: "SCALE", value: 1 } }]);
    await exec(env, "return await h.shot('2:2', {format: 'svg'})");
    check("svg outlines text by default", mid.exported, { format: "SVG", svgOutlineText: true, svgIdAttribute: false });
    await exec(env, "return await h.shot('2:2', {format: 'jpeg', width: 400})");
    check("jpg by width", mid.exported, { format: "JPG", constraint: { type: "WIDTH", value: 400 } });
    await exec(env, "return await h.shot('2:2', {width: 400, absolute: true})");
    check("absolute bounds only when asked", mid.exported,
      { format: "PNG", constraint: { type: "WIDTH", value: 400 }, useAbsoluteBounds: true });
    for (const f of ["svg", "pdf"]) {
      await exec(env, "return await h.shot('2:2', {format: '" + f + "', absolute: true})");
      check(f + " takes absolute bounds too", mid.exported.useAbsoluteBounds, true);
    }
    r = await exec(env, "return await h.shot('2:2', {format: 'gif'})");
    check("an unknown format", [r.type, /png, jpg, svg/.test(r.text)], ["error", true]);
    // Figma exports a hidden layer, and one inside a hidden layer, as an empty 1×1 picture (#11).
    env.build({ id: "2:5", name: "Drawer", type: "FRAME", width: 300, height: 600, visible: false,
                children: [{ id: "2:6", name: "Menu", type: "FRAME", width: 300, height: 40 }] });
    r = await exec(env, "return [await h.shot('2:5'), await h.shot('2:6'), await h.shot('2:2')]");
    check("a hidden layer says so, and one in a hidden layer names it", r.value.map((s) => s.hidden),
      [true, { id: "2:5", name: "Drawer" }, undefined]);
  }

  // ─── fonts ──────────────────────────────────────────────────────────────
  {
    const env = makeFigma({ missingFonts: ["Font Awesome 6 Pro"] });
    const icons = env.build({ id: "3:1", name: "Icons", type: "TEXT", characters: "star bell", fontName: MIXED,
      getRangeAllFontNames() { return [{ family: "Inter", style: "Regular" }, { family: "Font Awesome 6 Pro", style: "Solid" }]; } });
    const plain = env.build({ id: "3:2", name: "Plain", type: "TEXT", characters: "a", fontName: MIXED,
      getRangeAllFontNames() { return [{ family: "Inter", style: "Regular" }, { family: "Inter", style: "Bold" }]; } });
    let r = await exec(env, "return await h.fonts('3:1')".replace("'3:1'", "await h.node('3:1')"));
    check("h.fonts loads the rest and names the missing one", [r.value.loaded, r.value.missing], [1, ["Font Awesome 6 Pro Solid"]]);
    check("…with the Font Awesome installed here",
      /Font Awesome installed here: Font Awesome 7 Pro \(Light, Solid\); Font Awesome Kit a1b2c3d4e5 — custom icons/.test(r.value.note), true);
    check("…and says so in the log", r.logs.length === 1 && r.logs[0] === r.value.note, true);
    r = await exec(env, "return await h.fa()");
    check("h.fa", [r.value.pro, r.value.kit, Object.keys(r.value.families).length],
      ["Font Awesome 7 Pro", "Font Awesome Kit a1b2c3d4e5", 4]);
    r = await exec(env, "await h.setText(await h.node('3:1'), 'x'); return 1");
    check("h.setText refuses a text whose font is missing", [r.type, /fonts not installed/.test(r.text), icons.characters], ["error", true, "star bell"]);
    r = await exec(env, "await h.setText(await h.node('3:2'), 'new'); return 1");
    check("h.setText on a mixed-font text", [r.value, plain.characters, env.loads.indexOf("Inter Bold") !== -1], [1, "new", true]);
    r = await exec(env, "return await h.withFonts(await h.node('3:1'), async () => 'ran')");
    check("h.withFonts runs with a font missing", [r.value, r.logs.length], ["ran", 1]);
  }

  // ─── auto-layout ────────────────────────────────────────────────────────
  {
    const env = makeFigma();
    const box = (id, extra) => Object.assign({ id, name: id, type: "FRAME", width: 100, height: 40,
      layoutSizingHorizontal: "FIXED", layoutSizingVertical: "FIXED", layoutPositioning: "AUTO" }, extra);
    const row = env.build(box("4:1", { layoutMode: "HORIZONTAL", layoutSizingHorizontal: "HUG", children: [
      box("4:2"), Object.assign(box("4:3"), { type: "TEXT", characters: "Long text", fontName: { family: "Inter", style: "Regular" } }),
      box("4:4", { layoutPositioning: "ABSOLUTE" })] }));
    const loose = env.build(box("4:9", { children: [box("4:10")] }));
    let r = await exec(env, "const n = await h.node('4:2'); await h.fill(n); return n.layoutSizingHorizontal");
    check("h.fill", r.value, "FILL");
    check("…warns when the parent hugs that axis", /HUG/.test(r.logs[0] || ""), true);
    r = await exec(env, "await h.fill(await h.node('4:10')); return 1");
    check("h.fill outside auto-layout", [r.type, /only a child of an auto-layout frame can FILL/.test(r.text)], ["error", true]);
    r = await exec(env, "await h.fill(await h.node('4:4'), 'y'); return 1");
    check("h.fill on an absolute child", [r.type, /absolute/.test(r.text)], ["error", true]);
    r = await exec(env, "await h.hug(await h.node('4:9')); return 1");
    check("h.hug on a frame without auto-layout", [r.type, /without a layoutMode/.test(r.text)], ["error", true]);
    r = await exec(env, "const n = await h.node('4:1'); await h.hug(n, 'y'); return [n.layoutSizingHorizontal, n.layoutSizingVertical]");
    check("h.hug one axis", r.value, ["HUG", "HUG"]);
    r = await exec(env, "const n = await h.node('4:1'); await h.fixed(n, 300); return [n.width, n.height, n.layoutSizingHorizontal, n.layoutSizingVertical]");
    check("h.fixed keeps the other axis as it was", r.value, [300, 40, "FIXED", "HUG"]);
    r = await exec(env, "const n = await h.node('4:3'); await h.wrapText(n); return [n.textAutoResize, n.layoutSizingHorizontal]");
    check("h.wrapText in auto-layout", r.value, ["HEIGHT", "FILL"]);
    check("…after its font loaded", env.loads.indexOf("Inter Regular") !== -1, true);
    r = await exec(env, "const n = await h.node('4:3'); await h.wrapText(n, 320); return [n.width, n.textAutoResize]");
    check("h.wrapText to a width", r.value, [320, "HEIGHT"]);
    r = await exec(env, "await h.fill(await h.node('4:2'), 'diagonal'); return 1");
    check("a bad axis", [r.type, /the axis is/.test(r.text)], ["error", true]);
    void row; void loose;
  }

  // ─── find ───────────────────────────────────────────────────────────────
  {
    const env = makeFigma();
    card(env);
    let r = await exec(env, "return h.find(await h.node('1:1'), {type: 'text'}).map((n) => n.name)");
    check("by type, with findAllWithCriteria", [r.value, env.figma.criteria], [["Title", "Label"], 1]);
    check("the flag is back as it was", env.figma.skipInvisibleInstanceChildren, false);
    r = await exec(env, "return h.find(await h.node('1:1'), {nameHas: 'S'}).map((n) => n.name)");
    check("hidden layers inside instances are skipped", r.value, []);
    r = await exec(env, "return h.find(await h.node('1:1'), {nameHas: 'S', hidden: true}).map((n) => n.name)");
    check("…unless hidden: true", r.value, ["Spinner"]);
    r = await exec(env, "return h.find(await h.node('1:1'), {name: 'Badge'}).map((n) => n.id)");
    check("a hidden layer outside an instance is found", r.value, ["1:4"]);
    r = await exec(env, "return h.find(await h.node('1:1'), {textHas: 'wor'})");
    check("by text", r.value, [{ id: "1:2", name: "Title", type: "TEXT", w: 288, h: 24, chars: "Hello world" }]);
    r = await exec(env, "return h.find(await h.node('1:1'), {type: 'frame', textHas: 'x', hidden: true})");
    check("text asked of a type that has none finds nothing", [r.type, r.value], ["result", []]);
    env.figma.skipInvisibleInstanceChildren = true;
    r = await exec(env, "return h.find(await h.node('1:1'), {nameHas: 'S', hidden: true}).map((n) => n.name)");
    check("hidden: true finds them though a script before left the flag on",
      [r.value, env.figma.skipInvisibleInstanceChildren], [["Spinner"], true]);
    env.figma.skipInvisibleInstanceChildren = false;
    env.nodes.get("1:2").characters = "Hello\nworld";
    r = await exec(env, "return h.dumpTree(await h.node('1:1'), {maxDepth: 1})");
    check("tree: a text of two lines stays on its layer's line", r.value.split("\n")[1],
      '  Title [TEXT] 1:2 288×24 "Hello\\nworld"');
  }

  console.log(failed ? `\n${failed} FAILED` : "\nall command checks passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
