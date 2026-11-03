// The plugin names a document by its file key and writes nothing into it on
// its own — it gets run in shared libraries too.
//
//     node tests/plugin.test.js
const fs = require("fs");
const path = require("path").join(__dirname, "..", "plugin", "code.js");
const src = fs.readFileSync(path, "utf8");

// Load code.js the way Figma does, with a stub that records every write into the
// document and every message posted to the UI.
function load(fileKey) {
  const writes = [];
  const posted = [];
  const opened = [];
  const record = (name) => (...args) => { writes.push([name, ...args]); };
  const figma = {
    root: {
      name: "UI kit",
      children: [{ id: "0:1" }],
      getPluginData: () => "",
      setPluginData: record("setPluginData"),
      setSharedPluginData: record("setSharedPluginData"),
      setRelaunchData: record("setRelaunchData"),
    },
    showUI() {},
    openExternal(url) { opened.push(url); },
    ui: { onmessage: null, postMessage(m) { posted.push(m); } },
    currentPage: { selection: [] },
    variables: {},
    on() {}, off() {},  // code.js listens for documentchange
  };
  if (fileKey instanceof Error) {
    Object.defineProperty(figma, "fileKey", { get() { throw fileKey; } });
  } else {
    figma.fileKey = fileKey;
  }
  const api = new Function("figma", "__html__", src + "\nreturn { docSignature };")(figma, "");
  return { api, writes, figma, opened, identity: posted.find((m) => m.type === "identity") };
}

let failed = 0;
function check(label, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  const ok = a === e;
  if (!ok) failed++;
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}${ok ? "" : `\n         got ${a}\n         want ${e}`}`);
}

// With the key (enablePrivatePluginApi): the document id is the key itself.
const keyed = load("AbCdEf0123456789xyzKey");
check("identity carries the file key", keyed.identity.fileKey, "AbCdEf0123456789xyzKey");
check("document id is the file key", keyed.identity.docSig, "AbCdEf0123456789xyzKey");
check("nothing written with a key", keyed.writes, []);

// Without a key: an id for this run only — stable while it runs, never stored.
const bare = load(undefined);
check("id without a key is a run id", /^r[0-9a-z]+$/.test(bare.identity.docSig), true);
check("run id is stable within a run", bare.api.docSignature(), bare.identity.docSig);
check("next run gets another id", load(undefined).identity.docSig !== bare.identity.docSig, true);
check("nothing written without a key", bare.writes, []);

// A sandbox that throws on fileKey behaves like one without it.
const throwing = load(new Error("fileKey is not available"));
check("throwing fileKey falls back to a run id", /^r[0-9a-z]+$/.test(throwing.identity.docSig), true);
check("nothing written when fileKey throws", throwing.writes, []);

// The window opens only the project's pages on GitHub. A bridge from before the
// repository moved sends its old address, which GitHub only redirects: the new one opens.
const linked = load("AbCdEf0123456789xyzKey");
for (const url of [
  "https://github.com/artem-levchenko/figaro/releases/tag/v1.9.2",
  "https://github.com/artem-levchenko-2/figaro/releases/tag/v1.9.1",
  "https://github.com/artem-levchenko-2/figaro-fork/releases",
  "https://github.com/artem-levchenko/figaro.example.com/",
  "https://example.com/artem-levchenko/figaro/",
  42,
]) linked.figma.ui.onmessage({ type: "open-url", url });
check("release links open the project's pages only, the old address as the new", linked.opened, [
  "https://github.com/artem-levchenko/figaro/releases/tag/v1.9.2",
  "https://github.com/artem-levchenko/figaro/releases/tag/v1.9.1",
]);

console.log(failed ? `\n${failed} FAILED` : "\nall plugin checks passed");
process.exit(failed ? 1 : 0);
