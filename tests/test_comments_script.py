"""The script `figaro comments` runs in the file to find what each pin is on
(comments_text.pin_code), run by node on a stub of Figma's layers: a page of
frames, one of them with a card and a text in it.

    pytest tests/test_comments_script.py
"""
import json
import shutil
import subprocess

import pytest

import comments_text

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")

RUNNER = r"""
const code = process.argv[1];
const mk = (id, name, type, x, y, w, h, extra) => Object.assign(
  { id, name, type, width: w, height: h, absoluteBoundingBox: { x, y, width: w, height: h }, absoluteTransform: [[1, 0, x], [0, 1, y]], children: [] }, extra || {});
const page = { id: "1:2", name: "Page 1", type: "PAGE", children: [] };
const hero = mk("1:55", "Hero", "FRAME", 0, 0, 800, 600);
const pricing = mk("4:12", "Pricing", "FRAME", 1000, 100, 400, 300);
const card = mk("4:20", "Card", "FRAME", 1020, 120, 200, 100);
const label = mk("4:21", "Get started", "TEXT", 1030, 130, 80, 20, { characters: "Get started" });
const hidden = mk("4:22", "Hidden", "RECTANGLE", 1030, 130, 80, 20, { visible: false });
const empty = mk("4:30", "Empty frame", "FRAME", 2000, 0, 100, 100);
// a section far off, with a frame turned 90° in it and a text 10, 20 into that frame
const flows = mk("7:1", "Flows", "SECTION", 9000, 0, 1000, 1000);
const turned = mk("7:2", "Turned", "FRAME", 9400, 100, 300, 100, { absoluteTransform: [[0, -1, 9500], [1, 0, 100]] });
const badge = mk("I7:3;1:9", "Badge", "TEXT", 9470, 110, 10, 40, { absoluteTransform: [[0, -1, 9480], [1, 0, 110]], width: 40, height: 10 });
card.children.push(label, hidden); pricing.children.push(card); page.children.push(hero, pricing, empty, flows);
flows.children.push(turned); turned.children.push(badge);
const all = [page, hero, pricing, card, label, hidden, empty, flows, turned, badge];
for (const n of all) if (n !== page) n.parent = n === card ? pricing : n === label || n === hidden ? card : page;
for (const n of [hero, pricing, empty, flows]) n.parent = page;
turned.parent = flows; badge.parent = turned;
const byId = Object.fromEntries(all.map((n) => [n.id, n]));
const h = { node: async (id) => { if (!byId[id]) throw new Error("no node " + id); return byId[id]; } };
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction("figma", "h", code)({}, h).then((r) => console.log(JSON.stringify(r)));
"""


def run_code(code, selection=None):
    runner = RUNNER.replace('new AsyncFunction("figma", "h", code)({}, h)',
                            'new AsyncFunction("figma", "h", code)({ currentPage: { selection: SEL.map((i) => byId[i]) } }, h)')
    runner = runner.replace("const byId =", "const SEL = JSON.parse(process.argv[2] || '[]');\nconst byId =")
    out = subprocess.run(["node", "-e", runner, code, json.dumps(selection or [])],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def run_script(pins):
    threads = [{"id": str(i), "pin": p} for i, p in enumerate(pins)]
    code = comments_text.pin_code(threads)
    out = subprocess.run(["node", "-e", RUNNER, code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_pin_on_a_frame_gets_the_deepest_visible_layer_under_it():
    (r,) = run_script([{"node": "4:12", "x": 40, "y": 40}])  # frame at (1000, 100): the point is (1040, 140)
    assert r["node"] == {"id": "4:12", "name": "Pricing", "type": "FRAME"}
    assert r["page"] == {"id": "1:2", "name": "Page 1"} and r["at"] == {"x": 1040, "y": 140}
    assert [u["id"] for u in r["under"]] == ["4:20", "4:21"]  # the hidden layer, on top of the text, is skipped
    assert r["under"][1]["text"] == "Get started"


def test_a_pin_on_a_frame_with_nothing_under_it_lists_its_children():
    (r,) = run_script([{"node": "4:12", "x": 350, "y": 250}])
    assert r["under"] == [] and r["kids"] == 1
    assert r["children"] == [{"id": "4:20", "name": "Card", "type": "FRAME"}]


def test_a_pin_on_the_pages_empty_canvas_gets_the_nearest_layers():
    (r,) = run_script([{"node": "1:2", "x": 3288, "y": 924}])
    assert r["node"]["type"] == "PAGE" and r["under"] == [] and r["at"] == {"x": 3288, "y": 924}
    assert [(n["id"], n["away"]) for n in r["nearest"]] == [("4:30", 1446), ("4:12", 1959), ("1:55", 2509)]
    near = run_script([{"node": "1:2", "x": 1166, "y": 0}])[0]["nearest"][0]
    assert near["id"] == "4:12" and near["away"] == 100  # 100 px above the Pricing frame


def test_a_pin_on_a_page_over_a_layer_gets_the_layer():
    (r,) = run_script([{"node": "1:2", "x": 1040, "y": 140}])
    assert [u["id"] for u in r["under"]] == ["4:12", "4:20", "4:21"] and "nearest" not in r


def test_a_layer_that_is_gone_a_missing_point_and_no_node():
    gone, nopoint, nonode = run_script([{"node": "9:9", "x": 1, "y": 1}, {"node": "4:12", "x": None, "y": None},
                                        {"node": None, "x": 5, "y": 5}])
    assert gone == {"id": "0", "gone": True}
    assert nopoint["node"]["id"] == "4:12" and "under" not in nopoint
    assert nonode == {"id": "2"}


def test_no_pin_on_a_layer_needs_no_script():
    assert comments_text.pin_code([{"id": "1", "pin": {"node": None, "x": 1, "y": 2}}]) is None
    assert comments_text.pin_code([]) is None


def test_the_script_only_reads():
    code = comments_text.pin_code([{"id": "1", "pin": {"node": "1:2", "x": 1, "y": 2}}])
    for word in ("create", ".remove(", "setPluginData", "appendChild", "insertChild", "figma.root.findAll"):
        assert word not in code


# ─── the layer a new comment goes on ──────────────────────────────────────

PAGE = {"id": "1:2", "name": "Page 1"}


def test_a_new_comment_is_pinned_to_its_layers_top_level_frame_at_the_layers_point():
    # 4:21 is at 1030, 130 in Pricing at 1000, 100: the pin goes 12, 10 into the text (half of 24 at most)
    assert run_code(comments_text.layer_code("4:21")) == {
        "id": "4:21", "name": "Get started", "type": "TEXT", "page": PAGE,
        "top": {"id": "4:12", "name": "Pricing", "type": "FRAME"}, "pin": {"node": "4:12", "x": 42, "y": 40}}
    # --at is a point in the layer, from its corner
    assert run_code(comments_text.layer_code("4:21", [40, 2.5]))["pin"] == {"node": "4:12", "x": 70, "y": 32.5}


def test_a_top_level_layer_takes_the_pin_itself():
    got = run_code(comments_text.layer_code("4:12"))
    assert got["top"] is None and got["pin"] == {"node": "4:12", "x": 12, "y": 12}
    assert run_code(comments_text.layer_code("4:30", [0, 0]))["pin"] == {"node": "4:30", "x": 0, "y": 0}


def test_in_a_section_the_top_level_layer_is_the_sections_child_and_a_turned_frame_is_turned_back():
    # Badge, inside an instance (its id starts with I), sits 10, 20 into Turned, which is turned 90°:
    # 12, 5 into Badge is 22, 25 into Turned, whatever the canvas says
    got = run_code(comments_text.layer_code("I7:3;1:9"))
    assert got["top"] == {"id": "7:2", "name": "Turned", "type": "FRAME"}
    assert got["pin"] == {"node": "7:2", "x": 22, "y": 25}
    assert run_code(comments_text.layer_code("7:2"))["top"] is None


def test_sel_is_the_first_selected_layer_and_nothing_selected_is_gone():
    assert run_code(comments_text.layer_code("sel"), ["4:20", "4:21"])["id"] == "4:20"
    assert run_code(comments_text.layer_code("sel")) == {"gone": True}


def test_a_layer_the_file_doesnt_have_and_a_page_are_said_so():
    assert run_code(comments_text.layer_code("9:9")) == {"gone": True}
    assert run_code(comments_text.layer_code("1:2")) == {"isPage": True, "id": "1:2", "name": "Page 1"}


def test_the_layer_script_only_reads():
    code = comments_text.layer_code("4:21")
    for write in ("remove(", "appendChild", "selection =", ".name =", "setPluginData", "create"):
        assert write not in code


