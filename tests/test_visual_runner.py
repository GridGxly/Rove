"""Exercise the game's generic input tools on synthetic pages, never puzzle answers."""

import importlib.util
from pathlib import Path

import pytest
import test_form_reading

reader = test_form_reading.reader
state = test_form_reading.state
spec = importlib.util.spec_from_file_location(
    "visual_runner", Path(__file__).resolve().parents[1] / "scripts/benchmark_visual_game.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_typing_emits_keyboard_events_and_references_work_while_zoomed(reader):
    reader.page.set_content("""<input aria-label="Answer" value="old">
        <button onclick="document.body.dataset.sent='yes'">Submit</button>
        <script>document.body.dataset.keys='0';document.querySelector('input').onkeyup=e=>{
        if(e.key.length===1&&!e.metaKey&&!e.ctrlKey)document.body.dataset.keys++;};</script>""")
    view = {"x": 0, "y": 0, "width": 100, "height": 100}
    controls = runner.visible_controls(reader.page)
    try:
        runner.perform(reader.page, {"action": "type", "ref": 0, "text": "Ab9"}, view, controls)
        assert reader.page.locator("input").input_value() == "Ab9"
        assert reader.page.locator("body").get_attribute("data-keys") == "3"
        runner.perform(reader.page, {"action": "control", "ref": 1}, view, controls)
        assert reader.page.locator("body").get_attribute("data-sent") == "yes"
    finally:
        for handle, _info in controls:
            handle.dispose()


def test_stale_references_cannot_click_replacement_controls(reader):
    reader.page.set_content("<button>Continue</button>")
    controls = runner.visible_controls(reader.page)
    try:
        reader.page.set_content('<button onclick="document.body.dataset.sent=1">Submit</button>')
        with pytest.raises(ValueError, match="changed"):
            runner.perform(reader.page, {"action": "control", "ref": 0}, {}, controls)
        assert reader.page.locator("body").get_attribute("data-sent") is None
    finally:
        for handle, _info in controls:
            handle.dispose()
