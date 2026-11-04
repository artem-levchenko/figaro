#!/usr/bin/env python3
"""figaro — CLI client for the Figaro bridge.

Commands:
    figaro exec "<js>"             # run arbitrary JS in plugin context
    figaro exec --file f.js
    figaro exec --stdin
    figaro status                   # check server / plugin connection
    figaro doctor                   # diagnose the whole chain, with fixes
    figaro sel                      # what is selected in Figma right now
    figaro tree <id> [--depth N] [--layout]
    figaro find <id> <filter>       # find descendants (name=X, name~X, type=X, text=X)
    figaro text <id> "<new text>"   # set TEXT node characters (autoloads font)
    figaro variant <id> "P=V" ...   # set INSTANCE variant property values
    figaro clone <id> [--right|--left|--up|--down] [--gap N] [--name N]
    figaro rm <id> [<id> ...]       # remove one or more nodes
    figaro import-component <key>   # import library component, instantiate (--focus: show it)
    figaro undo                     # undo the file's last script, when that is safe
    figaro reload                   # load plugin/code.js from disk into the open plugin
    figaro done "<note>"            # you are done in the file: the plugin's window says so, with the note
    figaro notes -A <you> [--wait]  # the designer's answer to the reviewer's notes, once
    figaro wait                     # until the file is ready: connected, nothing stuck in it
    figaro inspect <id> [--json | -o f.json]   # layout, styles, text, keys of what it uses
    figaro shot <id> ...            # picture(s) saved to files; tall ones cut in parts
    figaro link <id|sel> ...        # clickable links to layers, for reports
    figaro image <id> <file> [--mode fill|fit|crop|tile]   # fill a layer with a PNG, JPEG or GIF
    figaro comments [<link>]        # the file's open Figma comments, each with the layer it is pinned to
    figaro comment <id> "<text>"    # a new Figma comment on a layer; --reply, --react, --delete <comment id>
    figaro "<js>"                   # shorthand for `exec`

-R / --read-only: the script must not change the file; if it does, the plugin
rolls the change back and the call fails. sel, tree, find, inspect, shot, link
and doctor run as quick reads: their own code changes nothing, so they skip the
undo step and the ~150 ms wait for Figma's change report. Any other script
whose code only reads skips the wait too: the plugin reads the code, and the
script answers when it ends. Only a script that may write waits for the report.
A writing script gets a checkpoint (a version in File → Version history) before
the first one in a file and then hourly; --checkpoint LABEL forces one,
--no-checkpoint skips it. Every reply says what the script changed.

Anywhere a node id is taken, `page` (current page) and `sel` (first selected
node) work too, and so does a Figma link (Copy link to selection): its file
becomes the target. -T takes a link as well. Bridge address comes from
FIGARO_HOST / FIGARO_PORT; when nothing answers there, the CLI starts the
bridge (start-bridge.sh) and waits for the plugin — FIGARO_AUTOSTART=0 stops it.

exec --lib f.js (repeatable, or FIGARO_LIB): the file's top-level functions
and consts reach the script as lib.<name>; the plugin keeps it compiled, so
later calls send only its hash. exec --shot [id]: a picture after the script.
exec --image NAME=FILE (repeatable): a PNG, JPEG or GIF for h.image(node, "NAME").

Helpers available inside exec'd code (as `h.*`):
    h.bF(node, idx, varOrId)    h.bS(node, idx, varOrId)   h.bN(node, prop, varOrId)
    h.findByName(root, name)    h.findAllByName(root, name)
    h.dumpTree(node, {maxDepth, showSize, showText, showLayout})
    h.withFonts(root, asyncFn)  h.setText(node, text)
    h.cloneNext(node, {direction, gap, name})
    h.variant(instance, props)  h.variantsOf(instance)
    h.sel()                     h.resolve(idOrAlias)
    h.hex("#1a2b3c")            h.solid("#1a2b3c", opacity?)
    h.frame(parent, {layout, w, h, spacing, padding, align, fill, radius, name})
    h.node(id)                  h.var_(idOrKey)
    h.importComp(key)           h.importVar(key)
    h.inspect(node, {depth})    h.shot(node, {format, scale, width, absolute})   h.link(node)
    h.image(node, source, mode) fill with a picture: a file name from exec --image, or bytes
    h.fonts(node)  h.fa()       h.fill(node, axis)  h.hug(node, axis)  h.fixed(node, w, h)
    h.wrapText(node, w?)        h.find(root, {name, nameHas, type, text, textHas, hidden})
    h.annotations(scope?)       Dev Mode notes: [{frame, node, name, visible, labels, properties?}]
"""

import argparse
import json
import os
import socket
import sys
import time

# On Windows `figaro` is the figaro.exe from tools/install.ps1, which starts
# Python outside this folder: the modules next to this file are found anyway.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cli_extras  # noqa: E402  links, autostart, inspect / shot / link, --lib
import figaro_token  # noqa: E402  the bridge's shared secret
import figma_links  # noqa: E402  file keys in links


# 127.0.0.1, not "localhost": on Windows "localhost" tries ::1 first, and against a
# bridge that only listens on IPv4 every call stalls ~2 s before falling back.
DEFAULT_HOST = os.environ.get("FIGARO_HOST", "127.0.0.1")


def _env_port():
    raw = os.environ.get("FIGARO_PORT") or "8788"
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"figaro: FIGARO_PORT is {raw!r}, not a port number")


DEFAULT_PORT = _env_port()

HOST = DEFAULT_HOST
PORT = DEFAULT_PORT
TARGET = None  # which connected Figma file to route to (file name / fileKey / substring)
PARALLEL = False  # skip the bridge's per-file lock — READ-ONLY scripts only
AGENT = os.environ.get("FIGARO_AGENT") or None  # who is calling, shown in the file's queue
QUEUE_TIMEOUT = None  # max seconds to wait for a busy file; bridge default = --timeout
READ_ONLY = False  # the plugin rolls back whatever the script changed
CHECKPOINT = None  # a label forces a checkpoint, False skips it, None = hourly rule

# An agent's sandbox that keeps commands off the network (Codex, Cursor) blocks
# 127.0.0.1 too: the call fails with "Operation not permitted", not "refused".
SANDBOXED = ("a sandbox keeps this command off the network, 127.0.0.1 included. Run figaro "
             "outside the sandbox: ask the user to approve that, or to allow figaro for good")

def force_utf8_output():
    """Windows consoles default to cp1252 and raise on anything outside it.

    Layer names are routinely Cyrillic, CJK or emoji, so without this a plain
    `figaro tree` blows up with UnicodeEncodeError instead of printing.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def read_stdin():
    """The script on stdin, read as UTF-8: on Windows Python reads a pipe in the
    ANSI code page, which garbles every character outside ASCII in a script's
    texts, and Figma would get them garbled."""
    data = sys.stdin.buffer.read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        import locale
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def node_expr(id_or_alias):
    """JS that resolves a node id, or the aliases `page` / `sel`."""
    return f"await h.resolve({json.dumps(id_or_alias)})"


def _request(method, path, payload=None, timeout=65):
    import http.client  # here, not at the top: `-h` and a wrong flag never ask the bridge
    # The token goes only to what said it is the bridge (one `GET /` per process): any other
    # program on the port would get a secret that runs code in the user's Figma file.
    found = cli_extras.probe(HOST, PORT)
    if found["state"] == "foreign":
        return 0, cli_extras.foreign_answer(HOST, PORT, found["why"])
    if found["state"] == "refused":  # the bridge itself, refusing the Host header
        return found["status"], found["body"]
    if found["state"] == "unsure":  # hung up or silent, maybe a bridge that restarts: no token
        return 0, {"ok": False, "error": f"connection: port {PORT} did not answer as the bridge "
                                         f"({found['why']}); nothing was sent to it"}
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    token = figaro_token.read_token(PORT) if found["state"] == "ours" else None
    if token:  # read each time: a restarted bridge has a new one
        headers["X-Figaro-Token"] = token
    try:
        status, body = cli_extras.fetch(HOST, PORT, method, path, data, headers, timeout)
    except cli_extras.Unreached as e:
        cli_extras.forget(HOST, PORT)
        # Nothing listens — start our bridge once and ask again.
        if isinstance(e.reason, ConnectionRefusedError) and cli_extras.autostart(HOST, PORT):
            return _request(method, path, payload, timeout)
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            return 0, {"ok": False, "error": "request timed out"}
        resp = {"ok": False, "error": f"connection: {e.reason}"}
        if isinstance(e.reason, PermissionError):
            resp["hint"] = SANDBOXED
        return 0, resp
    except (TimeoutError, socket.timeout):  # socket.timeout is no TimeoutError before 3.10
        return 0, {"ok": False, "error": "request timed out"}
    except (http.client.HTTPException, ConnectionError) as e:
        cli_extras.forget(HOST, PORT)
        # The bridge hung up without an answer: it stopped or restarted meanwhile.
        return 0, {"ok": False, "error": f"connection: {e or type(e).__name__}"}
    # A 401 to a call that went without the token: the look found the port empty, and a
    # bridge started since (another call did it). Look again and ask with the token.
    if (status == 401 and found["state"] == "none"
            and cli_extras.probe(HOST, PORT, fresh=True)["state"] == "ours"):
        return _request(method, path, payload, timeout)
    return status, _answer(body, status)


def _answer(body, status):
    """The bridge's JSON answer, or an error made of whatever else came back."""
    try:
        value = json.loads(body or b"{}")
    except ValueError:
        text = body.decode("utf-8", "replace").strip()[:300]
        return {"ok": False, "error": text or f"HTTP {status} with no answer"}
    if not isinstance(value, dict):
        return {"ok": False, "error": f"unexpected answer: {str(value)[:300]}"}
    return value


def _exec(code, timeout=60, read_only=False, quick=False, libs=None, probe=False, images=None,
          print_result=False):
    payload = {"code": code, "timeout": timeout}
    if TARGET:
        payload["target"] = TARGET
    if PARALLEL:
        payload["parallel"] = True
    if AGENT:
        payload["agent"] = AGENT
    if READ_ONLY or read_only:
        payload["read_only"] = True
    if CHECKPOINT is not None:
        payload["checkpoint"] = CHECKPOINT
    if quick:  # a built-in reader — no undo step, change list or wait for Figma's events
        payload["quick"] = True
    if probe:  # doctor's check of the connection: the plugin's window doesn't show it
        payload["probe"] = True
    if libs:
        payload["libs"] = libs
    if images:  # files for h.image, as base64 in the body: a command line would be too short
        payload["images"] = images
    if print_result:  # this call prints `result` itself: the bridge need not send it with `value`
        payload["value_only"] = True
    queue = timeout if QUEUE_TIMEOUT is None else QUEUE_TIMEOUT
    payload["queue_timeout"] = queue
    # The socket deadline has to outlast the bridge's own: time in the file's
    # queue plus the run itself. Hanging up earlier loses the bridge's error
    # payload, and used to leave a queued script to run with nobody waiting.
    status, resp = _request("POST", "/exec", payload, timeout=queue + timeout + 5)
    if resp.get("queued_ms", 0) >= 1000:
        print(f"  (waited {resp['queued_ms'] / 1000:.1f}s in the file's queue)", file=sys.stderr)
    if print_result and resp.get("ok") and "result" not in resp:
        # The bridge left it out: the same text, made from the value. An older bridge always
        # sends it, so this is never reached there, and nothing is loaded for it.
        import result_text
        resp["result"] = result_text.render(resp.get("value"))
    return status, resp


def _run(args, code, **kwargs):
    """_exec, then print its reply: what a command that shows the script's value does."""
    return _emit(_exec(code, args.timeout, print_result=not args.raw, **kwargs)[1], raw=args.raw)


def _emit(resp, raw=False):
    if raw:
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        return 0 if resp.get("ok") else 1

    for line in resp.get("logs") or []:
        print(f"  log: {line}", file=sys.stderr)
    if resp.get("notice"):
        print(f"   ⚠ {resp['notice']}", file=sys.stderr)
    for line in _change_lines(resp):
        print(line, file=sys.stderr)

    if resp.get("ok") is False:
        print(f"figaro: {resp.get('error', 'unknown')}", file=sys.stderr)
        if resp.get("line"):
            print(f"   line {resp['line']}: {resp.get('source', '')}", file=sys.stderr)
        lib = resp.get("lib")  # the error is inside a --lib file
        if isinstance(lib, dict):
            print(f"   {lib.get('name')}, line {lib.get('line')}: {lib.get('source', '')}",
                  file=sys.stderr)
        if resp.get("warning"):
            print(f"   ⚠ {resp['warning']}", file=sys.stderr)
        if resp.get("hint"):
            print(f"   hint: {resp['hint']}", file=sys.stderr)
        if resp.get("stack"):
            print(resp["stack"], file=sys.stderr)
        return 1

    if resp.get("result"):
        print(resp["result"])
    print(f"  ({resp.get('elapsed_ms', '?')}ms)", file=sys.stderr)
    return 0


def _change_lines(resp):
    """What the script changed, and the checkpoint made before it."""
    out = []
    c = resp.get("changes")
    if isinstance(c, dict):
        parts = [f"{sign}{c[key]}" for sign, key in (("+", "created"), ("−", "deleted"),
                                                     ("~", "changed")) if c.get(key)]
        if c.get("styles"):
            parts.append(f"styles {c['styles']}")
        line = "  changed: " + (" ".join(parts) or "nothing")
        if c.get("props"):
            line += f" ({', '.join(c['props'])})"
        if c.get("remote"):
            line += f"; {c['remote']} more by other people in the file"
        if c.get("shared"):
            line += "; other scripts ran at the same time — their changes are here too"
        if c.get("late"):
            line += "; Figma reported late — the list may be incomplete"
        out.append(line)
        made = None if resp.get("rolled_back") else cli_extras.created_line(c)
        if made:
            out.append(made)
    cp = resp.get("checkpoint")
    if isinstance(cp, dict):
        if cp.get("error"):
            out.append(f"   ⚠ checkpoint not saved: {cp['error']}")
        else:
            out.append(f"  checkpoint: \"{cp.get('title')}\" — File → Version history")
    return out


# ─── command builders ─────────────────────────────────────────────────────

def cmd_status(args):
    status, resp = _request("GET", "/status")
    print(json.dumps(resp, indent=2, ensure_ascii=False))
    return 0 if status == 200 else 2


def cmd_targets(args):
    status, resp = _request("GET", "/targets")
    if status == 0:  # no bridge to ask
        return _emit(resp)
    files = resp.get("files") or []
    if not files:
        print("no files connected — Run the Figaro plugin in each Figma file",
              file=sys.stderr)
        return 1
    for f in files:
        name = f.get("name") or "(unidentified)"
        key = f.get("fileKey") or "-"
        q = f.get("queue") or {}
        run = q.get("running")
        busy = f"busy: {run.get('agent') or '?'} {run.get('for_s')}s" if run else "idle"
        if q.get("waiting"):
            busy += f", waiting: {', '.join(q['waiting'])}"
        same = f"\tsame document as {f['sameDocAs']}" if f.get("sameDocAs") else ""
        # doc is stable (stored in the file) — that's the id to use with -T.
        print(f"{name}\t{f.get('doc') or '-'}\t{key}\t{f.get('conn')}\t{busy}{same}")
    return 0


def cmd_clear(args):
    """Drop a file's abandoned-script interlock after a 504."""
    payload = {"target": TARGET} if TARGET else {}
    status, resp = _request("POST", "/clear", payload)
    if status != 200:
        print(resp.get("error", "clear failed"), file=sys.stderr)
        if resp.get("hint"):
            print(f"   hint: {resp['hint']}", file=sys.stderr)
        return 2
    cleared = resp.get("cleared") or []
    print(f"cleared {len(cleared)} abandoned script(s) on {resp.get('file')!r}"
          + (f": {', '.join(cleared)}" if cleared else ""))
    return 0


def cmd_sel(args):
    return _run(args, "return h.sel();", quick=True)


def cmd_undo(args):
    """Revert the file's last script — the plugin refuses when that is unsafe."""
    payload = {"force": args.force}
    if TARGET:
        payload["target"] = TARGET
    if AGENT:
        payload["agent"] = AGENT
    status, resp = _request("POST", "/undo", payload)
    if args.raw:
        return _emit(resp, raw=True)
    if not resp.get("ok"):
        print(f"figaro: {resp.get('error', 'undo failed')}", file=sys.stderr)
        if resp.get("hint"):
            print(f"   hint: {resp['hint']}", file=sys.stderr)
        return 1
    v = resp.get("value") or {}
    done = v.get("undone") or {}
    # Figma may have kept one script as several Cmd+Z steps (addComponentProperty)
    steps = f" ({v['steps']} Cmd+Z steps)" if v.get("steps") else ""
    print(f"undone: {done.get('changes')}{steps} — {done.get('agent') or 'a script without -A'}, "
          f"{done.get('ago')}; can still undo: {v.get('left', 0)}")
    if v.get("warning"):
        print(f"   ⚠ {v['warning']}", file=sys.stderr)
    return 0


def cmd_reload(args):
    """Run plugin/code.js and ui.html from disk in the open plugin."""
    payload = {"target": TARGET} if TARGET else {}
    status, resp = _request("POST", "/reload", payload)
    if args.raw:
        return _emit(resp, raw=True)
    if not resp.get("ok"):
        print(f"figaro: {resp.get('error', 'reload failed')}", file=sys.stderr)
        if resp.get("hint"):
            print(f"   hint: {resp['hint']}", file=sys.stderr)
        return 1
    print(f"plugin {resp.get('plugin')} runs in \"{resp.get('file')}\" "
          f"({resp.get('elapsed_ms')} ms)")
    return 0


def cmd_done(args):
    """Say this agent is done in the file, for now: the plugin's window shows
    it done, with the note for the user, until its next script there that can
    change the file."""
    payload = {"text": args.text} if args.text else {}
    if TARGET:
        payload["target"] = TARGET
    if AGENT:
        payload["agent"] = AGENT
    status, resp = _request("POST", "/done", payload, timeout=120)  # a check may wait for the file
    if status == 404:
        resp = {"ok": False, "error": "the bridge runs code older than `figaro done`",
                "hint": ("restart it when no other agent is using it:  "
                         + cli_extras.start_hint(restart=True))}
    if args.raw:
        return _emit(resp, raw=True)
    if not resp.get("ok"):
        print(f"figaro: {resp.get('error', 'done failed')}", file=sys.stderr)
        if resp.get("hint"):
            print(f"   hint: {resp['hint']}", file=sys.stderr)
        return 1
    note = f", with your note: {resp['text']}" if resp.get("text") else ""
    print(f"the Figaro window in \"{resp.get('file')}\" shows you done{note}")
    print("  until your next script in this file that can change it", file=sys.stderr)
    review = resp.get("review")
    if isinstance(review, dict) and review.get("text"):  # the Reviewer checked what you changed
        print(review["text"])
    return 0


WAIT_FOR = 90      # figaro wait's default: under the two minutes an agent's shell gives a command
WAIT_EVERY = 0.5   # how often it asks the bridge
RELOAD = ("Figma isn't answering in \"{}\": reload the file in Figma (close its tab and open it "
          "again, or quit Figma if the tab won't close), then run Figaro in it")


def cmd_wait(args):
    """Wait until the file is ready: connected, and nothing stuck in it — no
    script that ran out of time may still be running, and its window didn't
    close mid-script. Other agents' scripts within their time don't count:
    the file's queue takes the next script after them. It asks /status,
    which every bridge has, so it works with an older one too."""
    target = TARGET
    if args.text is not None and figma_links.parse(args.text):  # `figaro wait "<link>"`: its file
        target = target or args.text
    elif args.text is not None:  # `figaro wait "<note>"`: what done was called before 1.1.0
        print("figaro: `figaro wait \"<note>\"` is `figaro done \"<note>\"` now — ran done",
              file=sys.stderr)
        return cmd_done(args)
    end = time.monotonic() + args.timeout
    said = None
    while True:
        cli_extras.forget(HOST, PORT)  # a long wait: look at the port again, it may have changed hands
        status, resp = _request("GET", "/status", timeout=10)
        state = _file_state(status, resp, target)
        if state.get("ready"):
            print(f"ready: connected, nothing stuck in \"{state['name']}\"")
            return 0
        if state.get("fail"):  # waiting won't help
            print(f"figaro: {state['fail']}", file=sys.stderr)
            if resp.get("hint"):
                print(f"   hint: {resp['hint']}", file=sys.stderr)
            return 2
        if said is None:
            said = state["why"]
            print(f"  waiting: {said}", file=sys.stderr)
        if time.monotonic() + WAIT_EVERY > end:
            break
        time.sleep(WAIT_EVERY)
    print(f"figaro: not ready after {args.timeout:g} s: {state['why']}", file=sys.stderr)
    print(f"   tell the user: {state['tell']}", file=sys.stderr)
    return 1


def _file_state(status, resp, target):
    """What figaro wait sees in an answer of /status: {ready, name}, {why,
    tell} while it is not ready, or {fail} when waiting can't help. A script
    that runs or waits in the file's queue within its time leaves it ready."""
    if status == 0:
        if resp.get("hint"):  # a sandbox keeps the command off the network
            return {"fail": resp.get("error")}
        return {"why": f"the bridge isn't answering ({resp.get('error')})",
                "tell": "Figaro's bridge doesn't start here: `figaro doctor` says why"}
    if status != 200:
        return {"fail": resp.get("error") or f"the bridge answered {status}"}
    files = [f for f in resp.get("files") or [] if isinstance(f, dict)]
    picked, error = _pick_file(files, target)
    if error:
        return {"fail": error}
    if not picked:
        lost, _ = _pick_file([f for f in resp.get("lost") or [] if isinstance(f, dict)], target)
        if lost:
            name = lost[0].get("name") or "Untitled"
            return {"why": f"Figaro's window in \"{name}\" closed while a script of "
                           f"{lost[0].get('agent') or 'an agent'} ran there",
                    "tell": RELOAD.format(name)}
        open_it = (figma_links.open_hint(target) if target else None) or (
            "open the file in Figma and run Plugins → Development → Figaro there")
        return {"why": f"{target!r} is not connected" if target else "no file is connected",
                "tell": open_it}
    name = picked[0].get("name") or "Untitled"
    stuck = next((f["stuck"] for f in picked if isinstance(f.get("stuck"), dict)), None)
    if stuck:
        return {"why": f"Figma isn't answering in \"{name}\": a script of {stuck.get('agent')} is "
                       f"still running, {stuck.get('for_s')} s past its time",
                "tell": RELOAD.format(name)}
    conns = {f.get("conn") for f in picked}
    late = [a for a in resp.get("abandoned") or [] if isinstance(a, dict) and a.get("conn") in conns]
    if late:
        return {"why": f"a script that ran out of time may still be running in \"{name}\" "
                       f"(it began {late[0].get('age_s')} s ago)",
                "tell": RELOAD.format(name)}
    return {"ready": True, "name": name}


def _pick_file(files, target):
    """The /status entries (windows) of the one file `target` names, picked
    as the bridge picks it (bridge.resolve_target), and an error when the
    target fits several files. [] when that file isn't connected."""
    def doc(f):
        return f.get("doc") or f.get("conn")

    def one(found, ambiguous):
        if not found or len({doc(f) for f in found}) == 1:
            return found, None
        names = ", ".join(repr(f.get("name") or f.get("conn")) for f in found)
        return [], ambiguous + f" — name one with -T (a link, a key or the doc id from `figaro targets`): {names}"

    if not target:
        return one(files, f"{len(files)} files connected")
    link = figma_links.parse(target)
    if link:
        return [f for f in files if f.get("fileKey") == link.key], None
    t = target.lower()
    if len(t) >= 6:
        by_doc = [f for f in files if (f.get("doc") or "").lower().startswith(t)]
        if by_doc and len({f.get("doc") for f in by_doc}) == 1:
            return by_doc, None
        by_conn = [f for f in files if (f.get("conn") or "").startswith(t)]
        if len(by_conn) == 1:
            return [f for f in files if doc(f) == doc(by_conn[0])], None
    exact = [f for f in files if (f.get("name") or "").lower() == t]
    if exact:
        return one(exact, f"{len(exact)} connected files are named {target!r}")
    by_key = [f for f in files if f.get("fileKey") == target]
    if by_key:
        return by_key, None
    return one([f for f in files if t in (f.get("name") or "").lower()],
               f"{target!r} matches several files")


def cmd_doctor(args):
    """Walk the chain bridge -> plugin -> Figma, naming the fix at each break."""
    def ok(text):
        print(f"  ✓  {text}")

    def fail(text, fix):
        print(f"  ✗  {text}")
        print(f"     → {fix}")

    status, resp = _request("GET", "/status")
    if status == 0:
        if resp.get("foreign"):  # the error says it all: another program holds the port
            fail(resp.get("error"), resp.get("hint"))
            return 1
        start = f"start it:  {cli_extras.start_hint()}"
        fail(f"bridge not answering on {cli_extras.hostport(HOST, PORT)} — {resp.get('error')}",
             resp.get("hint") or start)
        return 1
    if status == 403:
        fail(f"bridge refused the request: {resp.get('error')}",
             "a proxy is rewriting Host/Origin — talk to the bridge directly")
        return 1
    ok(f"bridge answering on {cli_extras.hostport(HOST, PORT)}"
       + (f" — Figaro {resp['version']}" if resp.get("version") else ""))
    token_file = figaro_token.token_path(PORT)
    if figaro_token.read_token(PORT) is None:
        # /status is open, so only this line tells the cause of the 401 every other command gets.
        fail(f"no token in {token_file}",
             "the bridge writes it when it starts: restart it with  "
             + cli_extras.start_hint(restart=True)
             + "  (another home folder or sandbox? set FIGARO_TOKEN_FILE to that file)")
        return 1
    stale = False
    if resp.get("bridge_outdated"):
        stale = True
        fail("bridge.py changed since the bridge started — it runs old code",
             "restart it when no other agent is using it:  "
             + cli_extras.start_hint(restart=True))

    if not resp.get("plugin_connected"):
        fail("plugin not connected",
             "in Figma Desktop: Plugins → Development → Figaro")
        return 1
    ok("plugin connected")

    current = resp.get("plugin_version")
    for f in resp.get("files") or []:
        if f.get("outdated"):
            stale = True
            fail(f"\"{f.get('name')}\" runs an older plugin build "
                 f"({f.get('plugin') or 'unversioned'}, current {current})",
                 f"figaro reload -T {f.get('fileKey') or f.get('doc')}   "
             "(or re-run it in that file: Plugins → Development → Figaro)")
    if current and not stale:
        ok(f"plugin build {current} everywhere")
    update = resp.get("update")
    if update and update.get("latest") == resp.get("skipped"):
        ok(f"Figaro {update.get('latest')} is released (this is {update.get('current')}): "
           "the user skipped it in the plugin's window")
    elif update:
        # Not a failure — everything works, there is just something newer. Not "git pull":
        # an agent's pull lands under other agents' scripts, while Update waits for them.
        print(f"  !  Figaro {update.get('latest')} is released (this is {update.get('current')})")
        how = ("Update in the plugin's window installs it" if resp.get("update_button", True)
               else "it is updated by hand here")
        print(f"     → tell the user: {how}   ({update.get('url')})")

    status, r = _exec("return 1 + 1;", 10, quick=True, probe=True)
    if status == 401:
        fail(f"the bridge refused the token in {token_file}",
             "it is from another bridge run: restart this one with  "
             + cli_extras.start_hint(restart=True))
        return 1
    if status == 409 and r.get("abandoned"):
        fail(f"file is interlocked: {r.get('error')}",
             r.get("hint") or "figaro wait -T <file> returns once that script has ended; "
             "still blocked after that:  figaro clear -T <file>")
        return 1
    if status == 409:
        # Several files connected and no -T, or an ambiguous one — the plugin is
        # fine, the request just doesn't say which file it means.
        fail(r.get("error", "target required"),
             "pick one with -T: a file name, or the doc id from `figaro targets` "
             "when two files share a name")
        return 1
    if not r.get("ok") or r.get("value") != 2:
        fail(f"round trip failed: {r.get('error', r)}",
             "close the plugin window in Figma and run it again")
        return 1
    ok(f"round trip works ({r.get('elapsed_ms', '?')}ms)")

    status, r = _exec(
        "return {file: figma.root.name, page: figma.currentPage.name, "
        "pages: figma.root.children.length};", 10, quick=True, probe=True)
    if r.get("ok"):
        v = r.get("value") or {}
        ok(f"editing \"{v.get('file')}\" — page \"{v.get('page')}\" "
           f"of {v.get('pages')}")
        print("\n  The plugin is bound to whichever file was open when you ran it.")
        print("  Switched files? Run the plugin again in the new one.")
    return 1 if stale else 0


def cmd_exec(args):
    if args.file:
        try:
            with open(args.file, encoding="utf-8-sig") as f:
                code = f.read()
        except OSError as e:
            print(f"figaro: can't read {args.file}: {e.strerror or e}", file=sys.stderr)
            return 2
        except UnicodeDecodeError:
            print(f"figaro: {args.file} is not UTF-8 text", file=sys.stderr)
            return 2
    elif args.stdin:
        code = read_stdin()
    elif args.code:
        code = args.code
    else:
        print("figaro: provide code (positional, --file, or --stdin)", file=sys.stderr)
        return 2
    libs = cli_extras.read_libs(args.lib)
    images = cli_extras.read_images(args.image)
    text = not args.raw  # --raw prints the whole reply, `result` included: the bridge sends it
    resp = _exec(code, args.timeout, libs=libs, images=images, print_result=text)[1]
    if resp.get("lib_missing"):  # the plugin lost a --lib before the script ran; now the code goes
        resp = _exec(code, args.timeout, libs=libs, images=images, print_result=text)[1]
    rc = _emit(resp, raw=args.raw)
    if rc == 0 and args.shot is not None:
        rc = cli_extras.shot_after(args, resp)
    return rc


def cmd_tree(args):
    opts = json.dumps({
        "maxDepth": args.depth,
        "showSize": not args.no_size,
        "showText": not args.no_text,
        "showLayout": args.layout,
    })
    code = (
        f"const n = {node_expr(args.node_id)};"
        f"if (!n) throw new Error('node not found: ' + {json.dumps(args.node_id)});"
        f"return h.dumpTree(n, {opts});"
    )
    return _run(args, code, quick=True)


def cmd_find(args):
    if "=" not in args.filter and "~" not in args.filter:
        print("figaro: filter must be key=value or key~value", file=sys.stderr)
        print("  forms: name=X (exact), name~X (substring), type=X, text=X (exact), text~X (substring)", file=sys.stderr)
        return 2

    # Whichever separator comes first decides the mode, so a value may contain
    # the other character (name~a=b is a substring search for "a=b").
    tilde = args.filter.find("~")
    equals = args.filter.find("=")
    substring_mode = tilde != -1 and (equals == -1 or tilde < equals)

    # H.find searches by type with findAllWithCriteria and skips layers
    # hidden inside instances unless --hidden.
    if substring_mode:
        key, value = args.filter.split("~", 1)
        if key not in ("name", "text"):
            print(f"figaro: substring filter only supports name~ and text~ (got {key}~)", file=sys.stderr)
            return 2
        query = {key + "Has": value}
    else:
        key, value = args.filter.split("=", 1)
        if key not in ("name", "type", "text"):
            print(f"figaro: unknown filter key '{key}'. Use name, type, text", file=sys.stderr)
            return 2
        query = {key: value.upper() if key == "type" else value}
    if args.hidden:
        query["hidden"] = True

    code = (
        f"const root = {node_expr(args.node_id)};"
        f"if (!root) throw new Error('node not found: ' + {json.dumps(args.node_id)});"
        f"return h.find(root, {json.dumps(query, ensure_ascii=False)});"
    )
    return _run(args, code, quick=True)


def cmd_text(args):
    code = (
        f"const n = {node_expr(args.node_id)};"
        f"if (!n) throw new Error('node not found');"
        f"if (n.type !== 'TEXT') throw new Error('not a TEXT node (got ' + n.type + ')');"
        f"const before = n.characters;"
        f"await h.setText(n, {json.dumps(args.text)});"
        f"return {{id: n.id, before, after: n.characters}};"
    )
    return _run(args, code)


def cmd_variant(args):
    props = {}
    for kv in args.props:
        if "=" not in kv:
            print(f"figaro: variant prop must be 'Property=Value', got {kv!r}", file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        props[k.strip()] = v.strip()

    code = (
        f"const n = {node_expr(args.node_id)};"
        f"if (!n) throw new Error('node not found');"
        f"if (n.type !== 'INSTANCE') throw new Error('not an INSTANCE (got ' + n.type + ')');"
        # H.variant takes short names ("Label") and "true"/"false" for a BOOLEAN
        f"await h.variant(n, {json.dumps(props, ensure_ascii=False)});"
        f"const out = {{}};"
        f"for (const k in n.componentProperties) out[k] = n.componentProperties[k].value;"
        f"return {{id: n.id, applied: {json.dumps(props)}, current: out}};"
    )
    return _run(args, code)


def cmd_clone(args):
    direction = "right"
    for d in ("left", "right", "up", "down"):
        if getattr(args, d, False):
            direction = d
    opts = {"direction": direction, "gap": args.gap}
    if args.name:
        opts["name"] = args.name

    code = (
        f"const n = {node_expr(args.node_id)};"
        f"if (!n) throw new Error('node not found');"
        f"const c = h.cloneNext(n, {json.dumps(opts)});"
        # The view moves only when asked — it is the user's screen too.
        + ("figma.viewport.scrollAndZoomIntoView([n, c]);" if args.focus else "")
        + f"return {{clone_id: c.id, x: c.x, y: c.y, name: c.name}};"
    )
    return _run(args, code)


def cmd_rm(args):
    # Cleaning up after an experiment is almost always plural.
    # Every id is found first, so a typo removes nothing; a layer already
    # gone with its parent earlier in the list is skipped. `sel` is every
    # selected layer.
    code = (
        f"const ids = {json.dumps(args.node_ids)};"
        f"const nodes = [], missing = [];"
        f"for (const id of ids) {{"
        f"  let found = [];"
        f"  try {{ found = id === 'sel' ? figma.currentPage.selection.slice() : [await h.resolve(id)]; }} catch (e) {{}}"
        f"  if (found.length && found[0]) nodes.push(...found);"
        f"  else missing.push(id === 'sel' ? 'sel (nothing is selected)' : id);"
        f"}}"
        f"if (missing.length) throw new Error('node not found: ' + missing.join(', ') + ' — nothing removed');"
        f"const removed = [], skipped = [];"
        f"for (const n of nodes) {{"
        f"  if (n.removed) {{ skipped.push(n.id); continue; }}"
        f"  removed.push({{id: n.id, name: n.name, type: n.type}});"
        f"  n.remove();"
        f"}}"
        f"if (skipped.length) print('already removed with a parent or listed twice: ' + skipped.join(', '));"
        f"return removed;"
    )
    return _run(args, code)


def cmd_import_component(args):
    code = (
        f"const comp = await figma.importComponentByKeyAsync({json.dumps(args.key)});"
        f"const inst = comp.createInstance();"
        f"figma.currentPage.appendChild(inst);"
        + ("figma.viewport.scrollAndZoomIntoView([inst]);" if args.focus else "")
        + f"return {{component: comp.name, instance_id: inst.id, w: inst.width, h: inst.height}};"
    )
    return _run(args, code)


# ─── argparse / dispatch ───────────────────────────────────────────────────

def _add_common_flags(p):
    p.add_argument("--timeout", "-t", type=cli_extras.above_zero, default=60)
    p.add_argument("--raw", action="store_true", help="print full JSON response")
    p.add_argument("--target", "-T", default=None,
                   help="which connected file to route to (name, fileKey, or substring)")
    p.add_argument("--parallel", action="store_true",
                   help="bypass the per-file lock so reads can fan out. "
                        "READ-ONLY scripts only — a parallel writer interleaves.")
    p.add_argument("--agent", "-A", default=None,
                   help="your name in the plugin's window and the file's queue (env FIGARO_AGENT)")
    p.add_argument("--queue-timeout", type=float, default=None,
                   help="seconds to wait while another caller holds the file "
                        "(default: same as --timeout); gives up with 'file busy' "
                        "without running anything")
    p.add_argument("--read-only", "-R", action="store_true",
                   help="the script must not change the file: whatever it changes "
                        "is rolled back and the call fails")
    cp = p.add_mutually_exclusive_group()
    cp.add_argument("--checkpoint", metavar="LABEL", default=None,
                    help="save a version in File → Version history before the script")
    cp.add_argument("--no-checkpoint", action="store_true",
                    help="skip the hourly checkpoint for this script")


def build_parser():
    ap = argparse.ArgumentParser(prog="figaro", description=__doc__.splitlines()[0],
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help="bridge host (env FIGARO_HOST)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="bridge port (env FIGARO_PORT)")

    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", help="the bridge's raw state, as JSON")
    sub.add_parser("targets", help="list connected Figma files (name / fileKey / conn)")

    p_clear = sub.add_parser("clear",
                             help="drop a file's abandoned-script interlock after a 504")
    _add_common_flags(p_clear)

    p_doctor = sub.add_parser("doctor", help="diagnose the bridge -> plugin -> Figma chain")
    _add_common_flags(p_doctor)

    p_sel = sub.add_parser("sel", help="what is selected in Figma right now")
    _add_common_flags(p_sel)

    p_exec = sub.add_parser("exec", help="run a JS script in the file: the Plugin API plus h.* helpers")
    _add_common_flags(p_exec)
    g = p_exec.add_mutually_exclusive_group()
    g.add_argument("code", nargs="?", help="the script, inline")
    g.add_argument("--file", "-f", help="read the script from a file")
    g.add_argument("--stdin", action="store_true", help="read the script from stdin")
    cli_extras.add_exec_flags(p_exec)

    p_tree = sub.add_parser("tree", help="the layer tree under a node: name, type, id, size")
    _add_common_flags(p_tree)
    p_tree.add_argument("node_id", help="node id, or `page` / `sel`")
    p_tree.add_argument("--depth", type=int, default=99)
    p_tree.add_argument("--no-size", action="store_true")
    p_tree.add_argument("--no-text", action="store_true")
    p_tree.add_argument("--layout", action="store_true",
                        help="show layoutMode, gap, padding and sizing modes")

    p_find = sub.add_parser("find", help="find layers under a node by name, type or text")
    _add_common_flags(p_find)
    p_find.add_argument("node_id")
    p_find.add_argument("filter", help="name=X | name~X | type=X | text=X | text~X")
    p_find.add_argument("--hidden", action="store_true",
                        help="search layers hidden inside instances too (slower)")

    p_text = sub.add_parser("text", help="set a text layer's characters (loads its fonts)")
    _add_common_flags(p_text)
    p_text.add_argument("node_id")
    p_text.add_argument("text")

    p_variant = sub.add_parser("variant", help="set an instance's variant and properties: Size=L Label=\"Pay now\"")
    _add_common_flags(p_variant)
    p_variant.add_argument("node_id")
    p_variant.add_argument("props", nargs="+")

    p_clone = sub.add_parser("clone", help="copy a layer next to itself")
    _add_common_flags(p_clone)
    p_clone.add_argument("node_id")
    p_clone.add_argument("--right", action="store_true")
    p_clone.add_argument("--left", action="store_true")
    p_clone.add_argument("--up", action="store_true")
    p_clone.add_argument("--down", action="store_true")
    p_clone.add_argument("--gap", type=int, default=100)
    p_clone.add_argument("--name")
    p_clone.add_argument("--focus", action="store_true", help="scroll Figma's view to the clone")

    p_rm = sub.add_parser("rm", help="delete layers; checks every id first")
    _add_common_flags(p_rm)
    p_rm.add_argument("node_ids", nargs="+", help="one or more node ids, `sel` for every selected layer")

    for name in ("import-component", "icomp"):
        p = sub.add_parser(name, help="an instance of a published library component, by key"
                           if name == "import-component" else "short for import-component")
        _add_common_flags(p)
        p.add_argument("key")
        p.add_argument("--focus", action="store_true", help="scroll Figma's view to the instance")

    p_undo = sub.add_parser("undo", help="undo the file's last script, when that is safe")
    _add_common_flags(p_undo)
    p_undo.add_argument("--force", action="store_true",
                        help="revert it even though another agent made it")

    p_reload = sub.add_parser("reload",
                              help="load plugin/code.js and ui.html from disk into the open plugin")
    _add_common_flags(p_reload)

    p_done = sub.add_parser("done", help="you are done in the file, for now: the plugin's window "
                                         "says so, with a note for the user, until your next script "
                                         "that can change the file")
    _add_common_flags(p_done)
    p_done.add_argument("text", nargs="?", default="",
                        help="what the user should look at or answer, in their language")

    p_wait = sub.add_parser("wait", help="wait until the file is ready: connected, and no script that "
                                         "ran out of time still runs in it; at most --timeout "
                                         f"seconds ({WAIT_FOR})")
    _add_common_flags(p_wait)
    p_wait.set_defaults(timeout=WAIT_FOR)
    p_wait.add_argument("text", nargs="?", default=None, help=argparse.SUPPRESS)  # done's old name

    cli_extras.add_parsers(sub, _add_common_flags)

    return ap


# Every command. A first word that is none of them is a script: `figaro "<js>"`.
DISPATCH = {
    "status": cmd_status,
    "targets": cmd_targets,
    "clear": cmd_clear,
    "doctor": cmd_doctor,
    "sel": cmd_sel,
    "exec": cmd_exec,
    "tree": cmd_tree,
    "find": cmd_find,
    "text": cmd_text,
    "variant": cmd_variant,
    "clone": cmd_clone,
    "rm": cmd_rm,
    "import-component": cmd_import_component,
    "icomp": cmd_import_component,
    "undo": cmd_undo,
    "reload": cmd_reload,
    "done": cmd_done,
    "wait": cmd_wait,
    "notes": cli_extras.cmd_notes,
    "inspect": cli_extras.cmd_inspect,
    "shot": cli_extras.cmd_shot,
    "link": cli_extras.cmd_link,
    "image": cli_extras.cmd_image,
    "comments": cli_extras.cmd_comments,
    "comment": cli_extras.cmd_comment,
}


def main():
    force_utf8_output()

    # Backward-compat shorthand: `figaro "<js>"` → `figaro exec "<js>"`
    if (
        len(sys.argv) >= 2
        and sys.argv[1] not in DISPATCH
        and not sys.argv[1].startswith("-")
    ):
        sys.argv.insert(1, "exec")

    ap = build_parser()
    args = ap.parse_args()

    if args.cmd is None:
        ap.print_help()
        sys.exit(2)

    global HOST, PORT, TARGET, PARALLEL, AGENT, QUEUE_TIMEOUT, READ_ONLY, CHECKPOINT
    HOST = args.host
    PORT = args.port
    if args.cmd == "exec":
        cli_extras.fix_exec_args(args)
    TARGET = cli_extras.target_and_ids(args)  # Figma links in -T and in node ids
    PARALLEL = getattr(args, "parallel", False)
    AGENT = getattr(args, "agent", None) or AGENT
    QUEUE_TIMEOUT = getattr(args, "queue_timeout", None)
    READ_ONLY = getattr(args, "read_only", False)
    CHECKPOINT = (False if getattr(args, "no_checkpoint", False)
                  else getattr(args, "checkpoint", None))

    try:
        rc = DISPATCH[args.cmd](args)
        sys.stdout.flush()
    except BrokenPipeError:
        # Whoever read the output went away (`figaro tree page | head -1`).
        # Without this Python prints a traceback, and one more at exit.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        rc = 1
    sys.exit(rc)


cli_extras.attach(sys.modules[__name__])

if __name__ == "__main__":
    main()
