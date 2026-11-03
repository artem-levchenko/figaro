"""README.md keeps up with the CLI and the window's settings.

Every command is in its list of commands, and every switch of the window's settings has a row in its Settings
table, under the label the window shows.

    pytest tests/test_readme.py
"""
import re
from pathlib import Path

import bridge_settings
import figaro

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
UI = (ROOT / "plugin" / "ui.html").read_text(encoding="utf-8")


def test_readme_names_every_command():
    missing = [cmd for cmd in set(figaro.DISPATCH) - {"import-component"}  # icomp is its short name
               if f"figaro {cmd}" not in README]
    assert not missing, missing


def labels():
    """What the window's settings call each switch: ["recent", "Show recent changes", "…"]."""
    found = dict(re.findall(r'^\s*\["(\w+)", "([^"]+)", "', UI, re.M))
    return {name: found.get(name) for name in bridge_settings.NAMES}


def test_the_window_labels_every_switch():
    unlabelled = [name for name, label in labels().items() if not label]
    assert not unlabelled, unlabelled


def test_readme_settings_has_a_row_for_every_switch():
    section = README.split("\n## Settings\n", 1)[1].split("\n## ", 1)[0]
    missing = [label for label in labels().values() if label and f"\n| {label} |" not in section]
    assert not missing, missing


def test_readme_counts_the_switches():
    words = {3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}
    assert f"shows {words[len(bridge_settings.NAMES)]} switches" in README
