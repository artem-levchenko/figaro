"""The exec core against live Figma — run by hand, not by pytest. macOS.

    FIGARO_TEST_FILE=<key or link> venv/bin/python tests/live_exec.py          # ~15 s
    FIGARO_TEST_FILE=<key or link> venv/bin/python tests/live_exec.py --idle   # + the tone stops (~2 min)

Only in a scratch file of your own (tests/live_file.py): it makes a frame
"figaro-live" far off the canvas on the current page, changes it, has a
read-only script try to change it, has a loop that never ends stopped at
--timeout, undoes it all and checks that nothing is left. Needs the bridge (8788) and the plugin of the current build running in
that file (`figaro reload`).

The first writing script of the hour saves a checkpoint, a version named
"Figaro · before changes by live-exec" in the file's history.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import live_file

BRIDGE = live_file.BRIDGE
DRAFT = live_file.key()
AGENT = "live-exec"
AGENTS = [AGENT, "live-exec cleanup", "someone-else"]  # every name the test runs under
STATE = Path(os.environ.get("FIGARO_STATE_DIR") or Path.home() / ".cache" / "figaro")
FIND = "figma.root.findAll((n) => n.name.startsWith('figaro-live'))"

failed = 0


def post(path, body, timeout=90):
    req = urllib.request.Request(BRIDGE + path, data=json.dumps(body).encode(),
                                 headers=live_file.HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def run(code, **kw):
    return post("/exec", {"code": code, "target": DRAFT, "agent": AGENT, "timeout": 30, **kw})


def read(code):
    status, r = run(code, read_only=True)
    if status != 200:
        sys.exit(f"read failed: {r.get('error')}")
    return r.get("value")


def undo(**kw):
    return post("/undo", {"target": DRAFT, "agent": AGENT, **kw})


def check(label, ok, detail=None):
    global failed
    if not ok:
        failed += 1
    print(f"{'  ok  ' if ok else ' FAIL '} {label}"
          + ("" if ok or detail is None else f"\n         {json.dumps(detail, ensure_ascii=False)[:600]}"))


def awake_assertion():
    """Figma's "Playing audio" sleep assertion — on while the tone plays."""
    out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True).stdout
    return any("Figma" in line and "audio" in line.lower() for line in out.splitlines())


def checkpoint_due():
    try:
        last = json.loads((STATE / "checkpoints.json").read_text()).get(f"file:{DRAFT}")
    except (OSError, ValueError):
        last = None
    return last is None or time.time() - last >= 3600


def main():
    live_file.connected(DRAFT)
    if read(f"return {FIND}.length;"):
        sys.exit("figaro-live nodes are left from an earlier run — remove them first")

    # ── a writing script: one step, with what it changed ─────────────────
    due = checkpoint_due()
    status, r = run("""
const f = figma.createFrame();
f.name = "figaro-live"; f.x = -20000; f.y = -20000; f.resize(200, 120);
const x = figma.createRectangle();
x.name = "figaro-live rect";
x.fills = [{ type: "SOLID", color: { r: 0.2, g: 0.4, b: 0.8 } }];
f.appendChild(x);
return { frame: f.id, rect: x.id };""")
    if status != 200:
        sys.exit(f"could not create the test frame: {r.get('error')}")
    frame, rect = r["value"]["frame"], r["value"]["rect"]
    c = r.get("changes") or {}
    check(f"create: 2 new nodes reported (changes {c.get('created')}/{c.get('changed')})",
          c.get("created") == 2, c)
    cp = r.get("checkpoint")
    if due:
        check("first writer of the hour saved a checkpoint", bool(cp and cp.get("id")), cp)
    else:
        check("no checkpoint within the hour", cp is None, cp)

    status, r = run(f"""
const x = await figma.getNodeByIdAsync("{rect}");
x.fills = [{{ type: "SOLID", color: {{ r: 1, g: 0, b: 0 }} }}];
(await figma.getNodeByIdAsync("{frame}")).name = "figaro-live changed";
return 1;""")
    c = r.get("changes") or {}
    check("change: 2 nodes, fills and name", c.get("changed") == 2
          and {"fills", "name"} <= set(c.get("props") or {}), c)

    # ── read-only ────────────────────────────────────────────────────────
    status, r = run("return figma.currentPage.name;", read_only=True)
    check("read-only read: fine, nothing changed", status == 200 and "changes" not in r, r)
    status, r = run(f'(await figma.getNodeByIdAsync("{rect}")).opacity = 0.5; return 1;',
                    read_only=True)
    check("read-only write: refused and rolled back",
          status == 500 and r.get("rolled_back") is True, r)
    check("…opacity is 1 again", read(f'return (await figma.getNodeByIdAsync("{rect}")).opacity;') == 1)
    status, r = run(f'(await figma.getNodeByIdAsync("{rect}")).opacity = 0.5; return 1;',
                    parallel=True)
    check("a parallel script is read-only too", status == 500 and r.get("rolled_back") is True, r)

    # ── errors and ids ───────────────────────────────────────────────────
    status, r = run("const a = 1;\nconst b = 2;\nnull.boom;\nreturn a + b;", read_only=True)
    check("an error names line 3 and its source",
          (r.get("line"), r.get("source")) == (3, "null.boom;"), r)
    t0 = time.time()
    status, r = run('return await figma.getNodeByIdAsync("99999:99999");', read_only=True)
    took = time.time() - t0
    check(f"an id from elsewhere is null at once ({took:.2f}s)",
          status == 200 and r.get("value") is None and took < 3, r)
    status, r = run('return await h.node("99999:99999");', read_only=True)
    check("…and h.node says so", status == 500 and "no node" in r.get("error", ""), r)

    # ── a loop that never ends ───────────────────────────────────────────
    t0 = time.time()
    status, r = run("let n = 0;\nwhile (true) n++;", read_only=True, timeout=3)
    took = time.time() - t0
    check(f"while (true) ends at --timeout ({took:.1f}s), on its line",
          status == 500 and "the loop on line 2 was still running" in r.get("error", "")
          and r.get("line") == 2 and took < 6, r)
    status, r = run("return 1;", read_only=True)
    check("…and the file is free at once", status == 200 and r.get("value") == 1, r)

    # ── awake ────────────────────────────────────────────────────────────
    t0 = time.time()
    status, r = run("for (let i = 0; i < 20; i++) await new Promise((r) => setTimeout(r, 10));"
                    "return 1;", read_only=True)
    took = time.time() - t0
    check(f"20 chained timers took {took:.1f}s — not throttled", status == 200 and took < 5, r)
    check("the tone holds Figma's audio assertion", awake_assertion())

    # ── undo ─────────────────────────────────────────────────────────────
    status, r = undo()
    done = (r.get("value") or {}).get("undone") or {}
    check(f"undo reverts the change ({done.get('changes')})",
          status == 200 and done.get("changes") == "~2", r)
    name, fill = read(f'const f = await figma.getNodeByIdAsync("{frame}");'
                      f'const x = await figma.getNodeByIdAsync("{rect}");'
                      "return [f.name, x.fills[0].color];")
    blue = {"r": 0.2, "g": 0.4, "b": 0.8}  # Figma keeps floats: 0.2 comes back as 0.2000000029…
    check("…name and fill are back", name == "figaro-live"
          and all(abs(fill[k] - v) < 1e-6 for k, v in blue.items()), [name, fill])
    status, r = undo()
    left = (r.get("value") or {}).get("left")
    check("undo again reverts the creation", status == 200, r)
    check("…the frame is gone", read(f"return {FIND}.length;") == 0)
    if left == 0:
        status, r = undo()
        check("nothing left to undo", status == 500 and "nothing to undo" in r.get("error", ""), r)

    # Another agent's script needs --force.
    run('const f = figma.createFrame(); f.name = "figaro-live other"; f.x = -20000; return f.id;')
    status, r = post("/undo", {"target": DRAFT, "agent": "someone-else"})
    check("another agent's script is not undone without force",
          status == 500 and "someone-else" in r.get("error", ""), r)
    status, r = post("/undo", {"target": DRAFT, "agent": "someone-else", "force": True})
    check("…and is with it", status == 200 and read(f"return {FIND}.length;") == 0, r)

    # An edit after the script, not by a script (a timer stands in for a person).
    status, r = run('const f = figma.createFrame(); f.name = "figaro-live late"; f.x = -20000;'
                    'setTimeout(() => { f.name = "figaro-live late, renamed"; }, 300); return 1;')
    time.sleep(1.5)
    status, r = undo()
    check("an edit after the script blocks undo",
          status == 500 and "more changed in the file" in r.get("error", ""), r)
    check("…and nothing was reverted",
          read(f"return {FIND}.map((n) => n.name);") == ["figaro-live late, renamed"])
    # Under another name, so a later `figaro undo -A live-exec` stops at it
    # instead of bringing the node back.
    run(f"for (const n of {FIND}) n.remove(); return 1;", checkpoint=False,
        agent="live-exec cleanup")
    check("clean: no figaro-live nodes left", read(f"return {FIND}.length;") == 0)

    if "--idle" in sys.argv:
        print("  …waiting 1.5 min with no scripts for the tone to stop")
        time.sleep(90)
        check("the tone stopped after 1 min idle", not awake_assertion())

    print(f"\n{failed} FAILED" if failed else "\nall live checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    try:
        main()
    finally:
        live_file.forget(DRAFT, AGENTS)  # off the window's board
