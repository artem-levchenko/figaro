"""Update and Reload, from the plugin's window.

`update-now` (Update, when a newer release is out) and `reload-all`
(Reload, when this folder has newer code than what runs) come from any window.
Both wait until no file runs a script. Then an update moves the Figaro folder
to the release and nothing after it: `git fetch --tags`, then
`git merge --ff-only vX.Y.Z`, and pip when requirements.txt changed. A clone
of the repository's old address gets the new one first (moved_origin). Then every
plugin that runs older code gets the code on disk, and a bridge older than its
files restarts in place. With FIGARO_UPDATE_BUTTON=off, or with no newer release
known, the bridge refuses an update and pulls nothing.

Every window shows the step. A script sent during the pull or the restart is
refused with a 503: nothing was run, so running it again is safe. When the pull
can't be done (local changes, commits of its own, no network), the windows say
why and offer the release page.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from aiohttp import web

import bridge
import bridge_board
import bridge_exec
import bridge_idle

WAIT_MAX = 10 * 60    # seconds to wait for the files to go quiet
FAILED_FOR = 10 * 60  # how long the windows show why it failed
RELOAD_WAIT = 5.0     # for the reloaded windows to leave before the bridge restarts
RESTART_GRACE = 30.0  # for start-bridge.ps1 to replace this process

STATE = {"step": None, "what": None, "to": None}  # step: wait | pull | restart
FAILED: dict = {}                                   # {"what", "error", "detail", "to", "url", "at"}
RESTART = {"argv": None, "port": 8788}              # bridge.main() fills it in
_task = None


def board():
    """This module's part of the board."""
    out = {"updating": None, "failed": None}
    if STATE["step"]:
        out["updating"] = dict(STATE)
    if FAILED and time.time() - FAILED["at"] < FAILED_FOR:
        out["failed"] = {k: FAILED.get(k) for k in ("what", "error", "detail", "to", "url")}
    return out


def start(pull):
    """Begin an update (pull=True) or a reload, unless one is under way or the
    update is refused; the windows say why."""
    global _task
    if STATE["step"] or (_task is not None and not _task.done()):
        return False  # one under way, or a second press in the same moment
    if pull and not bridge.update_allowed():
        _fail("update", "turned off", None,
              "this bridge runs with FIGARO_UPDATE_BUTTON=off: update Figaro by hand")
        return False
    if pull and not bridge.UPDATE:
        _fail("update", "no new release", None,
              "the bridge knows of no release newer than this one: nothing was pulled")
        return False
    _task = asyncio.get_running_loop().create_task(run(pull))
    return True


def refusal():
    """A 503 for a script sent while the bridge pulls or restarts."""
    if STATE["step"] not in ("pull", "restart"):
        return None
    return web.json_response({
        "ok": False, "updating": True,
        "error": ("Figaro is updating and the bridge restarts in a few seconds. Nothing was run: "
                  "run the command again in ~10 s."),
    }, status=503)


def _set(step, what=None, to=None):
    STATE.update(step=step, what=what if step else None, to=to if step else None)
    if step:
        print(f"[update] {what}: {step}" + (f" (to {to})" if to else ""), flush=True)
    bridge_board.changed()


def _fail(what, error, to=None, detail=None):
    """`error` fits the window's row ("Couldn't update: local changes");
    `detail` says more, in the window's settings and in the bridge's log."""
    print(f"[update] {what} failed: {error}" + (f" — {detail}" if detail else ""), flush=True)
    FAILED.clear()
    FAILED.update(what=what, error=error, detail=detail, at=time.time(), to=to,
                  url=bridge.release_url(to) if to else None)
    _set(None)
    # A board once the line is old, or the windows keep it until something
    # else changes.
    try:
        asyncio.get_running_loop().call_later(FAILED_FOR + 1, bridge_board.changed)
    except RuntimeError:
        pass


def forget_failed(what):
    """Skip on a release whose update failed: the windows stop showing it."""
    if FAILED.get("what") == what:
        FAILED.clear()
        bridge_board.changed()


def _busy():
    return (any(q.get("running") or q.get("waiting") for q in bridge.QUEUE.values())
            or bool(bridge.PENDING) or bool(bridge.ABANDONED))


async def run(pull):
    what = "update" if pull else "reload"
    to = (bridge.UPDATE or {}).get("latest") if pull else None
    FAILED.clear()
    _set("wait", what, to)
    try:
        t0 = time.time()
        while _busy():
            if time.time() - t0 > WAIT_MAX:
                return _fail(what, "files stayed busy", to,
                             "agents kept running scripts for 10 minutes; try again later")
            await asyncio.sleep(0.25)
        if pull:
            _set("pull", what, to)
            failed = await pull_release(to)
            if failed:
                return _fail(what, *failed[:1], to, *failed[1:])
        stale = [(cid, i) for cid, i in bridge._live_plugins()
                 if i.get("hello") and bridge.plugin_outdated(i)]
        restart_bridge = bridge.bridge_outdated()
        if not stale and not restart_bridge:
            if pull:
                return _fail(what, "nothing new", to, f"the Figaro folder already has v{to}")
            return _set(None)
        _set("restart", what, to)
        await asyncio.sleep(0.2)  # the windows show the step before they go
        await reload_plugins(stale)
        if not restart_bridge:
            return _set(None)
        if await restart():
            await asyncio.sleep(RESTART_GRACE)  # on Windows start-bridge.ps1 ends this process meanwhile
        _fail(what, "restart by hand", to,
              "the bridge did not restart: run start-bridge.sh (or start-bridge.ps1 -Restart) in the Figaro folder")
    except Exception as e:  # never leave the windows stuck on a step
        _fail(what, "unexpected error", to, f"{type(e).__name__}: {e}")


# ─── the pull ─────────────────────────────────────────────────────────────

async def pull_release(version, folder: Path | None = None):
    """Move the folder to the release vX.Y.Z, never past it: the tip of main may
    hold changes made since. Then pip if the requirements changed. None when it
    worked, else (why in two or three words, what git or pip said)."""
    folder = folder or bridge.HERE
    if not (folder / ".git").exists():
        return "not a git clone", f"{folder} has no .git: download the release by hand"
    tag = f"v{version}"
    req = folder / "requirements.txt"
    before = _read(req)
    git = ["git", "-C", str(folder)]
    await _move_origin(git)
    # --force: without it the whole fetch fails when any local tag differs from
    # GitHub's, as in a clone older than a tag that was moved there (v1.1.0 was).
    code, out = await _run(git + ["fetch", "--tags", "--force"], 120)
    if code != 0:
        return why_pull_failed(out), _first_line(out)
    code, _ = await _run(git + ["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"], 30)
    if code != 0:
        return "no such release", f"git fetch brought no tag {tag}: download the release by hand"
    code, out = await _run(git + ["merge", "--ff-only", tag], 120)
    if code != 0:
        return why_pull_failed(out), _first_line(out)
    if out.strip():
        print(f"[update] git merge {tag}: " + out.strip().splitlines()[-1], flush=True)
    if _read(req) != before:
        code, out = await _run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req)], 600)
        if code != 0:
            return "pip failed", "the new Python packages did not install: run the Figaro installer again"
    return None


def moved_origin(url):
    """The address a clone of the repository's old one (bridge.MOVED_FROM) moves
    to, in the same form: https, ssh, with .git or without. None for any other."""
    m = re.match(r"((?:https://|ssh://git@|git@)github\.com[/:])" + re.escape(bridge.MOVED_FROM)
                 + r"(\.git)?/?$", url.strip(), re.I)
    return m.group(1) + bridge.REPO + (m.group(2) or "") if m else None


async def _move_origin(git):
    """Point a clone of the old address at the new one before it fetches: the old
    one works only through GitHub's redirect. Remotes the user set, a fork's
    among them, stay as they are, and so does origin when git can't change it:
    the fetch then goes through the redirect."""
    code, out = await _run(git + ["config", "--get", "remote.origin.url"], 30)
    new = moved_origin(out) if code == 0 else None
    if new:
        code, out = await _run(git + ["remote", "set-url", "origin", new], 30)
        print(f"[update] origin is now {new}" if code == 0
              else f"[update] origin stays at the old address: {_first_line(out)}", flush=True)


# What git says -> the few words the window has room for.
PULL_FAILURES = (
    (("would be overwritten", "commit your changes", "unmerged"), "local changes"),
    (("not possible to fast-forward", "diverg"), "local commits"),
    (("no remote repository", "does not appear to be a git repository"), "no remote"),
    (("could not resolve", "unable to access", "connection"), "offline"),
)


def why_pull_failed(out):
    low = (out or "").lower()
    for needles, why in PULL_FAILURES:
        if any(n in low for n in needles):
            return why
    return "git failed"


def _first_line(out):
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    useful = [ln for ln in lines if ln.lower().startswith(("error", "fatal"))] or lines
    return ("git: " + useful[0])[:200] if useful else "git failed"


def _read(path):
    try:
        return path.read_bytes()
    except OSError:
        return None


async def _run(cmd, timeout):
    """(exit code, output) of a command; the code is None if it could not run."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env)
    except OSError as e:
        return None, f"{Path(cmd[0]).name} did not start: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return None, f"{Path(cmd[0]).name} took more than {timeout} s"
    return proc.returncode, out.decode("utf-8", "replace")


# ─── reload and restart ───────────────────────────────────────────────────

async def reload_plugins(items):
    """The code on disk into each window, as `figaro reload` sends it. Waits
    a little for the windows to go, so the bridge restarts after they did."""
    if not items:
        return
    message = bridge_exec.reload_message()
    for _, info in items:
        try:
            await info["ws"].send_str(json.dumps({"id": str(uuid.uuid4()), **message}))
        except Exception:
            pass
    deadline = time.time() + RELOAD_WAIT
    while any(cid in bridge.PLUGINS for cid, _ in items) and time.time() < deadline:
        await asyncio.sleep(0.1)


async def restart():
    """Run the bridge's new code in this process's place. False if it can't."""
    argv = RESTART["argv"]
    if argv is None:  # not started from the command line (tests)
        return False
    print("[update] restarting the bridge with the code on disk", flush=True)
    await bridge_idle.close_plugins(None)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "nt":
        # start-bridge.ps1 stops this process and starts the new one.
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(bridge.HERE / "start-bridge.ps1"), "-Port", str(RESTART["port"]), "-Restart"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        return True
    # Same pid, same tmux pane or log: start-bridge.sh still knows this bridge.
    os.execv(sys.executable, [sys.executable, "-u", str(bridge.HERE / "bridge.py"), *argv])
    return True
