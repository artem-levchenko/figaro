# Figaro — notes for contributors and coding agents

Figaro lets AI agents work in Figma Desktop. `figaro.py` (the CLI) sends a script over HTTP to `bridge.py`
(aiohttp on 127.0.0.1:8788), and the bridge passes it over a WebSocket to the Figma development plugin in
`plugin/` (`code.js` runs in Figma's sandbox, `ui.html` is the plugin's window). The plugin runs the script as
the body of an async function in strict mode that gets `figma`, `print`, `h` and `lib` (`compileScript`). Each
Figma file has its own queue: one script at a time per file, so parallel agents never interleave their edits.

How agents *use* Figaro is the skill in `skill/figaro/` (`SKILL.md` and `references/`), in the open Agent
Skills format. `tools/install.sh` (macOS, Linux) and `tools/install.ps1` (Windows) install the `figaro` command
and link the skill into `~/.claude/skills` (Claude Code) and `~/.agents/skills` (Codex and other agents).

## Setting Figaro up for someone

When a user gives you this repository's link and asks you to set it up:

1. Clone it into `$HOME/figaro`, unless they name another folder; if it is there already, `git pull` in it
   instead. (In PowerShell write `$HOME`: `~` in an argument of `git` makes a folder named `~`.)
2. Run the installer. It is safe to run again, and it never overwrites what it didn't make.
   - macOS, Linux: `bash $HOME/figaro/tools/install.sh`
   - Windows, from PowerShell or Git Bash: `powershell -ExecutionPolicy Bypass -File $HOME/figaro/tools/install.ps1`

   It needs Python 3.9+. On a Mac without it, `xcode-select --install` brings `python3`; on Windows, the
   installer from python.org with *Add python.exe to PATH* ticked; Debian and Ubuntu also need `python3-venv`.
   If the installer prints a note about PATH, follow it; until then call the command by the full path it
   printed.
3. Ask the user to do the two steps only they can do, in Figma Desktop: **Plugins → Development → Import plugin
   from manifest…** with the full path to `plugin/manifest.json`, then **Plugins → Development → Figaro** in
   the file they want to work in. The browser version of Figma can't run development plugins; on Linux the
   desktop app is the unofficial figma-linux.
4. Run `figaro doctor` and fix what it reports, until every line starts with ✓.
5. Tell them that agents get the skill in their next session, and that an agent that runs commands in a
   sandbox has to be allowed to run `figaro` outside it (README, "Let your agent run figaro").

WSL2: install inside WSL with `install.sh`. Figma on Windows reaches the bridge in WSL through `localhost`,
but imports a plugin only from a Windows folder: copy `plugin/` to one (`/mnt/c/Users/<name>/figaro-plugin`)
and import it from there, and copy it again after every update.

## Commands

```sh
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt   # once after cloning
venv/bin/python -m pytest -q             # bridge, board, update, CLI, queue, fuzz, stress; no Figma needed
/usr/bin/python3 -m venv /tmp/figaro-py39 && /tmp/figaro-py39/bin/pip install -q -r requirements-dev.txt
/tmp/figaro-py39/bin/python -m pytest -q # the same on macOS's own python3 (3.9), the oldest we support
node tests/helpers.test.js               # pure helpers of plugin/code.js on a stub of Figma
node tests/plugin.test.js                # the plugin writes nothing to the file; the document id is the fileKey;
                                         # the window opens only the project's pages, the old address as the new
node tests/audit.test.js                 # h.audit: every rule flagged and quiet, the cap, the bounded walk, h.ck()
node tests/exec.test.js                  # the exec core: change reports, Cmd+Z, read-only, undo, error lines
node tests/commands.test.js              # links, quick reads, --lib, inspect, shot, fonts, auto-layout, find
node tests/ui.test.js                    # the plugin window: islands, buttons; the tone plays only during scripts
node tests/window.test.js                # the window's height and place: grows down, moves up, comes back, scrolls
node tests/loops.test.js                 # a loop that never ends stops at --timeout; strings, comments, lines stay
node tests/variants.test.js              # h.variant: short names and #ids, "true"/"false"; h.bN with a key

export FIGARO_TEST_FILE="<link to a draft of yours>"   # the live tests need the plugin running in it
venv/bin/python tests/live_exec.py       # the exec core against real Figma, ~15 s
venv/bin/python tests/live_cli.py        # the CLI commands against real Figma, ~30 s
venv/bin/python tests/live_stress.py 20  # the file queue under load, ~10 s
                                         # each live test takes its made-up agents off the window's board at the end

bash tools/install.sh                    # `figaro` in ~/.local/bin, the skill in ~/.claude/skills and ~/.agents/skills
bash tools/install.sh --uninstall        # remove them, only if they are ours
bash start-bridge.sh                     # (re)start the bridge in the background; log in /tmp/figaro-bridge-8788.log
bash start-bridge.sh --stop              # stop it
figaro reload -T "<link>"                # load a changed plugin/ into the running plugin, no re-run by hand
```

On Windows, in PowerShell:

```powershell
python -m venv venv; venv\Scripts\pip install -r requirements-dev.txt
venv\Scripts\python -m pytest -q        # adds tests/test_install_ps1.py and a real start with start-bridge.ps1
powershell -ExecutionPolicy Bypass -File tools\install.ps1   # figaro.exe in %USERPROFILE%\.local\bin (put on PATH), junctions to the skill
powershell -ExecutionPolicy Bypass -File tools\install.ps1 -Uninstall
.\start-bridge.ps1 -Restart              # or -Stop; log in %TEMP%\figaro-bridge-8788.log
```

The bridge saves a fresh token to `~/.figaro/token-<port>` once its port is bound; every POST wants it in the
`X-Figaro-Token` header (`_guard`, which the `guard` middleware runs before every handler). `tests/conftest.py`
gives every test client the right one, and Figma's User-Agent (`FIGMA_UA`), which a socket with
`Origin: null` needs to count as the plugin; it points `FIGARO_TOKEN_FILE` at a temp file. A test of a check
removes the header from `client.session.headers`. Run a third-party script against a bridge with
`-H "X-Figaro-Token: $(cat ~/.figaro/token-8788)"`.

CI (`.github/workflows/tests.yml`) runs pytest on Linux and Windows with Python 3.9 and 3.13, the Node tests,
and both installers; nothing else tests Windows, so check the run after changing anything it touches. A push
that changes only `*.md` or `assets/` runs no tests. Keep a parametrized test's id short: on Windows pytest
puts it in the `PYTEST_CURRENT_TEST` variable, which takes at most 32767 characters.

A smoke test of the skill — a fresh Claude Code session that has only the skill and the command does a small
task in your draft:

```sh
T=$(mktemp -d) && bash tools/install.sh --bin $T/bin --skills $T/proj/.claude/skills && cd $T/proj && \
  PATH="$T/bin:$PATH" claude -p "<a small task with a link to your draft>" --setting-sources project,local \
  --max-turns 60 --allowedTools Bash Read Skill Glob Grep Write --output-format stream-json --verbose > $T/run.jsonl
```

`--setting-sources project,local` keeps a skill installed for your user (`~/.claude/skills/figaro`) from taking the
place of the one under test: `run.jsonl` names the skill's folder ("Base directory for this skill").

Then read `run.jsonl` (commands, errors, the final report) and clean up what it made in the draft.

## Where things are

- `figaro.py` — the CLI: commands, how replies are printed, `exec` flags (`-R`, `--checkpoint`,
  `--no-checkpoint`, `--lib`, `--shot`), `figaro wait` (polls `/status`, so it works with older bridges;
  `_pick_file` mirrors the bridge's `resolve_target`).
- `cli_extras.py` — links in arguments, starting the bridge on demand, `inspect`, `shot`, `link`, `image`,
  `--lib`, `exec --shot`, `exec --image` (a file is checked here: format by its bytes, 4096 px, 30 MB), and
  `probe`: before the token goes anywhere, `GET /` must answer as the bridge (`"service": "figaro-bridge"`).
  Only a sure verdict is kept (`_verdicts`, which `tests/conftest.py` clears for every test): a port that
  hangs up or stays silent is asked again. `fetch` is the CLI's one HTTP call (http.client, which takes no
  proxy; `Unreached` is a connect or a send that failed). `inspect_text.py` — the text `inspect` prints.
  `variant_diff.py` — `inspect` of a component set: from the data `h.inspect` returns, the base variant, the
  axes and what the other variants change (the `variants` key of `--json`, and the text); the CLI makes a
  second read with the hidden layers when there are any.
- `bridge.py` — HTTP and WebSocket, routing to files (a file's scripts go to its first live window, and a
  later window of the same file waits until that one closes), the queue and lock per file, Origin/Host
  checks, the API token and the plugin socket's User-Agent (`_guard`; `figaro_token.py` is the path and the
  reader both sides share), the release check and the release the user skipped (the window's Skip,
  kept in `skipped-release` next to the token), `ERROR_HINTS`.
- `bridge_board.py` — what every plugin window shows (the `board`: each file's agents — at work from their
  first script until five quiet minutes or `figaro done`, its note, a failed last script, the user's Stop —
  and recent changes; a read leaves a done or a Stop on show, and `figaro doctor`'s checks (`probe`) stay
  off it), its buttons (Stop for one agent, Update, Reload, Skip, a switch in the settings, ↻ there) and
  `POST /done`. `POST /forget {target?, agents: [name]}` takes agents off the board, and their lines in Recent,
  in every window at once; one with a script running stays. It is for tests that run under made-up names, and
  needs nothing of the window: an older bridge answers 404 and the live tests carry on (`live_file.forget`).
  A file turns red when Figma stops answering there: a script runs on `STUCK_AFTER` past its time (`stuck`), or its window closed
  mid-script (`lost`, kept `LOST_KEEP`; a window back within `LOST_GRACE` was a Hot reload).
- `bridge_update.py` — the window's Update and Reload: waits until no file runs a script, moves the folder
  to the release and never past it (`git fetch --tags`, `git merge --ff-only vX.Y.Z`), reloads the plugins
  that run older code, restarts the bridge (`os.execv`; on Windows `start-bridge.ps1 -Restart`). Scripts sent
  during the pull or the restart get a 503 and run nothing; while it waits, they run, and it waits on. It
  refuses an update with the button off or no newer release known. A clone of the repository's old address
  (`bridge.MOVED_FROM`, which GitHub only redirects) gets the new one, `REPO`, before the fetch
  (`moved_origin`); other remotes, a fork's among them, stay.
- `bridge_settings.py` — the window's settings, the bridge's for every window and kept in `settings.json` next
  to the token: Sounds (until switched, `FIGARO_SOUNDS`), Show other files, Show recent changes (`recent`;
  off: the islands show no Recent list), Reviewer (`reviewer`; off: no Reviewer row, no check on `figaro done` in any file), Save versions as agents work (off: no automatic
  checkpoint, a label still saves one). And what the settings show about the version: what is new (the newest
  release's notes from GitHub, else this version's from `CHANGELOG.md`, each item's first clause), the folder,
  when GitHub last answered (`bridge.CHECKED`; ↻ is `bridge.look_now`).
- `bridge_exec.py` — the bridge's side of exec: the fields it sends (`readOnly`, `checkpoint`, `quick`,
  `libs` — a library's code goes once, then its hash; `images` — base64 of the files `h.image` fills a layer
  with, in the body and the socket, never in an argument), when checkpoints are due, links as targets,
  `POST /undo`, `POST /reload`. `value_only` is the request's own and never goes to the plugin: the reply
  leaves out `result` when it only repeats `value` as text (a text the plugin sent itself, as for a script
  with no `return`, stays). The CLI sends it where it prints the text itself, never with `--raw`, and makes
  the text with `result_text.render`; an older bridge ignores the field and sends `result`, which the CLI
  then prints. Keep both ways working.
- `bridge_comments.py` — Figma comments and the designer's Figma token. The token is in `figma-token` next to
  the API token (`figaro_token.figma_token_path`, mode 0600): never in `settings.json`, the board, a log or a
  reply — the board's `figma` has only its masked form and how a check goes. The window's Save sends it
  (`figma-token`) and the bridge checks it on the file's version history (REST, `API`; a fake server in the
  tests, none reaches Figma). `POST /comments` lists a file's open comments from REST; without a token it
  answers 403 `needs_token` and puts the file's `ask` on the board (the island's callout; `ask-hide` is Not now,
  kept until the bridge restarts; a stored token Figma refuses with a 401, its answer to a bad token, sets the
  board's `figma.rejected`, which brings the ask back and shows the token block as refused; a 403 is a missing
  scope or a file the token's account can't open, and leaves the token as it is). A file key from the plugin's `hello` is checked like a
  link's before it goes in a REST path. `comments_text.py` is the CLI's side: the quick script that finds each
  pin's layer in the plugin, and the text it prints. A new route or field of the board needs the older
  window/bridge cases of "Rules" kept.
- `bridge_review.py` — the Reviewer, a built-in agent in each file's Agents (`board.files[].reviewer`, only
  with the settings' Reviewer on). Its switch is per file, kept by file key in `reviewer.json` next to the token,
  off by default. `bridge_board.finished` hands it the layers a script changed (`track`: the ids of `changes.top`
  and the `+` and `~` items, per agent); `figaro done` (`POST /done`) then runs `h.audit` on the top-most ones
  that still exist in a read-only built-in script through the file's queue (`board: False`, `quick`; the code is
  ours and only the layer ids are put in it), and answers with `review`. Its notes wait for the designer; the
  window's `reviewer`, `review-answer` (with the notes' `at`, so an answer to replaced notes is ignored) and the row's Stop (`stop` with agent `reviewer`) come through
  `bridge_board.handle`. `POST /notes` hands the designer's answer to the agent once (long-poll, `HOLD` per
  call); after `EXPIRE` the notes show `expired`, and a later answer still reaches `figaro notes`.
  A note is one per rule: its `text` sums up the rule's lines when they differed, and `details` (`[{text, layers: [ids]}]`,
  `detailsMore` when cut) keep each original line with its layers, and `names` (`{id: {name, type}}`) the name and type
  of each of those layers the check could read, on the board and in `/notes` (which adds `links`, a link per id the details name). The ids
  stay plain in `details`, as the 1.8.0 window reads them; a 1.8.0 bridge sends no `names`, and the id is printed alone.
  The window shows the summary and the chips; Copy for chat (`reviewerText`) and
  `review_text.py`, the CLI's side (the text `figaro notes` prints), list each line under its summary.
- `bridge_idle.py` — the bridge exits after `--idle-exit` with no plugins and no requests; on any exit it
  closes the plugins' connections at once.
- `figma_links.py` — Figma links: the file key and node id in a link, links to layers for reports.
- `result_text.py` — the text of a script's value, `render`: a string as it is, a boolean as true or false,
  anything else as JSON, indented up to 1 MB and compact past it. The bridge's `result` and the CLI's print
  come from it; it imports only `json`, so the CLI loads it without the bridge.
- `plugin/` — `manifest.json`, `code.js` (the `HELPERS` object behind `h.*`, running scripts with a deadline,
  a guard in every loop's head that ends it there (`guardLoops`), change tracking and whose change it was
  (`codeMayWrite`), undo, selecting a layer from the window, the window's size and place (`fitWindow`), and
  its view, which a file keeps on this computer (`keepView`, `clientStorage`)),
  `ui.html` (the window: one island per file where Figaro runs, in a list that scrolls, and under them a line
  that is always there: the version, with Update, Reload or a failure's "!" by it, and ⚙ Settings and ⤡
  Minimize. The settings take the islands' place (`settingsView`, `versionCard`); minimized, the window is
  one card, 240 px wide, or 70 px held (`miniView`). `glide` moves from one view to another); the Reviewer's row, its switch and its notes callout are `reviewerRow` and `reviewerAsk`, shown only when the file's board entry has `reviewer` and `settings.reviewer` isn't off).
- `skill/figaro/` — `SKILL.md` (the loop, the commands, safety, house rules) and `references/`:
  `helpers.md`, `craft.md`, `pitfalls.md`. The recipes in them were checked against real Figma.
- `tools/install.sh` — the command and the skill's links (`~/.claude/skills`, `~/.agents/skills`); never
  overwrites what it didn't make. `tools/install.ps1` does the same on Windows: `figaro.exe` is the launcher
  pip makes in the venv from `pyproject.toml` (an editable install, used for nothing else), copied to the bin
  folder — an .exe, because a .cmd file would hand Figma links to cmd.exe, which cuts them at `&`. The skill
  links are junctions, which need no admin rights.
  `start-bridge.sh` / `start-bridge.ps1` — start and stop the bridge (tmux or nohup; per-port session, log
  and pid file). tmux starts a session's command with its server's environment, so `start-bridge.sh` hands
  the `FIGARO_*` settings to the bridge by name.
- `tests/` — pytest (`test_*.py`), Node (`*.test.js`), live (`live_*.py`, with `live_file.py`), and
  `plugin_fingerprint.json`.
- `assets/banner.svg` — the README's banner, drawn by `assets/banner.py` (run it after changing the
  letters or the colours).
- `assets/hero.png` — the README's picture: the plugin's real window (`plugin/ui.html` with a made-up board)
  and notes pointing at it, shot at 2x; its corners match the banner's. It leaves out the window's last line,
  the version, so a release needs no new picture. Redo it when the window changes.

## Rules

- **`main` is what people install.** A new clone and a manual `git pull` take its tip, so no change goes
  there straight away: it lives in a branch of its own (push it, CI tests every branch), another agent reviews
  it in a fresh session, and it reaches `main` as a fast-forward once the maintainer approves, usually with a
  release. No squash: commits keep their authors.
- **Changed a command, a helper or a behaviour — update the skill in the same commit.** `tests/test_install.py`
  checks that the skill names every CLI command. The installed skill is a symlink to `skill/figaro/`, so an
  uncommitted edit there is live in every agent's session at once: don't leave it half done.
- **The skill is for any agent.** Claude Code, Codex, Cursor and others read the same `SKILL.md`, so it names
  no agent's own tools: "open the PNG", not "open it with Read".
- **Any change to `plugin/code.js` or `plugin/ui.html`** → bump `PLUGIN_VERSION` in `plugin/code.js` (date
  and counter, `2026-10-08.1`) and add its line to `tests/plugin_fingerprint.json` — the failing
  `pytest tests/test_plugin_version.py` prints it. Without that the bridge can't tell that Figma runs an
  older plugin. A changed `manifest.json` reaches Figma no sooner than the plugin's next run from its menu:
  Hot reload and `figaro reload` keep the one it started with. For a new list of `permissions` that run was
  enough (checked live); for other fields, import it again.
- **A window and a bridge of different releases meet during an update by hand** (`git pull`, then a re-run of
  the plugin or a restart of the bridge). Builds from before 1.0.0 need nothing, but the 1.0.0 window and
  bridge have no `board`: keep the bridge's `update`, `outdated` and `peers` messages, the window's view
  without a board, and `figaro done`'s answer to a 404. A window before 1.5.0 reads the board's `sounds`, not
  `settings`; a 1.5.0 window with an older bridge has no `settings` and can't switch them. A window with the
  Show recent changes switch and a bridge without `settings.recent` shows no such switch, and Recent stays; an
  older window ignores the field. The Reviewer works alike: a window sees no row when `settings` has no
  `reviewer`. The Reviewer agent (`board.files[].reviewer`, the window's `reviewer` and `review-answer`) works
  alike: an older window ignores the key, so it shows no row and nobody turns the Reviewer on; a new window with
  an older bridge gets no key and shows no row. `figaro done`'s `review` is optional.
- **`plugin/ui.html` passes the exec fields to the sandbox by name.** A new field in
  `bridge_exec.exec_fields` must be added there too, or it never arrives; `tests/test_exec.py` catches it.
- **The plugin never writes to the document.** The document id is `figma.fileKey`, which a development
  plugin gets thanks to `"enablePrivatePluginApi": true`; `tests/plugin.test.js` checks that nothing calls
  `setPluginData` on the file. Figaro runs in shared libraries too.
- **`quick` is for the CLI's built-in readers only.** The plugin doesn't track what a quick script changes,
  so code from outside must never run as `quick`. A normal exec, with or without `-R`, is safe: when its code
  only reads it answers as soon as it ends, and only a script that may write waits ~150 ms for Figma's report.
- **The CLI loads on every call, so keep its imports light.** A module that only some command needs
  (`subprocess`, `tempfile`, `http.client`, the text of `inspect`, `comments`, `notes`) is imported inside the
  function that uses it, not at the top of `figaro.py` or `cli_extras.py`; `tests/test_cli.py` checks that `-h`
  and a call to the bridge load no `subprocess`, `tempfile` or `urllib`.
- **Python 3.9** — the `python3` of Apple's Command Line Tools, what a Mac without Homebrew runs. No `match`,
  no `zip(strict=…)`, no nested same-kind quotes inside f-strings (3.12+); `X | None` only in annotations of
  modules with `from __future__ import annotations`. The 3.9 run in Commands checks it.
- **`start-bridge.ps1` and `tools/install.ps1` stay pure ASCII:** Windows PowerShell 5.1 reads a BOM-less
  script as ANSI (`tests/test_launch.py` checks). In them, don't redirect a program's stderr while
  `$ErrorActionPreference` is `Stop`: 5.1 turns it into an error that stops the script.
- **Experiment only in a draft of your own.** Figma has one Cmd+Z for the whole file, and a script can touch
  hundreds of layers. The live tests take the file from `FIGARO_TEST_FILE`.
- **Never stop the bridge with `lsof -ti tcp:8788 | xargs kill`:** that list includes Figma itself, which
  holds a connection to the bridge. Find the listener (`lsof -nP -iTCP:8788 -sTCP:LISTEN`), check that it is
  `bridge.py`, and stop only that PID — or `bash start-bridge.sh --stop`.
- **Code from other projects:** MIT code may be reused with its copyright line kept. Figma's official skills
  have no license — take ideas from them, not text.

## Releasing

Which number grows: a release with something new is a minor (1.6.0), one with fixes only a patch (1.6.1).
The major stays 1 for as long as scripts, agents and the bridge's API written for 1.x keep working, however
many releases that takes: after 1.9.0 comes 1.10.0 (the bridge compares the numbers, not the text,
`parse_version`). A 2.0.0 would mean that something agents rely on stops working.

1. Move the notes under `## [Unreleased]` in `CHANGELOG.md` to a new `## [X.Y.Z] — YYYY-MM-DD` section.
2. Set `VERSION` in `bridge.py` and `version` in `pyproject.toml` to `X.Y.Z` (`tests/test_bridge.py` checks
   both against the changelog).
3. Commit that as the last commit of the release's branch. Once the maintainer approves it, fast-forward
   `main` to the branch (`git merge --ff-only <branch>` on `main`), then tag and push:
   `git tag vX.Y.Z && git push origin main vX.Y.Z`.
4. Publish the release on GitHub with its changelog section as the notes, each bullet on one line (release
   notes turn every line break into a break on the page):

   ```sh
   awk '/^## \[X.Y.Z\]/ {f = 1; next} /^## \[/ {f = 0} f' CHANGELOG.md | perl -0pe 's/\n +(?=[^\s-])/ /g' |
     gh release create vX.Y.Z --title "Figaro X.Y.Z" --notes-file -
   ```

Every six hours a running bridge reads this repository's `vX.Y.Z` tags from GitHub (`REPO` in `bridge.py`)
and, when there is a newer one, the plugin's window offers **Update to X.Y.Z** on its last line: it installs
that tag, not the tip of `main`, and restarts the bridge and the plugins (`bridge_update.py`), so a published
tag must never move. A private repository answers 404, and the check stays silent. `FIGARO_NO_UPDATE_CHECK=1`
skips the check; `FIGARO_UPDATE_BUTTON=off` keeps it, and the window only links to the new release: the board
sends it as `release` instead of `update`, and the bridge refuses `update-now` (`bridge.update_allowed()`).

## Figma facts that cost time

- **The plugin's window connects with `Origin: null`** and a User-Agent that names the app (Electron):
  `Figma/<version>`, `FigmaBeta/` and `FigmaGov/` in those builds, `figma-linux/<version>` in lowercase in
  figma-linux. A web page can make its Origin "null" (a sandboxed iframe) but can't change its User-Agent,
  so the bridge takes a plugin socket only with both (`FIGMA_APP`). Another plugin running in Figma has
  both, if its manifest lets it reach `localhost`: it can still pose as Figaro's window.
- **Hot reload.** With Figma's *Plugins → Development → Hot reload plugin* on, any write to `plugin/code.js`
  or `ui.html` (even `touch`) restarts the plugin in every file within a second. A running script is cut off
  ("plugin disconnected mid-request") and `undo` forgets everything. Don't touch `plugin/` while agents work.
- **The window is 320 px wide (minimized 240, or 70, the narrowest Figma allows) and as tall as its islands
  and its last line**, and it stays on Figma's canvas:
  `ui.html` measures itself, and `code.js` (`fitWindow`) finds the canvas's edges from `figma.ui.getPosition()`
  and `figma.viewport`, calls `figma.ui.resize` and, when there is no room under the window, moves it up with
  `figma.ui.reposition`, given canvas coordinates; it comes back down as the window gets shorter, never past
  where the user put it, and what the user left below the canvas's bottom edge stays there. Only a window
  taller than the canvas is cut short, and its islands scroll. `getPosition` gives the window's corner both in
  window space and on the canvas, so together with `viewport.bounds` they say where the canvas starts (window
  space starts under Figma's tabs, at its window's left edge); the corner is the top of `FIGMA_HEADER` (40 px),
  Figma's header with the line under it. It reads a move at once, `getPosition` sees a `reposition` at once,
  and the window stays where it was put, between canvas units too (all checked live). But `getPosition` gives
  the corner in whole pixels and on the canvas in whole canvas units, 2.8 px at 280 %, so where the canvas
  starts comes out a little different from each place of the window: `canvasEdges` narrows a range for it over
  every look (`narrow`), and `moveTo` reads the window back right after a move, which pins it to a pixel. Taken
  from one look, it made a window held at the canvas's bottom move a pixel back and forth every second, and
  sideways at a large zoom. Figma has no event for a moved window or a resized canvas, so the window asks again
  every second, and a window that isn't where the sandbox put it was moved by the user. An island that opens or
  closes drives the window frame by frame (`drive` in `ui.html`): every frame it asks for the next height along
  the island's curve, and the fold takes the height Figma has given the window by then, as Figma moves and
  sizes the window a frame or two after it is asked. An open island's row sticks to the top only because
  `.island` has `overflow: clip`: `hidden` would make it a scroll container. Minimized or restored, the window
  keeps its bottom right corner (`corner`), so Minimize and Restore stay under the pointer.
  The header above the window (icon, title, close button) is Figma's: a plugin can put nothing there but the
  title, "Figaro Relay", which `figma.showUI` sets and only a new `showUI` changes, restarting the window. The
  menu shows the manifest's name, "Figaro".
- **Figma's theme reaches the window** through `themeColors: true`: Figma sets the `--figma-color-*` variables
  and marks `<html>` `figma-light` or `figma-dark`. The islands take their colours from the `--is-*` tokens in
  `ui.html`, which `html.figma-light` redefines: black islands in the dark theme, white ones with a hairline
  edge on a grey-blue ground in the light one.
- **The keep-awake tone.** While scripts run, the window plays an inaudible tone (20 Hz, −66 dBFS); without it
  Chromium throttles a background Figma to up to a minute per step. Meanwhile macOS doesn't sleep
  (`pmset -g assertions` shows Figma "Playing audio"). The tone stops 3 minutes after the last script ends
  (a script whose answer never comes, after its `--timeout` and those 3 minutes) or ~2 s after the bridge
  goes away — that is why the bridge closes the plugins' connections on exit (`bridge_idle.close_plugins`).
- **The window's sounds** (`CUES` in `ui.html`) play in an AudioContext of their own, apart from the tone's,
  and it sleeps 3 s after the last one, or after the pointer goes still. Waking it takes the system tens of ms
  to a few hundred (Windows the longest), and the click that asks for it goes on drawing meanwhile: a cue that
  waited for it came as the island opened, not as it was clicked. So the pointer moving or pressing in the
  window wakes it (`wakeCues`), and a cue later than `CUE_LATE_MS` is dropped. The Sounds switch (`FIGARO_SOUNDS` until it is switched) reaches the
  window as the board's `settings.sounds` and silences them, never the tone.
- **A script runs on Figma's main thread.** A loop that never yields freezes Figma, and nothing gets in while
  it runs: not the bridge's abort, not Stop in the window. So `compileScript` and `compileLib` put a guard in
  the head of every `while`, `do … while` and `for (;;)` loop (`guardLoops`, found on `codeMask`: strings,
  comments and a template's text stay as they are, and no line moves); it throws at the script's deadline.
  `for … of` and `for … in` aren't guarded: a long one needs `h.ck()`. In a WASM build of QuickJS a guard
  costs ~25 ns a round (1e6 rounds of an empty loop: 73 ms, guarded 98 ms).
- **`getNodeByIdAsync` for a layer inside an instance** (`I2:1068;2:1041`) once hung for 10 s ("Unable to
  establish connection") while `figma.getNodeById` found it. So `h.node` and the script's
  `figma.getNodeByIdAsync` ask synchronously first (`nodeHere`).
- **A Figma call that never answers ends at the deadline.** Every `*Async` call through the script's `figma`
  races `makeGate`: on `--timeout` or abort the script fails with "did not answer before --timeout ran out",
  and the bridge waits `GATE_GRACE` (1 s) more, so the agent gets that error rather than a 504 and the file
  isn't blocked. The helpers that import by key (`h.importComp`, `h.importVar`, and `h.var_`, `h.bF`, `h.bS`,
  `h.bN` with a key) call through it too (`figmaOf`); node methods (`getMainComponentAsync`) and the other
  helpers aren't covered. `figma.teamLibrary` and import by key are the usual culprits; in a draft, import by
  key may fail even for what the file already uses.
- **`documentchange` doesn't say who made a change**, only `origin` LOCAL or REMOTE: Figma's own updates
  (instances on a page that a script loads for the first time in a large file) and the user's edits during
  a script come as the script's, though they aren't its undo step — and the Plugin API has no redo. So the
  plugin reads the script's code (`codeMayWrite`): while only code that reads runs, local changes count as
  made in Figma itself — no rollback, no step for `undo`. Such a script answers the moment it ends, with no
  wait for the batch: what Figma updates after it (in a large file, hundreds of instances, reported
  ~100 ms later) counts as outside any run. A script that may write, started inside that window (`afterRead`:
  until the first batch after the last read, or `FLUSH_MS` after it; `AFTER_READ_MAX` at most), waits for it,
  or it would take that batch as its own — in its report, `-R` rollback and step for `undo`. A `--parallel`
  read that ends while it waits makes its report `shared`, as scripts that overlap do. A first undo that touches none of the script's
  layers took back someone else's step: the answer asks for Cmd+Shift+Z.
- **`documentchange` doesn't see variables:** `-R` can't roll their changes back, and `undo` refuses after a
  script that may have changed them.
- **`documentchange` does report annotations**, as a `PROPERTY_CHANGE` with `annotations` in `properties`,
  though the Plugin API typings don't list that property: `-R` and `undo` work for them (checked live). Groups,
  sections and boolean layers have no `annotations`, and a script's write there does nothing, silently.
- **Deleting a component or a component set isn't reported** in `documentchange`: it is missing from the
  report, `-R` can't roll it back, and `undo` refuses after a script that only deleted components
  (`.remove()` is in `VARIABLE_WRITES`).
- **Every `addComponentProperty` is a separate Cmd+Z step.** `undo` and `-R` go back step by step while
  traces of the script remain — created layers present, deleted ones absent (`revertSteps`) — and never past
  a step that doesn't touch its layers. Properties of an existing component can't be checked this way; the
  answer warns.
- **`undo` walks back through the scripts of this plugin run**, whoever ran them under the same `-A`. A second
  `undo` may undo someone else's cleanup — read "can still undo".
- **Checkpoints go to the file's version history**, visible to everyone with access. The time of the last one
  per file is kept in `~/.cache/figaro/checkpoints.json`; the tests use a temporary folder
  (`tests/conftest.py`).
- **The CLI starts the bridge** when nothing listens on 8788 (`cli_extras.autostart`) and waits up to 6 s for
  the plugin; another program on the port gets no token and no bridge on top of it. A background Figma window can take a minute to connect — then the first call says "run it
  again". `FIGARO_AUTOSTART=0` keeps the bridge stopped. The bridge exits after 3 idle hours.
- **`shot` captures what Figma shows:** a layer that sticks out of a frame with `clipsContent` comes out cut.
  An export also takes in what sticks out of the layer (a shadow, an outside stroke, the children of a frame
  that doesn't clip: a 200×100 card with a shadow gives 248×148), and a text layer comes out cut to its
  letters; `useAbsoluteBounds` (`--absolute`) gives the layer's own box in PNG, JPG, SVG and PDF alike.
  Cutting tall pictures into parts needs Pillow; without it there is one file and a warning.
- **Figma's official skills call `figma.createAutoLayout()`, `node.query()` and `node.set()`** — APIs of their
  MCP server only, not the public Plugin API. Check against the Plugin API typings before porting a recipe.
