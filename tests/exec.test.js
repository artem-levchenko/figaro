// The exec core in plugin/code.js against a stub of Figma that reports
// edits the way Figma does — `documentchange` in a batch a moment after them —
// and keeps an undo stack: what a script changed, one Cmd+Z step per script,
// read-only rollback, whose change it was, a script that only reads answering
// at once and the window after it, `undo`, line numbers in errors, the guarded
// `figma`.
//
//     node tests/exec.test.js
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "plugin", "code.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "..", "plugin", "ui.html"), "utf8");

// "+5:1 Box" creates, "-5:1" deletes, "~1:1 Card fills strokes" changes
// properties, "s9 Brand" creates a style.
function parse(spec, origin) {
  const [head, name, ...props] = spec.split(" ");
  const op = head[0], id = head.slice(1);
  // A deleted node is a RemovedNode: id, type and `removed`, no name.
  const node = op === "-" ? { id, type: "FRAME", removed: true } : { id, name, type: "FRAME", removed: false };
  // "+5:2 Label ^5:1": made inside 5:1
  const up = props.find((p) => p[0] === "^");
  if (up) node.parent = { id: up.slice(1) };
  if (op === "+") return { origin, type: "CREATE", id, node };
  if (op === "-") return { origin, type: "DELETE", id, node };
  if (op === "~") return { origin, type: "PROPERTY_CHANGE", id, node, properties: props };
  if (op === "s") return { origin, type: "STYLE_CREATE", id, style: { name } };
  throw new Error("bad spec " + spec);
}

function makeFigma() {
  const handlers = [];
  const posted = [];
  const shown = [];
  const saved = [];
  const fontLoads = [];
  const undo = { steps: [], pending: [], log: [], reverted: [] };
  const nodes = new Map([["1:1", { id: "1:1", name: "Card", type: "FRAME" }]]);
  const made = new Map();  // nodes created by edit(), to remove some with a parent
  const gone = new Map();  // deleted nodes, for an undo to bring back
  const keepAlive = new Set();  // nodes an undo fails to remove (a broken Figma)
  // The document follows the edits, and an undo puts it back.
  const apply = (changes) => {
    for (const c of changes) {
      if (c.type === "CREATE") nodes.set(c.id, c.node);
      if (c.type === "DELETE") { gone.set(c.id, nodes.get(c.id) || c.node); nodes.delete(c.id); }
    }
  };
  const unapply = (changes) => {
    for (const c of changes.slice().reverse()) {
      if (c.type === "CREATE" && !keepAlive.has(c.id)) nodes.delete(c.id);
      if (c.type === "DELETE") nodes.set(c.id, gone.get(c.id) || { id: c.id, name: "?", type: "FRAME" });
    }
  };
  let buffer = [];
  let timer = null;
  let failSave = false;
  // Figma batches its reports ~100 ms after an edit; 20 ms here.
  const emit = (changes) => {
    buffer.push(...changes);
    if (!timer) {
      timer = setTimeout(() => {
        timer = null;
        const batch = buffer;
        buffer = [];
        for (const fn of handlers.slice()) fn({ documentChanges: batch });
      }, 20);
    }
  };
  const figma = {
    root: { name: "Draft", children: [{ id: "0:1" }] },
    fileKey: "KEY",
    showUI(h) { shown.push(h); },
    ui: { onmessage: null, postMessage(m) { posted.push(m); } },
    currentPage: { selection: [] },
    variables: {
      setValueForMode() {},
      getVariableByIdAsync: () => Promise.resolve(null),
      // An import by key that never settles, as in a draft; "gone" is a key Figma refuses.
      importVariableByKeyAsync: (key) => key === "gone"
        ? Promise.reject(new Error("could not find variable with key")) : new Promise(() => {}),
    },
    importComponentByKeyAsync: () => new Promise(() => {}),
    // An import by key that answers, slowly, as one from a large library does.
    importStyleByKeyAsync: () => new Promise((r) => setTimeout(() => r({ name: "Style" }), 150)),
    // A library call that never settles, as figma.teamLibrary sometimes does.
    teamLibrary: { getAvailableLibraryVariableCollectionsAsync: () => new Promise(() => {}) },
    on(event, fn) { if (event === "documentchange") handlers.push(fn); },
    off(event, fn) { const i = handlers.indexOf(fn); if (i !== -1) handlers.splice(i, 1); },
    commitUndo() {
      undo.log.push("commit");
      if (undo.pending.length) { undo.steps.push(undo.pending); undo.pending = []; }
    },
    triggerUndo() {
      undo.log.push("undo");
      // As in Figma: edits not yet committed go together with the
      // step before them.
      const reverted = undo.pending.concat(undo.steps.pop() || []);
      undo.pending = [];
      undo.reverted.push(reverted.map((c) => c.id));
      unapply(reverted);
      if (reverted.length) {
        emit(reverted.map((c) => ({ origin: "LOCAL", type: "PROPERTY_CHANGE", id: c.id,
          node: c.node, properties: ["undo"] })));
      }
    },
    getNodeById(id) { return nodes.get(String(id)) || null; },
    async getNodeByIdAsync(id) { return nodes.get(String(id)) || null; },
    async saveVersionHistoryAsync(title) {
      if (failSave) throw new Error("no edit access");
      saved.push(title);
      return { id: "v" + saved.length };
    },
    async loadFontAsync(font) { fontLoads.push(font.family + " " + font.style); },
    // Figma's pictures: it takes PNG, JPEG and GIF, and names an image by a hash of its bytes.
    base64Decode(text) { return new Uint8Array(Buffer.from(text, "base64")); },
    createImage(bytes) {
      const png = bytes[0] === 0x89 && bytes[1] === 0x50;
      if (!png) throw new Error("Image type is unsupported");
      return { hash: require("crypto").createHash("sha1").update(bytes).digest("hex") };
    },
    // A script's edits: they wait in the undo stack until committed.
    edit(...specs) {
      const changes = specs.map((s) => parse(s, "LOCAL"));
      for (const c of changes) if (c.type === "CREATE") made.set(c.id, c.node);
      apply(changes);
      undo.pending.push(...changes);
      emit(changes);
    },
    // What Figma does inside addComponentProperty: it closes an undo step.
    splitUndo() {
      if (undo.pending.length) { undo.steps.push(undo.pending); undo.pending = []; }
    },
    // A node removed with its parent: Figma reports only the parent's removal.
    removeWith(parent, ...children) {
      for (const id of children) made.get(id).removed = true;
      figma.edit("-" + parent);
    },
    remoteEdit(...specs) { emit(specs.map((s) => parse(s, "REMOTE"))); },
  };
  // Someone working in Figma by hand: each action is its own undo step.
  const userEdit = (...specs) => {
    const changes = specs.map((s) => parse(s, "LOCAL"));
    apply(changes);
    undo.steps.push(changes);
    emit(changes);
  };
  // Figma changing layers itself, as in a large file that was just opened:
  // reported as local, never an undo step.
  const figmaEdit = (...specs) => {
    const changes = specs.map((s) => parse(s, "LOCAL"));
    apply(changes);
    emit(changes);
  };
  return { figma, handlers, posted, shown, saved, fontLoads, undo, userEdit, figmaEdit, nodes, keepAlive,
           failSaves() { failSave = true; } };
}

function load() {
  const env = makeFigma();
  env.api = new Function("figma", "__html__", src +
    "\nreturn { HISTORY, OUTSIDE, RUNS, ABORTED, GATES, markAborted, SCRIPT_LINE_OFFSET, codeMayWrite, whyMayWrite };")(env.figma, html);
  return env;
}

// The reply as the bridge sees it: the value parsed, the log lines gathered.
let seq = 0;
async function send(env, msg) {
  const id = (msg.type === "undo" ? "u" : "e") + (++seq);
  await env.figma.ui.onmessage(Object.assign({ id }, msg));
  const reply = env.posted.find((m) => m.id === id && (m.type === "result" || m.type === "error"));
  const logs = [].concat(...env.posted.filter((m) => m.id === id && m.type === "log").map((m) => m.lines));
  const value = typeof reply.valueJson === "string" ? JSON.parse(reply.valueJson) : reply.value;
  return Object.assign({}, reply, { value, logs });
}
const exec = (env, code, opts) => send(env, Object.assign({ type: "exec", code, timeout: 10 }, opts));
const undoLast = (env, opts) => send(env, Object.assign({ type: "undo" }, opts));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}

(async () => {
  // ─── the script ─────────────────────────────────────────────────────────
  {
    const env = load();
    check("line 1 lands where it should", env.api.SCRIPT_LINE_OFFSET, 3);
    const r = await exec(env, "return 42 // the answer");
    check("a trailing comment does not eat the script's end", [r.type, r.value], ["result", 42]);

    const e = await exec(env, "const a = 1;\nconst b = 2;\nnull.boom;\nreturn a + b;");
    check("error names the script line", [e.type, e.line, e.source], ["error", 3, "null.boom;"]);
    check("stack points into the script", /script:3:\d+/.test(e.stack), true);
    check("…with its frames alone, not those of the plugin that ran it",
      e.stack.split("\n").filter((l) => /^\s*at\s/.test(l)).map((l) => /script:\d+:\d+/.test(l)), [true]);

    const bare = await exec(env, "throw {};");
    check("an error without text is explained", /^an error with no text/.test(bare.text), true);

    // Strict mode: a write that can't happen fails instead of doing nothing.
    const frozen = await exec(env, "const paint = Object.freeze({ color: 1 });\npaint.color = 2;\nreturn paint.color;");
    check("a write to a frozen object fails, on its line",
      [frozen.type, frozen.line, /read.only/.test(frozen.text)], ["error", 2, true]);
    const undeclared = await exec(env, "total = 1;\nreturn total;");
    check("…and so does a variable never declared",
      [undeclared.type, /total is not defined/.test(undeclared.text)], ["error", true]);

    const printed = await exec(env, 'const loop = { name: "a" }; loop.self = loop;\n' +
      'print(new Error("boom"), { e: new TypeError("t"), n: 10n });\nprint(loop, null, 1n);\nreturn 1;');
    check("print shows an error's text, a BigInt and a loop back to itself", printed.logs,
      ['Error: boom {\n  "e": "TypeError: t",\n  "n": "10"\n}',
       '{\n  "name": "a",\n  "self": "[Circular]"\n} null 1']);
  }

  // ─── what a script changed ──────────────────────────────────────────────
  {
    const env = load();
    const r = await exec(env, 'figma.edit("+5:1 Box", "~1:1 Card fills", "~1:1 Card strokes", "-2:2 Old"); return 1;');
    const c = r.changes;
    check("counts", [c.created, c.deleted, c.changed, c.styles], [1, 1, 1, 0]);
    check("properties", c.props, { fills: 1, strokes: 1 });
    check("items by node", c.items.map((i) => i.op + i.id + (i.name ? " " + i.name : "")),
      ["+5:1 Box", "~1:1 Card", "-2:2"]);
    check("item props merge", c.items[1].props, ["fills", "strokes"]);
    check("the script is one undo step", env.undo.steps.length, 1);

    const made = await exec(env, 'figma.edit("+6:1 New"); figma.edit("~6:1 New fills"); return 1;');
    check("a new node's first properties are part of creating it",
      [made.changes.created, made.changes.changed], [1, 0]);

    const gone = await exec(env, 'figma.edit("+7:1 Tmp"); figma.edit("-7:1 Tmp"); return 1;');
    check("made and dropped in one script: nothing to report", gone.changes, undefined);

    const nested = await exec(env, 'figma.edit("+7:2 Box", "+7:3 Label", "~1:1 Card fills");' +
      'figma.removeWith("7:2", "7:3"); return 1;');
    check("…and so is a layer that went with its new parent",
      [nested.changes.created, nested.changes.changed, nested.changes.items.map((i) => i.op + i.id)],
      [0, 1, ["~1:1"]]);

    const quiet = await exec(env, "return figma.root.name;");
    check("a script that changed nothing has no changes", [quiet.value, quiet.changes], ["Draft", undefined]);

    const style = await exec(env, 'figma.edit("s9 Brand"); return 1;');
    check("styles are counted", style.changes.styles, 1);

    const remote = await exec(env, 'figma.remoteEdit("~1:1 Card fills"); return 1;');
    check("other people's edits are counted apart", [remote.changes.remote, remote.changes.changed], [1, 0]);

    const many = Array.from({ length: 40 }, (_, i) => `"~${i + 100}:1 N${i} fills"`).join(", ");
    const big = await exec(env, `figma.edit(${many}); return 1;`);
    check("at most 30 items, the rest counted", [big.changes.items.length, big.changes.more], [30, 10]);
  }

  // ─── one undo step per script ───────────────────────────────────────────
  {
    const env = load();
    env.undo.pending.push(parse("~1:1 Card x", "LOCAL"));  // an edit nobody committed yet
    await exec(env, 'figma.edit("~1:1 Card fills"); return 1;');
    await exec(env, 'figma.edit("~1:1 Card opacity"); return 1;');
    check("what came before and each script are separate steps",
      env.undo.steps.map((s) => s.map((c) => c.properties[0])), [["x"], ["fills"], ["opacity"]]);
  }

  // ─── read-only ──────────────────────────────────────────────────────────
  {
    const env = load();
    const ok = await exec(env, "return figma.root.name;", { readOnly: true });
    check("a read-only script that reads", [ok.type, ok.value], ["result", "Draft"]);

    const r = await exec(env, 'figma.edit("~1:1 Card fills", "+8:1 Box"); return 1;', { readOnly: true });
    // The note says what in the code made it count as one that may write.
    const why = ". The code may write (`figma.edit()`), so every change made while it ran counted as its own, " +
      "Figma's updates on a page loaded for the first time too. If it only reads, keep what it gathers " +
      "in variables of its own and run it again";
    check("a read-only script that wrote fails", [r.type, r.text], ["error", "read-only: the script changed 2 — rolled back" + why]);
    check("…and is rolled back", [r.rolledBack, env.undo.steps.length, env.undo.reverted.length], [true, 0, 1]);
    check("…which is not news", [env.api.OUTSIDE.local, env.api.HISTORY.length], [0, 0]);

    const t = await exec(env, 'figma.edit("~1:1 Card fills"); throw new Error("oops");', { readOnly: true });
    check("a read-only script that wrote and threw", t.text, "oops — read-only: the script changed 1 — rolled back" + why);

    // Two at once: whose edit it was is unknown, so nothing is rolled back.
    const writer = exec(env, 'await new Promise((r) => setTimeout(r, 80)); figma.edit("~1:1 Card fills"); return 1;');
    const reader = exec(env, 'figma.edit("~1:1 Card opacity"); return 1;', { readOnly: true });
    const [w, rd] = await Promise.all([writer, reader]);
    check("overlapping read-only: a notice, no rollback", [rd.type, /nothing rolled back/.test(rd.notice), rd.changes.shared],
      ["result", true, true]);
    check("overlapping writer still reports", w.changes.changed, 1);
  }

  // ─── whose change it was ────────────────────────────────────────────────
  {
    const env = load();
    const may = (code, libs) => env.api.codeMayWrite(code, libs);
    const reads = [
      "return figma.root.name;",
      'const p = await figma.getNodeByIdAsync("0:1"); await p.loadAsync(); let n = 0;\n' +
        'for (const c of p.children) if ("findAll" in c) n += c.findAll(() => true).length;\nreturn n;',
      'const out = {}; out.count = 1; const rows = []; rows[0] = { id: 1 }; rows[1] = { name: "x" }; return [out, rows];',
      "const counts = {};\nfor (const n of figma.currentPage.findAll()) counts[n.type] = (counts[n.type] || 0) + 1;\nreturn counts;",
      "figma.skipInvisibleInstanceChildren = true; figma.currentPage = figma.root.children[1];" +
        " figma.currentPage.selection = []; return 1;",
      'const m = new Map(); m.set("a", 1); return [...m.keys()];',
      'return figma.currentPage.findAll((n) => n.name === "Header" || n.width >= 10 || n.x != 0).length;',
      'print("n.name = 1"); // n.visible = false\n/* n.remove() */ return 1;',
      "const [a, b] = [1, 2]; let x = 0; x += a; return x + b;",
      'const n = await h.node("1:1"); const data = await h.inspect(n); return [h.link(n), h.sel(), data];',
      'const notes = await h.annotations("page"); return notes.filter((r) => !r.visible);',
      'const r = await h.audit(await h.node("1:1"), { only: ["low-contrast"] }); return r.notes;',
      "return figma.currentPage.children.map((n) => n.name.replace(/\"/g, \"'\"));",
      "const half = figma.root.children.length / 2; const n = 10 / half; return n;",
      "const { width: w, height: h } = figma.currentPage.children[0]; return [{ w, h }];",
      "let a = 1, b = 2; [a, b] = [b, a]; return a;",
      'const stats = new Map()\nfor (const n of figma.currentPage.children) stats.set(n.id, n.name)\nreturn [...stats]',
      "return `${figma.root.name}: ${figma.root.children.map((p) => `${p.name}`).join(\", \")}`;",
      // importing by key, and what the team library lists
      'const c = await figma.importComponentByKeyAsync("k1"); return [c.name, c.key];',
      'const set = await figma.importComponentSetByKeyAsync("k1"); const st = await figma.importStyleByKeyAsync("k2"); return [set.name, st.name];',
      'const v = await figma.variables.importVariableByKeyAsync("k3"); return v.resolvedType;',
      'const c = await h.importComp("k1"); const v = await h.importVar("k3"); return [c.name, v.name];',
      "const cols = await figma.teamLibrary.getAvailableLibraryVariableCollectionsAsync();\n" +
        "return Promise.all(cols.map((c) => figma.teamLibrary.getVariablesInLibraryCollectionAsync(c.key)));",
    ];
    check("code that only reads", reads.map((c) => may(c)), reads.map(() => false));
    const writes = [
      'return figma.currentPage.findAll((n) => n.name = "Header").length;',
      '(await h.node("1:1")).name = "x";',
      'const n = await h.node("1:1"); n.fills = [];',
      "const n = {}; for (const n of figma.currentPage.children) n.visible = false;",
      'const n = await h.node("1:1"); n.resize(10, 10);',
      "figma.createFrame();",
      'await h.setText(await h.node("1:1"), "Hi");',
      'const n = await h.node("1:1"); n.setProperties({ Size: "L" });',
      'const n = await h.node("1:1"); Object.assign(n, { x: 1 });',
      'const n = await h.node("1:1"); n["opacity"] = 0.5;',
      'const n = await h.node("1:1"); n.x++;',
      'figma.edit("~1:1 Card fills");',
      'let o = {}; o = await h.node("1:1"); o.name = "x";',
      // through what an array or a map of its own holds
      'const rows = []; for (const n of figma.currentPage.children) rows.push(n); rows[0].name = "x";',
      'const by = {}; for (const n of figma.currentPage.children) by[n.name] = n; by["Card"].visible = false;',
      'const m = new Map(); m.set("a", figma.currentPage.children[0]); m.get("a").name = "x";',
      'const nodes = [figma.currentPage.children[0]]; ++nodes[0].x;',
      // a value that only starts as its own
      "const first = Array.from(figma.currentPage.children)[0]; first.name = \"x\";",
      "const n = [figma.currentPage.children[0]][0]; n.name = \"x\";",
      // a regular expression with a quote in it, then a write
      "const q = /\"/; figma.currentPage.children[0].name = \"x\";",
      // patterns, calls by name, aliases
      "const n = figma.currentPage.children[0]; [n.x, n.y] = [0, 0];",
      "const n = figma.currentPage.children[0]; ({ a: n.name } = { a: \"x\" });",
      'const n = figma.currentPage.children[0]; n["remove"]();',
      "const n = figma.currentPage.children[0]; const r = n.remove.bind(n); r();",
      "const n = figma.currentPage.children[0]; n.removeOverrides();",
      "const H = h; await H.fill(figma.currentPage.children[0]);",
      "const n = figma.currentPage.children[0]; with (n) { name = \"x\"; }",
      // optional calls, and code inside a template
      "const n = figma.currentPage.children[0]; n.remove?.();",
      "figma.createFrame?.();",
      "return `removed: ${figma.currentPage.children[0].remove()}`;",
      "return `${figma.root.name} ${`${(figma.currentPage.children[0].name = \"x\")}`}`;",
      // what is done with an import, and code from elsewhere
      'const c = await figma.importComponentByKeyAsync("k1"); figma.currentPage.appendChild(c.createInstance());',
      'const v = await h.importVar("k3"); await h.bF(figma.currentPage.children[0], 0, v.id);',
      'const m = await import("./more.js"); return m;',
    ];
    check("code that may write", writes.map((c) => may(c)), writes.map(() => true));
    check("…and so does code with --lib files", may("return 1;", [{ name: "a", hash: "x" }]), true);

    // One level deeper into an object of its own, through a key that only ever
    // holds the script's own objects: what a scan gathers (#11).
    const deepReads = [
      "const out = { types: {}, total: 0 };\nfor (const n of figma.currentPage.findAll()) {\n" +
        "  out.types[n.type] = (out.types[n.type] || 0) + 1; out.total++; }\nreturn out;",
      "const stats = { libs: {} }; stats.libs[\"k\"] = 1; ++stats.libs.k; return stats;",
      "const out = { by: {} }; for (const n of figma.currentPage.children) (out.by[n.type] ||= []).push(n.id); return out;",
      "const out = {}; out.libs = {}; out.libs.count = 1; return out;",
    ];
    check("a slot of its own object's own object", deepReads.map((c) => may(c)), deepReads.map(() => false));
    const deepWrites = [
      "const out = { types: {} }; out.types = figma.currentPage.children[0]; out.types.name = \"x\";",
      "const base = {}; const out = { ...base, types: {} }; out.types.name = \"x\";",
      "const out = { types: {} }; out[k] = figma.currentPage; out.types.name = \"x\";",
      "const out = { types: {} }; Object.assign(out, { types: figma.currentPage }); out.types.name = \"x\";",
      "const out = { rows: [] }; out.rows.push(figma.currentPage); out.rows[0].name = \"x\";",
      "const out = { types: figma.currentPage }; out.types.name = \"x\";",
      "function walk(n, acc) { acc.count++; } walk(figma.currentPage, { count: 0 });",
    ];
    check("…not through a key that may hold a layer, nor a parameter", deepWrites.map((c) => may(c)),
      deepWrites.map(() => true));
    check("what in the code may write, quoted", [
      env.api.whyMayWrite("const out = { types: figma.currentPage }; out.types.name = \"x\";"),
      env.api.whyMayWrite("function walk(n, acc) { acc.count++; }"),
      env.api.whyMayWrite("figma.currentPage.children[0].remove();"),
      env.api.whyMayWrite("const n = await h.node(\"1:1\"); n.resize(10, 10);"),
      env.api.whyMayWrite("await h.fill(n, \"#fff\");"),
      env.api.whyMayWrite("figma.createFrame();"),
      env.api.whyMayWrite("const H = h; return H;"),
      env.api.whyMayWrite("rows[figma.currentPage.children.length - 1].name = 1;"),
      env.api.whyMayWrite("return 1;", [{ name: "a", hash: "x" }]),
      env.api.whyMayWrite("return figma.root.name;"),
    ], ["`out.types.name =`", "`acc.count++`", "`figma.currentPage.children[0].remove()`", "`n.resize()`",
        "`h.fill()`", "`figma.createFrame()`", "it passes h on", "`rows[…].name =`", "it runs --lib files", null]);
  }
  {
    // A read-only script in a large file that was just opened: Figma updates
    // layers while it runs. Its code only reads, so nothing is undone — an
    // undo would take back the user's last edit instead.
    const env = load();
    env.userEdit("~2:2 Header name");
    await sleep(50);
    const run = exec(env, 'const p = await figma.getNodeByIdAsync("0:1");' +
      " await new Promise((r) => setTimeout(r, 60)); return 1;", { readOnly: true });
    await sleep(20);
    env.figmaEdit("~1:1 Card width", "~1:1 Card height");
    const r = await run;
    check("Figma's own changes during a script that only reads are not rolled back",
      [r.type, r.value, r.rolledBack, r.changes, env.undo.log.includes("undo"), env.undo.steps.length],
      ["result", 1, undefined, undefined, false, 1]);
    check("…but told", r.notice, "read-only: 1 changed in the file while the script ran (Card) — not by the " +
      "script: its code only reads, so Figma itself or someone in the file made them; nothing rolled back");
    check("…and counted as made in Figma itself", env.api.OUTSIDE.local, 3);
  }
  {
    // The same with a scan that gathers its results in nested objects of its own (#11): it
    // used to count as a writer, and -R rolled back what Figma updated as the script's.
    const env = load();
    const run = exec(env, 'const out = { types: {}, total: 0 };\nconst p = await figma.getNodeByIdAsync("1:1");\n' +
      "await new Promise((r) => setTimeout(r, 60));\nout.types[p.id] = (out.types[p.id] || 0) + 1;\n" +
      "out.total++;\nreturn out;", { readOnly: true });
    await sleep(20);
    env.figmaEdit("~1:1 Card width");
    const r = await run;
    check("a scan into nested objects of its own: Figma's changes are not rolled back",
      [r.type, r.value, r.rolledBack, env.undo.log.includes("undo"), /^read-only: 1 changed/.test(r.notice)],
      ["result", { types: { "1:1": 1 }, total: 1 }, undefined, false, true]);
  }
  {
    // A write named only in a comment or a string is no write: Figma's own
    // changes during this read-only script are not rolled back as its own.
    const env = load();
    const run = exec(env, "// no addMode here, no .remove()\nconst note = 'n.remove()';" +
      " await new Promise((r) => setTimeout(r, 60)); return note;", { readOnly: true });
    await sleep(20);
    env.figmaEdit("~1:1 Card width");
    const r = await run;
    check("a write named in a comment or a string is no write",
      [r.type, r.rolledBack, env.undo.log.includes("undo")], ["result", undefined, false]);
  }
  {
    const env = load();
    const run = exec(env, "await new Promise((r) => setTimeout(r, 60)); return figma.root.name;", { readOnly: true });
    await sleep(20);
    env.userEdit("~1:1 Card fills");
    const r = await run;
    check("the user's edit while a read-only script reads stays",
      [r.type, env.undo.steps.length, env.undo.log.includes("undo")], ["result", 1, false]);
  }
  {
    const env = load();
    await exec(env, 'figma.edit("~1:1 Card fills"); return 1;', { agent: "anna" });
    const run = exec(env, "await new Promise((r) => setTimeout(r, 60)); return 1;", { agent: "anna" });
    await sleep(20);
    env.userEdit("~2:2 Header name");
    const r = await run;
    check("a script that only reads does not take the user's edit for its own",
      [r.changes, /^1 changed in the file while the script ran \(Header\) — not by the script/.test(r.notice),
       env.api.HISTORY.length], [undefined, true, 1]);
    const u = await undoLast(env, { agent: "anna" });
    check("…so undo does not revert past it",
      [u.type, /1 more changed in the file \(Header\)/.test(u.text), env.undo.log.includes("undo")],
      ["error", true, false]);
  }
  {
    // Code that may write, and Figma's own change while it ran: the undo meant
    // for the script takes back the user's step. It can't be redone from here.
    const env = load();
    env.userEdit("~2:2 Header name");
    await sleep(50);
    const code = 'const n = await h.node("1:1"); if (n.name === "nope") n.name = "x";' +
      " await new Promise((r) => setTimeout(r, 60)); return 1;";
    const run = exec(env, code, { readOnly: true });
    await sleep(20);
    env.figmaEdit("~1:1 Card width");
    const r = await run;
    check("an undo that took back someone else's step says how to bring it back",
      [r.type, r.rolledBack, /the undo meant to roll that back took back a step that was not the script's/.test(r.text),
       /press Cmd\+Shift\+Z in Figma at once/.test(r.text)], ["error", undefined, true, true]);

    env.userEdit("~2:2 Header name");
    await sleep(50);
    const again = exec(env, code);
    await sleep(20);
    env.figmaEdit("~1:1 Card width");
    await again;
    const u = await undoLast(env, {});
    check("…and so does undo", [u.type, /^undo: Figma's undo took back a step that was not the script's/.test(u.text),
      env.api.HISTORY.length], ["error", true, 0]);

    // A collaborator changes the script's layer just as the undo runs: no sign
    // that the undo took back the script's step.
    env.userEdit("~2:2 Header name");
    await sleep(50);
    const trigger = env.figma.triggerUndo;
    env.figma.triggerUndo = () => { trigger(); env.figma.remoteEdit("~1:1 Card width"); };
    const third = exec(env, code, { readOnly: true });
    await sleep(20);
    env.figmaEdit("~1:1 Card width");
    const t = await third;
    check("…also when someone else changes the script's layer meanwhile",
      [t.type, /press Cmd\+Shift\+Z/.test(t.text)], ["error", true]);
  }

  // ─── a script that only reads answers at once ──────────────────────────
  // It has no edits of its own to wait for. What Figma updates a moment after
  // it (a page it loaded for the first time) is made in Figma itself, and a
  // script that may write, started right after, must not take it as its own.
  {
    // The scripts call these to tell the test what Figma does meanwhile.
    const hookLater = (env) => { globalThis.__later = (...specs) => env.figmaEdit(...specs); };
    const readThenUpdate = 'const p = await figma.getNodeByIdAsync("0:1");' +
      ' globalThis.__later("~1:1 Card width", "~1:1 Card height"); return 1;';

    {
      const env = load();
      const t0 = Date.now();
      const r = await exec(env, "return figma.root.name;");
      check("a script that only reads answers before FLUSH_MS is over", [r.value, Date.now() - t0 < 100], ["Draft", true]);
      const t1 = Date.now();
      const again = await exec(env, "return figma.root.name;", { readOnly: true });
      check("…and reads do not wait for each other", [again.value, Date.now() - t1 < 100], ["Draft", true]);
    }
    {
      const env = load();
      hookLater(env);
      const r = await exec(env, readThenUpdate, { readOnly: true });
      check("a read answers before Figma's update after it comes",
        [r.type, r.value, r.notice, r.changes, env.api.OUTSIDE.local], ["result", 1, undefined, undefined, 0]);
      await sleep(60);
      check("…which is made in Figma itself: counted outside, in no run",
        [env.api.OUTSIDE.local, env.api.RUNS.size, env.api.HISTORY.length, env.undo.log.includes("undo")],
        [2, 0, 0, false]);
      const u = await undoLast(env, {});
      check("…and undo has no step of it", [u.type, /nothing to undo/.test(u.text)], ["error", true]);
    }
    {
      // What Figma reports while the script still runs is told, as before.
      const env = load();
      hookLater(env);
      const r = await exec(env, 'const p = await figma.getNodeByIdAsync("0:1");' +
        ' globalThis.__later("~1:1 Card width"); await new Promise((r) => setTimeout(r, 60)); return 1;', { readOnly: true });
      check("a batch that comes while a read still runs is told, not rolled back",
        [r.type, r.rolledBack, r.changes, /^read-only: 1 changed in the file while the script ran \(Card\) — not by the script/.test(r.notice),
         env.undo.log.includes("undo"), env.api.OUTSIDE.local], ["result", undefined, undefined, true, false, 1]);
    }
    {
      // A script that may write, right after a read: it starts once Figma has reported what it updated.
      const env = load();
      hookLater(env);
      let probe = null;
      globalThis.__probe = () => { probe = { outside: env.api.OUTSIDE.local, runs: env.api.RUNS.size, at: Date.now() }; };
      await exec(env, readThenUpdate);
      const read = Date.now();
      const w = await exec(env, 'globalThis.__probe(); figma.edit("+9:1 Box"); return 1;', { agent: "anna" });
      check("a writer after a read starts after Figma's update of it: counted outside, no run but its own open",
        [probe.outside, probe.runs], [2, 1]);
      check("…as soon as the update is reported, not FLUSH_MS later", probe.at - read < 100, true);
      check("…its report lists its own change alone",
        [w.changes.created, w.changes.changed, w.changes.items.map((i) => i.op + i.id), w.notice],
        [1, 0, ["+9:1"], undefined]);
      check("…and so does its step for undo", [env.api.HISTORY.length, env.api.HISTORY[0].own, env.api.OUTSIDE.local], [1, 1, 2]);
      const u = await undoLast(env, { agent: "anna" });
      check("…which undo reverts, and only that",
        [u.type, env.undo.reverted.slice(-1)[0], u.value && u.value.undone.changes], ["result", ["9:1"], "+1"]);
    }
    {
      // The same with -R: the rollback must not take Figma's step for the script's.
      const env = load();
      hookLater(env);
      await exec(env, 'const p = await figma.getNodeByIdAsync("0:1");' +
        ' globalThis.__later("~2:2 Header width", "~3:3 Footer width"); return 1;');
      const w = await exec(env, 'figma.edit("~1:1 Card fills"); return 1;', { readOnly: true });
      check("a -R writer after a read rolls back its own change alone",
        [w.type, w.text, w.rolledBack, env.undo.reverted.slice(-1)[0]],
        ["error", "read-only: the script changed 1 — rolled back" + ". The code may write (`figma.edit()`), so every change made while it ran counted as its own, " +
          "Figma's updates on a page loaded for the first time too. If it only reads, keep what it gathers " +
          "in variables of its own and run it again", true, ["1:1"]]);
    }
    {
      // No update comes: the window ends with FLUSH_MS, and the writer starts then.
      const env = load();
      let startedAt = null;
      globalThis.__probe = () => { startedAt = Date.now(); };
      await exec(env, "return figma.root.name;");
      const t0 = Date.now();
      const w = await exec(env, 'globalThis.__probe(); figma.edit("~1:1 Card fills"); return 1;');
      check("a writer after a read waits for the window, FLUSH_MS at most",
        [w.type, startedAt - t0 >= 100, startedAt - t0 < 1000, w.changes.changed], ["result", true, true, 1]);
      // …and a writer after a writer waits for nothing.
      const t1 = Date.now();
      await exec(env, 'globalThis.__probe(); figma.edit("~1:1 Card opacity"); return 1;');
      check("…not one after a writer", startedAt - t1 < 100, true);
    }
    {
      // Stop (the bridge's abort) while it waits: the script never starts.
      const env = load();
      await exec(env, "return figma.root.name;");
      const w = env.figma.ui.onmessage({ type: "exec", id: "w-stop", timeout: 10,
        code: 'figma.edit("~1:1 Card fills"); return 1;' });
      const t0 = Date.now();
      await env.figma.ui.onmessage({ type: "abort", id: "w-stop" });
      await w;
      const ended = env.posted.find((m) => m.id === "w-stop" && (m.type === "result" || m.type === "error"));
      check("a writer stopped while it waits never starts",
        [ended.type, /^aborted/.test(ended.text), env.undo.pending.length, env.api.RUNS.size, env.api.GATES.size,
         env.api.ABORTED.has("w-stop")], ["error", true, 0, 0, 0, false]);
      check("…and stops at once, not when the wait runs out", Date.now() - t0 < 100, true);
    }
    {
      // A --parallel read that ends while a writer waits: Figma's update after
      // it comes later than the writer's FLUSH_MS from its own start. The
      // writer waits for it, and its report says another script ran meanwhile.
      const env = load();
      hookLater(env);
      await exec(env, "return figma.root.name;");
      const w = exec(env, 'figma.edit("+9:1 Box"); return 1;', { agent: "anna" });
      await sleep(90);
      await exec(env, 'setTimeout(() => globalThis.__later("~1:1 Card width"), 60); return 1;', { readOnly: true });
      const r = await w;
      check("a read that ends while a writer waits: its update stays out of the writer's report",
        [r.type, r.changes.items.map((i) => i.op + i.id), r.changes.shared, env.api.HISTORY[0].own, env.api.OUTSIDE.local],
        ["result", ["+9:1"], true, 1, 1]);
    }
    delete globalThis.__later;
    delete globalThis.__probe;
  }

  // ─── undo ───────────────────────────────────────────────────────────────
  {
    const env = load();
    await exec(env, 'figma.edit("+5:1 Box", "~1:1 Card fills"); return 1;', { agent: "anna" });
    await exec(env, "return 1;", { agent: "anna" });                       // changed nothing
    await exec(env, 'figma.edit("~1:1 Card x"); return 1;', { readOnly: true });  // rolled back
    const u = await undoLast(env, { agent: "anna" });
    check("undo reverts the last script that changed something",
      [u.type, u.value.undone.agent, u.value.undone.changes, u.value.left], ["result", "anna", "+1 ~1", 0]);
    check("…and only it", [env.undo.steps.length, env.undo.reverted.slice(-1)[0]], [0, ["5:1", "1:1"]]);
    check("…which is not news", env.api.OUTSIDE.local, 0);
    const none = await undoLast(env, {});
    check("nothing left to undo", [none.type, /nothing to undo/.test(none.text)], ["error", true]);
  }
  {
    const env = load();
    await exec(env, 'figma.edit("~1:1 Card fills"); return 1;');
    await exec(env, 'figma.edit("~1:1 Card opacity"); return 1;');
    const a = await undoLast(env, {});
    const b = await undoLast(env, {});
    check("undo walks back script by script", [a.value.left, b.value.left, env.undo.steps.length], [1, 0, 0]);
  }
  {
    const env = load();
    await exec(env, 'figma.edit("~1:1 Card fills"); return 1;');
    env.userEdit("~1:1 Card name");
    const u = await undoLast(env, {});
    check("an edit by hand after the script blocks undo", [u.type, /1 more changed in the file \(Card\)/.test(u.text)], ["error", true]);
    check("…and nothing is reverted", env.undo.log.filter((x) => x === "undo").length, 0);
  }
  {
    const env = load();
    await exec(env, 'figma.edit("~1:1 Card fills"); return 1;', { agent: "anna" });
    const other = await undoLast(env, { agent: "bob" });
    check("another agent's script needs --force", [other.type, /by anna, not bob/.test(other.text)], ["error", true]);
    const forced = await undoLast(env, { agent: "bob", force: true });
    check("…and goes with it", forced.type, "result");
  }
  {
    const env = load();
    await exec(env, 'figma.variables.setValueForMode(); return 1;');
    const u = await undoLast(env, {});
    check("variable edits are invisible, so undo stops there", [u.type, /may have changed variables/.test(u.text)], ["error", true]);
    const ro = await exec(env, "const c = figma.root; if (false) c.remove(); return 1;", { readOnly: true });
    check("a read-only script that may remove what Figma does not report gets a notice",
      [ro.type, /-R cannot roll it back/.test(ro.notice)], ["result", true]);
  }
  {
    // A script that made layers and removed them left nothing, but Figma
    // still keeps its Cmd+Z step: undo must revert that step, not the one before.
    const env = load();
    await exec(env, 'figma.edit("~1:1 Card fills"); return 1;');
    const tmp = await exec(env, 'figma.edit("+7:1 Tmp"); figma.edit("-7:1 Tmp"); return 1;');
    check("a script that only made and dropped layers reports nothing", tmp.changes, undefined);
    const u = await undoLast(env, {});
    check("…yet undo takes its own step",
      [u.type, u.value.undone.changes, u.value.left, env.undo.reverted.slice(-1)[0]],
      ["result", "nothing: layers made and removed", 1, ["7:1", "7:1"]]);
  }

  // ─── the new layers a report links to ───────────────────────────────────
  {
    const env = load();
    const r = await exec(env, 'figma.edit("+5:2 Label ^5:1", "+5:3 Icon ^5:1", "+5:1 Set ^1:2", "+6:1 Note"); return 1;');
    check("the report names the new layers that are not inside other new ones",
      r.changes.top.map((t) => t.id + " " + t.name), ["5:1 Set", "6:1 Note"]);
    const many = await exec(env, "for (let i = 1; i <= 7; i++) figma.edit(\"+9:\" + i + \" Card\"); return 1;");
    check("…at most five of them", [many.changes.top.length, many.changes.topMore], [5, 2]);
  }

  // ─── Figma's own undo steps inside a script ─────────────────────────────
  {
    const env = load();
    env.userEdit("~1:1 Card fills");  // the user's step, before the script
    await sleep(50);  // reported before the script starts
    const r = await exec(env, 'figma.edit("+5:1 Set", "+5:2 Label"); figma.splitUndo();' +
      ' figma.edit("~5:1 Set componentPropertyDefinitions"); figma.splitUndo(); figma.edit("+5:3 Icon"); return 1;');
    check("a script Figma split into steps reports as one", r.changes.created, 3);
    const u = await undoLast(env, {});
    check("undo reverts every step of it",
      [u.type, u.value.steps, ["5:1", "5:2", "5:3"].some((id) => env.nodes.has(id)), u.value.warning],
      ["result", 3, false, undefined]);
    check("…and not the step before it", [env.undo.steps.length, env.undo.steps[0][0].id], [1, "1:1"]);
  }
  {
    const env = load();
    env.userEdit("~1:1 Card fills");
    await sleep(50);
    const r = await exec(env, 'figma.edit("+6:1 Box"); figma.splitUndo(); figma.edit("-1:1 Card"); return 1;',
      { readOnly: true });
    check("a read-only script is rolled back across Figma's steps",
      [r.type, r.rolledBack, env.nodes.has("6:1"), env.nodes.has("1:1"), env.undo.steps.length],
      ["error", true, false, true, 1]);
  }
  {
    const env = load();
    await exec(env, 'figma.edit("~1:1 Card componentPropertyDefinitions"); figma.splitUndo();' +
      ' figma.edit("~1:1 Card componentPropertyDefinitions"); return 1;');
    const u = await undoLast(env, {});
    check("properties of a component that was there get a warning",
      [u.type, u.value.steps, /properties of existing components/.test(u.value.warning)], ["result", undefined, true]);
  }
  {
    // An undo that cannot remove what the script made must not eat the stack.
    const env = load();
    env.userEdit("~1:1 Card fills");
    env.userEdit("~1:1 Card name");
    await sleep(50);
    await exec(env, 'figma.edit("+8:1 Stuck"); return 1;');
    env.keepAlive.add("8:1");
    const u = await undoLast(env, {});
    check("undo stops once a step is not the script's",
      [u.type, u.value.steps, /not everything was undone: 1 layers/.test(u.value.warning), env.undo.steps.length],
      ["result", 2, true, 1]);
  }

  // ─── ids from elsewhere ─────────────────────────────────────────────────
  {
    const env = load();
    const e = await exec(env, 'return await h.node("9:9");');
    check("h.node fails at once for an id this file lacks", /no node 9:9 in "Draft"/.test(e.text), true);
    const n = await exec(env, 'const n = await figma.getNodeByIdAsync("9:9"); return n === null;');
    check("getNodeByIdAsync gives null at once", n.value, true);
    check("…and says why", /has no such node/.test(n.logs.join("\n")), true);
    const ok = await exec(env, 'return (await h.resolve("1:1")).name;');
    check("a real id still resolves", ok.value, "Card");
  }

  // ─── the figma a script gets ────────────────────────────────────────────
  {
    const env = load();
    const close = await exec(env, "figma.closePlugin();");
    check("closePlugin is off", /closePlugin\(\) is turned off/.test(close.text), true);
    const ui = await exec(env, "figma.ui.onmessage = () => {};");
    check("ui.onmessage is guarded", /keeps the plugin connected/.test(ui.text), true);
    const show = await exec(env, 'figma.showUI("<p>hi</p>");');
    check("showUI is off", /showUI\(\) is turned off/.test(show.text), true);
    const read = await exec(env, 'return [figma.root.name, "root" in figma, typeof figma.commitUndo];');
    check("everything else passes through", read.value, ["Draft", true, "function"]);

    const fonts = await exec(env, 'const f = {family: "Inter", style: "Regular"};' +
      "await figma.loadFontAsync(f); await figma.loadFontAsync(f); return 1;");
    check("a loaded font is not loaded again", [fonts.type, env.fontLoads], ["result", ["Inter Regular"]]);

    const late = await exec(env, 'await new Promise((r) => setTimeout(r, 120));' +
      'await figma.loadFontAsync({family: "Inter", style: "Bold"}); return 1;', { timeout: 0.05 });
    check("a Figma call past the deadline aborts", [late.type, /^aborted/.test(late.text)], ["error", true]);

    const hung = await exec(env, "return await figma.teamLibrary.getAvailableLibraryVariableCollectionsAsync();",
      { timeout: 0.05 });
    check("a Figma call that never settles ends at the deadline",
      [hung.type, /^aborted: figma\.teamLibrary\.getAvailableLibraryVariableCollectionsAsync\(\) did not answer before --timeout ran out/.test(hung.text)],
      ["error", true]);
    check("…and names the script's line that awaited it", [hung.line, /^return await figma\.teamLibrary/.test(hung.source)],
      [1, true]);
    check("…which is all its stack shows, not the plugin's gate",
      hung.stack.split("\n").filter((l) => /^\s*at\s/.test(l)).map((l) => (/script:(\d+):\d+/.exec(l) || [l])[1]), ["1"]);
    const stuck = env.figma.ui.onmessage({ id: "e-stuck", type: "exec", timeout: 10,
      code: "await figma.teamLibrary.getAvailableLibraryVariableCollectionsAsync(); return 1;" });
    await sleep(20);
    const t0 = Date.now();
    await env.figma.ui.onmessage({ type: "abort", id: "e-stuck" });
    await stuck;
    const ended = env.posted.find((m) => m.id === "e-stuck" && (m.type === "result" || m.type === "error"));
    check("…or at once on the bridge's abort", [ended.type, /^aborted/.test(ended.text), Date.now() - t0 < 1000],
      ["error", true, true]);
    check("no gate outlives its script", env.api.GATES.size, 0);
    const caught = await exec(env, "try { await figma.teamLibrary.getAvailableLibraryVariableCollectionsAsync(); }" +
      ' catch (e) { return "caught"; }', { timeout: 0.05 });
    check("a script may catch it and still answer", caught.value, "caught");
    // Without the deadline these would never answer: give up on them after a second.
    const within = (p) => Promise.race([p, sleep(1000).then(() => ({ type: "no answer", text: "" }))]);
    const comp = await within(exec(env, 'return await h.importComp("KEY");', { timeout: 0.05 }));
    check("a helper's import by key ends at the deadline too",
      [comp.type, /^aborted: figma\.importComponentByKeyAsync\(\) did not answer before --timeout ran out/.test(comp.text)],
      ["error", true]);
    const bound = await within(exec(env, 'return await h.bF({name: "Box", fills: []}, 0, "KEY");', { timeout: 0.05 }));
    check("…and h.bF with a library key, which imports it",
      [bound.type, /^aborted: figma\.variables\.importVariableByKeyAsync\(\) did not answer/.test(bound.text)],
      ["error", true]);
    // Many slow calls that each answer use the time up: the error says so, and where the time went (#11).
    const slow = await exec(env, 'for (const k of ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"])\n' +
      "  await figma.importStyleByKeyAsync(k);\nreturn 1;", { timeout: 1.1 });
    check("many slow calls: the time ran out while the script waited, not a call that hung",
      [slow.type, /^aborted: --timeout ran out while the script waited for figma\.importStyleByKeyAsync\(\) — the script was stopped; of the 1\.\d s it ran, its calls to Figma took 1\.\d s: figma\.importStyleByKeyAsync\(\) ×8, 1\.\d s$/.test(slow.text), slow.line],
      ["error", true, 2]);
    check("…and one call that hung still says so, with no time summary under a second",
      [/did not answer before --timeout ran out — the script was stopped$/.test(hung.text)], [true]);
    const refused = await exec(env, 'return await h.var_("gone");');
    check("a key Figma refuses is still null, not an error", [refused.type, refused.value], ["result", null]);
    check("no gate outlives a helper's script either", env.api.GATES.size, 0);

    for (let i = 0; i < 150; i++) env.api.markAborted("x" + i);
    check("aborted ids are bounded", [env.api.ABORTED.size, env.api.ABORTED.has("x0"), env.api.ABORTED.has("x149")],
      [100, false, true]);
  }

  // ─── checkpoints ────────────────────────────────────────────────────────
  {
    const env = load();
    const r = await exec(env, 'figma.edit("~1:1 Card fills"); return 1;', { checkpoint: "Figaro · test" });
    check("a checkpoint is saved before the script", [env.saved, r.checkpoint.id, r.checkpoint.title],
      [["Figaro · test"], "v1", "Figaro · test"]);
    env.failSaves();
    const f = await exec(env, "return 2;", { checkpoint: "Figaro · again" });
    check("a failed checkpoint is reported, the script still runs",
      [f.value, f.checkpoint.error], [2, "no edit access"]);
  }

  // ─── hot reload ─────────────────────────────────────────────────────────
  {
    const env = load();
    const before = env.figma.ui.onmessage;
    await env.figma.ui.onmessage({ type: "reload", id: "r1", code: src, html: "<p>new</p>" });
    check("the new code shows its window", env.shown.slice(-1)[0], "<p>new</p>");
    check("…takes the messages", env.figma.ui.onmessage !== before, true);
    check("…and the old listener is gone", env.handlers.length, 1);
    const r = await exec(env, 'figma.edit("~1:1 Card fills"); return 1;');
    check("changes are counted once after a reload", r.changes.changed, 1);

    await env.figma.ui.onmessage({ type: "reload", id: "r2", code: "throw new Error('broken')", html: "" });
    const err = env.posted.find((m) => m.id === "r2");
    check("a broken reload says so", err && err.text, "reload: broken");
  }

  // ─── the window's Show all: several layers selected at once ──────────────
  {
    const env = load();
    const pageA = { id: "0:1", type: "PAGE", parent: null }, pageB = { id: "0:2", type: "PAGE", parent: null };
    const a1 = { id: "1:1", type: "FRAME", parent: pageA }, a2 = { id: "1:2", type: "FRAME", parent: pageA };
    const b1 = { id: "2:1", type: "FRAME", parent: pageB };
    for (const n of [a1, a2, b1]) env.nodes.set(n.id, n);
    env.figma.currentPage = pageA;
    env.figma.viewport = { scrollAndZoomIntoView() {} };
    env.figma.setCurrentPageAsync = async (p) => { env.figma.currentPage = p; };
    env.posted.length = 0;
    await env.figma.ui.onmessage({ type: "select", id: "1:1", ids: ["1:1", "2:1", "1:2", "9:9"] });
    check("Show all selects the layers of the first one's page together", env.figma.currentPage.selection.map((n) => n.id), ["1:1", "1:2"]);
    check("…and only a layer that is really gone is reported gone, not one on another page",
          env.posted.filter((m) => m.type === "selected").map((m) => [m.id, m.ok]),
          [["1:1", true], ["2:1", true], ["1:2", true], ["9:9", false]]);
    // Figma keeps only the ancestor when a frame and its child are both set: the plugin sets the specific ones
    let held = [];
    Object.defineProperty(env.figma.currentPage, "selection", { configurable: true, get: () => held,
      set(list) { held = list.filter((n) => !list.some((o) => o !== n && (() => { for (let q = n.parent; q; q = q.parent) if (q === o) return true; return false; })())); } });
    const card = { id: "1:5", type: "FRAME", parent: pageA }, kid = { id: "1:6", type: "TEXT", parent: card }, sib = { id: "1:7", type: "TEXT", parent: pageA };
    for (const n of [card, kid, sib]) env.nodes.set(n.id, n);
    env.posted.length = 0;
    await env.figma.ui.onmessage({ type: "select", id: "1:5", ids: ["1:5", "1:6", "1:7"] });
    check("Show all with a frame and its child selects the child, not only the frame",
          [env.figma.currentPage.selection.map((n) => n.id), env.posted.filter((m) => m.type === "selected").map((m) => m.id)],
          [["1:6", "1:7"], ["1:5", "1:6", "1:7"]]);
    env.posted.length = 0;
    await env.figma.ui.onmessage({ type: "select", id: "2:1" });
    check("one id works as before", [env.figma.currentPage.id, env.figma.currentPage.selection.map((n) => n.id), env.posted.filter((m) => m.type === "selected")],
          ["0:2", ["2:1"], [{ type: "selected", id: "2:1", ok: true }]]);
  }

  // ─── pictures that come with a script ───────────────────────────────────
  {
    const env = load();
    const png = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47]), Buffer.from("-picture-")]);
    const files = [{ name: "image", data: png.toString("base64") },
                   { name: "logo", data: Buffer.from("not a picture").toString("base64") }];
    const rect = { id: "1:2", name: "Photo", type: "RECTANGLE", fills: [{ type: "SOLID" }] };
    env.nodes.set("1:2", rect);
    env.nodes.set("1:3", { id: "1:3", name: "Group", type: "GROUP" });  // a group has no fills
    const hash = require("crypto").createHash("sha1").update(png).digest("hex");

    const r = await exec(env, "return h.image(await h.resolve('1:2'), 'image', 'fit');", { images: files });
    check("a file that came with the script fills a layer", [r.type, r.value],
      ["result", { id: "1:2", name: "Photo", mode: "fit", hash }]);
    check("…instead of its fills, by the picture's hash",
      rect.fills, [{ type: "IMAGE", scaleMode: "FIT", imageHash: hash }]);
    await exec(env, "h.image(await h.resolve('1:2'), 'image');", { images: files });
    check("…fill is the default mode", rect.fills[0].scaleMode, "FILL");
    await exec(env, "h.image(await h.resolve('1:2'), 'image', 'CROP');", { images: files });
    check("crop starts with the whole picture", [rect.fills[0].scaleMode, rect.fills[0].imageTransform],
      ["CROP", [[1, 0, 0], [0, 1, 0]]]);
    await exec(env, "h.image(await h.resolve('1:2'), 'image', 'tile');", { images: files });
    check("tile keeps the picture's own size", [rect.fills[0].scaleMode, rect.fills[0].scalingFactor], ["TILE", 1]);
    await exec(env, "h.image(await h.resolve('1:2'), new Uint8Array([0x89, 0x50, 1, 2]), 'fit');");
    check("bytes work without a file", rect.fills[0].scaleMode, "FIT");

    const fails = async (code, opts) => {
      const e = await exec(env, code, opts);
      return e.type === "error" ? e.text : "no error: " + JSON.stringify(e.value);
    };
    check("a group has no fills, and the error says what to do instead",
      /GROUP .* has no fills.*frame, a rectangle or a shape/.test(
        await fails("h.image(await h.resolve('1:3'), 'image');", { images: files })), true);
    check("…and a bridge that dropped the file is named as a second cause",
      /bridge is older than the CLI.*start-bridge\.sh/.test(
        await fails("h.image(await h.resolve('1:2'), 'photo');")), true);
    check("a name that did not come lists those that did",
      /no file named "photo" came with the script \(it has: image, logo\)/.test(
        await fails("h.image(await h.resolve('1:2'), 'photo');", { images: files })), true);
    check("…and with none, says how to bring one",
      /no file named "photo".*figaro exec --image photo=FILE/.test(
        await fails("h.image(await h.resolve('1:2'), 'photo');")), true);
    check("a mode that does not exist is refused",
      /the mode is fill, fit, crop or tile, not "stretch"/.test(
        await fails("h.image(await h.resolve('1:2'), 'image', 'stretch');", { images: files })), true);
    check("Figma's refusal of the bytes comes with what it takes",
      /Figma refused the picture: Image type is unsupported \(it takes PNG, JPEG and GIF up to 4096 px/.test(
        await fails("h.image(await h.resolve('1:2'), 'logo');", { images: files })), true);
    check("a refused picture leaves the fills as they were", rect.fills[0].scaleMode, "FIT");
    check("a script that fills a layer counts as one that writes",
      [env.api.codeMayWrite("h.image(n, 'image');"), env.api.codeMayWrite("return h.shot(n);")], [true, false]);
  }

  // ─── a helper's long walk ───────────────────────────────────────────────
  {
    const env = load();
    const r = await exec(env, "return await h.annotations();");
    check("h.annotations reads the page by default", [r.type, r.value], ["result", []]);
    // Every clock reading jumps 100 ms: the walk stops to breathe at once, and
    // the script's own h.ck() finds the deadline gone.
    const now = Date.now;
    let clock = now();
    Date.now = () => (clock += 100);
    let late;
    try {
      late = await exec(env, "return await h.annotations();", { timeout: 0.15 });
    } finally {
      Date.now = now;
    }
    check("…and stops when the script's time is up", [late.type, /^aborted:/.test(late.text)], ["error", true]);
  }

  await sleep(50);
  console.log(failed ? `\n${failed} FAILED` : "\nall exec checks passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
