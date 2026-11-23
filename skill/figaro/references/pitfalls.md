# Pitfalls of Figaro and the Figma Plugin API

Read the `hint:` in the answer first: the bridge knows most of the cases below.

## Connection and the queue

| What you see | What it is | What to do |
|---|---|---|
| `connection: [Errno 1] Operation not permitted` | your own sandbox keeps commands off the network, 127.0.0.1 included (Codex, Cursor) | run `figaro` outside the sandbox: ask the user to approve that, and to allow `figaro` for good |
| `missing or wrong X-Figaro-Token` (401) | the command didn't find the token the bridge saved when it started: it runs with another home folder (a sandbox, another user), or the file was removed | nothing ran. `figaro doctor` names the file and what to do; in a sandbox, run `figaro` outside it |
| `port 8788 on 127.0.0.1 is held by another program, not the Figaro bridge` | another program listens on the bridge's port and didn't answer `GET /` as the bridge does. The command sent it nothing (no token) and started no bridge | nothing ran. Tell the user: `lsof -nP -iTCP:8788 -sTCP:LISTEN` shows who listens; it has to stop, since the plugin connects to 8788 only. Don't kill it yourself |
| `plugin not connected` (503), "open … in Figma and run … Figaro there" | the plugin is not running in that file | tell the user: open the link in Figma and run Plugins → Development → Figaro (⌘⌥P repeats the last plugin). Then `figaro wait -T …`, not a loop of retries |
| `N files connected — specify a target` (409), "ambiguous" | several files, and no target or an ambiguous one | `-T` with a key or a link; `figaro targets` |
| `file busy` (503) | another agent has held the file longer than `--queue-timeout` | nothing ran — retry later; the message says who holds it |
| `Figaro is updating` (503) | the user pressed Update in the plugin's window: the bridge pulls the new version and restarts | nothing ran — run the command again in ~10 s |
| `(waited 4.2s in the file's queue)` | you waited in the queue | normal; keep your scripts short so others don't wait for you |
| 504 timeout | the script didn't finish within `--timeout` — most often a long `for … of` loop without `h.ck()` | it may still be running and writing: tell the user in your first line. `figaro wait -T …` returns once it has ended (the file answers 409 until then); look at the result with `inspect` or `shot` before you retry, and use smaller batches. `figaro clear -T …` only once you know it has ended |
| `Figma isn't answering in "…"` (from `figaro wait`; the file is red in the plugin's window) | a script there runs on long past its time, or the plugin's window closed while one ran | tell the user in your first line: reload the file in Figma (close its tab and open it again, or quit Figma if the tab won't close), then run Figaro in it. Don't retry until `figaro wait` says it is ready |
| `the loop on line N was still running when --timeout ran out — the script was stopped` | a `while` or `for (;;)` loop never ended: its condition stayed true | the file is free. What the script changed before stays: look at it, or `figaro undo`. Fix the condition, so that what it tests changes each time round |
| `figma.…() did not answer before --timeout ran out — the script was stopped` | Figma never answered the call on the line it names | the file is free, but don't repeat that call. It happens with `figma.teamLibrary` and import by key — take what the file already has by id from `figaro inspect` |
| `--timeout ran out while the script waited for figma.…()` or `the loop on line N waited for Figma most of its time`, then `of the 181 s it ran, its calls to Figma took …` | many calls that each answered used the time up; the line named is only the one that ran last | the file is free. Split the work into scripts of a few dozen calls each, or give a longer `--timeout`. An import by key takes seconds (41 components and 27 styles took 181 s): import each one once, and take what the file already has by id |
| `plugin disconnected mid-request` | the plugin restarted (Figma restarted it after a code change, or the user closed its window) | check what the script managed to do (`inspect`) and run it again; `undo` remembers nothing from before a restart |
| `⚠ … runs an older build — figaro reload` | Figma runs old plugin code | `figaro reload -T <file>`; if that fails, ask the user to press Reload in the plugin's window or to restart the plugin |
| `⚠ Figaro X is released (this is Y) — tell the user` | a new release is out, and the user hasn't skipped it in the window | tell the user once, in your report. Don't update Figaro yourself unless they ask: the window's Update waits until no agent's script runs |
| the first call is slow, "the plugin has not connected yet" | the bridge has just started, and a Figma window in the background takes up to a minute to connect | retry in a few seconds |
| `zsh: = not found` | the command line starts with `=` (`echo ======`) | use `echo ---` as a divider |

## Ids and files

- **`no node 12:34 in "…"`** — a layer from another file (check `-T`) or a deleted one. The id in a link is
  `node-id=12-34` → `12:34`; Figaro understands both.
- **Ids inside an instance** (`I12:3;4:5`) change when the instance is detached or gets another component.
  After `detachInstance()` or `swapComponent()` find layers again from a stable parent.
- **Search by id, not by name**, when other people edit the file: a name may change between your calls.
- `findOne(n => n.name.startsWith("06"))` also finds a text "06" — limit the search to `root.children` or a
  `type`.

## Auto-layout

The helpers `h.frame`, `h.fill`, `h.hug`, `h.fixed` and `h.wrapText` already do everything in the right
order. If you write it by hand:

- **The order**: `layoutMode` → `resize()` → sizing modes. Padding, spacing and alignment can be set at any
  point, and so can `appendChild` — before `layoutMode` or after, with children already inside (checked live:
  none of it was lost). What is lost is a size: turning `layoutMode` on makes the main axis HUG, so a frame
  resized to 200×100 first ends up 200 wide and as tall as its children with `"VERTICAL"`, and 100 tall and
  as wide as its children with `"HORIZONTAL"`. `resize()` after the mode, then HUG or FIXED.
- **`resize()` fixes the axes whose size it changes.** `resize(200, 100)` on a hugging frame fixes both;
  `resize(200, node.height)` fixes the width and leaves the height hugging. Same for a text: a new width alone
  gives `textAutoResize = "HEIGHT"`, a new height alone `"NONE"`. On a FILL child, the axis it changes turns
  FIXED and the other keeps FILL. Size first, modes after; or `h.fixed(node, w, null)`.
- **FILL** — only for a child of an auto-layout frame, and only after `appendChild`: before it, Figma throws
  "node must be an auto-layout frame or a child of an auto-layout frame". A FILL child of a HUG parent
  collapses: give the parent FIXED or FILL.
- **HUG** — only for an auto-layout frame or a text.
- **`counterAxisAlignItems` has no `STRETCH`**: to stretch a child across, `h.fill(child, "x")`.
- **The text turned into a column one character wide** — its width is 0: `h.wrapText(text)` in auto-layout or
  `h.wrapText(text, 320)`. Check `width > 0` afterwards.
- **A FIXED frame clips new children** when it has `clipsContent`: after adding them, HUG on that axis.
- **An absolute child** (`layoutPositioning = "ABSOLUTE"`) can't FILL; badges and overlays — yes, the rest —
  no.
- **Don't mix** `layoutSizingHorizontal/Vertical` (`FIXED`, `HUG`, `FILL`) with `primaryAxisSizingMode` /
  `counterAxisSizingMode` (`FIXED`, `AUTO`): they are two ways of saying the same thing.

## Text and fonts

- **`unloaded font`** — the font isn't loaded: `h.setText`, or `h.fonts(node)` before editing text
  properties.
- **`could not be loaded`** — this font isn't installed on this computer. `h.fonts(node)` tells you which are
  missing.
- **Icon fonts** (Font Awesome and the like): an icon is a text holding the glyph's name (`chart-network`);
  in a font that isn't installed it shows as letters stacked in a column, "S O L I D". The fix is to switch
  `fontName` to an installed icon font and leave the text alone. `h.fa()` lists the installed Font Awesome
  fonts.
- **Mixed fonts** (`fontName === figma.mixed`) — `h.setText` and `h.fonts` load them; new text takes the
  first character's style. To keep the formatting of parts, edit by ranges (`helpers.md`).
- **Text in an instance**: when the component has a TEXT property, change it with
  `h.variant(inst, {Label: "…"})`, not through `characters` inside — that keeps the link to the property.
- **Timeouts on bulk text** (2 to 5 minutes in past runs): ~25 layers per script, and fonts once at the start
  (`h.fonts(root)`).
- A text style's font must be loaded too before you set the style: `h.fonts([text.fontName,
  style.fontName])`.
- **A text style replaces what a text already has.** On a text with its own look, `setTextStyleIdAsync`
  swaps the font, size, line height, letter spacing, case and decoration for the style's, and flattens
  formatting inside the line (a bold word turns regular); a new text just takes the style whole. So on an
  existing text attach a style when its properties already match, or when changing them is the point.
  Compare the main ones first:
  `["fontName", "fontSize", "lineHeight", "letterSpacing", "textCase", "textDecoration", "paragraphSpacing",
  "paragraphIndent", "listSpacing", "leadingTrim"].every((k) => JSON.stringify(text[k]) ===
  JSON.stringify(style[k]))`. A text with mixed ranges (`getStyledTextSegments` returns more than one
  segment) keeps its formatting only without the style: leave it and say that no style fits.
- **`textTruncation` and `maxLines` cut a text that has more lines than the limit, and the box follows.**
  Checked on `WIDTH_AND_HEIGHT`: a text of three `\n` lines, 45 px tall, with `"ENDING"` and `maxLines = 2`
  came out 30 px; a one-line text has nothing to cut, so nothing seems to happen. `maxHeight` cuts the same
  way (and switches truncation to `ENDING` itself, leaving `maxLines` `null`), but only on a text inside an
  auto-layout frame: elsewhere it throws "Can only set maxHeight on auto layout nodes and their children".
  Changing `textAutoResize` afterwards resets truncation and `maxLines` (`DISABLED`, `null`), and a text
  that grew to 700 px keeps that width. Set truncation before `maxLines`: `"ENDING"` makes `maxLines` 1,
  and a `maxLines` set while truncation is off is dropped without an error. On `NONE` truncation turns the
  mode into the legacy `TRUNCATE`, and `maxLines` stays `null`. For a paragraph that wraps:

  ```js
  await h.wrapText(title, 240);      // HEIGHT, 240 px wide (in auto-layout: no width, it fills the parent)
  title.textTruncation = "ENDING";   // first: it sets maxLines to 1
  title.maxLines = 2;                // then the number of lines — checked: two lines, 30 px tall
  ```

## Fills, variables, styles

- **`'color' is read-only`**, **`object is not extensible`** — `fills`, `strokes`, `effects` and the paints in
  them are frozen: copy the array (`JSON.parse(JSON.stringify(node.fills))`), change it and assign it whole.
  To bind — `h.bF`/`h.bS`.
- **`setBoundVariableForPaint` returns a new paint** — it has to be assigned; `h.bF` does that itself.
  On the layer, a paint bound to a colour variable takes the colour's alpha as its opacity: a fill at 10%
  bound to an opaque colour is at 100% (`h.bF` and `h.bS` print a `⚠` line when that happens). An opacity
  set on the bound paint afterwards holds only until the variable's value changes. For a fill at 10%, bind a
  variable whose colour has that alpha; the layer's `opacity` would fade the whole layer, not only the fill.
- **Empty `fills`** — nothing to bind: first `node.fills = h.solid("#000")`, then `h.bF(node, 0, v)`.
- **A fill's opacity** lives in `paint.opacity`, not in the variable's colour: don't lose it when you
  overwrite the fill.
- **What the file already has — by id, not by key.** The components, styles and variables (its own and its
  libraries') that the file's layers use, `figaro inspect` prints with their ids:
  `(await h.node(id)).createInstance()`, `setFillStyleIdAsync(id)`, `h.bF(node, 0, id)`. Import by key takes
  only what is published in a library enabled for the file. In a draft it may fail even for what the file
  already uses: `importStyleByKeyAsync` never answers, and a variable fails with
  `could not find variable with key`.

## Components

- **`New parent is an instance`** — an instance can't take new layers. A one-off state — `inst.detachInstance()`
  and edit inside (a house rule); a change for everyone — in the main component.
- **`clone()` loses `componentPropertyReferences`** — hook the properties up again after cloning.
- **A nested instance's properties don't show on the parent** until `nested.isExposedInstance = true`.
- **`addComponentProperty` returns the full name** (`Label#12:0`) — keep it, don't guess it.
- **After `combineAsVariants` the variants lie on top of each other** — lay them out in a grid.
- **`Could not find a component property with name`** — `setProperties` wants the full name with the emoji
  and the `#id` (`✒️ Label#164:0`; `inspect` of the set prints them). Use `h.variant(inst, {Label: "…"})` or
  `figaro variant`: they find the full name from a short one (`Label`) or from the `#id` (`Label#164:0`).
- **`resetOverrides`** is deprecated — use `removeOverrides`.
- **Figma records every `addComponentProperty` as a separate Cmd+Z step.** `undo` and `-R` go back step by
  step until everything the script created is gone. Properties added to an **existing** component can't be
  checked that way — `undo` warns; compare with `inspect`.
- **`inst.mainComponent = other` drops the overrides**, `inst.swapComponent(other)` keeps them.

## Scripts

- **The answer is `Done` or `null`** — you forgot the `return`.
- **Scripts run in strict mode.** `'x' is not defined` — declare it first (`const`, `let`). `'x' is read-only`
  for a variable — it is a `const`: declare it with `let` to give it a new value. `no setter for property` —
  the property can only be read: `width` and `height` change with `resize()` or `h.fixed`.
- **`not a function`** — the method was renamed or exists only as an async version (`getNodeByIdAsync`,
  `setTextStyleIdAsync`, `getMainComponentAsync`). Check the name.
- **`figma.createAutoLayout()`, `node.query()`, `node.set()` and `node.screenshot()`** from Figma's own advice
  exist only in its MCP server — the Plugin API doesn't have them, and calling one fails with `not a function`.
- **`… permission not specified in manifest`** — Figaro doesn't let scripts see who is in the file:
  `figma.currentUser`, `figma.activeUsers` and the authors of FigJam stamps are off. Leave them out, and don't
  edit Figaro's `manifest.json`.
- **Building needs no `figma.setCurrentPageAsync`**: `figma.create*()` puts a node on the current page, and
  `page.appendChild(node)` or `h.frame(page, …)` moves it where it belongs; to read another page,
  `await page.loadAsync()`. A switched page jumps on the user's screen — don't switch it unless they ask.
- **While a script runs, Figma is busy with it**: it doesn't repaint, and nothing else gets in — even a
  `--parallel` read waited until a 10 s script was over. `h.ck()` or `--timeout` ends your script, but
  nothing makes Figma responsive before that: batches of a few seconds.
- **Search inside a node, never `figma.root.findAll` or `figma.currentPage.findAll`** on a file you don't know
  the size of. In a large design file (hundreds of thousands of layers) one such call freezes Figma for tens
  of seconds. Start from the frame you work on (`(await h.node(id)).findAll(…)`, `h.find(root, {…})`,
  `figaro find <layer> …`) and narrow by `type` (`findAllWithCriteria`).
- **A big `return`** (thousands of nodes) is slow: return only what you need.
- **An error in a `--lib` library** prints as `file.js, line N`; `libMissing` — the plugin restarted, and the
  CLI sends the library again by itself.

## Groups, sections, bounds

- **An empty group deletes itself.** Remove the last child of a group and the group is gone (`group.removed`
  is `true`); a later `group.remove()` or `group.children` throws `The node with id "…" does not exist`. To
  delete a group, remove the group, not its children. To empty it on purpose, check first:
  `if (!group.removed) group.remove();`.
- **A section never wraps its children.** It keeps its size however much you put in (a 900 px frame in a
  400×300 section sticks out), and a new one is 496×496. A child's `x` and `y` are relative to the section:
  `appendChild` keeps the numbers, not the place on the canvas. Size the section yourself after filling it —
  `resize` and `resizeWithoutConstraints` both work on it:

  ```js
  const pad = 80, kids = sec.children;
  const minX = Math.min(...kids.map((c) => c.x)), minY = Math.min(...kids.map((c) => c.y));
  const maxX = Math.max(...kids.map((c) => c.x + c.width)), maxY = Math.max(...kids.map((c) => c.y + c.height));
  for (const c of kids) { c.x += pad - minX; c.y += pad - minY; }
  sec.resizeWithoutConstraints(maxX - minX + 2 * pad, maxY - minY + 2 * pad);
  ```

  Sections sit on a page only, and have no auto-layout, constraints or `clipsContent`.
- **`absoluteRenderBounds` is cut by every parent that clips.** A 200×200 rectangle in a 100×100 frame with
  `clipsContent` has an `absoluteBoundingBox` of 200×200 and a render box of 100×100; fully cut off or hidden,
  it is `null`. It includes a shadow, though (a 100×100 frame with a drop shadow: 148×148) and a text comes
  out as its letters. For a layer's own box use `absoluteBoundingBox` or `width`/`height`. (A render box that
  lags behind the change inside one script wasn't reproduced for rectangles, text, effects and auto-layout:
  all were current at once.)

## Prototypes

- **Reactions go through `await node.setReactionsAsync([...])`.** Write `actions: [...]`: the old single
  `action` field is refused ("update the `actions` field instead of the `action` field"). A NAVIGATE needs
  another frame that sits straight on the page **or in a section** as the destination (checked: section to
  section, page to section and section to page all work); a frame nested in another frame is rejected with
  "destination … was rejected".

  ```js
  await a.setReactionsAsync([{ trigger: { type: "ON_CLICK" }, actions: [{
    type: "NODE", destinationId: b.id, navigation: "NAVIGATE", resetVideoPosition: false,
    transition: { type: "SMART_ANIMATE", easing: { type: "EASE_IN_AND_OUT" }, duration: 0.4 },
  }] }]);
  ```
- **Smart Animate pairs layers by name and type**, so the two states need the same layer names and the same
  properties on both sides. Make the second state a clone of the first (`a.clone()`: same names and types)
  and change only what moves. A property that exists in one state only doesn't animate: use opacity 0 instead
  of a missing fill, and a fixed size instead of HUG. (Only the API side of this was checked live: play the
  prototype to see the animation.)

## Annotations

- **`node.annotations` is a new array on every read**, with new items: `includes(item)` with an item from an
  earlier read never matches. Compare by label text, and read the array once into a variable.
- **`Only one of label or labelMarkdown should be given`** — an item read back has both, even one written as
  `{label}`, so what you read can't be written back as it is. That is why `[...node.annotations, {label}]`
  fails on a layer that already has an annotation, inside an instance or not. Drop `label` and keep the rest
  (`labelMarkdown`, `properties`, `categoryId`):
  `node.annotations = node.annotations.map(({label, ...rest}) => rest).concat([{label: "New"}])`.
- **`Setting an annotation with no content is invalid`** — an annotation needs a label or pinned
  `properties`. To remove them all: `node.annotations = []`.
- **Groups, sections and boolean layers have no annotations**: writing them there does nothing, and no error
  says so. `"annotations" in node` tells; put the note on a frame, a shape, a text or an instance.
- **An annotation on a hidden layer, or inside a hidden parent, doesn't show in Dev Mode.** `h.annotations`
  says `visible: false` for it.

## Undo

- **`undo` refuses another agent's script** (when both name themselves with `-A`); `--force` takes it back
  anyway — only if the user says so.
- **`undo` skips scripts that changed nothing**: right after one of them it undoes the script before. Read
  whose script and how long ago in the answer.
- **`undone: … (3 Cmd+Z steps)`** — Figma recorded the script as several steps, and all of them were undone.
  `⚠ not everything was undone` — look at what is left and tell the user.
- **Figma has one Cmd+Z for the whole file**: it undoes the last step, whoever made it.
- **`read-only: the script changed N — rolled back. The code may write (…)`** — Figma doesn't say who made a
  change, so while code that may write runs, every change counts as the script's: Figma's own updates of a
  page loaded for the first time too. The quotes are the first three places, in the code's order, that made
  the code look like it writes. If the script only reads, rewrite those parts and run it again: gather results
  in objects and arrays of the script's own (`const out = { types: {} }; out.types[n.type] = …` is fine), not
  in an object a function got as a parameter or one that may hold a layer.
- **`⚠ N changed in the file while the script ran … not by the script`** — the code only reads, so Figma or
  someone in the file made those changes, and nothing was rolled back. In a large file Figma updates
  instances on a page the first time a script loads it; the same script once more gives a clean answer.
  A script that only reads answers the moment it ends: what Figma updates right after it isn't in its
  answer, so no `⚠` line shows it.

## Pictures

- **`shot` captures what Figma shows**: a layer that sticks out of a frame with `clipsContent` comes out cut.
- **A hidden layer, or one inside a hidden layer, comes out as an empty 1×1 picture**, and so does a layer
  with nothing to draw (no width or height). The CLI warns: `⚠ the picture is empty: it is hidden`. To see
  it, show it in a script (`visible = true`), shoot, and hide it again.
- **A picture takes in what sticks out of the layer**: a shadow, an outside stroke, the children of a frame
  that doesn't clip. A 200×100 card with a shadow comes out 248×148, and a text layer is cut to its letters
  (a 300×40 box holding "Hi" gives 10×9). `--absolute` (`h.shot(node, {absolute: true})`) gives exactly the
  layer's own box in every format: for a picture that goes into another mockup or must match the layer.
  To check that nothing sticks out, shoot without it.
- **Don't shoot a whole long page** — shoot it in sections: each one reads better. The CLI cuts a tall layer
  into parts of up to 1600 px.
- **Don't group or ungroup layers for an export**: that once dropped sections out of their frame. `shot`
  changes nothing in the file.
