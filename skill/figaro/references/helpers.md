# The `h.*` helpers and the script environment

## Environment

A `figaro exec` script runs as the body of an async function with the parameters `figma`, `print`, `h` and
`lib`.

- **`figma`** — the Plugin API, with guards: `closePlugin()`, `showUI()`, `ui.close()` and replacing
  `ui.onmessage` throw, because they would cut the plugin off from the bridge.
- **`--timeout`** (60 s) — the script's deadline. Calls to `figma.*Async`, also under `figma.variables` and
  `figma.teamLibrary`, check it themselves: after the deadline a new call throws, and a call Figma never
  answered ends with the error "did not answer before --timeout ran out". So does an import by key in
  `h.importComp`, `h.importVar`, `h.var_`, `h.bF`, `h.bS` and `h.bN`. A `while`, `do … while` or `for (;;)`
  loop ends at the deadline too: "the loop on line N was still running when --timeout ran out". Node methods
  (`getMainComponentAsync`, `exportAsync`, `setTextStyleIdAsync`…), the other helpers and `for … of` loops
  don't know the deadline — call `h.ck()` in a long `for … of` loop.
- **`print(...)`** — lines in the answer's `logs`; the CLI prints them as `log: …`.
- **`return`** — the result: a string, a number or JSON. Return ids, names and numbers. A Figma node does not
  turn into JSON.
- **`lib`** — the functions of the `--lib` files (`{}` without them). A file's top-level `function`, `class`,
  `const` and `module.exports` become `lib.<name>` and see the current script's `figma`, `h` and `print`.
- Both the sync and the async API work, but take async: `await figma.getNodeByIdAsync(id)`, or simpler,
  `await h.node(id)`.
- An error comes back with `line N` of your script, the line itself and a `hint` for the common cases. A
  `// comment` on the last line breaks nothing.

## Finding a layer

| Helper | What it does |
|---|---|
| `await h.node(id)` | a node by id, layer link or `1-11`. An id this file doesn't have fails at once: `no node … in "…"` |
| `await h.resolve(x)` | the same, plus `"page"` (the current page) and `"sel"` (the first selected layer) |
| `h.sel()` | the selection: `[{id, name, type, w, h, chars, url}]` |
| `h.find(root, {name, nameHas, type, text, textHas, hidden})` | a fast search; returns **records** `{id, name, type, w, h, chars}`, not nodes — get the node with `h.node(r.id)`. Skips layers hidden in instances unless `hidden: true` |
| `h.findByName(root, name)` · `h.findAllByName(root, name)` | the node, or the nodes, with this exact name |
| `await h.annotations(scope)` | the Dev Mode annotations under a node, link or id; by default, or with `"sel"`, under every selected layer, else the page: `[{frame, node, name, visible, labels, properties?}]`, one per annotated layer. `labels` are the texts, `properties` the pinned ones (`width`, `fills`…); `visible` is false when the layer or a parent is hidden, and Dev Mode doesn't show it |
| `h.dumpTree(node, {maxDepth, showSize, showText, showLayout})` | the tree as text, like `figaro tree` |
| `h.link(nodeOrId)` | a link to the layer, for a report |

## Reading and seeing

| Helper | What it does |
|---|---|
| `await h.inspect(node, {depth, hidden})` | the data behind `figaro inspect`: layout, fills with style and variable names, texts, instances, keys (the comparison of a set's variants is made by the CLI from this data, not by the helper) |
| `await h.shot(node, {format, scale, width, absolute})` | a picture in base64; `absolute: true` exports exactly the layer's size even if children or effects stick out of it (for a picture reused inside another mockup). The CLI is handier: `figaro shot` or `exec --shot` write a file |

## Pictures

| Helper | What it does |
|---|---|
| `h.image(node, source, mode)` | fills the layer with a picture, instead of its fills: `source` is the name of a file that came with the script (`figaro exec --image logo=./logo.png`, then `h.image(n, "logo", "fit")`) or the bytes of a PNG, JPEG or GIF (a `Uint8Array`, e.g. from `figma.base64Decode`); `mode` is `fill` (default), `fit`, `crop` or `tile`. Returns `{id, name, mode, hash}`. A layer with no fills (a group) or a picture Figma refuses (not PNG, JPEG or GIF; over 4096 px wide or high) fails with an error that says so. The CLI is handier for one layer: `figaro image` |

## Colour

| Helper | What it does |
|---|---|
| `h.hex("#1a2b3c")` | `{r, g, b}` in the 0..1 range, as Figma wants it |
| `h.solid("#1a2b3c", opacity?)` | a ready fills array: `node.fills = h.solid("#fff")` |

A colour from a token goes through a variable (`h.bF`, "Variables" below) or a style (`setFillStyleIdAsync`,
the recipes below).

## Auto-layout

| Helper | What it does |
|---|---|
| `h.frame(parent, opts)` | an auto-layout frame, set up in the order Figma needs (`pitfalls.md`, "Auto-layout"). `opts`: `layout` (`"V"`, `"H"`), `w`, `h`, `spacing`, `padding` (a number; as in CSS, `[vertical, horizontal]`, `[top, horizontal, bottom]` or `[top, right, bottom, left]`; or `{top, right, bottom, left}`), `align: {primary, counter}`, `fill` (a hex or `false`), `radius`, `name`. An axis with a number is FIXED, one without HUGs |
| `await h.fill(node, "x" \| "y" \| "xy")` | FILL; only for a child of an auto-layout frame, so `parent.appendChild(node)` first. Warns if the parent HUGs on that axis |
| `await h.hug(node, "x" \| "y" \| "xy")` | HUG; only an auto-layout frame or a text |
| `await h.fixed(node, w, h)` | a fixed size; `null` leaves that axis as it was (`resize()` fixes the axes whose size it changes) |
| `await h.wrapText(text, width?)` | text that wraps: in auto-layout to the parent's width, otherwise to `width` px. Warns if the text turned into a column |

The auto-layout helpers load the text's fonts themselves and explain what Figma won't allow.

## Text and fonts

| Helper | What it does |
|---|---|
| `await h.setText(node, "text")` | new text; loads the fonts, mixed ones too. The new text takes the first character's style |
| `await h.fonts(target)` | loads the fonts of a layer (with all its ranges), of a `FontName` or of a list of them; doesn't throw, returns `{loaded, missing, note?}` |
| `await h.withFonts(root, async () => { … })` | loads every font in the subtree, then runs the function |
| `await h.fa()` | the installed Font Awesome fonts: `{pro, kit, families}` |

## Components and instances

| Helper | What it does |
|---|---|
| `await h.variant(inst, props)` | `inst.setProperties(props)`. Property names short (`"Label"`, `"Size"`) or full (`"Label#12:0"`); `"true"`/`"false"` for a BOOLEAN become booleans |
| `await h.variantsOf(inst)` | `{current, groups, all}` — which variants the set has |
| `await h.importComp(key)` | a component of a **published** library, by key |
| `h.cloneNext(node, {direction, gap, name})` | a copy right after the node in its parent, `gap` px (100) to the `right`, `left`, `up` or `down`; in auto-layout it takes the next place. Not for a page |

A component the file already has (its own or a library's; `inspect` prints its id) needs no import:
`(await h.node(id)).createInstance()`.

## Variables

| Helper | What it does |
|---|---|
| `await h.var_(idOrKey)` | a variable by id (`VariableID:…`; also a library variable the file already uses — `inspect` prints its id) or by key (imports it) |
| `await h.importVar(key)` | a variable of an enabled library, by key; Figma imports only published ones |
| `await h.bF(node, i, v)` · `await h.bS(node, i, v)` | bind fill or stroke number `i` to a variable (an id, a key or the variable itself) |
| `await h.bN(node, prop, v)` | bind a number: `itemSpacing`, `counterAxisSpacing`, `paddingTop`…, `topLeftRadius`…, `width`, `height`, `minWidth`…, `strokeWeight`, `opacity` |

A text's font to a variable: `text.setBoundVariable("fontSize", v)` — and the same for `fontFamily`,
`fontStyle`, `fontWeight`, `lineHeight`, `letterSpacing` and `paragraphSpacing`.

## Control

`h.ck()` throws as soon as the script's `--timeout` has passed. Call it on every iteration of a long
`for … of` loop, or the loop keeps going while the next script waits for the file (`while` and `for (;;)`
loops check the deadline themselves). `h.aborted()` is the same without throwing: `true` or `false`.

## Checking your work

`await h.audit(layer, opts?)` walks the layers under `layer` (a node, an id, a link, or `"page"`) and says what
looks wrong, with the layer's id. It is what the Reviewer runs on the layers you changed when you run
`figaro done` (`SKILL.md`, `figaro notes`); you don't run it yourself, except when the user asks you for a
check: then run it with `-R` on what they name and fix what is real. It only hints: a note about something
that is on purpose is not a mistake.

```js
return await h.audit(await h.node("12:34"));
// {
//   notes: [
//     { rule: "low-contrast", id: "12:40", name: "Caption", count: 3, ids: ["12:40", "12:47", "12:52"],
//       text: "contrast 2.32:1 of #AAAAAA on #FFFFFF, needs 4.5:1; use a stronger colour" },
//     { rule: "no-auto-layout", id: "12:35", name: "Row",
//       text: "3 children placed by hand with no auto-layout; add auto-layout so spacing follows the content" },
//   ],
//   total: 5, more: 0, counts: { "low-contrast": 3, "no-auto-layout": 1, ... }, visited: 48
// }
```

- **`notes`** — the top `limit` (50) notes by importance (the table's order, then the order of the layers):
  a flood of a quiet rule never pushes out a louder one. Layers with the same rule and the same text come as
  one note with `count` and their `ids` (up to 30), so a dozen texts of one grey are one thing to fix.
  `total` is the number of layers with a note, `more` those not in `notes`, `counts` them by rule, all
  honest however long the page; `partial` says the walk stopped early (see below), `errors` how many layers
  could not be read (removed while it ran) and so were skipped. Links: `figaro link <id>…`, or
  `links: true` for a `url` in each note.
- **`opts`** — `only: [rule…]` and `skip: [rule…]` choose rules (an unknown id fails and lists them),
  `limit` (50), `group: false` (a note for each layer), `minTarget` (24; 44 for a phone design), `links`,
  `maxNodes` (8000 layers) and `maxMs` (8000 ms).
- **The walk** is iterative and cheap (about 0.5 ms a layer), and it calls `h.ck()`. A page or a huge frame
  is cut at `maxNodes` or `maxMs`: the answer then has `partial` and still holds the notes so far. Hidden
  layers and what is under them are skipped. Instances count as a unit: their insides belong to the
  component, and only their texts are read, for placeholders and missing fonts. It never changes the file.

The rules, loudest first, with the thresholds:

| Rule | Says | How it decides |
|---|---|---|
| `missing-font` | a text uses a font that is not installed | `hasMissingFont` |
| `text-clipped` | a text is cut or runs out of its box | a fixed box (`textAutoResize` none) the text overflows by more than 40% of its font size; or a frame that clips (`clipsContent`; also one above the audited layer) cuts it. A text with truncation (ellipsis) is on purpose and skipped |
| `content-clipped` | a drawing, an image or a stroke is cut | a frame that clips (`clipsContent`; also one above the audited layer) cuts more than 0.5 px of a vector, boolean, star, polygon, ellipse or instance, or of a rectangle or frame with an image or a stroke: it sticks out on one side (an icon half out of its card), its outside or centred stroke reaches past the frame's edge (a button flush with its parent), or the frame's rounded corners (a radius of a quarter of its short side or more) cut a square layer that fills it (a square logo in a round 20 × 20 frame comes out a circle). Quiet for what is made to be cut: a layer past both edges of a side (a cropped photo, a bleed), a shape at least half the frame's size (a backdrop), gradients only or a layer blur (a fade, a glow), a mask and what it masks, a frame that scrolls, a layer as round as its frame, and a frame or layer named with *avatar*, *photo*, *userpic*, *picture*, *portrait*, *profile*, *flag* or *cover*. A plain fill is a background and is not judged |
| `zero-size` | a layer shows nothing | a text, frame, rectangle, ellipse or instance under 1 px wide or high; a text with no visible characters. Lines are meant to be flat |
| `low-contrast` | text is hard to read | WCAG AA: contrast under 4.5:1, or under 3:1 for large text (24 px, or 18.66 px and bold 700). The text colour is blended with its own opacity and the layers' above it, per run of one colour and size. Disabled text is exempt (WCAG 1.4.3), not text that is only not selected (an *inactive* tab): a layer from the text up to the root named with the word *disabled* (`Button / Disabled`), or a variant name or an instance's variant values where a *Disabled* property is true or yes (`Disabled=True`) or a value is exactly *Disabled* (`State=Disabled`). `Disabled=False`, `State=Inactive` and `Inactive projects` are judged |
| `low-contrast-ui` | an icon is hard to see | a vector under 64 px with a solid colour: under 3:1 (WCAG 1.4.11). Only one that is part of a control or a state graphic: on a layer up to four levels above, a name with one of the words *button*, *btn*, *cta*, *chip*, *tab*, *toggle*, *checkbox*, *radio*, *switch*, *link*, *nav*, *menu*, *action*, *badge*, *status*, *indicator* (whole words: `Navigation` and `Menus` do not count), or a click in the prototype. An illustration (`Dots`, `Illustration`) is not judged, nor a disabled control. One note per parent. A layer of the same colour right beside it counts as a part of one drawing |
| `placeholder-text` | leftover text | `TBC`/`TBD` as a word, "lorem ipsum", or a whole text that is `todo`, `xxx`, `placeholder`, `text`, `your text here`, `???`, `...`. Texts that fill an instance count |
| `small-target` | a button is too small | under 24 × 24 px (WCAG 2.2, SC 2.5.8, AA). Something is a button when its name has the word *button*, *btn*, *cta*, *chip*, *tab*, *toggle*, *checkbox*, *radio*, *switch* or *link* (words, so *Tabs* and *Table* are not), or its component (or the component set) is named so, or it has a click in the prototype. A badge or an icon that is not named so is not judged |
| `no-auto-layout` | children placed by hand | a frame with two or more visible children, no auto-layout, and no overlap among them (a stack of a photo and a caption is on purpose). Skipped: frames of shapes only (a drawing), component sets, and a frame on the page or in a section that is at least 320 px on its short side and 440 on its long one, in either orientation (a screen, a device mock) |
| `empty-frame` | a frame with nothing in it | no children, no fill, stroke or effect to see, and still Figma's default name (`Frame`, `Frame 12`). A named empty frame is a spacer or a spec on purpose |
| `detached-instance` | a frame that was an instance | `detachedInfo`. A one-off state is a fine reason (`SKILL.md`, house rule 7): the note asks you to check |
| `unbound-color` | a colour with no token | a visible solid fill or stroke of a frame, component, rectangle, ellipse or text, with no style and no variable. Quiet when the file has no paint styles or colour variables and nothing in the walked layers uses one: there is nothing to bind to. Gradients, images and vector drawings are not judged |
| `no-text-style` | text without a text style | quiet when the file has no text styles and no walked text uses one. A text with a variable bound to its font size, family, style, weight, line height or letter spacing counts as styled |
| `default-name` | Figma's own name | a frame, group, component or section named `Frame`, `Frame 12`, `Group 3`, `Component 1`, `Section 1`, `Auto layout`. Shapes (`Rectangle 4`) are not judged: drawings are full of them |
| `uneven-siblings` | siblings that are nearly equal | in an auto-layout frame, same-type, same-named (numbers at the end ignored) siblings of a fixed size on an axis, whose sizes differ by more than 0.5 and up to 8 px. A bigger gap is a decision |
| `heading-order` | a skipped heading level | headings are texts whose layer name is exactly `H1`…`H6` or `Heading 2` (a text still named by its own words, "H2 2024 results", is not one), or with a text style whose name has `H2`/`Heading 2` as a word. Read top to bottom within each screen (each frame on a page or in a section; the audited layer itself otherwise), a heading more than one level below the one before |
| `absolute-in-layout` | content that left the flow | a text, frame, group or instance set to absolute position inside an auto-layout frame. Shapes (a dot, a ring, a backdrop) are meant to sit over their neighbours |

Contrast needs the colour behind a layer: going up, a rectangle or frame below it that covers its middle, then
each parent's fill; the first one with a fill decides. When that is an image, a gradient, a translucent paint, a
faded or blending layer, or there is no fill up to the page, there is no note rather than a guess.

## Recipes

**A card from the file's styles** (the style names come from `inspect` of the layers around it)

```js
const texts = await figma.getLocalTextStylesAsync();
const paints = await figma.getLocalPaintStylesAsync();
function pick(list, name) {
  const s = list.find((x) => x.name === name);
  if (!s) throw new Error('no style "' + name + '"; there are: ' + list.map((x) => x.name).join(", "));
  return s;
}
const heading = pick(texts, "Heading/Medium");
const surface = pick(paints, "Surface/Card"), ink = pick(paints, "Text/Primary");

const card = h.frame(await h.node(PARENT), { layout: "V", w: 360, spacing: 8, padding: 24, fill: false,
  radius: 12, name: "Card" });                    // no number for the height — HUG
await card.setFillStyleIdAsync(surface.id);       // the colour from a style, not a hex
const title = figma.createText();
card.appendChild(title);
await h.fonts([title.fontName, heading.fontName]); // the style's font — before setTextStyleIdAsync
await title.setTextStyleIdAsync(heading.id);
await title.setFillStyleIdAsync(ink.id);           // and the text's colour
await h.setText(title, "Payouts");
await h.wrapText(title);                           // to the card's width
return { id: card.id };
```

Spacing and radius take variables the same way: `await h.bN(card, "itemSpacing", v)`, `"paddingTop"`…,
`"topLeftRadius"`… ("Variables" above).

**A library's style, component and variable** (`figaro inspect` of a layer that uses them prints their ids
and keys)

```js
// already in the file — by id
await frame.setFillStyleIdAsync(STYLE_ID);       // "S:…,2739:8"; also setTextStyleIdAsync, setStrokeStyleIdAsync
const inst = (await h.node(COMP_ID)).createInstance();
parent.appendChild(inst);
await h.variant(inst, { Size: "L", Label: "Pay now", Icon: false });
await h.bN(frame, "topLeftRadius", VAR_ID);      // "VariableID:…"

// not in the file yet — by key, from a library enabled for the file
const s = await figma.importStyleByKeyAsync(STYLE_KEY);
const set = await figma.importComponentSetByKeyAsync(SET_KEY);
const v = await figma.variables.importVariableByKeyAsync(VAR_KEY);
```

Import by key takes only what is published in an enabled library, and sometimes Figma never answers it at
all: give the script `--timeout 20`.

**The variables of the enabled libraries** (the collections list holds variables only; components can't be
listed this way). Give it `--timeout 20`: when Figma doesn't answer `teamLibrary`, take the variables' keys
from `figaro inspect` of the layers that already use them.

```js
const cols = await figma.teamLibrary.getAvailableLibraryVariableCollectionsAsync();
const out = [];
for (const c of cols) {
  const vars = await figma.teamLibrary.getVariablesInLibraryCollectionAsync(c.key);
  out.push({ library: c.libraryName, collection: c.name, vars: vars.map((v) => v.name + " " + v.resolvedType + " " + v.key) });
}
return out;
```

**Formatting inside a line** (as in the source: bold, italic, code, links)

```js
const t = await h.node(ID);
await h.setText(t, "Pay with cards or bank transfers.");
const i = t.characters.indexOf("bank transfers");
const n = "bank transfers".length;
await figma.loadFontAsync({ family: "Inter", style: "Semi Bold" });
t.setRangeFontName(i, i + n, { family: "Inter", style: "Semi Bold" });
t.setRangeHyperlink(i, i + n, { type: "URL", value: "https://example.com" });
t.setRangeTextDecoration(i, i + n, "UNDERLINE");
```

A text style on a range is `await t.setRangeTextStyleIdAsync(start, end, styleId)`, a colour is
`t.setRangeFills(start, end, h.solid("#1e35ff"))`. Read the existing formatting with
`t.getStyledTextSegments(["fontName", "fontSize", "fills", "hyperlink", "textDecoration"])` (`inspect` shows
it too).

**Changing many texts in batches** — ~25 layers per call, safe to run again:

```js
const map = { "12:1": "Payouts", "12:2": "Cards" /* … ≤ 25 */ };
const done = [];
for (const [id, text] of Object.entries(map)) {
  const n = await h.node(id);
  if (n.characters !== text) await h.setText(n, text);
  done.push(id);
}
return done.length;
```
