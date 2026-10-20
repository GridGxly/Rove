"""Synthetic equivalents of Oracle's radio pills and imported education cards."""

import pytest
import test_form_reading

from rove import workflow

reader = test_form_reading.reader
state = test_form_reading.state
approved = test_form_reading.approved

AUTH = "Are you lawfully authorized to work in the United States?"
GOVERNMENT = "Have you been employed by the government in the past five years?"


def pills(question, name):
    return f"""<div class="input-row input-row--radiogroup" role="radiogroup"
      aria-labelledby="label-{name}"><form-element-label>
      <label id="label-{name}" class="input-row__label input-row__label--required">
      {question}<span aria-hidden="true" class="input-row__label--required-star"></span>
      </label></form-element-label><div class="input-row__control-container">
      <button type="button" role="radio" aria-checked="false" value="Y"
       onclick="choose(this)">Yes</button>
      <button type="button" role="radio" aria-checked="false" value="N"
       onclick="choose(this)">No</button></div></div>"""


SCRIPT = """<script>function choose(button) {
  button.parentElement.querySelectorAll('[role=radio]').forEach(b =>
    b.setAttribute('aria-checked', String(b === button)));
}</script>"""


def test_radio_pills_expose_every_required_question_and_click_only_approved_fact(reader, approved):
    seen = reader.read(pills(AUTH, "auth") + pills(GOVERNMENT, "gov") + SCRIPT)
    asked = test_form_reading.questions(seen)
    assert {AUTH, GOVERNMENT} <= set(asked)
    assert all(asked[q]["required"] for q in (AUTH, GOVERNMENT))
    assert test_form_reading.option_labels(asked[AUTH]) == ["Yes", "No"]
    filled, pending = reader._fill_page(test_form_reading.RUN, seen, approved, {})
    assert [f["label"] for f in filled] == [AUTH]
    assert [f["label"] for f in pending] == [GOVERNMENT]
    assert (
        reader.page.locator('[aria-labelledby="label-auth"] [aria-checked=true]').inner_text()
        == "Yes"
    )
    assert reader.page.locator('[aria-labelledby="label-gov"] [aria-checked=true]').count() == 0
    assert not workflow.approved_answers(test_form_reading.RUN)


def test_hidden_native_radios_use_visible_labels_without_exposing_hidden_groups(reader):
    markup = (
        """<fieldset><legend class="required">Work schedule</legend>
      <input style="display:none" type="radio" name="schedule" id="day" value="D">
      <label for="day"><span aria-hidden="true">Day</span></label>
      <input style="display:none" type="radio" name="schedule" id="night" value="N">
      <label for="night"><span aria-hidden="true">Night</span></label></fieldset>
      <div hidden>"""
        + pills("Hidden question", "hidden")
        + "</div>"
    )
    seen = reader.read(markup)
    asked = test_form_reading.questions(seen)
    assert list(asked) == ["Work schedule"]
    assert asked["Work schedule"]["required"]
    assert test_form_reading.option_labels(asked["Work schedule"]) == ["Day", "Night"]
    assert reader.select_choice(asked["Work schedule"], "Night")
    assert reader.page.locator("#night").is_checked()


def test_native_radio_inside_role_wrapper_is_not_read_twice(reader):
    seen = reader.read("""<fieldset><legend>Work schedule</legend>
      <div role="radio"><label><input type="radio" name="shift" value="day">Day</label></div>
      <div role="radio"><label><input type="radio" name="shift" value="night">Night</label></div>
      </fieldset>""")
    field = test_form_reading.questions(seen)["Work schedule"]
    assert len(field["member_refs"]) == 2


@pytest.mark.parametrize("native", [True, False])
def test_checkbox_faces_are_clicked_and_verified_against_their_actual_state(reader, native):
    control = (
        '<input type="checkbox" style="display:none" id="consent">'
        '<label for="consent">I understand</label>'
        if native
        else '<button role="checkbox" aria-checked="false" '
        "onclick=\"this.setAttribute('aria-checked', "
        "String(this.getAttribute('aria-checked')!=='true'))\">I understand</button>"
    )
    seen = reader.read("<fieldset><legend>Consent</legend>" + control + "</fieldset>")
    field = test_form_reading.questions(seen)["Consent"]
    assert reader.set_checkboxes(field, ["I understand"])
    assert reader.set_checkboxes(field, [])


@pytest.mark.parametrize("school", ["Example College", "Honors Program"])
def test_collapsed_education_card_is_observed_even_without_an_open_input(reader, school):
    seen = reader.read(f"""<div class="apply-flow-block"><h2 class="apply-flow-block__title">
      Education</h2><div class="profile-item-container">
      <article class="apply-flow-profile-item-tile">
      <div data-profile-item-tile="true" data-profile-item-invalid="true" role="group">
      <div class="apply-flow-profile-item-tile__summary-title">Unnamed Major</div>
      <div class="apply-flow-profile-item-tile__summary-subtitle">{school}</div>
      <div>Fields to fix: 2</div><button aria-label="Edit"
      class="apply-flow-profile-item-tile__edit-item-icon"></button></div></article></div></div>""")
    assert seen["profile_cards"][0]["school"] == school
    assert seen["profile_cards"][0]["invalid"]
    assert seen["profile_cards"][0]["kind"] == "education"
