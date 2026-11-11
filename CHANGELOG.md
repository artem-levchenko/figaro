# Changelog

Notable changes, newest first. Versions follow [semver](https://semver.org/): what is versioned is what
scripts and agents depend on — the CLI, the bridge's HTTP API and the `h.*` helpers.

After an update, `figaro doctor` says what to restart: the bridge, or the plugin's code
(`figaro reload -T <file>`). If `plugin/manifest.json` changed, run the plugin again from Figma's menu in each
file; the release notes say when it has to be imported again.

## [Unreleased]

### Added

- **The Reviewer sees content that a frame cuts off: a new `h.audit` rule, `content-clipped`.** Until now only a
  text cut by its frame was a note. Now a drawing, an icon, an instance, an image or a stroke that a frame with
  *Clip content* cuts is one too: an icon half out of its card, a button's outside stroke cut by the frame it
  sits flush in, a square logo that a round 20 × 20 frame turns into a circle. What is made to be cut stays
  quiet: a photo cropped past both edges, a big backdrop shape, a fade or a glow, a mask, a frame that scrolls,
  and an avatar or a photo in a round frame (by its name). It comes right after `text-clipped`, the second
  loudest rule. It judges what shows, not the box, where Figma gives it (`absoluteRenderBounds`, as
  `text-clipped` does): an icon component with built-in padding, whose box sticks out of the frame while its
  glyph is inside, and a full-width pen line whose centred stroke stops at its ends are quiet. A layer with a
  drop shadow or a blur is judged by its box, as before, because the render bounds count the effect.
- **A note of the Reviewer can be marked On purpose.** Until now a note you didn't send came back on the next
  check. Now each note in the window has **On purpose**: the note leaves, and the Reviewer won't raise its rule
  on those layers again in that file, after a restart too. The agent's `figaro notes` lists what you marked, so
  it leaves those alone. The Reviewer's row says how many layers are marked, with a **Clear** that raises them
  again. A window shows On purpose only with a bridge that has it.

### Changed

- **`h.audit` counts the frames that clip above the layer it checks.** The Reviewer audits each changed layer on
  its own; a text cut by a frame above it, which the change didn't touch, was no note. Now it is, and so is
  content cut that way.

## [1.10.0] — 2026-11-04

### Added

- **`figaro comment` lets an agent answer in Figma comments**, with the designer's token: a new comment on a
  layer, a reply in a comment's thread (`--reply <id>`), a reaction (`--react <id>`, ✅ unless it names another
  emoji), or deleting a comment Figaro posted (`--delete <id>`). Figaro looks the layer up in the file before it
  posts, as Figma takes a pin on any id, and pins the comment to the layer's top-level frame at the layer's point:
  Figma refuses a pin on a layer inside an instance. It deletes only the comments it posted itself, though Figma
  would let the token delete any of the designer's. Figma can't resolve a comment through its API, so the skill
  tells agents to reply with what they did, react with ✅, and leave the resolving to the designer. A token that
  can only read comments gets an answer that says the designer needs a new one that can write them.
- **Sign agents' comments, a switch in the window's settings**, on until you switch it off. Figma shows a comment
  an agent posts under your account, as it comes with your token; with the switch on, each one starts with
  `[AI agent]`, so your team can tell it from yours. It names no model: any agent may post it.
- An agent that wants to post a comment and finds no token asks on the file's island: "<agent> wants to post
  comments". A window from before it says "wants to read comments", and shows no switch to sign comments.

### Changed

- The Figma token block asks for a token that can read and write comments, and read file versions.

## [1.9.3] — 2026-11-03

### Fixed

- **A layer's name in the window shows the tails of g, p and y.** In Recent and in the Reviewer's notes, the
  box of a layer's name was as tall as its letters, and it cut off their lower part: "Pricing" read as
  "Pricinq".

## [1.9.2] — 2026-11-03

### Fixed

- **Figaro lives at github.com/artem-levchenko/figaro.** The repository's old address,
  github.com/artem-levchenko-2/figaro, works only through GitHub's redirect, which lasts while no other
  repository takes that name. Now the release check, the window's links and the README use the new one, and
  the window opens a link that an older bridge still sends with the old address at the new one. From the next
  update on, the window's Update first points a clone of the old address at the new one (forks and other
  remotes stay as they are): the update to this release is still made by the bridge before it. To move a
  clone now, run this in Figaro's folder: `git remote set-url origin https://github.com/artem-levchenko/figaro.git`.
- **Figma help, in the window's Figma token block, opens Figma's article on tokens.** Since the link came in
  1.7.0, it played its click and opened nothing: the window let only the project's GitHub pages open.

## [1.9.1] — 2026-10-30

### Fixed

- **The window's sounds play as you click, not a moment later.** The sounds' audio sleeps 3 s after the last
  one, and waking it took the system up to a few hundred ms — on Windows most of all — so a cue came as the
  island it opened was opening. Now the pointer moving or pressing in the window wakes it before the click,
  and a cue that would still come more than 120 ms late is left out rather than played late.

## [1.9.0] — 2026-10-28

### Changed

- **A script that only reads answers as soon as it ends.** The plugin already read the code to tell whether a
  script can write, but still waited ~150 ms for Figma's report of changes. Now only a script that may write
  waits for it. What Figma reports while a reading script runs is told as before, in the `⚠` line; what Figma
  updates after it (in a large file, hundreds of instances on a page the script loaded) counts as made in
  Figma itself and isn't in its answer. A script that may write, started right after a read, waits for that
  report (150 ms after the read at most), so Figma's updates never end up in its change report, its `-R`
  rollback or its `undo` step. If a `--parallel` read ends while it waits, its report says that other scripts
  ran meanwhile, as when scripts overlap. Nothing changes for the bridge or the CLI. An `exec` of a script
  that only reads now takes about 90 ms instead of about 260.
- **Every `figaro` call starts about 20 ms sooner** (the CLI's own part of a call took about 100 ms). The CLI
  loads only the modules its command needs, and asks the bridge with `http.client` instead of `urllib`. The
  output and the errors are the same, and the token still goes only to what answered `GET /` as the bridge,
  never through a proxy.
- **The bridge sends a script's value once to `figaro exec` and the commands that print it.** A reply held the
  value twice: as `value`, and as `result`, the same value as text. The CLI now asks with `"value_only": true`
  where it prints the text itself, the bridge leaves `result` out, and the CLI makes the same text from
  `value`: what it prints does not change. The reply is about half the size (a 5 MB string: 10.5 MB, now
  5.2 MB). Without the field the reply is as it was, `result` included, so scripts that read the HTTP API
  see no change, and `--raw` still prints the whole reply. An older bridge ignores the field and sends
  `result`, which the new CLI prints.
- **The skill sends agents to `exec --shot` for the picture** after a script that builds or fixes, instead of
  a separate `figaro shot`: one step less. `figaro shot` stays for looking without changing anything and for
  several layers, `--width`, `--format` and `--absolute`.
- **The README tells how to skip Claude Code's auto-mode check for `figaro` commands:** the rule
  `Bash(figaro *)` in `permissions.allow` of `~/.claude/settings.json`.

### Speed

One `figaro exec` from the command line, in ms (the median of 9 calls, the same Mac and test file):

| Script | 1.8.1 | 1.9.0 |
| :-- | --: | --: |
| `return 1` | 259 | 86 |
| `return 1` with `-R` | 265 | 92 |
| Read 1,000 layers and return them (0.19 MB) | 306 | 124 |
| Read 1,000 layers and return their count | 299 | 109 |
| Return 50,000 objects (4 MB) | 491 | 297 |
| Return a 5 MB string | 592 | 438 |
| `print` 20,000 lines | 360 | 185 |
| A script that writes: waits for Figma's report, as before | 221 | 223 |
| `figaro -h` | 105 | 64 |

## [1.8.1] — 2026-10-28

### Fixed

- **The Reviewer's notes name every layer in `figaro notes` and Copy for chat.** A note that sums up several
  lines lists each line with its layers, and a layer that wasn't among the note's first five came as an id and
  a link alone. Now each comes with its name and type, as the others do. Copy for chat also leaves a blank line
  before "Fix these", as `figaro notes` does.
- **Recent names the agent the designer's answer went to.** "Sent 2 notes to designer" and "All fine, nothing
  sent to designer" said "designer" whatever the agent was called; now they say "to copywriter" for copywriter.

## [1.8.0] — 2026-10-27

### Added

- **`figaro inspect` of a component set shows what differs between its variants.** It prints the variant axes,
  one variant in full (the base: the one that has every property's default, else the first) and, for the
  others, only what is different from it: sizes, padding, auto-layout, radius, opacity, fills, strokes and
  effects with their style and variable names, text and font, layers shown, hidden or missing, swapped
  instances and their properties. Each change comes with the variants that have it, written as conditions
  (`Size=S`) when every variant with that value has it, so a set of 50 stays short. It says what it did not compare: layers below `--depth` or the layer limit, hidden layers it could not read, and a vector's path (only its size and paints are compared). `--json` and `-o` keep all
  their keys and add `variants` (the base, the axes and the differences). Other layers print as before.
- **The Reviewer agent** is the last row of every file's Agents. When an agent runs `figaro done`, it checks
  the layers that agent changed since its last check and shows its notes to the designer, not to the agent. The
  designer ticks the notes the agent should fix and sends them; the agent waits for that answer with the new
  `figaro notes -A <agent> --wait` (up to 10 minutes), fixes what was sent and runs `figaro done` again. The
  Reviewer is off in every file until its switch in the row is turned on (kept per file); the Reviewer switch in
  the settings turns it off for all files, and on by default. Agents no longer check themselves. The designer sees
  one note per rule, summed up when the rule's lines differ; the agent still gets each original line with its layers.
- **`h.audit(layer, opts?)`**, what the Reviewer runs, is a helper for when you ask an agent for a check. It
  returns notes (the rule, the
  layer's id and name, one line on what is wrong and what to do) sorted by importance, capped at 50 by rank with a
  count of the rest, and layers with the same note come as one. It only hints. Rules: a colour without a style
  or variable and text without a text style (only where the file works with them), weak contrast of text (WCAG
  AA, 4.5:1, 3:1 for large text) and of icons in controls (3:1), buttons under 24 × 24 px, text that is clipped, runs out
  of its box or has a missing font, placeholder text such as "TBC", frames with several children and no
  auto-layout, empty and zero-size layers, detached instances, default names like "Frame 12", siblings a few
  px apart, skipped heading levels and absolutely positioned content inside auto-layout. The walk is iterative
  and bounded by layers and time, so a page or a huge frame can't freeze Figma. The rules and thresholds are
  in `helpers.md`, "Checking your work".

### Changed

- **The window's indicators follow one rule.** A dot means calm: the file is ready (green), or there is no
  bridge yet (grey). A disc with a mark is an event or a request: done ✓, failed ×, stopped ■, Figma isn't
  answering !, a new release ⤓, a new build ↻, an agent that asks for comments ?, and the yellow ! of an update
  that failed or of the Reviewer's notes waiting for the designer. Motion means work under way: the pixel grid
  for an agent, the spinner for Figaro itself. A failed agent was a red dot, a stopped one a grey square, a new
  build and an ask were dots; now each is a disc. The new build's purple is a little lighter in the dark theme.
- One size for each kind: discs 11 px, dots 7 px, spinners 10 px with a 1.5 px line, in the islands, the
  agents' rows, the version and the line under the islands alike. The halos round the dots are gone.
- Every coloured indicator glows in its colour, a soft light that shows on the dark theme and barely on the
  light one; a stopped agent's grey disc too, the other grey ones and the buttons don't.
- The yellow ! of an update that failed is the same bright yellow in the light theme as in the dark one, with a
  white mark; it was a darker orange there.
- Line icons keep a 1.3 px line at any size, and the small arrows by links and buttons are 10 px.

## [1.7.0] — 2026-10-26

### Added

- **`figaro comments [<link>]`** lets an agent read the file's open Figma comments, which the Plugin API can't
  show: who wrote each, when, the replies under it, and what it is pinned to. For each comment it names the
  layer, with a link, and the deepest layer under the pin; a pin on the empty canvas of a page says so, with
  the page, the point and the nearest layers. A comment is often pinned to a frame and not to the text it talks
  about, so the skill tells agents to look among the frame's children. Resolved comments are left out, and it
  only reads. A file that isn't open in Figma can be named by its link or key: its layers then come as ids.
  It reads up to 200 open comments and says how many it left out past that.
- **A Figma token block in the window's settings**, between the switches and the version, with "Optional"
  by its title. Figma shows comments only to an app with the designer's personal access token, with the scopes
  to read comments and file versions (the same token will serve the version history later). Save checks the
  token on the file's version history before it keeps it, and says plainly when Figma didn't accept it.
  Figaro keeps the token on this computer only, in a file of its own next to the bridge's API token, readable
  by you alone; it is not in the settings file, the board, the logs or any reply, and the window shows just its
  last four characters. Remove forgets it.
- **An agent that wants comments and finds no token asks the designer on the file's island**: "designer wants
  to read comments", with Not now and Add a Figma token (opens the settings). The agent's reply says the
  designer was asked and how to go on. After Not now, `figaro comments` in that file answers at once that the
  designer chose not to add a token, and the window isn't asked again for that file until the bridge restarts.
  A window with a bridge from before it shows no token block and no ask, and `figaro comments` with such a
  bridge says to update it.
- A saved token that Figma stops accepting (expired, revoked) shows as refused in the settings, with the field
  for a new token and Remove, and the file's island asks again. A new token that works, or Remove, clears it.

### Changed

- **The window's agent icon and sounds are Figaro's own.** An agent at work shows the same 3x3 grid, now with a
  wave running along the diagonal from the top left cell to the bottom right one, once every 0.9 s. An island
  opens with a sine sweep up and closes with one down, a layer or a link clicks twice like a mechanical
  switch, and the sounds are a little quieter.

## [1.6.0] — 2026-10-26

### Added

- **`figaro image <layer> <file> [--mode fill|fit|crop|tile]`** fills a layer with a PNG, JPEG or GIF from a
  file. The file travels in the request's body, not in the command line, so a large one is fine; the CLI
  checks the format by its bytes and the size (Figma takes up to 4096 px wide and high) and says what is wrong
  before it asks Figma. The helper behind it is `h.image(node, source, mode)`; `figaro exec --image NAME=FILE`
  hands a file to a script of your own. A layer with no fills, such as a group, gets an error that says so.
- **Show recent changes**, a switch in the window's settings, after Show other files: off, an island lists its
  agents only, without what they changed lately, in every file. On by default; a folded island's line is the
  same. The bridge keeps it with the other switches. A window with a bridge from before it shows no such
  switch, and the lists stay.

### Changed

- **The skill knows more traps of Figma, each checked in a draft:** a text style replaces an existing text's
  font, size and in-line formatting; `textTruncation` is set before `maxLines`, and `maxHeight` cuts too; the
  auto-layout order is `layoutMode`, `resize()`, sizing modes (the parent and the padding can come at any
  point, the old rule was stricter than Figma), and `resize()` fixes only the axes it changes; an empty group
  deletes itself; a section keeps its size and its children's coordinates are relative to it;
  `absoluteRenderBounds` is cut by clipping parents; prototype reactions go through `setReactionsAsync`; and
  while a script runs Figma doesn't repaint, so search inside a node and never `figma.root.findAll`.

### Fixed

- **An open island always has its Agents section.** An agent's row goes an hour after its last work, and
  when only the changes were left, the island showed Recent alone, as if no agent had ever worked there. Now
  Agents stays above it with a dashed row: "No agents at work", and "No agents yet" when there are no changes
  either.
- **The token goes only to the Figaro bridge.** The CLI took any program that answered HTTP on the port for the
  bridge and sent it the token. It now asks `GET /` once per call and trusts the port only when the answer is
  the bridge's JSON (`"service": "figaro-bridge"`, which every release since 1.0.0 gives). Another program there
  gets nothing, no bridge is started on top of it, and `figaro`, `doctor` and `wait` say that the port is held
  by another program and how to see which (it has to go: the plugin connects to 8788 only). This guards against
  a stray server on the port, not against a program that copies the bridge's answer on purpose. Only a sure
  answer is remembered: a port that hangs up or stays silent (a bridge restarting) is asked again, so
  `figaro wait` keeps waiting.
- **A call that lands just as the bridge starts** asks again with the token instead of failing with 401.
- **`counterAxisAlignItems = "STRETCH"` gets its hint again** (use `h.fill`): Figma's error no longer names the
  value, and the bridge looked for it.
- **`FIGARO_HOST=::1` works:** an IPv6 address goes into the URL in square brackets (`http://[::1]:8788`), and
  `[::1]` counts as this computer for starting the bridge, like `::1`.

## [1.5.2] — 2026-10-20

### Changed

- **Settings' gear has six teeth:** with eight, at its 12 px on a screen that isn't Retina, it looked like a
  ring.

### Fixed

- **The window no longer jumps a pixel back and forth every second** when it is held at the canvas's bottom
  edge (an open island or the settings with no room under the window), up and down or, at a large zoom,
  sideways too. Figma gives the window's place on the canvas in whole canvas units (at 280 %, 2.8 px), so where
  the canvas starts came out a little different from each place of the window. The window now works it out
  from every look and from where it has just put itself, and lands on the very pixel it asks for.

## [1.5.1] — 2026-10-19

### Fixed

- **The 70 px window lines up with Figma's header:** the file's state sits under the plugin's icon, as in the
  full window; it sat 5 px to the left of it.
- **Restore stays under the pointer over the layers too:** a minimized window that sits over Figma's left
  panel or by the canvas's left edge grows to the left from its bottom right corner, as it does elsewhere, and
  stops only at the edge of Figma's window. It used to grow to the right there, away from the pointer.

## [1.5.0] — 2026-10-19

### Added

- **Settings in the plugin's window:** ⚙ on its last line shows them in place of the islands, and Back
  returns. Three switches, which the bridge keeps for every window and across restarts
  (`~/.figaro/settings.json`): **Sounds**; **Show other files**, off for this file's island and those where
  Figma isn't answering; and **Save versions as agents work**, off for no version before an agent's first
  change and none hourly (`--checkpoint "label"` still saves one). Under them the version: Update, what is new in the release, Skip,
  ↻ to look for a release now and when the bridge last did, and the bridge, the plugin's build and the folder.
  A release you skipped is still there, with its Update.
- **The window minimizes** to one card, 240 px wide, with ⤡ on its last line: this file, and how many others
  have agents at work, or a red ! when Figma isn't answering in one. Hold ⤡ and it goes down to 70 px, only
  this file's state. A click brings it back. It changes size about its bottom right corner, so the button
  stays under the pointer, and a file opens minimized next time if you left it so (kept on this computer, not
  in the file).

### Changed

- **The window's last line is always there:** the version, ⚙ and ⤡. A new release is a blue button with its
  number, which installs it, and Skip; with `FIGARO_UPDATE_BUTTON=off` the number links to the release.
  Its buttons say what they do when you point at them.
- **A failed update or reload is a yellow "!" by the version**, not a line under it. It opens the settings,
  which say in plain words what happened, with the button to try again and a copy of the error, the
  versions and the folder for your agent. Skip takes it away with the release.
- `FIGARO_SOUNDS` now says where the Sounds switch starts; once it is switched in a window, the switch counts.

### Fixed

- In the plugin's window, `figaro doctor` in "Waiting for the bridge" no longer stands a wide gap away from the
  word before it.

## [1.4.0] — 2026-10-15

### Added

- **The plugin's window plays short, quiet sounds** for what you press: a blip up when an island opens and the
  same blip down when it closes, a tick for a layer or a release link, three quick rising pulses for Update
  and Reload, a thud for Stop, a short whoosh for Skip. And two low knocks when Figma stops answering in the
  window's own file, heard once, not in every window. The blip and the tick are Beautiful UI's interaction
  sounds; the others are made of the same parts. `FIGARO_SOUNDS=off` for the bridge turns them all off.

### Changed

- **An agent at work shows a pixel grid** instead of the spinner: a 3x3 grid with a wavefront driving right,
  after Beautiful UI's Loading State, and the word "working" shimmers. Every grid and shimmer in the window
  keeps one beat, wherever and whenever it was drawn. What Figaro itself does, connecting, updating or
  restarting, keeps the spinner.
- **An open island with no agent and no change yet** isn't blank any more: where the first agent will show, the
  faint outline of an agent's row says "No agents yet" and to give an agent a link to the file.

## [1.3.0] — 2026-10-15

### Added

- `figaro wait [-T <file>] [--timeout S]` waits until a file is ready: connected, and no script that ran
  out of time still runs in it. Other agents' scripts within their time don't count, as the file's queue
  runs the next script after them. It gives up after 90 s, under the two minutes an agent's shell allows a
  command, with a line for the agent to pass on to the user. It works with older bridges too. The skill has
  agents use it instead of retrying in a loop, and say a timeout to the user first.

### Changed

- **The window's last line shows only when there is something to do:** a new release (Update, or a link with
  `FIGARO_UPDATE_BUTTON=off`), a new build of the plugin (Reload), or why an update or a reload failed, which
  now comes under the line, like a field's error; after a failed reload the line offers Reload again. While
  the pointer is on the window, **Skip** hides a release until a newer one, in every window and across
  restarts (the bridge keeps it in `~/.figaro/skipped-release`), and agents are no longer asked to tell the
  user about it. Update has a download icon, Reload a circular arrow. `figaro doctor` still shows the version.

### Fixed

- **The plugin's window stays on the canvas.** With a long island open, or many files, it grew past the bottom
  of the screen, and what was under the edge couldn't be reached. Now an island that opens with room under the
  window grows down; with no room, the window moves up instead, just enough and no higher than the canvas's
  top, and it goes back where you put it when the island closes, unless you moved it meanwhile. The island
  opens and closes with the window, frame by frame, so nothing jumps, and the islands fill the window on the
  way, whichever edge moves. What you dragged below the canvas's bottom edge stays out of sight there. Only a
  window taller than the whole canvas is cut short, and its islands scroll as one list: an open island's row
  stays at the top while its panel scrolls under it, and the scrollbar is thin and shows only while the list
  scrolls or the pointer is on it. The line with the version, when there is one, comes last, at the window's
  bottom.
- **A loop that never ends no longer holds Figma.** A script runs on Figma's main thread, so a `while (true)`
  froze Figma, nothing could stop it, and the file stayed busy. Now every `while`, `do … while` and `for (;;)`
  loop of a script or a `--lib` file ends at the script's `--timeout`: "the loop on line N was still running
  when --timeout ran out — the script was stopped", and the file is free. Strings, comments and line numbers
  in errors stay as they were; a round costs ~25 ns more. `for … of` loops are left as they are: call `h.ck()`
  in a long one. Its error, like every other, shows only the lines of the script and its `--lib` files.
- **A file where Figma stops answering turns red** in every window, with what to do: reload the file (or quit
  Figma, if its tab won't close) and run Figaro in it. Before, it looked busy forever, or vanished from the
  board when its window closed mid-script.
  It turns red when a script runs on 30 s past its `--timeout`, or when the file's window closes while a
  script ran and doesn't come back within 10 s. The red goes once the script ends (or `figaro clear`), or
  the window is back; a closed window's file leaves the board after 15 minutes. `figaro status` shows both
  (`stuck`, `lost`).
- After a 504 the file's next script gets a 409 that now says what to do from a shell: `figaro wait -T
  <file>` until the abandoned script ends, and `figaro clear -T <file>` if the file stays blocked after
  that. It named only the HTTP API (`POST /clear`, `"force": true`), which `figaro doctor` repeated.
- A script that imports by key (`importComponentByKeyAsync`, `importVariableByKeyAsync`, `h.importComp`,
  `h.importVar`, …) counted as one that writes, so changes Figma made while it ran were rolled back by `-R`
  as the script's. Importing changes no layer: such a script reads, and those changes are reported instead.

## [1.2.2] — 2026-10-12

### Security

- **Only Figma's desktop app can connect as the plugin.** While the bridge ran, a web page could open the
  plugin's socket from a sandboxed frame (`Origin: null`): see the names of the open files and the agents'
  notes in the window, press Stop or Update, and pose as a file to take agents' scripts and send back false
  answers. Now the socket also needs Figma's User-Agent, which a page can't change. A file's scripts go to
  the window that connected first, while it is open; the window's buttons count only from a window that said
  which file it is; an answer counts only from the window its script went to. Another plugin running in
  Figma, if its manifest lets it reach `localhost`, can still pose as the window.
- **`figaro` never goes through a proxy to the bridge.** With `http_proxy` or `HTTP_PROXY` set, every command
  failed, and the token and the script went to the proxy. `start-bridge.sh` asks the bridge directly too.

### Changed

- **Scripts and `--lib` files run in strict mode.** A write that can't happen fails instead of doing nothing:
  `node.fills[0].color = …` says `'color' is read-only`, `node.width = 100` says `no setter for property`.
  A variable needs `const` or `let` before its first use. The hints say what to do in each case.
- `figaro rm sel` removes every selected layer, as `shot` and `link` take them. Before, it removed the first.

### Fixed

- `h.frame` left an axis hugging when its number was 100, the size of a new frame: `{layout: "V", w: 200,
  h: 100}` came out 200 × 15. An axis with a number is FIXED now.
- `figaro clone page` copied the whole page and then failed, leaving the copy. `h.cloneNext` refuses a page
  now. In auto-layout the copy goes right after the original, not to the end. `RIGHT` works as `right`, and
  an unknown direction fails.
- A `-R` script took a write named in a comment or a string (`// .remove()`) for a real one, so changes Figma
  made meanwhile were rolled back as the script's, and `undo` refused. `n.remove?.()` and a write inside a
  template's `${…}` count as writes now.
- `print(error)` printed `{}`. It prints the error now, and an object with a cycle or a BigInt no longer
  fails the script.
- The keep-awake tone stopped 3 minutes after a script began, even while it still ran. It plays until the
  last script ends, and 3 minutes more.
- Several commands at once, with no bridge running, each started one. Now one starts it and the others wait.
- An emoji cut in half in a note left every plugin window without its board. A failed Update's line stays
  10 minutes, as meant, and two presses at once start one update.
- A plugin window that sent odd fields made `/status`, `/targets` and `/exec` answer 500. A malformed body
  or a token that isn't text gets a 400 or a 401.
- `h.find` with `textHas` failed on layers that aren't text, and `hidden: true` missed layers after a script
  had turned on `figma.skipInvisibleInstanceChildren`. `padding` takes the CSS forms `[top, horizontal,
  bottom]` and `[top, right, bottom, left]`. `tree` keeps a text with line breaks on its layer's line.
- Commands end with a message, not a traceback: `exec -f` with a missing or non-UTF-8 file, `-o` into a
  folder you can't write, `--tile 0`, `--timeout -5`, an empty `FIGARO_PORT`, a token file that isn't text,
  output into a closed pipe (`figaro tree page | head -1`), a lost connection, an answer that isn't JSON.
- `shot` deleted every file in its folder whose name began with the layer's id. It deletes only its own old
  pictures now.
- `--lib` and `exec -f` lost the first line of a file saved with a byte order mark.
- `figaro link` didn't print the script's notes and logs.
- `FIGARO_NO_UPDATE_CHECK=0` turned the release check off, as any value did. `0`, `false`, `no` and `off` leave
  it on now.
- `start-bridge.sh` said FAILED without `curl`, though the bridge was up.
- The skill: what `sel` means in each command, the flags each command takes, `--shot` before inline code,
  and which script `undo` takes back.
- The README's first script failed on its first line: `h.node` doesn't take `"page"`, `h.resolve` does. It
  uses `figma.currentPage` now.

## [1.2.1] — 2026-10-12

### Changed

- The skill says each rule once (#3): the house rules live in `SKILL.md` and `craft.md` keeps the recipes
  that follow them; what `SKILL.md` already says left `pitfalls.md` and `helpers.md`. The skill is 59 lines
  shorter, and no rule has two versions.
- Cleaner code, with no change in what Figaro does (#3): the bridge checks every request in one place, so a
  new endpoint can't go without the check; the first copies of `h.withFonts`, `h.setText`, `h.variant` and
  `h.sel`, which never ran, are gone, and so is the code for plugin builds from before Figaro 1.0.0.
- The plugin asks Figma for nothing but the team library (#3): `currentuser`, `activeusers` and `fileusers`
  are gone from `plugin/manifest.json`, so a script can't read who is in the file. Nothing used them. Figma
  takes the new list when the plugin is run again from its menu: after the update, close the plugin's window
  and run Plugins → Development → Figaro once in each file. Update, Reload and `figaro reload` keep the old
  list, and nothing breaks meanwhile. There is no need to import the plugin again.

### Fixed

- An import by key in `h.importComp`, `h.importVar`, `h.var_`, `h.bF`, `h.bS` or `h.bN` stops at `--timeout`
  when Figma never answers it, as the script's own `figma` calls do (#3). Before, the agent got a 504 and the
  file stayed blocked until `figaro clear`.

## [1.2.0] — 2026-10-10

### Added

- `FIGARO_UPDATE_BUTTON=off` takes the Update button out of the plugin's window (#2): the window only says
  that a new release is out and links to it, and the bridge refuses to update. The release check,
  `figaro doctor` and Reload work as before.
- `figaro shot --absolute` and `h.shot(node, {absolute: true})` export a layer at exactly its own size (#2).
  Without it a shadow, an outside stroke or children that overflow a frame make the picture larger, and a
  text layer comes out cut to its letters: that matters when the picture is reused inside another mockup.
- `h.annotations(scope)` lists the Dev Mode annotations under a layer, the selection or the page (#2): the
  frame, the layer, whether Dev Mode shows it, the labels and the pinned properties. The pitfalls gain an
  Annotations part, checked in Figma: why a second annotation fails with `Only one of label or labelMarkdown
  should be given`, and which layers can't have annotations at all.

### Changed

- **The bridge's HTTP API wants a token.** Each start of the bridge makes a secret and saves it, for the user
  alone (mode 0600), in `~/.figaro/token-<port>` — `FIGARO_TOKEN_FILE` names another file, for the bridge and
  the command both. `POST /exec`, `/clear`, `/undo`, `/reload` and `/done` answer 401 unless the header
  `X-Figaro-Token` has it, so a web page or another local program that can't read the file can no longer run
  code in your Figma file. `figaro` sends it by itself; a third-party client reads the file and sends the
  header (`curl -H "X-Figaro-Token: $(cat ~/.figaro/token-8788)" ...`). `GET /status`, `/targets` and `/`, and
  the plugin's socket, stay open; `figaro doctor` says when the token is missing or refused.
- **Update installs exactly the release it names.** It used to `git pull` the tip of `main`, which could hold
  changes made after the release; now it fetches the tags and moves the Figaro folder to that release, never
  past it. The bridge updates only to a release it knows is newer, and refuses otherwise.
- The note about a new release, in command answers and in `figaro doctor`, asks the agent to tell the user
  instead of pulling: an agent's pull lands under other agents' scripts, while Update waits until none runs.

### Fixed

- A bridge that `start-bridge.sh` runs in tmux gets the `FIGARO_*` settings even when a tmux server was
  already running: tmux starts a session's command with its server's environment, so `FIGARO_TOKEN_FILE`,
  `FIGARO_UPDATE_BUTTON` and the rest, set after the server started, never reached the bridge.

## [1.1.2] — 2026-10-09

### Fixed

- A read-only script (`-R`) no longer rolls back what it didn't change (#1). Figma reports every change made
  while a script runs without saying who made it: the instances it updates when a script first loads a page
  of a large file, or the user's own edits meanwhile, came as the script's, and the undo meant for them took
  back the last step of the user or another agent. Figaro now reads the script's code: while code that only
  reads runs, what changes in the file is told in a `⚠` line and left alone, and `undo` doesn't take it for
  the script's step. If an undo takes back a step that wasn't the script's, the answer says so and asks for
  Cmd+Shift+Z in Figma, which brings it back.

## [1.1.1] — 2026-10-09

### Changed

- The plugin's window shows every file where Figaro runs, quiet or not: its own file first, then the others in
  the order they connected. Before, another file showed only while agents worked there and ten minutes after.
- In the plugin's window an agent's name is a little heavier, and the note it leaves is grey like its status,
  so the names stand out.

## [1.1.0] — 2026-10-09

### Added

- **Islands in the plugin's window.** The slim bar became a window with one island per file, black in
  Figma's dark theme and white in its light one: its own file first, then each file where agents are at work,
  until ten quiet minutes after their last script. Open an island for its agents and the recent changes with
  their layers (a click on a layer of this file selects it and zooms to it); it unfolds and moves the islands
  under it down. Each agent has one row that says what it is doing: at work from its first script until five
  quiet minutes pass or it says it is done, done with its note, stopped by you, or failed when its last script
  failed. The error of its last script stays under its name until a script of its succeeds. The window grows
  and shrinks with its islands, and its last line shows the Figaro version.
- **Stop** on the row of an agent at work, shown while you point at the row. Its running script ends and fails
  with a 409 that says the user pressed Stop; pressed between its scripts, its next script in the file is
  refused with that 409 and runs nothing. The other agents go on.
- **`figaro done "<note>"`**: an agent says it is done in a file, for now. Its row shows the note until its
  next script there that can change the file: a last look with `shot` or `link` leaves it.
- **Update.** When a new release is out, an **Update to X.Y.Z** button appears next to the version. Once no
  file runs a script, the bridge pulls the release (`git pull --ff-only`), reloads the plugins and restarts
  itself; scripts sent meanwhile get a 503 and run nothing. When it can't pull (local changes, local commits,
  no connection), the window says why and links to the release. **Reload** in the same place does the same
  without the pull, when the code on disk is newer than what runs.

### Changed

- The plugin's window is titled **Figaro Relay**. In Figma's menu the plugin is still **Figaro**.
- A failed script no longer flashes the bar red for two seconds: the window lists it with the file's recent
  changes and puts the error under the agent's name, and an agent that goes quiet after one shows as failed.

### Upgrading from 1.0.0

- Run `git pull` in the Figaro folder, restart the bridge (`bash start-bridge.sh`, on Windows
  `.\start-bridge.ps1 -Restart`) and run the plugin again in each open file, or `figaro reload -T <file>`.
  From now on the window's **Update** button does it for you.

## [1.0.0] — 2026-10-08

The first release of Figaro.

### Added

- **The `figaro` command**: `exec`, `inspect`, `shot`, `tree`, `find`, `sel`, `link`, `text`, `variant`,
  `clone`, `icomp`, `rm`, `undo`, `reload`, `doctor`, `targets`, `status` and `clear`. Figma links work
  wherever a file or a layer is expected.
- **The bridge**: one queue per Figma file, checkpoints in the file's version history, a change report for
  every script, read-only runs that roll back, a guarded `undo`, hints for known errors. The CLI starts it on
  demand (`start-bridge.sh`, or `start-bridge.ps1` on Windows), and it exits after three idle hours. When an
  agent's sandbox keeps the CLI off the network, the error says so.
- **The Figaro plugin** with thirty `h.*` helpers for auto-layout, text and fonts, variables, components and
  variants, and a deadline for every script.
- **An agent skill** (`skill/figaro`) in the open Agent Skills format, for Claude Code, Codex, Cursor and
  other agents, and installers that install the command and link the skill into `~/.claude/skills` and
  `~/.agents/skills`: `tools/install.sh` for macOS and Linux, `tools/install.ps1` for Windows. An agent can
  set Figaro up from the repository's link: `AGENTS.md` tells it how.
- **macOS, Windows and Linux.** On Windows the command is a real `figaro.exe`, so Figma links with `&` pass
  through PowerShell, cmd and Git Bash; scripts on stdin are read as UTF-8, and pictures go to
  `%TEMP%\figaro\shots`. On Linux, Figma's desktop app is the unofficial figma-linux. CI runs the tests on
  Linux and Windows.
