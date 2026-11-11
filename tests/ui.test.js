// The plugin window (plugin/ui.html) against stubs of the browser.
//
// The keep-awake tone starts with a request from the bridge, plays while it
// runs, stops 3 min after the last one ends, and stops at once when the bridge
// goes away: it never plays while the bridge is down. The islands show what the bridge's `board` says —
// each agent at work, done, stopped or failed, and the error of its last script
// until one succeeds — the line under them, always at the bottom, the Figaro
// version, a newer one, and the tools: Settings and Minimize (a click: mini,
// held: nano), and their buttons send the bridge and the sandbox what they
// should. The settings change the bridge's; Show other files off leaves this
// file's island alone. An
// agent's Stop shows while the pointer is on its row; an island's panel folds
// open and shut, at once for those who ask for less motion. The window asks the
// sandbox for the height of all it shows, again every second. An island that
// opens or closes drives the window a frame at a time, and its fold takes the
// height Figma has given the window, so the islands fill it on every frame;
// one that opens low in a window that can't grow scrolls into view. A file that stopped
// answering (a script running on past its time, or a window that closed while
// one ran) has a red island, first after this file's, that says what to do.
//
//     node tests/ui.test.js
const fs = require("fs");
const path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "plugin", "ui.html"), "utf8");
const script = html.slice(html.indexOf("<script>") + "<script>".length, html.lastIndexOf("</script>"));

const flush = () => new Promise((r) => setImmediate(r));

// Timers on a fake clock, so the 3-minute idle runs in no time.
function makeClock() {
  let now = 0, next = 1;
  const timers = new Map();
  return {
    now: () => now,
    setTimeout: (fn, ms = 0) => { timers.set(next, { fn, at: now + ms }); return next++; },
    setInterval: (fn, ms) => {
      const id = next++;
      const tick = () => { timers.set(id, { fn: tick, at: now + ms }); fn(); };
      timers.set(id, { fn: tick, at: now + ms });
      return id;
    },
    clearTimeout: (id) => { timers.delete(id); },
    // Run every timer due within `ms` in time order, letting promises settle between them.
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        await flush();
        let due = null;
        for (const [id, t] of timers) if (t.at <= end && (!due || t.at < due.at)) due = { id, ...t };
        if (!due) break;
        timers.delete(due.id);
        now = due.at;
        due.fn();
      }
      now = end;
      await flush();
    },
  };
}

// Load the window's script with a stub WebSocket (the test opens, feeds and
// drops it) and a stub AudioContext whose state is the tone. `calm`: the
// system asks for less motion. `layout`: boxes as a browser would lay the
// window out — every island a 30 px row and, open, its `panel` under it, 6 px
// apart, the list 6 px from the top and 6 px over its end, and the last line,
// when there is one, 28 px at the bottom — in a window `window` px tall. `figma`: frames
// (requestAnimationFrame every 16 ms) and a Figma that gives the window the
// height it asks for `lag` frames later, half a frame before one is drawn, at
// most `room` px (the sandbox's cap); `deaf`: it never does. The clipboard keeps
// what it's given (`copied()`), or refuses it with `clip.deny`, as Figma's does.
function load({ calm = false, layout = null, figma = null } = {}) {
  const clock = makeClock();
  const clip = { texts: [], deny: false };
  const navigator = { clipboard: { writeText: (t) => (clip.deny ? Promise.reject(new Error("denied")) : (clip.texts.push(t), Promise.resolve())) } };
  const sockets = [];
  const contexts = [];
  const posted = [];
  class WebSocket {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    send(data) { this.sent.push(JSON.parse(data)); }
    open() { this.readyState = 1; this.onopen(); }
    receive(m) { this.onmessage({ data: JSON.stringify(m) }); }
    drop() { this.readyState = 3; if (this.onerror) this.onerror(); this.onclose(); }
  }
  Object.assign(WebSocket, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
  // A sound's layers, as `heard()` lists them: "sine 660", "noise", …
  const voices = [];
  const audio = { lag: 0 };
  const param = () => ({ setValueAtTime() {}, linearRampToValueAtTime() {}, setTargetAtTime() {}, exponentialRampToValueAtTime() {} });
  class AudioContext {
    constructor() { this.state = "suspended"; this.destination = {}; this.currentTime = 0; this.sampleRate = 48000; this.oscs = []; contexts.push(this); }
    createOscillator() {
      const osc = { type: "sine", frequency: Object.assign(param(), { setValueAtTime(v) { this.value = v; } }), connect() {},
                    start: () => voices.push(`${osc.type} ${osc.frequency.value}`), stop() {} };
      this.oscs.push(osc);
      return osc;
    }
    createBufferSource() { return { connect() {}, start: () => voices.push("noise"), stop() {} }; }
    createBuffer(channels, length) { const data = new Float32Array(length); return { getChannelData: () => data }; }
    createBiquadFilter() { return { frequency: param(), Q: {}, connect() {} }; }
    createGain() { return { gain: param(), connect() {} }; }
    // `audio.lag`: how long the system takes to wake a context, as on Windows
    resume() {
      if (!audio.lag) { this.state = "running"; return Promise.resolve(); }
      return new Promise((r) => clock.setTimeout(() => { this.state = "running"; r(); }, audio.lag));
    }
    suspend() { this.state = "suspended"; return Promise.resolve(); }
  }
  const elements = {};
  const listeners = {};
  const htmlClasses = new Set();  // <html>'s own classes: the rainbow's
  const document = {
    documentElement: { classList: { toggle: (c) => (htmlClasses.has(c) ? htmlClasses.delete(c) : htmlClasses.add(c)), contains: (c) => htmlClasses.has(c) } },
    getElementById: (id) => (elements[id] = elements[id] || {
      textContent: "", innerHTML: "", querySelectorAll: () => [],
    }),
    visibilityState: "hidden",
    body: { style: {} },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  };
  const window = { matchMedia: (q) => ({ matches: calm && /reduce/.test(q) }), innerHeight: layout ? layout.window : 0 };
  const parent = { postMessage: (m) => {
    posted.push(m.pluginMessage);
    if (figma && !figma.deaf && m.pluginMessage.type === "resize") {
      const h = Math.min(m.pluginMessage.height, figma.room || Infinity);
      clock.setTimeout(() => { window.innerHeight = h; if (layout) layout.window = h; }, 16 * (figma.lag || 0) + 8);
    }
  } };
  const frame = figma ? (fn) => clock.setTimeout(() => fn(clock.now()), 16) : undefined;
  new Function("document", "window", "parent", "WebSocket", "AudioContext",
               "setTimeout", "clearTimeout", "setInterval", "Date", "console", "requestAnimationFrame", "performance", "navigator", script)(
    document, window, parent, WebSocket, AudioContext,
    clock.setTimeout, clock.clearTimeout, clock.setInterval, { now: clock.now }, { log() {} }, frame, { now: clock.now }, navigator);
  if (layout) lay(elements, layout);
  const env = {
    clock, sockets, posted, clip, scrolled: layout ? layout.scrolled : [],
    copied: () => clip.texts.splice(0),
    inner: () => window.innerHeight,
    laid: () => document.body.style.width,  // the width the page lays itself out at
    // what the window's islands and last line take, as ui.html measures it; an island's fold height
    content: () => elements.scroll.getBoundingClientRect().top + elements.app.getBoundingClientRect().height
      + elements.end.getBoundingClientRect().height + elements.foot.getBoundingClientRect().height,
    fold: (fid) => (layout && layout.folds.has(fid) ? layout.folds.get(fid).style.height : null),
    room: (height) => window.onmessage({ data: { pluginMessage: { type: "room", height } } }),
    asks: () => posted.filter((m) => m.type === "resize").map((m) => m.height),
    ws: () => sockets[sockets.length - 1],
    tone: () => { const c = contexts.find((x) => x.oscs.some((o) => o.frequency.value === 20)); return c ? c.state : "none"; },
    heard: () => voices.splice(0),  // the sounds' layers since the last look
    audio,
    // The pointer moves over the window, or presses somewhere in it.
    move: () => { for (const fn of listeners.pointermove || []) fn({}); },
    down: () => { for (const fn of listeners.pointerdown || []) fn({ button: 0, target: { closest: () => null } }); },
    cues: () => { const c = contexts.find((x) => !x.oscs.some((o) => o.frequency.value === 20)); return c ? c.state : "none"; },
    exec: (id, timeout) => env.ws().receive({ type: "exec", id, code: "return 1", timeout }),
    // The sandbox's answer to a request, on its way to the bridge.
    selected: (id, ok) => window.onmessage({ data: { pluginMessage: { type: "selected", id, ok } } }),
    answer: (id) => window.onmessage({ data: { pluginMessage: { type: "result", id, value: 1 } } }),
    html: () => elements.app.innerHTML + elements.end.innerHTML + elements.foot.innerHTML,  // the islands, then the last line
    app: () => elements.app.innerHTML,
    foot: () => elements.foot.innerHTML,
    pinned: () => elements.foot.innerHTML !== "",  // the last line, at the bottom
    sent: (type) => env.ws().sent.filter((m) => m.type === type),
    identity: (name, key, view) => window.onmessage({ data: { pluginMessage: {
      type: "identity", name, fileKey: key, docSig: key, pluginVersion: "test", caps: [], view } } }),
    board: (files, extra) => env.ws().receive(Object.assign({ type: "board", now: 1000, version: "1.1.0", files }, extra)),
    // A click on a button: what the page's click listener gets from it; `detail`
    // 0 (a key, as here by default) or the pointer's count of clicks.
    click(act, fid, data, detail) {
      const slot = fid ? { dataset: { fid } } : null;
      const el = { dataset: Object.assign({ act }, data), disabled: false, classList: { toggle() {}, add() {}, remove() {} }, setAttribute() {},
                   closest: (sel) => (sel === "[data-fid]" ? slot : null) };
      for (const fn of listeners.click || []) fn({ detail, target: { closest: (sel) => (sel === "[data-act]" ? el : null) } });
    },
    // A click on a grid of the wave: in an agent's row, in an island's own row (inside its toggle),
    // or in the minimized card (inside its restore).
    tapGrid(where = "agent") {
      const holder = where === "agent" ? null : {
        dataset: { act: where === "mini" ? "restore" : "toggle" }, disabled: false,
        classList: { toggle() {}, add() {}, remove() {} }, setAttribute() {},
        closest: (sel) => (sel === "[data-fid]" && where === "row" ? { dataset: { fid: "here" } } : null) };
      const grid = { closest: (sel) => (sel === ".mini" && where === "mini" ? {} : null) };
      for (const fn of listeners.click || []) {
        fn({ detail: 1, target: { closest: (sel) => (sel === ".px" ? grid : sel === "[data-act]" ? holder : null) } });
      }
    },
    rainbow: () => htmlClasses.has("rainbow"),
    // The pointer pressed on Minimize; let go on it, or off it.
    press() {
      const cls = new Set();
      const el = { dataset: { act: "min" }, classList: { add: (c) => cls.add(c), remove: (c) => cls.delete(c) }, contains: (t) => t === el };
      el.closest = (sel) => (sel === '[data-act="min"]' || sel === "[data-act]" ? el : null);
      for (const fn of listeners.pointerdown || []) fn({ button: 0, target: el });
      return {
        holding: () => cls.has("hold"),
        // the press's click goes to what is under the pointer by then: `under`, a button's act
        up(on = true, under = "min") {
          for (const fn of listeners.pointerup || []) fn({ target: on ? el : { closest: () => null } });
          const at = { dataset: { act: under }, disabled: false, closest: (sel) => (sel === "[data-act]" ? at : null) };
          for (const fn of listeners.click || []) fn({ detail: 1, target: at });
        },
      };
    },
    // Text typed in the Figma token field, and Enter pressed in it.
    type(value) {
      const el = { dataset: { field: "figma-token" }, value };
      for (const fn of listeners.input || []) fn({ target: el });
    },
    enter() {
      const el = { dataset: { field: "figma-token" } };
      for (const fn of listeners.keydown || []) fn({ key: "Enter", target: el });
    },
    // The pointer on an agent's row, or with no agent anywhere else in the window.
    point(fid, name) {
      const slot = { dataset: { fid } };
      const row = name ? { dataset: { agent: name }, closest: (sel) => (sel === "[data-fid]" ? slot : null) } : null;
      for (const fn of listeners.pointerover || []) fn({ target: { closest: (sel) => (sel === ".agent[data-agent]" ? row : null) } });
    },
  };
  return env;
}

function lay(elements, layout) {
  const TOP = 6, ROW = 30, GAP = 6, END = 6, LINE = 28;
  const pinned = () => elements.foot.innerHTML !== "";
  const box = (top, height) => ({ top, height, bottom: top + height });
  const row = { getBoundingClientRect: () => box(0, ROW) };
  // An open or closing island's fold, while the window draws it: a height of its own
  // (style.height, from a drive) or the CSS's, the panel whole when open.
  let drawn = null;
  layout.folds = new Map();
  const foldOf = (fid) => {
    if (drawn !== elements.app.innerHTML) { drawn = elements.app.innerHTML; layout.folds = new Map(); }
    if (!layout.folds.has(fid)) layout.folds.set(fid, { style: { height: "" }, firstElementChild: { scrollHeight: layout.panel } });
    return layout.folds.get(fid);
  };
  // The islands the window drew, top to bottom.
  const slots = () => {
    let top = TOP;
    return [...elements.app.innerHTML.matchAll(/data-fid="([^"]+)"><div class="island([^"]*)"/g)].map((m) => {
      const fold = / open| closing/.test(m[2]) ? foldOf(m[1]) : null;
      const own = fold && fold.style.height !== "" ? parseFloat(fold.style.height) : null;
      const height = ROW + (own !== null ? own : / open/.test(m[2]) ? layout.panel : 0);
      const at = top;
      const island = { classList: { add() {} }, querySelector: (sel) => (sel === ".fold" ? fold : null) };
      const slot = { dataset: { fid: m[1] }, getBoundingClientRect: () => box(at, height),
        querySelector: (sel) => (sel === ".row" ? row : sel === ".island" ? island : null) };
      top += height + GAP;
      return slot;
    });
  };
  const listHeight = () => (/^<div class="mini/.test(elements.app.innerHTML) ? ROW
    : slots().reduce((h, el) => h + el.getBoundingClientRect().height + GAP, -GAP));
  layout.scrolled = [];
  Object.assign(elements.app, {
    getBoundingClientRect: () => box(TOP, listHeight()),
    querySelector: (sel) => (sel === ".row" ? row : null),
    querySelectorAll: (sel) => (sel === ".slot" ? slots() : []),
  });
  elements.end.getBoundingClientRect = () => box(TOP + listHeight(), END);
  elements.foot.getBoundingClientRect = () => (pinned() ? box(layout.window - LINE, LINE) : box(0, 0));
  Object.assign(elements.scroll, {
    getBoundingClientRect: () => box(TOP, layout.window - (pinned() ? LINE : 0) - TOP),
    scrollBy: (o) => layout.scrolled.push(o.top),
  });
}

// A file in the bridge's board and an agent in it; times are the bridge's
// clock, which reads 1000 s when the window gets its first board.
function file(key, name, extra) {
  return Object.assign({ doc: "doc:" + key, sig: key, name, agents: [], recent: [] }, extra);
}
function agent(name, extra) {
  return Object.assign({ name, since: null, last: 0, busy: false, done: null, stopped: null, error: null }, extra);
}

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}

const MIN = 60 * 1000;
const L = 28;  // the last line under the islands, always there
const RELEASE = "https://github.com/artem-levchenko/figaro/releases/tag/v1.2.0";

(async () => {
  // ─── scripts keep it up ─────────────────────────────────────────────────
  {
    const env = load();
    env.ws().open();
    check("connected: no tone until a script comes", env.tone(), "none");
    env.exec("e1");
    await env.clock.advance(300);
    check("a script from the bridge starts the tone", env.tone(), "running");
    check("…and reaches the sandbox once the window is awake",
          env.posted.filter((m) => m.type === "exec").map((m) => m.id), ["e1"]);
    env.answer("e1");
    await env.clock.advance(2 * MIN);
    env.exec("e2");
    await env.clock.advance(300);
    env.answer("e2");
    await env.clock.advance(2 * MIN);
    check("each script keeps it up 3 more minutes", env.tone(), "running");
    await env.clock.advance(1 * MIN);
    check("3 min after the last script it stops", env.tone(), "suspended");
  }
  {
    const env = load();
    env.ws().open();
    env.exec("long", 600);
    await env.clock.advance(5 * MIN);
    check("a script that runs longer than 3 min keeps it up", env.tone(), "running");
    env.answer("long");
    await env.clock.advance(3 * MIN - 1000);
    check("…until 3 min after it ends", env.tone(), "running");
    await env.clock.advance(2000);
    check("…and then it stops", env.tone(), "suspended");

    env.exec("lost", 60);
    await env.clock.advance(4 * MIN - 1000);
    check("a script whose answer never comes keeps it up 3 min past its time", env.tone(), "running");
    await env.clock.advance(2000);
    check("…not longer", env.tone(), "suspended");
  }

  // ─── the bridge goes away ───────────────────────────────────────────────
  {
    const env = load();
    env.ws().open();
    env.exec("e1");
    await env.clock.advance(300);
    env.ws().drop();
    check("the bridge is gone: the tone stops at once", env.tone(), "suspended");
    await env.clock.advance(10 * MIN);
    check("…and stays off while the window knocks", env.tone(), "suspended");
    env.ws().open();
    check("the bridge is back: still off until a script comes", env.tone(), "suspended");
    env.exec("e2");
    await env.clock.advance(300);
    check("…the next script brings it back", env.tone(), "running");
  }

  // ─── no bridge at all ───────────────────────────────────────────────────
  {
    const env = load();
    check("before the bridge answers: this file's island, connecting, with a spinner",
          [/connecting…/.test(env.html()), /class="spin idle"/.test(env.html()), /class="px"/.test(env.html())], [true, true, false]);
    for (let i = 0; i < 5; i++) {
      env.ws().drop();
      await env.clock.advance(2000);
    }
    check("no bridge: the window keeps knocking every 2 s", env.sockets.length, 6);
    check("…and never plays", env.tone(), "none");
    check("…and says there is no bridge yet", /no bridge yet/.test(env.html()), true);
    check("…and its last line has the tools, and no version to name yet",
          [env.pinned(), /<span class="ver">Figaro<\/span><span class="tools">/.test(env.foot()), /data-act="gear"/.test(env.foot()), /data-act="min"/.test(env.foot())],
          [true, true, true, true]);
  }

  // ─── this file's island ─────────────────────────────────────────────────
  // (The stubs have no layout: what keeps an island as narrow as the window,
  // whatever its names, is in the CSS.)
  check("the rainbow colours each diagonal of the wave, all nine cells",
        (html.match(/html\.rainbow \.px i:nth-child\(\d\)/g) || []).length, 9);
  check("an island is never wider than the window, however long the names in its row",
        /\n\.island \{[^}]*\bmin-width: 0;[^}]*overflow: clip;/.test(html), true);
  check("a layer's name in its chip keeps the tails of g, p and y: its box is taller than the 11 px letters",
        /\n\.layer span \{[^}]*\boverflow: hidden;[^}]*\bline-height: 14px;/.test(html) && /\n\.layer \{[^}]*height: 20px;/.test(html), true);
  {
    const env = load();
    const ago = (sec) => 1000 + env.clock.now() / 1000 - sec;  // the bridge's time, `sec` seconds ago
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    check("hello carries the file", env.sent("hello").map((m) => m.name).pop(), "Dashboard");
    env.board([file("KEY1", "Dashboard", { agents: [agent("designer", { since: ago(12), last: ago(1), busy: true })] })]);
    await env.clock.advance(400);
    const closed = env.html();
    check("an agent at work: the file, the agent and its clock",
          [/Dashboard/.test(closed), /designer/.test(closed), /data-clock/.test(closed)], [true, true, true]);
    check("nothing to do about the version: the last line has it and the tools, at the bottom",
          [/<span class="ver">Figaro 1\.1\.0<\/span><span class="tools">/.test(env.foot()), /class="foot"/.test(closed), env.pinned()], [true, true, true]);
    env.click("toggle", "here");
    const open = env.html();
    check("open: its panel grows from nothing", /class="island open opening"/.test(open), true);
    check("…the agent's own row says it works, its Stop there but hidden",
          [/working/.test(open), /data-act="stop" data-agent="designer"/.test(open), /class="agent hot"/.test(open)], [true, true, false]);
    check("…and the island's row keeps only the file's name", /class="sub"/.test(open), false);
    env.point("here", "designer");
    await env.clock.advance(400);
    check("the pointer on the row shows its Stop, through the redraw after the opening",
          [/class="agent hot" data-agent="designer"/.test(env.html()), /opening/.test(env.html())], [true, false]);
    const sentBefore = env.ws().sent.length, postedBefore = env.posted.length;
    env.tapGrid();
    check("a click on the agent's grid turns every grid into a rainbow, and sends nothing to the bridge or Figma",
          [env.rainbow(), env.ws().sent.length - sentBefore, env.posted.length - postedBefore], [true, 0, 0]);
    env.tapGrid();
    check("…and another click turns it back", env.rainbow(), false);
    env.tapGrid("row");
    check("a click on the grid in the island's own row: the rainbow too, and the island stays open",
          [env.rainbow(), /class="island open/.test(env.html())], [true, true]);
    env.tapGrid("row");
    check("…and back", [env.rainbow(), /class="island open/.test(env.html())], [false, true]);
    env.click("stop", "here", { agent: "designer" });
    check("Stop asks the bridge to stop that agent", env.sent("stop"), [{ type: "stop", doc: "doc:KEY1", agent: "designer" }]);
    check("…and the button waits for it", /disabled/.test(env.html()), true);

    env.point("here", null);
    env.board([file("KEY1", "Dashboard", {
      agents: [agent("designer", { since: ago(12), last: ago(1) })],
      recent: [{ agent: "designer", at: ago(1), kind: "ok", summary: "Created KPI card",
                 layers: [{ id: "5:6", name: "KPI card", type: "COMPONENT" }] }] })]);
    check("…shown while it waits, the pointer gone", /class="agent held" data-agent="designer"/.test(env.html()), true);
    await env.clock.advance(MIN);
    check("between its scripts the agent is still at work", /working/.test(env.html()), true);
    check("open: the recent changes", /Created KPI card/.test(env.html()), true);
    env.click("layer", "here", { id: "5:6" });
    check("a layer of this file: select it in Figma", env.posted.filter((m) => m.type === "select"), [{ type: "select", id: "5:6" }]);
    await env.clock.advance(4 * MIN);
    check("five quiet minutes after its last script it is idle, with no room kept for a Stop",
          [/working/.test(env.html()), /idle · <span[^>]*>5m</.test(env.html()), /class="act"/.test(env.html())], [false, true, false]);

    env.board([file("KEY1", "Dashboard", { agents: [
      agent("designer", { since: ago(300), last: ago(200), done: { text: "Check the card", at: ago(1) } })] })]);
    await env.clock.advance(400);
    check("an agent says it is done: its row, with the note, and no buttons",
          [/done · /.test(env.html()), /Check the card/.test(env.html()), /<button[^>]*data-act="(stop|dismiss)"/.test(env.html())],
          [true, true, false]);
    env.click("toggle", "here");
    check("closed: who is done", /designer is done/.test(env.html()), true);

    env.board([file("KEY1", "Dashboard", { agents: [agent("designer", { since: ago(30), last: ago(5), stopped: ago(1) })] })]);
    await env.clock.advance(400);
    check("stopped by the user", /designer stopped/.test(env.html()), true);

    env.board([file("KEY1", "Dashboard", { agents: [agent("designer", { since: ago(250), last: ago(1),
      error: { text: "Cannot read properties of null", line: 14, at: ago(1) } })] })]);
    await env.clock.advance(400);
    check("a failed script while the agent works: it is still at work", /designer <span data-clock[^>]*>4:1/.test(env.html()), true);
    env.click("toggle", "here");
    check("…its row says so, with the error under its name",
          /data-agent="designer">.*?working.*?<p class="msg err">Line 14: Cannot read properties of null<\/p>/.test(env.html()), true);
    env.click("toggle", "here");
    check("closing: the panel folds away first", [/class="island closing"/.test(env.html()), /class="fold"/.test(env.html())], [true, true]);
    await env.clock.advance(400);
    check("…then the island is its row alone", [/closing/.test(env.html()), /class="fold"/.test(env.html())], [false, false]);
    await env.clock.advance(5 * MIN);
    check("…and when it goes quiet after it: failed", /designer failed/.test(env.html()), true);
    env.click("toggle", "here");
    check("…with the error under its name", /Line 14: Cannot read properties of null/.test(env.html()), true);
  }

  // ─── the window's height ────────────────────────────────────────────────
  {
    const env = load({ layout: { panel: 400, window: 200 } });
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$")]);
    await env.clock.advance(400);
    const asks = () => env.posted.filter((m) => m.type === "resize");
    const last = () => asks()[asks().length - 1];
    check("the window asks the sandbox for the height and the width of all it shows", last(), { type: "resize", height: 6 + 30 + 6 + 30 + 6 + L, width: 320 });
    const n = asks().length;
    await env.clock.advance(1000);
    check("…and asks again every second, so the sandbox sees a window that moved", asks().length > n, true);
    env.click("toggle", "here");
    check("an island opens with no frames to drive it: the window asks for all it shows at once", last().height, 78 + L + 400);
    await env.clock.advance(400);
    check("…and nothing is under the window's edge to bring into view: the list stays", env.scrolled, []);
    env.click("toggle", "here");
    await env.clock.advance(400);
    check("it closes: the window asks for its height without the panel", last().height, 78 + L);
  }
  {
    const env = load({ layout: { panel: 300, window: 200 } });
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$"), file("KEY3", "Kit")]);
    await env.clock.advance(400);
    env.click("toggle", "doc:KEY3");
    await env.clock.advance(500);
    check("an island opens low in a short window: the list scrolls it up to the top, no further",
          env.scrolled, [6 + 2 * 36 - 6]);
  }
  {
    const env = load({ calm: true, layout: { panel: 400, window: 200 } });
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard")]);
    await env.clock.advance(400);
    env.click("toggle", "here");
    env.click("toggle", "here");
    check("less motion: the window asks for its height without the panel at once", env.asks().pop(), 6 + 30 + 6 + L);
  }
  {
    const env = load({ layout: { panel: 400, window: 200 } });
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$")], { update: { latest: "1.2.0", url: RELEASE } });
    await env.clock.advance(400);
    check("a new release on the last line: it is at the bottom, and the window asks for it too",
          [env.pinned(), env.asks().pop()], [true, 6 + 30 + 6 + 30 + 6 + L]);
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$")]);
    await env.clock.advance(400);
    check("…and with nothing left to do, the line stays as tall", [env.pinned(), env.asks().pop()], [true, 6 + 30 + 6 + 30 + 6 + L]);
    env.click("min");
    await env.clock.advance(400);
    check("minimized: the window asks for one row and 240 px, the corner kept by the sandbox",
          [env.pinned(), env.posted.filter((m) => m.type === "resize").pop()], [false, { type: "resize", height: 6 + 30 + 6, width: 240 }]);
    check("…and lays itself out at 240 px at once, before the sandbox narrows it", env.laid(), "240px");
    env.click("restore");
    await env.clock.advance(400);
    check("…and restored, for all it showed, 320 px wide", env.posted.filter((m) => m.type === "resize").pop(), { type: "resize", height: 78 + L, width: 320 });
    check("…measured at 320 px, not at the narrow window's width", env.laid(), "320px");
  }

  // ─── an island that opens or closes drives the window ───────────────────
  // Frames every 16 ms from the click; Figma resizes the window two frames
  // after it asks, half a frame before one is drawn; each frame is looked at
  // just after the window drew it.
  const watch = async (env, ms) => {
    const frames = [];
    await env.clock.advance(1);
    for (let t = 0; t < ms; t += 16) {
      await env.clock.advance(16);
      frames.push({ inner: env.inner(), content: env.content(), fold: env.fold("here"), ask: env.asks().pop() });
    }
    return frames;
  };
  const rises = (list) => list.every((h, i) => i === 0 || h >= list[i - 1]);
  {
    const env = load({ layout: { panel: 300, window: 106 }, figma: { lag: 2 } });
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$")]);
    await env.clock.advance(400);
    const h0 = env.inner(), before = env.asks().length;
    env.click("toggle", "here");
    let frames = await watch(env, 640);
    let asks = env.asks().slice(before);
    check("an island opens: every frame the window asks for its next height, up to all it shows",
          [asks.length > 15, rises(asks), asks[asks.length - 1]], [true, true, h0 + 300]);
    check("…the islands fill the window on every frame: the fold takes the height Figma has given it",
          frames.filter((f) => Math.abs(f.inner - f.content) > 1).length, 0);
    check("…it goes the island's curve, slow at first", frames.slice(0, 4).every((f) => f.inner - h0 < 40), true);
    check("…until the island is whole", [frames.some((f) => f.fold === "300px" && f.inner === h0 + 300)], [true]);
    await env.clock.advance(400);
    check("…and once drawn anew, the open island is as tall as when the fold was driven", env.content(), h0 + 300);
    const mid = env.asks().length;
    env.click("toggle", "here");
    frames = await watch(env, 560);
    asks = env.asks().slice(mid);
    check("it closes: the window asks for less every frame, down to its height without the panel",
          [asks.length > 12, rises(asks.slice().reverse()), asks[asks.length - 1]], [true, true, h0]);
    check("…the islands fill the window on every frame", frames.filter((f) => Math.abs(f.inner - f.content) > 1).length, 0);
    await env.clock.advance(400);
    check("…and the island is drawn shut", [env.content(), env.inner()], [h0, h0]);
  }
  {
    const env = load({ layout: { panel: 300, window: 106 }, figma: { lag: 2, room: 106 + 120 } });
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$")]);
    await env.clock.advance(400);
    env.room(106 + 120);  // the sandbox: no taller than this
    const before = env.asks().length;
    env.click("toggle", "here");
    const frames = await watch(env, 640);
    const driven = frames.filter((f) => f.fold !== "");
    check("the canvas lets the window grow only so much: it asks for no more while it drives",
          Math.max(...driven.filter((f) => f.fold !== "300px").map((f) => f.ask)), 106 + 120);
    check("…and the fold opens whole, past the window's bottom: the islands scroll",
          [frames[frames.length - 1].inner, driven[driven.length - 1].fold, env.content()], [106 + 120, "300px", 78 + L + 300]);
    check("…never short of the window on the way", frames.filter((f) => f.content < f.inner - 1).length, 0);
  }
  {
    const env = load({ layout: { panel: 300, window: 106 }, figma: { lag: 2, room: 106 } });
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$")]);
    await env.clock.advance(400);
    env.room(106);  // a window that can't grow at all
    env.click("toggle", "here");
    const frames = await watch(env, 640);
    const half = parseFloat(frames[10].fold);
    check("a window that can't grow: the fold follows the clock",
          [half > 50 && half < 250, frames.some((f) => f.fold === "300px"), env.inner()], [true, true, 106]);
  }
  {
    const env = load({ layout: { panel: 300, window: 106 }, figma: { deaf: true } });
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$")]);
    await env.clock.advance(400);
    env.click("toggle", "here");
    const frames = await watch(env, 400);
    await watch(env, 400);
    check("Figma never gives the window that height: the fold waits for it…", frames[frames.length - 1].fold, "0px");
    check("…and the island opens whole anyway soon after", env.content(), 78 + L + 300);
  }
  {
    const env = load({ calm: true, layout: { panel: 300, window: 106 }, figma: { lag: 2 } });
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$")]);
    await env.clock.advance(400);
    const before = env.asks().length;
    env.click("toggle", "here");
    check("less motion: no drive, the window asks for all it shows at once", env.asks().slice(before), [78 + L + 300]);
  }

  // ─── less motion ────────────────────────────────────────────────────────
  {
    const env = load({ calm: true });
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard")]);
    await env.clock.advance(400);
    env.click("toggle", "here");
    env.click("toggle", "here");
    check("the system asks for less motion: an island closes at once", /closing|class="fold"/.test(env.html()), false);
  }

  // ─── other files ────────────────────────────────────────────────────────
  {
    const env = load();
    const ago = (sec) => 1000 + env.clock.now() / 1000 - sec;
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY2", "Sandbox 5$"), file("KEY1", "Dashboard"), file("KEY3", "Kit")]);
    await env.clock.advance(400);
    const at = ["Dashboard", "Sandbox 5$", "Kit"].map((n) => env.html().indexOf(`title="${n}"`));
    check("every file where Figaro runs has an island, quiet or not: this file first, then the others as they connected",
          [at.every((i) => i >= 0), at[0] < at[1] && at[1] < at[2]], [true, true]);
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$", { agents: [
      agent("icons", { since: ago(10), last: ago(0), busy: true }), agent("tokens", { since: ago(5), last: ago(1) })] }),
      file("KEY3", "Kit")]);
    await env.clock.advance(400);
    check("agents at work in another file: its island names them",
          [/Sandbox 5\$/.test(env.html()), /icons, tokens/.test(env.html())], [true, true]);
    env.click("toggle", "doc:KEY2");
    env.click("stop", "doc:KEY2", { agent: "tokens" });
    check("…and each has its own Stop", env.sent("stop"), [{ type: "stop", doc: "doc:KEY2", agent: "tokens" }]);
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$", {
      agents: [agent("icons", { since: ago(10), last: ago(0) })],
      recent: [{ agent: "icons", at: ago(0), kind: "ok", summary: "Created Icons",
                 layers: [{ id: "1:2", name: "Icons", type: "FRAME" }] }] }), file("KEY3", "Kit")]);
    await env.clock.advance(400);
    check("its layers are names, not buttons: Figma can't switch the tab",
          [/Icons/.test(env.html()), /data-act="layer"/.test(env.html())], [true, false]);
    await env.clock.advance(11 * MIN);
    check("it stays while its plugin runs, quiet or not", /Sandbox/.test(env.html()), true);
    env.board([file("KEY1", "Dashboard"), file("KEY3", "Kit")]);
    await env.clock.advance(400);
    check("its plugin closes: its island goes", [/Sandbox/.test(env.html()), /Kit/.test(env.html())], [false, true]);
  }

  // ─── a file that stopped answering ──────────────────────────────────────
  {
    const env = load();
    const ago = (sec) => 1000 + env.clock.now() / 1000 - sec;
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$"),
               file("KEY3", "Kit", { stuck: { agent: "icons", since: ago(130) } })]);
    await env.clock.advance(400);
    const at = ["Dashboard", "Kit", "Sandbox 5$"].map((n) => env.html().indexOf(`title="${n}"`));
    check("a file where a script runs on past its time is red, first after this file",
          [at[0] < at[1] && at[1] < at[2], (env.html().match(/class="sub alarm">Figma isn't answering/g) || []).length],
          [true, 1]);
    env.click("toggle", "doc:KEY3");
    await env.clock.advance(400);
    check("…open, it says whose script it is, since when, and what to do",
          [/A script by icons is still running: its time ran out <span[^>]*>2m ago/.test(env.html()),
           /Reload the file in Figma: close its tab and open it again, or quit Figma if the tab won't close\. Then run Figaro in it\./.test(env.html())],
          [true, true]);
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$")],
              { lost: [file("KEY3", "Kit", { lost: { agent: "icons", at: ago(20) } })] });
    await env.clock.advance(400);
    check("its window closed while a script ran there: its island stays, red, and says so",
          [/title="Kit"/.test(env.html()), /Figaro's window there closed while a script by icons was running/.test(env.html())],
          [true, true]);
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Sandbox 5$"), file("KEY3", "Kit")]);
    await env.clock.advance(400);
    check("…and back, it is an island like the others", [/title="Kit"/.test(env.html()), /isn't answering/.test(env.html())],
          [true, false]);
    env.board([file("KEY1", "Dashboard", { stuck: { agent: "designer", since: ago(40) } })]);
    await env.clock.advance(400);
    env.click("toggle", "here");
    await env.clock.advance(400);
    check("this file's own script runs on past its time: its island says to reload this file",
          [/sub alarm|Figma isn't answering/.test(env.html()), /Reload this file in Figma/.test(env.html())], [true, true]);
    env.board([file("KEY1", "Dashboard")], { lost: [file("KEY1", "Dashboard", { lost: { agent: "x", at: ago(5) } })] });
    await env.clock.advance(400);
    check("…and a board that still calls this very file lost draws it once", (env.html().match(/title="Dashboard"/g) || []).length, 1);
  }

  // ─── updates and new builds ─────────────────────────────────────────────
  {
    const env = load();
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    const files = [file("KEY1", "Dashboard")];
    env.board(files, { update: { latest: "1.2.0", current: "1.1.0", url: RELEASE } });
    await env.clock.advance(400);
    check("a new release: the last line says the version, and the new one is a blue pill with ⤓ that updates",
          [/<span class="ver"><span class="ver-b">Figaro 1\.1\.0 <svg[^>]*>.*?<\/svg> <span class="tw"><button[^>]*class="go upv" data-act="update"[^>]*><span>1\.2\.0<\/span><svg[^>]*><path d="M5 1\.2v5\.5[^"]*h6\.4"/.test(env.foot()),
           /<span class="tip"[^>]*>Update to 1\.2\.0<\/span>/.test(env.foot()), env.pinned()],
          [true, true, true]);
    check("…and Skip after it, with its hint, until the next release",
          /data-act="update".*?<\/span><span class="tw"><button[^>]*class="skip" data-act="skip" data-version="1\.2\.0"[^>]*>Skip<\/button><span class="tip"[^>]*>Hide until the next release<\/span>/.test(env.foot()), true);
    env.click("update");
    check("Update asks the bridge", env.sent("update-now").length, 1);
    env.board(files, { update: { latest: "1.2.0" }, updating: { step: "wait", what: "update", to: "1.2.0" } });
    await env.clock.advance(400);
    check("…which waits for the scripts first, with Figaro's spinner, not an agent's grid",
          [/Updates once the scripts finish/.test(env.html()), /<div class="foot">.*class="spin"/.test(env.html()), /<div class="foot">.*class="px"/.test(env.html())],
          [true, true, false]);
    const DETAIL = "git: error: Your local changes to the following files would be overwritten by merge: bridge.py";
    env.board(files, { update: { latest: "1.2.0" }, failed: { what: "update", error: "local changes", detail: DETAIL, url: RELEASE } });
    await env.clock.advance(400);
    check("a failed update: a yellow ! where the download was, with a short hint, and Skip; nothing under the line",
          [/<span class="ver"><span class="ver-b">Figaro 1\.1\.0 .*?<span class="nv">1\.2\.0<\/span><\/span><span class="tw"><button[^>]*class="tl warn" data-act="why"[^>]*><span class="bdg caution"><svg[^>]*>.*?<\/svg><\/span><\/button><span class="tip"[^>]*>Couldn't update<\/span><\/span><span class="tw"><button[^>]*data-act="skip"/.test(env.foot()),
           /<\/div><\/div>$/.test(env.foot()), /local changes/.test(env.html())], [true, true, false]);
    env.click("why");
    await env.clock.advance(400);
    check("…which opens the settings at a yellow alert: what failed with Update in its corner, why in plain words, and Copy after them",
          [/<span class="ind"><span class="bdg caution"><svg[^>]*>.*<div class="alert"><div class="al-h"><b>Couldn't update to 1\.2\.0<\/b><button[^>]*class="go" data-act="update"[^>]*>Update<\/button><\/div><p>The Figaro folder has changes of its own, so it can't move to the new release\. <span class="tw"><button[^>]*class="tl cp" data-act="copy"[^>]*>.*?<span class="tip"[^>]*>Copy the error for your agent<\/span><\/span><\/p><\/div>/.test(env.app()),
           (env.app().match(/data-act="update"/g) || []).length], [true, 1]);
    env.click("copy");
    await env.clock.advance(50);
    check("Copy gives an agent the error, git's words, the versions and where to look",
          env.copied(), ["Figaro couldn't update to 1.2.0 in Figma: local changes.\n" + DETAIL + "\n"
            + "Figaro 1.1.0 on localhost:8788, plugin build test.\nExplain what happened and fix it. `figaro doctor` shows Figaro's state."]);
    check("…and its button says it did, for a moment",
          [/class="tl cp done" data-act="copy"[^>]*>.*?<span class="tip"[^>]*>Copied<\/span>/.test(env.app()), await env.clock.advance(2000), /Copied/.test(env.app())],
          [true, undefined, false]);
    env.click("update");
    check("Update in the alert asks the bridge again", env.sent("update-now").length, 2);
    env.click("back");
    await env.clock.advance(400);
    env.board(files, { update: { latest: "1.2.0" }, failed: { what: "update", error: "local changes", to: "1.2.0", url: RELEASE } });
    await env.clock.advance(400);
    env.click("skip", null, { version: "1.2.0" });
    check("Skip takes the ! with it, though the board still has the failure", [/data-act="why"/.test(env.foot()), /<span class="ver">Figaro 1\.1\.0<\/span>/.test(env.foot())], [false, true]);
    env.board(files, { failed: { what: "update", error: "local changes", to: "1.2.0", url: RELEASE }, skipped: "1.2.0" });
    await env.clock.advance(400);
    env.click("gear");
    check("…and from the settings: the version alone", [/class="alert"|class="bdg caution"/.test(env.app()), /data-act="why"/.test(env.foot())], [false, false]);
    env.click("gear");
    env.board(files, { update: { latest: "1.2.1" }, failed: { what: "update", error: "local changes", to: "1.2.0", url: RELEASE }, skipped: "1.2.0" });
    await env.clock.advance(400);
    check("a newer release: Update again, not the old failure", [/data-act="update"/.test(env.foot()), /data-act="why"/.test(env.foot()), /Update to 1\.2\.1/.test(env.foot())], [true, false, true]);

    env.ws().receive({ type: "outdated", running: "2026-10-08.6", expected: "2026-10-08.7" });
    env.board(files);
    await env.clock.advance(400);
    check("newer plugin code on disk: Reload with a circling arrow, on the last line, pinned, and no Skip",
          [/<div class="line">.*New build<\/span><span class="tw"><button[^>]*class="tl build" data-act="reload"[^>]*><svg[^>]*><path d="M8\.6 5\.9A/.test(env.html()), env.pinned(), /data-act="skip"/.test(env.html())],
          [true, true, false]);
    env.click("reload", "here");
    check("Reload asks the bridge", env.sent("reload-all").length, 1);
    env.board(files, { failed: { what: "reload", error: "files stayed busy" } });
    await env.clock.advance(400);
    check("a failed reload: the yellow ! in Reload's place, its hint, nothing under the line",
          [/<span class="ver">Figaro 1\.1\.0<span class="tw"><button[^>]*class="tl warn" data-act="why"[^>]*>.*?<span class="tip"[^>]*>Couldn't reload<\/span><\/span><\/span>/.test(env.foot()),
           /data-act="reload"|files stayed busy/.test(env.foot())], [true, false]);
    env.click("why");
    await env.clock.advance(400);
    check("…and in the settings the alert says why and has Reload again",
          /<b>Couldn't reload<\/b><button[^>]*class="go build" data-act="reload"[^>]*>Reload<\/button><\/div><p>Agents kept running scripts for 10 minutes\. Try again once they're done\. <span class="tw"><button[^>]*data-act="copy"/.test(env.app()), true);
    env.clip.deny = true;
    env.click("copy");
    await env.clock.advance(50);
    check("a clipboard Figma keeps shut: the copy button says so",
          [env.copied(), /class="tl cp" data-act="copy"[^>]*>.*?<span class="tip"[^>]*>Couldn't copy: Figma didn't let it<\/span>/.test(env.app())], [[], true]);
  }

  // ─── Skip: a release goes away until the next one ───────────────────────
  {
    const env = load();
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    const files = [file("KEY1", "Dashboard")];
    env.board(files, { update: { latest: "1.2.0", current: "1.1.0", url: RELEASE } });
    await env.clock.advance(400);
    env.click("skip", null, { version: "1.2.0" });
    check("Skip tells the bridge which release", env.sent("skip"), [{ type: "skip", version: "1.2.0" }]);
    const offered = () => /class="ver-b"|data-act="update"|data-act="release"/.test(env.foot());
    check("…and it leaves the line at once: the version alone", [offered(), /<span class="ver">Figaro 1\.1\.0<\/span>/.test(env.foot())], [false, true]);
    env.board(files, { update: { latest: "1.2.0", current: "1.1.0", url: RELEASE } });
    await env.clock.advance(400);
    check("…and stays away with a bridge that still offers it", offered(), false);
    env.board(files, { release: { latest: "1.2.0", url: RELEASE } });
    await env.clock.advance(400);
    check("…as a link too", offered(), false);
    env.ws().receive({ type: "outdated", running: "2026-10-08.6", expected: "2026-10-08.7" });
    env.board(files, { update: { latest: "1.2.0", url: RELEASE } });
    await env.clock.advance(400);
    check("newer plugin code on disk still shows, with Reload", [/New build/.test(env.html()), /data-act="update"/.test(env.html())], [true, false]);
  }
  {
    const env = load();
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    const files = [file("KEY1", "Dashboard")];
    env.board(files, { update: { latest: "1.2.0", url: RELEASE } });
    await env.clock.advance(400);
    env.click("skip", null, { version: "1.2.0" });
    env.board(files, { update: { latest: "1.2.1", url: RELEASE } });
    await env.clock.advance(400);
    check("a newer release than the skipped one brings the line back", /Update to 1\.2\.1/.test(env.html()), true);
  }
  {
    const env = load();
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.ws().receive({ type: "update", latest: "1.2.0", current: "1.1.0", url: RELEASE });
    env.board([file("KEY1", "Dashboard")], { skipped: "1.2.0" });
    await env.clock.advance(400);
    check("a release skipped in another window isn't offered here, though the bridge said update at hello",
          /class="ver-b"|data-act="update"/.test(env.foot()), false);
  }

  // ─── a bridge with the Update button off ────────────────────────────────
  {
    const env = load();
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    const files = [file("KEY1", "Dashboard")];
    env.board(files, { release: { latest: "1.2.0", current: "1.1.0", url: RELEASE } });
    await env.clock.advance(400);
    check("a new release: the last line names it, its number a link to it as in the settings, with no Update, and Skip after it",
          [/<span class="ver-b">Figaro 1\.1\.0 <svg[^>]*>.*?<\/svg> <span class="tw"><button type="button" class="lnk vlnk" data-act="release" data-url="[^"]*v1\.2\.0"><span>1\.2\.0<\/span><svg[^>]*>.*?<\/svg><\/button><span class="tip"[^>]*>Open the release\. <kbd>Update is off here<\/kbd><\/span><\/span><\/span><span class="tw"><button[^>]*data-act="skip"/.test(env.foot()),
           /data-act="update"/.test(env.html())], [true, false]);
    env.click("release", null, { url: RELEASE });
    check("…which opens in the browser and asks nothing of the bridge",
          [env.posted.filter((m) => m.type === "open-url").map((m) => m.url), env.sent("update-now").length], [[RELEASE], 0]);
    env.ws().receive({ type: "outdated", running: "2026-10-08.6", expected: "2026-10-08.7" });
    env.board(files, { release: { latest: "1.2.0", url: RELEASE } });
    await env.clock.advance(400);
    check("newer plugin code on disk comes first: Reload", [/data-act="reload"/.test(env.html()), /class="lnk vlnk"/.test(env.html())], [true, false]);
  }

  // ─── an older bridge, without boards ────────────────────────────────────
  {
    const env = load();
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.ws().receive({ type: "exec", id: "e1", code: "return 1", agent: "designer" });
    await env.clock.advance(400);
    check("its scripts still show in this file's island", /designer/.test(env.html()), true);
  }

  // ─── the grid's wave runs along the diagonal; the sounds are ours ─────────
  {
    const cell = {};
    for (const m of html.matchAll(/\.px i((?::nth-child\(\d\)(?:, \.px i)?)+) \{ animation-delay: ([\d.]+)ms/g)) {
      for (const n of m[1].matchAll(/nth-child\((\d)\)/g)) cell[n[1]] = Number(m[2]);
    }
    const want = {};
    for (let n = 1; n <= 9; n++) want[n] = ((Math.floor((n - 1) / 3) + (n - 1) % 3) / 4) * 0.5 * 900;
    check("the grid's delays follow the diagonal, 0 to 450 ms", cell, want);
    const back = Object.assign({}, cell);  // the rainbow's: the middle diagonal keeps its delay
    for (const m of html.matchAll(/((?:html\.rainbow \.px i:nth-child\(\d\)(?:, )?)+) \{ --c: [^;]+;(?: animation-delay: ([\d.]+)ms;)? \}/g)) {
      if (m[2] !== undefined) for (const n of m[1].matchAll(/nth-child\((\d)\)/g)) back[n[1]] = Number(m[2]);
    }
    const wantBack = {};
    for (let n = 1; n <= 9; n++) wantBack[n] = 450 - want[n];
    check("the rainbow's wave runs back, from the bottom right cell, 450 to 0 ms", back, wantBack);
    check("the grid's cycle is 0.9 s, on the beat of the 1.8 s shimmer",
          [/animation: px 900ms ease-in-out infinite/.test(html), /animation: shim 1\.8s /.test(html)], [true, true]);
    check("the grid's keyframes", /@keyframes px \{ 0%, 100% \{ opacity: \.18; \} 20% \{ opacity: 1; \} 50% \{ opacity: \.18; \} \}/.test(html), true);
    check("open and close sweep a sine between 440 and 660 Hz",
          [/open: \[\{ src: "sine", hz: \[440, 660\], env: \[0\.002, 0\.1, 0\.03\], gain: 0\.4 \}\]/.test(html),
           /close: \[\{ src: "sine", hz: \[660, 440\], env: \[0\.002, 0\.1, 0\.03\], gain: 0\.4 \}\]/.test(html)], [true, true]);
    check("a layer clicks twice, 14 ms apart",
          [/layer: \[\{ src: "noise", filter: \["highpass", 3500, 0\.7\], env: \[0\.0002, 0\.006\], gain: 0\.34 \},/.test(html),
           /\{ src: "noise", filter: \["bandpass", 1600, 3\], at: 0\.014, env: \[0\.0002, 0\.012\], gain: 0\.42 \}\]/.test(html)], [true, true]);
    check("the cues' volume", /const CUE_VOLUME = 0\.30;/.test(html), true);
  }

  // ─── at work: a pixel grid, and the word shimmers ───────────────────────
  {
    const env = load();
    const ago = (sec) => 1000 + env.clock.now() / 1000 - sec;
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard", { agents: [agent("designer", { since: ago(12), last: ago(1), busy: true })] })]);
    await env.clock.advance(400);
    const grids = () => (env.html().match(/<span class="px" aria-hidden="true">(<i><\/i>){9}<\/span>/g) || []).length;
    check("an agent at work: a 3x3 pixel grid by the file's name", [grids(), /class="spin"/.test(env.html())], [1, false]);
    env.click("toggle", "here");
    await env.clock.advance(400);
    check("…and open, one by the agent too, with the word shimmering and the clock steady",
          [grids(), /<span><span class="shim">working<\/span> <span[^>]*data-clock/.test(env.html())], [2, true]);
  }

  // ─── an open island with no agent at work ───────────────────────────────
  {
    const env = load();
    const ago = (sec) => 1000 + env.clock.now() / 1000 - sec;
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Kit")]);
    await env.clock.advance(400);
    env.click("toggle", "here");
    env.click("toggle", "doc:KEY2");
    const ghosts = () => (env.html().match(/class="agent empty"/g) || []).length;
    const sects = (name) => (env.html().match(new RegExp(`<div class="sect">${name}</div>`, "g")) || []).length;
    check("no agent, no change: Agents, and under it a ghost of an agent's row that says how to get one, in this file and the other",
          [ghosts(), sects("Agents"), sects("Recent"), (env.html().match(/No agents yet/g) || []).length,
           /a link to this file:/.test(env.html()), /a link to the file:/.test(env.html())],
          [2, 2, 0, 2, true, true]);
    env.board([file("KEY1", "Dashboard", { recent: [{ agent: "designer", at: ago(5), kind: "ok", summary: "Created KPI card" }] }),
               file("KEY2", "Kit", { agents: [agent("icons", { since: ago(30), last: ago(0), busy: true })] })]);
    await env.clock.advance(400);
    check("…an agent at work takes the ghost's place; changes with no agent at work leave Agents and say so",
          [ghosts(), sects("Agents"), sects("Recent"), /No agents at work/.test(env.html()), /No agents yet/.test(env.html()),
           /a link to this file:/.test(env.html()), /Created KPI card/.test(env.html())], [1, 2, 1, true, false, true, true]);
    env.board([file("KEY1", "Dashboard", { agents: [agent("designer", { since: ago(900), last: ago(700), done: { at: ago(600), text: "" } })],
                                           recent: [{ agent: "designer", at: ago(5), kind: "ok", summary: "Created KPI card" }] }), file("KEY2", "Kit")]);
    await env.clock.advance(400);
    check("…an agent that is done and still on show is a row of Agents, with no ghost beside it", [ghosts(), /No agents at work/.test(env.html())], [1, false]);
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Kit", { stuck: { agent: "icons", since: ago(300) } })]);
    await env.clock.advance(400);
    check("…a file where Figma isn't answering shows that first, with no ghost under it", [/isn't answering/.test(env.html()), ghosts()], [true, 1]);
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Kit")]);
    await env.clock.advance(400);
    env.ws().drop();
    await env.clock.advance(50);
    check("…nor shows while the bridge is away: the window says that instead",
          [ghosts(), /Looking for the bridge|Waiting for the bridge/.test(env.html())], [0, true]);
  }

  // ─── the settings ───────────────────────────────────────────────────────
  {
    const env = load();
    const ago = (sec) => 1000 + env.clock.now() / 1000 - sec;
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    const files = [file("KEY1", "Dashboard"), file("KEY2", "Kit", { agents: [agent("icons", { since: ago(30), last: ago(0), busy: true })] })];
    const settings = { sounds: true, others: true, versions: true };
    const notes = { version: "1.1.0", items: ["Sounds for what you press", "A pixel grid"] };
    env.board(files, { settings, notes, folder: "~/figaro", checked: 990 });
    await env.clock.advance(400);
    env.heard();
    env.click("gear");
    check("Settings: the switches, the version in an open island, no files, and Settings on in the last line",
          [/^<div class="head"><button type="button" class="back" data-act="back"[^>]*><svg[^>]*>.*?<\/svg><span>Back<\/span><\/button><\/div>/.test(env.app()),
           (env.app().match(/role="switch" aria-checked="true"/g) || []).length,
           /<div class="island open"><div class="row"><div class="tg">.*Figaro 1\.1\.0.*up to date/.test(env.app()),
           /data-fid/.test(env.app()), /class="tl on" data-act="gear"/.test(env.foot())], [true, 3, true, false, true]);
    check("…what is new in this version, and where Figaro runs",
          [/What's new in 1\.1\.0<\/div><ul class="notes"><li>Sounds for what you press<\/li>/.test(env.app()),
           /<b>Folder<\/b><span>~\/figaro<\/span>/.test(env.app()), /<b>Plugin<\/b><span>build test<\/span>/.test(env.app())], [true, true, true]);
    await env.clock.advance(50);
    check("…and a click opens them", env.heard(), ["square 1700", "sine 520"]);
    check("…no Reviewer row from a bridge that has no such setting", /<b>Reviewer<\/b>/.test(env.app()), false);
    check("…nor a switch for signing comments", /Sign agents' comments/.test(env.app()), false);
    env.board(files, { settings: Object.assign({ sign: true }, settings), notes, folder: "~/figaro", checked: 990 });
    await env.clock.advance(400);
    check("…and with one, the switch to sign agents' comments, on, with what it does",
          [/<b>Sign agents' comments<\/b><span>Agents post comments in Figma under your account\. This starts each one with \[AI agent\]<\/span>/.test(env.app()),
           /class="sw on" role="switch" aria-checked="true" aria-label="Sign agents' comments" data-act="set" data-name="sign"/.test(env.app())], [true, true]);
    env.click("set", null, { name: "sign" });
    check("…which tells the bridge", env.sent("set").slice(-1), [{ type: "set", name: "sign", on: false }]);
    env.ws().sent.length = 0;
    env.board(files, { settings: Object.assign({ reviewer: true }, settings), notes, folder: "~/figaro", checked: 990 });
    await env.clock.advance(400);
    check("…and with one, the row, its hint and a switch that is on",
          [/<b>Reviewer<\/b><span>A reviewer among each file's agents: it checks what agents change/.test(env.app()),
           /aria-label="Reviewer" data-act="set" data-name="reviewer"/.test(env.app()),
           /class="sw on" role="switch" aria-checked="true" aria-label="Reviewer"/.test(env.app())], [true, true, true]);
    env.board(files, { settings, notes, folder: "~/figaro", checked: 990 });
    await env.clock.advance(400);
    env.heard();
    env.click("set", null, { name: "others" });
    check("a switch tells the bridge, with a tick", [env.sent("set"), env.heard()], [[{ type: "set", name: "others", on: false }], ["noise", "noise"]]);
    env.click("set", null, { name: "sounds" });
    env.click("check");
    check("Sounds off: that switch and all after it are silent", env.heard(), []);
    check("↻ asks the bridge to look for a release, and turns meanwhile", [env.sent("check-update").length, /checking…/.test(env.app())], [1, true]);
    env.board(files, { settings: { sounds: false, others: false, versions: true }, checked: 1000 + env.clock.now() / 1000, sounds: false });
    await env.clock.advance(400);
    check("…until the board says it looked: up to date, just now", /up to date · <span[^>]*>just now/.test(env.app()), true);
    env.click("set", null, { name: "sounds" });
    check("Sounds on again: its switch ticks", env.heard(), ["noise", "noise"]);
    env.click("back");
    await env.clock.advance(400);
    check("back: the files, and with Show other files off, this one's island alone",
          [/data-fid="here"/.test(env.app()), /data-fid="doc:KEY2"/.test(env.app()), env.heard()], [true, false, ["square 1700", "sine 780"]]);
    env.board([files[0], file("KEY2", "Kit", { stuck: { agent: "icons", since: ago(300) } }), file("KEY3", "Landing", { agents: [agent("copy", { since: ago(20), last: ago(0), busy: true })] })],
      { settings: { sounds: true, others: false, versions: true } });
    await env.clock.advance(400);
    check("…but a file where Figma isn't answering shows all the same, red, and one where an agent works doesn't",
          [/data-fid="doc:KEY2"/.test(env.app()), /isn't answering/.test(env.app()), /data-fid="doc:KEY3"/.test(env.app())], [true, true, false]);
    env.board(files, { settings, update: { latest: "1.2.0", url: RELEASE }, skipped: "1.2.0" });
    env.click("gear");
    check("a skipped release still shows in the settings: the new version, with Update",
          [/class="bdg update".*Figaro 1\.1\.0 .*<span class="nv">1\.2\.0<\/span>.*data-act="update"[^>]*>Update<\/button>/.test(env.app()),
           /Skip this release/.test(env.app()), /Report a problem/.test(env.app())], [true, false, true]);
    env.board(files, { settings, release: { latest: "1.2.0", url: RELEASE } });
    await env.clock.advance(400);
    check("…and with the Update button off, the release and how to update by hand",
          [/data-act="release" data-url="[^"]*v1\.2\.0">Release/.test(env.app()), /FIGARO_UPDATE_BUTTON=off/.test(env.app()),
           /data-act="skip" data-version="1\.2\.0"><span>Skip this release/.test(env.app())], [true, true, true]);
    env.board(files, { settings, skipped: "1.2.0", newest: { latest: "1.2.0", url: RELEASE, button: true } });
    await env.clock.advance(400);
    check("a bridge that names the newest release: a skipped one shows here, and only here",
          [/<span class="nv">1\.2\.0<\/span>.*data-act="update"/.test(env.app()), /Skip this release/.test(env.app()), /class="ver-b"/.test(env.foot())], [true, false, false]);
    env.board(files, { settings, folder: "~/figaro", update: { latest: "1.2.0", url: RELEASE },
                       failed: { what: "update", error: "offline", detail: "git: fatal: unable to access the repository" } });
    await env.clock.advance(400);
    env.click("copy");
    await env.clock.advance(50);
    check("the copied error names the folder when the bridge says it",
          env.copied(), ["Figaro couldn't update to 1.2.0 in Figma: offline.\ngit: fatal: unable to access the repository\n"
            + "Figaro 1.1.0 on localhost:8788, plugin build test, folder ~/figaro.\nExplain what happened and fix it. `figaro doctor` shows Figaro's state."]);
    env.click("gear");
    check("Settings again: back to the files", /data-fid="here"/.test(env.app()), true);
  }
  {
    const env = load();
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard", { recent: [{ agent: "designer", at: 1000 - 120, kind: "ok", summary: "Created KPI card" }] })]);
    await env.clock.advance(400);
    env.click("toggle", "here");
    check("a bridge too old for settings: Recent stays on show", /Created KPI card/.test(env.html()), true);
    env.click("gear");
    check("…the settings show, but can't change, and none is for recent changes",
          [(env.app().match(/role="switch"[^>]*disabled/g) || []).length, /Restart the bridge to change them/.test(env.app()), /Show recent changes/.test(env.app())], [3, true, false]);
    env.click("set", null, { name: "sounds" });
    check("…and say nothing to it", env.sent("set"), []);
  }

  // ─── Show recent changes ────────────────────────────────────────────────
  {
    const env = load();
    const ago = (sec) => 1000 + env.clock.now() / 1000 - sec;
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    const change = { agent: "designer", at: ago(120), kind: "ok", summary: "Created KPI card" };
    const files = [file("KEY1", "Dashboard", { agents: [agent("designer", { since: ago(900), last: ago(700), done: { at: ago(600), text: "" } })], recent: [change] }),
                   file("KEY2", "Kit", { recent: [change] })];
    const settings = { sounds: true, others: true, recent: true, versions: true };
    env.board(files, { settings });
    await env.clock.advance(400);
    env.click("toggle", "here");
    env.click("toggle", "doc:KEY2");
    const recent = () => (env.html().match(/<div class="sect">Recent<\/div>/g) || []).length;
    check("Recent on: each open island lists what agents changed, under its agents", [recent(), /Created KPI card/.test(env.html())], [2, true]);
    env.click("gear");
    check("Settings: Show recent changes is a switch, after Show other files, on",
          [(env.app().match(/role="switch"/g) || []).length,
           /<b>Show other files<\/b>.*?<b>Show recent changes<\/b><span>What agents changed lately, under each file's agents\. A layer there selects it in Figma<\/span>.*?<b>Save versions as agents work<\/b>/.test(env.app()),
           /aria-checked="true" aria-label="Show recent changes"/.test(env.app())], [4, true, true]);
    env.click("set", null, { name: "recent" });
    check("a switch tells the bridge", env.sent("set"), [{ type: "set", name: "recent", on: false }]);
    env.board(files, { settings: Object.assign({}, settings, { recent: false }) });
    await env.clock.advance(400);
    env.click("back");
    await env.clock.advance(400);
    check("Recent off: the islands list their agents only, in every file",
          [recent(), /Created KPI card/.test(env.html()), /<div class="sect">Agents<\/div>/.test(env.html()), /data-fid="doc:KEY2".*No agents at work/.test(env.html())],
          [0, false, true, true]);
    env.click("toggle", "doc:KEY2");
    await env.clock.advance(400);
    check("…and a folded island keeps its line", /<span class="sub">designer · /.test(env.html()), true);
    env.click("toggle", "doc:KEY2");
    await env.clock.advance(400);
    env.board(files, { settings });
    await env.clock.advance(400);
    check("…and on again, the lists are back", recent(), 2);
    env.board(files, { settings: { sounds: true, others: true, versions: true } });
    await env.clock.advance(400);
    check("a bridge before the switch: the lists stay, as they were", recent(), 2);
    env.click("gear");
    check("…and the settings have no such switch", [(env.app().match(/role="switch"/g) || []).length, /Show recent changes/.test(env.app())], [3, false]);
  }

  // ─── the Figma token ────────────────────────────────────────────────────
  {
    const env = load();
    const ago = (sec) => 1000 + env.clock.now() / 1000 - sec;
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    const files = [file("KEY1", "Dashboard"), file("KEY2", "Kit")];
    const settings = { sounds: true, others: true, recent: true, versions: true };
    const figma = (extra) => Object.assign({ token: null, check: null, why: null }, extra);
    const field = () => /<input class="inp([^"]*)"[^>]*value="([^"]*)"/.exec(env.app());
    const save = () => /<button type="button" class="go" data-act="token-save"( disabled)?>/.exec(env.app());
    env.board(files, { settings, figma: figma() });
    await env.clock.advance(400);
    env.click("gear");
    check("the token block sits between the switches and the version, Optional by its title, and is open",
          [/<b>Save versions as agents work<\/b>.*<b>Figma token<span class="opt">Optional<\/span><\/b>.*class="fld".*Figaro 1\.1\.0/.test(env.app()),
           /Lets agents read and answer comments in Figma and see who changed a layer and when/.test(env.app()),
           /Figaro keeps it on this computer only\./.test(env.app())],
          [true, true, true]);
    check("…its help says where to make the token, with a link to Figma's",
          [/In Figma: account menu → <b>Settings<\/b> → <b>Security<\/b> → <b>Generate new token<\/b>\. Let it read and write comments, and read file versions\./.test(env.app()),
           /data-act="release" data-url="https:\/\/help\.figma\.com[^"]*"><span>Figma help<\/span>/.test(env.app())], [true, true]);
    check("empty: a figd_… field, Save disabled and no Add or Cancel",
          [field()[1], field()[2], /placeholder="figd_…"/.test(env.app()), save()[1], /Add|Cancel/.test(env.app().split("Figma token")[1].split("Figaro 1")[0])],
          ["", "", true, " disabled", false]);
    env.click("token-save");
    check("…and a click on the disabled Save sends nothing", env.sent("figma-token"), []);
    let tick = 0;
    const redraw = async (extra) => {  // the settings drawn anew: the version line's "checked" changes
      env.board(files, Object.assign({ settings, figma: figma(), checked: 900 + 10 * ++tick }, extra));
      await env.clock.advance(400);
    };
    env.type("figd_abc");
    await redraw();
    check("typed: the field keeps it through a redraw, and Save is on", [field()[2], save()[1]], ["figd_abc", undefined]);
    env.type("   ");
    await redraw();
    check("only blanks: Save is off again", save()[1], " disabled");
    env.type(" figd_abc ");
    env.click("token-save");
    check("Save sends the token and this file, and the field says Checking at once, with its spinner",
          [env.sent("figma-token"), /<button type="button" class="go" data-act="token-save" disabled><span class="spin"><\/span>Checking<\/button>/.test(env.app()),
           /<input class="inp dim"[^>]*disabled/.test(env.app())],
          [[{ type: "figma-token", token: "figd_abc", doc: "doc:KEY1" }], true, true]);
    env.click("token-save");
    check("…and a second press while it checks sends nothing", env.sent("figma-token").length, 1);
    env.board(files, { settings, figma: figma({ check: "checking" }) });
    await env.clock.advance(400);
    check("the bridge says it is checking: the same", /Checking<\/button>/.test(env.app()), true);
    env.board(files, { settings, figma: figma({ check: "error", why: "rejected" }) });
    await env.clock.advance(400);
    check("rejected: a red field that keeps what was typed, Save active, and Figma's words",
          [/ err/.test(field()[1]), field()[2], save()[1], /<p class="help err">Figma didn't accept this token\. Check that you copied all of it and that it can read comments and file versions\.<\/p>/.test(env.app())],
          [true, " figd_abc ", undefined, true]);
    check("…with the help and the promise still under it", [/In Figma: account menu/.test(env.app()), /Figaro keeps it on this computer only/.test(env.app())], [true, true]);
    for (const [why, text] of [["network", "Figma didn't answer"], ["nofile", "doesn't show this file"], ["open", "Open a Figma file where Figaro runs"], ["busy", "busy right now"]]) {
      env.board(files, { settings, figma: figma({ check: "error", why }) });
      await env.clock.advance(400);
      check(`…${why}: its own words`, new RegExp(`<p class="help err">[^<]*${text}`).test(env.app()), true);
    }
    env.board(files, { settings, figma: figma({ token: "figd_…a1c4" }) });
    await env.clock.advance(400);
    check("connected: the masked token, what agents can do, and Remove; no field, no help",
          [/<span class="okc">Connected<\/span> · figd_…a1c4\. Agents can read and answer comments, and read the version history of the files they work in/.test(env.app()),
           /<button type="button" class="go2" data-act="token-remove">Remove<\/button>/.test(env.app()), /class="inp/.test(env.app()), /In Figma: account menu/.test(env.app())],
          [true, true, false, false]);
    env.click("token-remove");
    check("Remove tells the bridge, and the field is back, empty", [env.sent("figma-token-remove"), field()[2], save()[1]],
          [[{ type: "figma-token-remove" }], "", " disabled"]);
    await redraw();
    env.type("figd_x");
    await redraw();
    check("what is typed survives the settings being drawn anew", field()[2], "figd_x");
    env.type("figd_y");
    env.enter();
    check("Enter in the field saves too", env.sent("figma-token").slice(-1), [{ type: "figma-token", token: "figd_y", doc: "doc:KEY1" }]);
    env.click("back");
  }
  {
    // a bridge before the token: no `figma` on its board, no block and no ask
    const env = load();
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    const files = [file("KEY1", "Dashboard", { ask: { agent: "designer" } })];
    env.board(files, { settings: { sounds: true, others: true, recent: true, versions: true } });
    await env.clock.advance(400);
    env.click("toggle", "here");
    check("an older bridge: no ask on the island", /wants to read comments/.test(env.html()), false);
    env.click("gear");
    check("…and no token block in the settings", [/Figma token/.test(env.app()), /class="inp/.test(env.app())], [false, false]);
  }
  {
    // an agent wants comments, and there is no token: the island asks
    const env = load();
    const ago = (sec) => 1000 + env.clock.now() / 1000 - sec;
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    const settings = { sounds: true, others: true, recent: true, versions: true };
    const asking = (n) => [file("KEY1", "Dashboard", { agents: [agent("designer", { since: ago(40), last: ago(0), busy: true })], ask: { agent: "designer" } }),
                           file("KEY2", "Kit", n ? { ask: { agent: "writer" } } : {})];
    env.board(asking(false), { settings, figma: { token: null, check: null, why: null } });
    await env.clock.advance(400);
    check("a folded island says what the agent wants", /<span class="sub">designer wants to read comments<\/span>/.test(env.html()), true);
    env.click("toggle", "here");
    await env.clock.advance(400);
    check("opened, it has the ask: the agent, why, and two buttons, above the agents",
          [/<div class="callout ask"><div class="h">.*designer wants to read comments<\/div>/.test(env.html()),
           /<p>Figma shows comments only to an app with your token\. Add one in the settings, and your agent can go on\.<\/p>/.test(env.html()),
           /<button type="button" class="go2" data-act="ask-hide">Not now<\/button><button type="button" class="go" data-act="ask-add">Add a Figma token<\/button>/.test(env.html()),
           env.html().indexOf("callout ask") < env.html().indexOf('<div class="sect">Agents</div>')], [true, true, true, true]);
    env.click("ask-add", "here");
    await env.clock.advance(1000);
    check("Add a Figma token opens the settings, at the token block", [/<b>Figma token<span/.test(env.app()), /class="tl on" data-act="gear"/.test(env.foot())], [true, true]);
    env.click("back");
    await env.clock.advance(400);
    env.click("ask-hide", "here");
    check("Not now tells the bridge which file, and the ask is gone at once",
          [env.sent("ask-hide"), /wants to read comments/.test(env.html()), /data-agent="designer"/.test(env.html())],
          [[{ type: "ask-hide", doc: "doc:KEY1" }], false, true]);
    env.board(asking(true), { settings, figma: { token: null, check: null, why: null } });
    await env.clock.advance(400);
    check("another file's ask is its own: it shows on that island", /data-fid="doc:KEY2".*writer wants to read comments/.test(env.html()), true);
    env.board([asking(true)[0], file("KEY2", "Kit", { ask: { agent: "writer", what: "write" } })], { settings, figma: { token: null, check: null, why: null } });
    await env.clock.advance(400);
    check("an agent that wanted to post says so", [/writer wants to post comments/.test(env.html()), /writer wants to read comments/.test(env.html())], [true, false]);
    env.board(asking(true), { settings, figma: { token: "figd_…a1c4", check: null, why: null } });
    await env.clock.advance(400);
    check("once there is a token, no ask shows", /wants to read comments/.test(env.html()), false);
    // a saved token that Figma turned down: the ask is back, and the settings say so
    env.board(asking(true), { settings, figma: { token: "figd_…a1c4", check: null, why: null, rejected: true } });
    await env.clock.advance(400);
    check("a refused saved token asks again, on each island that has an ask", [/designer wants to read comments/.test(env.html()), /writer wants to read comments/.test(env.html())], [true, true]);
    env.click("ask-add", "here");
    await env.clock.advance(1000);
    check("…its button leads to the settings, where the token shows as refused, with a field and Remove",
          [/<b>Figma token<span/.test(env.app()), /<p class="help err">Figma no longer accepts the saved token, figd_…a1c4\. Paste a new one, or remove it\.<\/p>/.test(env.app()),
           /class="inp err"/.test(env.app()), /data-act="token-remove"/.test(env.app()), /<span class="okc">Connected/.test(env.app())], [true, true, true, true, false]);
    env.type("figd_new");
    env.board(asking(true), { settings, figma: { token: "figd_…a1c4", check: null, why: null, rejected: true }, checked: 9000 });
    await env.clock.advance(400);
    env.click("token-save");
    check("…and a new token can be saved over it", env.sent("figma-token").slice(-1), [{ type: "figma-token", token: "figd_new", doc: "doc:KEY1" }]);
    env.click("back");
  }

  // ─── minimized ──────────────────────────────────────────────────────────
  {
    const env = load();
    const ago = (sec) => 1000 + env.clock.now() / 1000 - sec;
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    const busy = (n) => agent(n, { since: ago(30), last: ago(0), busy: true });
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Kit", { agents: [busy("icons")] }), file("KEY3", "Sandbox", { agents: [busy("tokens")] })]);
    await env.clock.advance(400);
    env.heard();
    env.click("min");
    await env.clock.advance(50);
    check("Minimize, by a key: mini — this file's card, the other files at work, restore, and no last line",
          [/<div class="mini enter">/.test(env.app()), /<b>Dashboard<\/b>/.test(env.app()), /<span class="mc"[^>]*>\+2<\/span>/.test(env.app()),
           /data-act="restore"/.test(env.app()), env.foot()], [true, true, true, true, ""]);
    check("…the sandbox keeps it, and it swooshes down", [env.posted.filter((m) => m.type === "view").pop(), env.heard()],
          [{ type: "view", view: "mini" }, ["noise", "sine 560"]]);
    await env.clock.advance(400);
    check("…springing in once", /<div class="mini">/.test(env.app()), true);
    env.board([file("KEY1", "Dashboard"), file("KEY2", "Kit", { agents: [busy("icons")], stuck: { agent: "icons", since: ago(40) } })]);
    await env.clock.advance(400);
    check("another file stops answering: this file's card stays, and a red ! by it takes the count's place",
          [/<b>Dashboard<\/b>/.test(env.app()), /class="island alarm"/.test(env.app()),
           /<span class="mc alarm" title="Figma isn't answering in Kit"><span class="ind"><span class="bdg alarm">/.test(env.app()), /\+\d/.test(env.app())],
          [true, false, true, false]);
    env.tapGrid("mini");  // the card's grid is its restore, not the rainbow
    await env.clock.advance(50);
    check("restore, from the card's grid: the files, and the swoosh up, and no rainbow",
          [/data-fid="here"/.test(env.app()), env.heard(), env.rainbow()], [true, ["noise", "sine 280"], false]);
    await env.clock.advance(400);

    const hold = env.press();
    await env.clock.advance(200);
    check("held on Minimize: a ring runs round it, a sound rises", [hold.holding(), env.heard()], [true, ["sine 180", "triangle 360"]]);
    await env.clock.advance(300);
    check("…and half a second on: nano, this file's state, not the alarm in another one",
          [/<div class="mini nano enter">/.test(env.app()), /alarm|class="bdg alarm"/.test(env.app()), /<b>/.test(env.app()), hold.holding(), env.heard()],
          [true, false, false, false, ["sine 1100", "noise", "sine 1320"]]);
    hold.up(true, "restore");
    check("…and the click as it's let go doesn't restore it, though restore is under the pointer by then", /mini nano/.test(env.app()), true);
    await env.clock.advance(400);
    env.click("restore");
    await env.clock.advance(400);

    const quick = env.press();
    await env.clock.advance(100);
    quick.up(true, "restore");
    check("a quick click on Minimize: mini", [/<div class="mini enter">/.test(env.app()), env.heard().slice(-2)], [true, ["noise", "sine 560"]]);
    env.click("restore");
    check("…and a second click right after, now on restore, leaves it so", /class="mini/.test(env.app()), true);
    await env.clock.advance(400);
    env.click("restore");
    await env.clock.advance(400);
    const off = env.press();
    await env.clock.advance(100);
    off.up(false);
    await env.clock.advance(600);
    check("pressed, then let go off the button: nothing", [/data-fid="here"/.test(env.app()), off.holding()], [true, false]);
    env.click("min", null, null, 1);
    check("a pointer's click on Minimize is the press's to handle, not the click's", /data-fid="here"/.test(env.app()), true);
    env.board([file("KEY1", "Dashboard", { agents: [busy("icons")], stuck: { agent: "icons", since: ago(40) } }), file("KEY2", "Kit")]);
    await env.clock.advance(400);
    env.click("min");
    await env.clock.advance(400);
    check("Figma stops answering in this file: mini, its card in red", [/class="island alarm"/.test(env.app()), /<b>Dashboard<\/b>/.test(env.app())], [true, true]);
    env.click("restore");
    await env.clock.advance(400);
    const held = env.press();
    await env.clock.advance(600);
    held.up(true, "restore");
    await env.clock.advance(400);
    check("…and nano, the alarm", [/mini nano/.test(env.app()), /class="island alarm"/.test(env.app())], [true, true]);
  }
  {
    const env = load();
    env.identity("Dashboard", "KEY1", "nano");
    env.ws().open();
    env.board([file("KEY1", "Dashboard")]);
    await env.clock.advance(400);
    check("minimized in this file before: the window opens so, without springing in", /<div class="mini nano">/.test(env.app()), true);
    env.identity("Dashboard", "KEY1", "files");
    await env.clock.advance(400);
    check("…and a later identity leaves it", /mini nano/.test(env.app()), true);
  }
  {
    const env = load();
    env.identity("Dashboard", "KEY1", null);  // the sandbox says who it is before it has read the view
    env.ws().open();
    env.board([file("KEY1", "Dashboard")]);
    await env.clock.advance(400);
    check("no view read yet: the files", /data-fid="here"/.test(env.app()), true);
    env.identity("Dashboard", "KEY1", "mini");
    await env.clock.advance(400);
    check("…then the view it read: minimized", [/<div class="mini[ "]/.test(env.app()), /nano/.test(env.app())], [true, false]);
  }

  // ─── sounds ─────────────────────────────────────────────────────────────
  {
    const env = load();
    const ago = (sec) => 1000 + env.clock.now() / 1000 - sec;
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    const designer = (extra) => agent("designer", Object.assign({ since: ago(60), last: ago(1), busy: true }, extra));
    const icons = agent("icons", { since: ago(30), last: ago(0), busy: true });
    const kit = (agents) => file("KEY2", "Kit", { agents });
    env.board([file("KEY1", "Dashboard", { agents: [designer()] }), kit([])]);
    await env.clock.advance(400);
    check("no sound for what the first board finds", env.heard(), []);
    check("…and no AudioContext for the sounds yet", env.cues(), "none");
    await env.clock.advance(200);
    check("…but a moment later there is one, asleep, so the first click needn't wait for it", env.cues(), "suspended");
    env.click("toggle", "here");
    await env.clock.advance(50);
    check("opening an island: a blip up", env.heard(), ["sine 440"]);
    env.click("toggle", "here");
    await env.clock.advance(50);
    check("…closing it: the same blip down", env.heard(), ["sine 660"]);
    const failing = (at) => agent("icons", { since: ago(30), last: ago(0), busy: true, error: { text: "boom", line: 3, at } });
    env.board([file("KEY1", "Dashboard", { agents: [designer(), icons] }), kit([])]);
    env.board([file("KEY1", "Dashboard", { agents: [designer({ busy: false, done: { text: "Have a look", at: ago(0) } }), failing(ago(0))] }), kit([])]);
    await env.clock.advance(50);
    check("an agent begins, is done, a script fails: quiet", env.heard(), []);
    env.board([file("KEY1", "Dashboard", { agents: [failing(ago(20))], stuck: { agent: "icons", since: ago(40) } }), kit([])]);
    await env.clock.advance(50);
    check("Figma stops answering in this file: two low knocks", env.heard(), ["square 220", "square 220"]);
    env.board([file("KEY1", "Dashboard", { agents: [failing(ago(20))], stuck: { agent: "icons", since: ago(40) } }),
               kit([]), file("KEY3", "Sandbox", { stuck: { agent: "tokens", since: ago(40) } })]);
    await env.clock.advance(50);
    check("…once, and another file's is that window's to play", env.heard(), []);
    env.click("stop", "here", { agent: "icons" });
    env.click("update");
    env.click("layer", null, { id: "1:2" });
    env.click("skip", null, { version: "1.2.0" });
    await env.clock.advance(50);
    check("Stop thuds down, Update starts up in three pulses, a layer ticks, Skip flies off", env.heard(),
          ["noise", "sine 340", "sine 330", "sine 415", "sine 523", "noise", "noise", "noise"]);
    await env.clock.advance(4000);
    check("the sounds' AudioContext sleeps a moment after the last", env.tone() === "none" && env.cues(), "suspended");

    // FIGARO_SOUNDS=off: the board says so, and nothing plays, the alarm neither
    env.board([file("KEY1", "Dashboard", { agents: [icons] }), kit([])], { sounds: false });
    env.click("toggle", "here");
    env.click("stop", "here", { agent: "icons" });
    env.click("update");
    env.board([file("KEY1", "Dashboard", { agents: [icons], stuck: { agent: "icons", since: ago(40) } }), kit([])],
              { sounds: false });
    await env.clock.advance(50);
    check("with the sounds turned off, nothing plays", env.heard(), []);
    env.board([file("KEY1", "Dashboard", { agents: [icons] }), kit([])], { sounds: true });
    env.click("toggle", "here");
    await env.clock.advance(50);
    check("…and turned on again, they do", env.heard(), ["sine 660"]);
  }
  {
    // A system slow to wake the sounds' AudioContext (Windows: up to a few hundred ms):
    // the pointer in the window wakes it before the click, so the click's cue plays at once.
    const env = load();
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard")]);
    await env.clock.advance(1000);
    check("asleep before the pointer comes", env.cues(), "suspended");
    env.audio.lag = 200;
    env.move();
    await env.clock.advance(250);
    check("the pointer moves in the window: the cues' AudioContext wakes", env.cues(), "running");
    env.click("toggle", "here");
    await flush();
    check("…and the click's cue plays at once", env.heard(), ["sine 440"]);
    await env.clock.advance(2000);
    env.move();
    await env.clock.advance(2000);
    check("…kept awake while the pointer moves", env.cues(), "running");
    await env.clock.advance(1100);
    check("…and asleep a moment after it goes still", env.cues(), "suspended");
    env.down();
    await env.clock.advance(250);
    check("a press wakes it too", env.cues(), "running");
    await env.clock.advance(4000);
    env.click("toggle", "here");
    await env.clock.advance(300);
    check("a cue that waits for the wake longer than CUE_LATE_MS is dropped, not played with the opening", env.heard(), []);
    await env.clock.advance(4000);
    check("…and the context it woke sleeps again", env.cues(), "suspended");
    env.audio.lag = 80;
    await env.clock.advance(4000);
    env.click("toggle", "here");
    await env.clock.advance(100);
    check("…one that waits less still plays", env.heard(), ["sine 440"]);
    env.board([file("KEY1", "Dashboard")], { sounds: false });
    await env.clock.advance(4000);
    env.move();
    await env.clock.advance(250);
    check("with the sounds off, the pointer wakes nothing", env.cues(), "suspended");
  }
  {
    const env = load();
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    env.board([file("KEY1", "Dashboard")], { sounds: false });
    await env.clock.advance(2000);
    check("with the sounds off from the start, no AudioContext is made for them", env.cues(), "none");
  }

  // ─── the reviewer: a row among the agents, its notes for the designer ───
  {
    const rv = (extra) => Object.assign({ on: true, state: "waiting", agent: "designer", layers: 0, since: null, at: 1000,
                                           notes: [], expired: false, sent: 0, left: 0 }, extra);
    const notes = [
      { id: "n1", rule: "unbound-color", title: "Colour isn't a token", text: "#2F6BFF, bg/brand matches",
        layers: [{ id: "I2:1068", name: "Get started", type: "INSTANCE" }], more: 0 },
      { id: "n2", rule: "text-style", title: "Text has no style", text: "Heading/H2 matches",
        layers: [{ id: "12:44", name: "Pricing", type: "TEXT" }, { id: "12:45", name: "Plan", type: "TEXT" }], more: 2 },
      { id: "n3", rule: "default-name", title: "Default name", text: "Frame 12, rename it",
        layers: [{ id: "12:50", name: "Frame 12", type: "FRAME" }], more: 0 },
    ];
    const make = async () => {
      const env = load();
      env.identity("Dashboard", "KEY1");
      env.ws().open();
      return env;
    };
    const show = async (env, reviewer, extra, settings) => {
      env.board([file("KEY1", "Dashboard", Object.assign({
        agents: [agent("designer", { since: 990, last: 990, done: { text: "ready", at: 995 } })] }, reviewer === undefined ? {} : { reviewer }, extra))],
        settings ? { settings } : {});
      await env.clock.advance(400);
    };
    const env = await make();
    env.board([file("KEY1", "Dashboard", { agents: [agent("designer", { since: 990, last: 990 })] })]);
    env.click("toggle", "here");
    await env.clock.advance(400);
    check("a bridge that sends no reviewer: no row, no switch", [/reviewer/.test(env.html()), /rv-toggle/.test(env.html())], [false, false]);

    await show(env, rv({ on: false, state: "off" }), {}, { reviewer: false });
    check("the settings' Reviewer off: no row though the file has one", /rv-toggle|reviewer/.test(env.app()), false);

    await show(env, rv({ on: false, state: "off" }), {}, { reviewer: true });
    check("off: a ghost avatar, the name, its hint, a switch that is off, and its own status",
          [/class="av ghost rvi"/.test(env.app()), /<span>reviewer<\/span><span class="qm"/.test(env.app()),
           /class="sw" role="switch" aria-checked="false" aria-label="Reviewer" data-act="rv-toggle" data-on="1"/.test(env.app()),
           /<span class="st">off<\/span>/.test(env.app())], [true, true, true, true]);
    check("…the switch sits in a slot that opens on hover, like Stop; the pointer on the row opens it",
          [/<span class="act swa"><button type="button" class="sw"/.test(env.app()), /class="agent rv" data-agent="reviewer"/.test(env.app())], [true, true]);
    env.point("here", "reviewer");
    await env.clock.advance(400);
    await show(env, rv({ on: false, state: "off" }), {}, { reviewer: true });
    check("…and keeps it through a redraw", /class="agent rv hot" data-agent="reviewer"/.test(env.app()), true);
    const sw = (env.app().match(/<button[^>]*data-act="rv-toggle"[^>]*>/) || [""])[0];
    check("…the keyboard reaches the switch: it is focusable, and focus inside the row opens its slot",
          [/tabindex="-1"/.test(sw), /disabled/.test(sw), /\.agent\.rv:focus-within \.act\.swa \{[^}]*width: 27px/.test(html),
           /\.agent\.rv:focus-within \.act\.swa \.sw \{[^}]*opacity: 1/.test(html),
           /\.agent\.rv \.act\.swa \.sw \{[^}]*(display: none|visibility: hidden)/.test(html)], [false, false, true, true, false]);
    check("…and the notes callout's buttons are styled once", (html.match(/^\.callout\.rq \.acts \{/gm) || []).length, 1);
    env.point("here", null);
    check("…the row is the last of the agents, with no divider",
          env.app().indexOf("designer") < env.app().indexOf("rv-toggle") && !/class="agent rv"[^]*class="agent/.test(env.app()), true);
    env.click("rv-toggle", "here", { on: "1" });
    check("the switch turns it on for the file", env.sent("reviewer"), [{ type: "reviewer", doc: "doc:KEY1", on: true }]);

    await show(env, rv(), {}, { reviewer: true });
    check("waiting: the colour of the agents' palette, the switch on, 'waits for agents'",
          [/class="av rvi" style="--av: #[0-9a-f]{6}"/.test(env.app()), /waits for agents/.test(env.app()),
           /class="sw on" role="switch" aria-checked="true" aria-label="Reviewer" data-act="rv-toggle" data-on=""/.test(env.app())], [true, true, true]);
    env.click("rv-toggle", "here", { on: "" });
    check("…and the switch turns it off", env.sent("reviewer").slice(-1), [{ type: "reviewer", doc: "doc:KEY1", on: false }]);

    await show(env, rv({ state: "checking", layers: 24, since: 999 }), {}, {});
    check("checking: the grid, the count of layers, a Stop and no switch",
          [/checking<\/span> 24 layers/.test(env.app()), /data-act="stop" data-agent="reviewer"/.test(env.app()),
           /rv-toggle/.test(env.app()), /class="agent rv" data-agent="reviewer"/.test(env.app())], [true, true, false, true]);
    env.click("stop", "here", { agent: "reviewer" });
    check("…its Stop is the agents' Stop", env.sent("stop").slice(-1), [{ type: "stop", doc: "doc:KEY1", agent: "reviewer" }]);
    env.point("here", "reviewer");
    await env.clock.advance(400);
    env.point("here", null);

    await show(env, rv({ state: "sent", sent: 2, left: 1 }), {}, {});
    check("sent: how many went and how many stay", [/sent 2 fixes · 1 left as is/.test(env.app()), /rv-toggle/.test(env.app())], [true, true]);
    await show(env, rv({ state: "sent", sent: 3, left: 0 }), {}, {});
    check("…with none left, nothing more", /sent 3 fixes</.test(env.app()), true);
    await show(env, rv({ state: "sent", sent: 1, left: 0 }), {}, {});
    check("…one fix is a fix", /sent 1 fix</.test(env.app()), true);
    await show(env, rv({ state: "sent", sent: 0, left: 3 }), {}, {});
    check("…All fine says so", /all fine</.test(env.app()) && !/left as is/.test(env.app()), true);
    await show(env, rv({ state: "good", layers: 24 }), {}, {});
    check("good: a tick and the layers", /bdg done[^]*all good · 24 layers/.test(env.app()), true);

    // notes
    await show(env, rv({ state: "notes", layers: 24, notes }), {}, {});
    const open = env.app();
    check("notes: the plain yellow ! in the row, the count in the text, and no disc in the callout",
          [/bdg caution[^]*3 notes for you<\/span>/.test(open), /bdg count/.test(open), /class="callout rq"><div class="h"><span class="ind">/.test(open)], [true, false, false]);
    check("…the callout names the agent, with a Show all", [/3 notes on designer's work/.test(open), /data-act="rv-all"/.test(open),
          /Tick the ones designer should fix/.test(open)], [true, true, true]);
    check("…every note comes ticked, with its text and a button for each layer of this file",
          [(open.match(/aria-checked="true" aria-label="[^"]*"><svg/g) || []).length, /Colour isn't a token<\/b> <em>· #2F6BFF, bg\/brand matches/.test(open),
           /data-act="layer" data-id="I2:1068"/.test(open), /<span class="more">\+2<\/span>/.test(open)], [3, true, true, true]);
    check("…All fine first, the blue button last, with the count and the agent",
          /data-act="rv-fine">All fine<\/button><button type="button" class="go" data-act="rv-send">Send 3 to designer<\/button><\/div>/.test(open), true);
    env.click("rv-tick", "here", { id: "n3" });
    check("a tick taken off: the count follows, the note dims",
          [/Send 2 to designer/.test(env.app()), /<li class="">/.test(env.app())], [true, true]);
    env.click("rv-send", "here");
    check("Send answers with the ticked ones", env.sent("review-answer"), [{ type: "review-answer", doc: "doc:KEY1", send: ["n1", "n2"], at: 1000 }]);
    env.click("rv-tick", "here", { id: "n3" });
    env.click("rv-fine", "here");
    check("All fine sends none, however many are ticked", env.sent("review-answer").slice(-1), [{ type: "review-answer", doc: "doc:KEY1", send: [], at: 1000 }]);
    for (const id of ["n1", "n2", "n3"]) env.click("rv-tick", "here", { id });
    check("with none ticked, Send is off", /data-act="rv-send" disabled>Send 0 to designer/.test(env.app()), true);
    env.click("rv-tick", "here", { id: "n1" });
    env.click("rv-tick", "here", { id: "n2" });
    env.click("rv-tick", "here", { id: "n3" });
    await show(env, rv({ state: "notes", layers: 24, notes: notes.slice(0, 2) }), {}, {});
    check("a new set of notes comes ticked again", /Send 2 to designer/.test(env.app()), true);

    env.click("layer", "here", { id: "I2:1068" });
    check("a note's layer is selected in Figma as Recent's are", env.posted.filter((m) => m.type === "select").slice(-1), [{ type: "select", id: "I2:1068" }]);
    env.click("rv-all", "here");
    check("Show all selects every layer of every note at once",
          env.posted.filter((m) => m.type === "select").slice(-1), [{ type: "select", id: "I2:1068", ids: ["I2:1068", "12:44", "12:45"] }]);

    // layers the file no longer has
    env.selected("12:44", false);
    await env.clock.advance(400);
    check("a note's layer that is gone is dimmed as in Recent, the others are not",
          [/class="layer gone" data-act="layer" data-id="12:44"/.test(env.app()), /class="layer comp" data-act="layer" data-id="I2:1068"/.test(env.app())], [true, true]);
    env.posted.length = 0;
    env.click("rv-all", "here");
    check("Show all leaves the gone ones out", env.posted.filter((m) => m.type === "select"), [{ type: "select", id: "I2:1068", ids: ["I2:1068", "12:45"] }]);
    env.selected("I2:1068", false);
    env.selected("12:45", false);
    await env.clock.advance(400);
    check("…and is gone itself once every layer of the notes is", /rv-all/.test(env.app()), false);
    env.selected("12:44", true);
    env.selected("I2:1068", true);
    env.selected("12:45", true);
    await env.clock.advance(400);
    check("no number disc is left anywhere", [/bdg count|bdg notes/.test(env.app()), /bdg count/.test(html)], [false, false]);

    // expired
    await show(env, rv({ state: "notes", layers: 24, notes, expired: true }), {}, {});
    check("expired: the agent stopped waiting, so Copy for chat takes the place of Send",
          [/designer has finished\. Copy the ones to fix/.test(env.app()), /data-act="rv-copy">Copy for chat/.test(env.app()), /rv-send/.test(env.app())], [true, true, false]);
    env.click("rv-tick", "here", { id: "n3" });
    env.click("rv-copy", "here");
    await env.clock.advance(50);
    const copied = env.copied();
    check("…it copies the ticked notes as `figaro notes` prints them, with a link to each layer",
          [copied.length, copied[0] && copied[0].split("\n").slice(0, 4), copied[0] && /node-id=I2-1068/.test(copied[0]),
           copied[0] && /Frame 12/.test(copied[0]), copied[0] && /and 2 more layers/.test(copied[0])],
          [1, ["The reviewer's notes on designer's work in Dashboard:",
               "",
               "1. Colour isn't a token: #2F6BFF, bg/brand matches",
               "   \"Get started\" (INSTANCE) I2:1068  https://www.figma.com/design/KEY1/Dashboard?node-id=I2-1068"], true, false, true]);

    // a note that sums up several lines: Copy for chat keeps each line with its layers, like the agent's text
    const sum = [{ id: "d1", rule: "unbound-color", title: "Colour isn't a token", text: "fills #FFFFFF and #000000 have no style",
                   layers: [{ id: "1:1", name: "Card", type: "FRAME" }, { id: "1:2", name: "Title", type: "TEXT" }], more: 0,
                   details: [{ text: "fill #FFFFFF", layers: ["1:1"] }, { text: "fill #000000", layers: ["1:2", "1:9"] }], detailsMore: 3 }];
    await show(env, rv({ state: "notes", layers: 5, at: 1100, notes: sum, expired: true }), {}, {});
    env.click("rv-copy", "here");
    await env.clock.advance(50);
    check("…with details: the summary, each original line with its layers, and the lines left out",
          (env.copied()[0] || "").split("\n").slice(2),
          ["1. Colour isn't a token: fills #FFFFFF and #000000 have no style",
           "   - fill #FFFFFF",
           "     \"Card\" (FRAME) 1:1  https://www.figma.com/design/KEY1/Dashboard?node-id=1-1",
           "   - fill #000000",
           "     \"Title\" (TEXT) 1:2  https://www.figma.com/design/KEY1/Dashboard?node-id=1-2",
           "     1:9  https://www.figma.com/design/KEY1/Dashboard?node-id=1-9",
           "     and 3 more lines like these",
           "",
           "Fix this one, then run figaro done again."]);
    sum[0].names = { "1:9": { name: "Badge", type: "INSTANCE" }, "1:1": { name: "Card", type: "FRAME" } };
    await show(env, rv({ state: "notes", layers: 5, at: 1200, notes: sum, expired: true }), {}, {});
    env.click("rv-copy", "here");
    await env.clock.advance(50);
    check("…a layer that isn't among the note's chips gets its name from the board's names (a 1.8.0 bridge sends none: its id)",
          (env.copied()[0] || "").split("\n")[7], "     \"Badge\" (INSTANCE) 1:9  https://www.figma.com/design/KEY1/Dashboard?node-id=1-9");
    check("…and the callout itself shows the summary and the chips only", [/fills #FFFFFF and #000000 have no style/.test(env.app()), /fill #000000/.test(env.app())], [true, false]);

    // the island's row and the collapsed island
    await show(env, rv({ state: "notes", layers: 24, notes }), {}, {});
    env.click("toggle", "here");
    await env.clock.advance(400);
    check("collapsed: the island's row shows the yellow !, and its line says 'reviewer: 3 notes for you'",
          [/<span class="ind"><span class="bdg caution">/.test(env.app()), /reviewer: 3 notes for you/.test(env.app())], [true, true]);
    await show(env, rv({ state: "notes", layers: 24, notes: notes.slice(0, 1) }), {}, {});
    check("…one note is a note", /reviewer: 1 note for you/.test(env.app()), true);
    await show(env, rv({ state: "notes", layers: 24, notes: notes.slice(0, 1) }), {}, { reviewer: false });
    check("…and with the switch off in the settings, back to the agents", /reviewer: /.test(env.app()), false);

    // a second check that returns the same ids comes all ticked too
    const again = (extra) => env.board([file("KEY1", "Dashboard", { agents: [agent("designer", { since: 990, last: 990 })], reviewer: rv(extra) })],
                                       { settings: { reviewer: true } });
    again({ state: "notes", at: 1010, notes });
    await env.clock.advance(400);
    env.click("toggle", "here");
    await env.clock.advance(400);
    env.click("rv-tick", "here", { id: "n3" });
    check("a tick taken off", /Send 2 to designer/.test(env.app()), true);
    again({ state: "good", layers: 5, at: 1020 });
    await env.clock.advance(400);
    again({ state: "notes", at: 1030, notes });
    await env.clock.advance(400);
    check("a second round with the same ids: all ticked again", /Send 3 to designer/.test(env.app()), true);

    // what comes from a file is text: names, titles, texts, types, the agent's name
    const evil = [{ id: "e1", rule: "x", title: "<img src=x onerror=alert(1)>", text: '"><b>x</b>',
                    layers: [{ id: "1:1", name: '<script>alert(2)</script>"', type: "constructor" }, { id: "1:2", name: "ok", type: "__proto__" }], more: 0 }];
    again({ state: "notes", at: 1040, notes: evil, agent: "<i>d</i>" });
    await env.clock.advance(400);
    const bad = env.app();
    check("names, titles, texts and the agent are escaped",
          [/<img|<script|<b>x|<i>d/.test(bad), /&lt;img src=x onerror=alert\(1\)&gt;/.test(bad), /&quot;&gt;&lt;b&gt;/.test(bad), /&lt;script&gt;alert\(2\)/.test(bad)],
          [false, true, true, true]);
    check("…and a type like constructor gets the frame's icon, not Object's source", /native code|function Object/.test(bad), false);

    // on purpose: a note the designer means; the bridge leaves its rule out on those layers from then on
    again({ state: "notes", layers: 24, notes, at: 1050, intentional: 0 });
    await env.clock.advance(400);
    check("on purpose: a button under each note", (env.app().match(/<button type="button" class="op" data-act="rv-purpose" data-id="n\d"/g) || []).length, 3);
    env.click("rv-tick", "here", { id: "n3" });
    env.click("rv-purpose", "here", { id: "n2" });
    check("…it marks the note, with the notes' at", env.sent("review-intentional"), [{ type: "review-intentional", doc: "doc:KEY1", note: "n2", at: 1050 }]);
    again({ state: "notes", layers: 24, notes: [notes[0], notes[2]], at: 1050, intentional: 2 });
    await env.clock.advance(400);
    check("…the note leaves, the other ticks stay, and no Clear while notes wait",
          [/Send 1 to designer/.test(env.app()), (env.app().match(/data-act="rv-purpose"/g) || []).length, /rv-unmark/.test(env.app())], [true, 2, false]);
    again({ state: "sent", sent: 1, left: 1, intentional: 2 });
    await env.clock.advance(400);
    check("the row says how many layers are marked, with a Clear", /<p class="msg rvm">2 layers marked on purpose<button type="button" class="showall" data-act="rv-unmark"/.test(env.app()), true);
    env.click("rv-unmark", "here");
    await env.clock.advance(50);
    check("…Clear asks first, inline, with the same count, and sends nothing yet",
          [/Clear marks on 2 layers\?<span class="ask"><button type="button" class="yn" data-act="rv-unmark-yes">Yes<\/button><button type="button" class="yn" data-act="rv-unmark-no">No<\/button>/.test(env.app()), env.sent("review-unmark").length], [true, 0]);
    env.click("rv-unmark-no", "here");
    await env.clock.advance(50);
    check("…No puts the row back", [/2 layers marked on purpose/.test(env.app()), /rv-unmark-yes/.test(env.app()), env.sent("review-unmark").length], [true, false, 0]);
    env.click("rv-unmark", "here");
    again({ state: "waiting", intentional: 2 });
    again({ state: "notes", layers: 24, notes: [notes[0]], at: 1055, intentional: 2 });
    await env.clock.advance(400);
    again({ state: "sent", sent: 1, left: 0, intentional: 2 });
    await env.clock.advance(400);
    check("…so does leaving the row", [/2 layers marked on purpose/.test(env.app()), /rv-unmark-yes/.test(env.app())], [true, false]);
    env.click("rv-unmark", "here");
    env.click("rv-unmark-yes", "here");
    check("…Yes forgets the file's marks", env.sent("review-unmark"), [{ type: "review-unmark", doc: "doc:KEY1" }]);
    again({ state: "waiting", intentional: 0 });
    await env.clock.advance(400);
    check("…none marked: no line", /marked on purpose/.test(env.app()), false);
    again({ state: "off", on: false, intentional: 4 });
    await env.clock.advance(400);
    check("…nor with the reviewer off in the file", /marked on purpose/.test(env.app()), false);
    again({ state: "notes", layers: 24, notes, at: 1060 });
    await env.clock.advance(400);
    check("a bridge from before the mark: no On purpose", [/Send 3 to designer/.test(env.app()), /rv-purpose/.test(env.app())], [true, false]);
  }
  {
    // another file: its notes answer through the bridge, its layers are names only
    const env = load();
    env.identity("Dashboard", "KEY1");
    env.ws().open();
    const other = file("KEY2", "Landing", { agents: [agent("tokens", { since: 990, last: 990 })],
      reviewer: { on: true, state: "notes", agent: "tokens", layers: 3, since: null, at: 1000, expired: false, sent: 0, left: 0,
                  notes: [{ id: "n1", rule: "x", title: "Default name", text: "Frame 2", layers: [{ id: "1:2", name: "Frame 2", type: "FRAME" }], more: 0 }] } });
    env.board([file("KEY1", "Dashboard"), other], { settings: { reviewer: true } });
    env.click("toggle", "doc:KEY2");
    await env.clock.advance(400);
    check("a file of another place: names only, no Show all, and the answer names its file",
          [/<span class="layer"><svg/.test(env.app()), /rv-all/.test(env.app()), /data-act="layer" data-id="1:2"/.test(env.app())], [true, false, false]);
    env.click("rv-send", "doc:KEY2");
    check("…Send names its file", env.sent("review-answer"), [{ type: "review-answer", doc: "doc:KEY2", send: ["n1"], at: 1000 }]);
  }

  console.log(failed ? `\n${failed} FAILED` : "\nall window checks passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
