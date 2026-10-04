"""Searchable grid dropdowns and compound phone fields, exercised in a real browser."""

from datetime import UTC, datetime, timedelta

import pytest
import test_form_reading

from rove import live_browser, pickers, questions, workflow

reader = test_form_reading.reader
state = test_form_reading.state
approved = test_form_reading.approved


def test_verified_fields_do_not_pay_another_pacing_delay(reader, approved, monkeypatch):
    def unexpected_pause(*args):
        raise AssertionError("Unchanged fields should not sleep")

    monkeypatch.setattr(live_browser, "pace", unexpected_pause)
    seen = reader.read("""<label>First Name<input value="Alex"></label>
        <label>Last Name<input value="Example"></label>""")
    filled, pending = reader._fill_page(reader.run["id"], seen, approved, {})
    assert len(filled) == 2 and not pending


def test_numbered_address_labels_remain_visible_questions(reader):
    seen = reader.read("""<label for="street">Address1</label><input id="street" required>
        <span id="line2">Address2</span><input aria-labelledby="line2">
        <input aria-label="Address3"><input name="question_12345">""")
    assert [f["label"] for f in seen["fields"][:3]] == ["Address1", "Address2", "Address3"]
    assert not any(f.get("label_missing") for f in seen["fields"][:3])
    assert seen["fields"][3]["label_missing"]
    assert questions.classify("Address1").canonical_id == "street_address"
    assert questions.classify("Address2").canonical_id == "address_line_2"
    assert questions.draft_gate(seen["fields"][0])["code"] == "profile_fact:street_address"
    assert questions.draft_gate(seen["fields"][2])["code"] == "profile_fact:address_line_3"


@pytest.mark.parametrize("buttons", [1, 0, 2])
def test_stale_code_resends_once_and_persists_a_new_mail_cutoff(reader, state, buttons):
    import json

    reader.read(
        '<h2>Enter the verification code emailed to you</h2><input aria-label="Verification code">'
        + '<button onclick="document.body.dataset.resends='
        'Number(document.body.dataset.resends||0)+1">Send New Code</button>' * buttons
    )
    reader.run["code_asked_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    before = reader.run["code_asked_at"]
    reader.refresh_mailed_code()
    reader.refresh_mailed_code()
    count = int(reader.page.locator("body").get_attribute("data-resends") or 0)
    assert count == int(buttons == 1)
    if buttons == 1:
        stored = json.loads((state / "applications" / reader.run["id"] / "run.json").read_text())
        assert stored["code_asked_at"] == reader.run["code_asked_at"] > before
    else:
        assert reader.run["code_asked_at"] == before


def test_a_name_truncated_by_the_form_never_becomes_an_approved_abbreviation(reader, approved):
    seen = reader.read('<label>First Name<input maxlength="3" required></label>')
    with pytest.raises(ValueError, match="Field verification failed"):
        reader._fill_page(reader.run["id"], seen, approved, {})


def test_unlabelled_city_popup_never_chooses_the_first_or_an_unrelated_suggestion(reader):
    profile = {"identity": {"state_region": "Kansas", "country": "United States"}}
    for cities, success in [
        (["Springfield, IL", "Springfield, KS"], True),
        (["Springfield, First County, KS", "Springfield, Second County, KS"], False),
    ]:
        import json

        reader.read(
            """<label for="city">City</label>
        <input id="city" aria-controls="cities" role="combobox" value="Springfield">
        <ul><li class="suggestion">Springfield, KS</li></ul><ul id="cities"></ul>
        <script>document.body.dataset.clicks=0;for(const text of """
            + json.dumps(cities)
            + """){
          const li=document.createElement('li');li.textContent=text;
          li.onclick=()=>{document.body.dataset.clicks++;document.querySelector('input').value=text;};
          document.querySelector('#cities').appendChild(li);}</script>"""
        )
        assert (
            pickers.custom_city(reader.form, reader.page.locator("input"), "Springfield", profile)
            is success
        )
        assert reader.page.locator("body").get_attribute("data-clicks") == str(int(success))


def test_shared_calling_codes_require_the_exact_country():
    field = {"label": "Country code"}
    profile = {"identity": {"country": "United States"}}
    assert pickers.matches("+1 (United States)", "+1", field, profile)
    assert not pickers.matches("+1 (United States Minor Outlying Islands)", "+1", field, profile)
    assert not pickers.matches("+1 (Canada)", "+1", field, profile)


def test_state_abbreviations_and_ambiguous_cities_are_not_guessed():
    profile = {"identity": {"state_region": "Kansas", "country": "United States"}}
    assert pickers.matches("KS", "Kansas", {"label": "State *"}, profile)
    assert not pickers.matches("AR", "Kansas", {"label": "State *"}, profile)
    assert pickers.search_value("Kansas", {"label": "State *"}, profile) == "KS"
    cities = ["Springfield, First County, KS", "Springfield, Second County, KS", "Springfield, IL"]
    assert pickers.city_options(cities, "Springfield", profile) == [0, 1]
    assert pickers.city_options(["Springfield Heights, KS"], "Springfield", profile) == []
    field = {"label": "ZIP Code *"}
    assert pickers.matches("66501, Maple Hill, KS", "66501", field, profile)
    assert not pickers.matches("66502, Manhattan, KS", "66501", field, profile)


def test_expired_session_resumes_without_clicking_final_submit(reader, monkeypatch):
    reader.read("""<p>Your page has expired.</p><button id="resume" onclick="
        document.querySelector('p').remove();this.remove();">RESUME APPLICATION</button>
        <button id="submit" onclick="document.body.dataset.sent='yes'">Submit</button>""")
    reader.run["target_url"] = reader.page.url
    monkeypatch.setattr(live_browser, "same_job", lambda target, page: True)
    monkeypatch.setattr(reader, "require_public_page", lambda: None)
    monkeypatch.setattr(reader, "note", lambda *args: None)
    reader.resume_expired_session()
    assert reader.page.locator("#resume").count() == 0
    assert reader.page.locator("#submit").count() == 1
    assert reader.page.locator("body").get_attribute("data-sent") is None


def test_session_recovery_does_not_click_a_different_jobs_button(reader, monkeypatch):
    reader.read("<p>Your page has expired.</p><button>Resume Application</button>")
    reader.run["target_url"] = "https://example.invalid/jobs/123"
    monkeypatch.setattr(reader, "click", lambda *args: (_ for _ in ()).throw(AssertionError()))
    reader.resume_expired_session()
    assert reader.page.get_by_role("button").count() == 1


@pytest.mark.parametrize("style", ["visibility:hidden", "opacity:0", "display:none"])
def test_hidden_loading_indicators_do_not_force_the_full_timeout(reader, style):
    reader.page.set_content(f'<div style="{style}"><div class="spinner">Loading</div></div>')
    assert reader.page.evaluate(live_browser.BUSY_JS) is True
    reader.page.set_content('<div class="spinner">Loading</div>')
    assert reader.page.evaluate(live_browser.BUSY_JS) is False


def test_keyup_grid_commits_before_reopening_discards_selection(reader):
    seen = reader.read("""<label for="country">Country</label>
    <input id="country" role="combobox" aria-autocomplete="list" aria-controls="countries"
      aria-expanded="false">
    <table id="countries" role="grid"></table>
    <script>
    const input=document.querySelector('input'), grid=document.querySelector('table');
    input.onfocus=()=>{input.setAttribute('aria-expanded','true');grid.innerHTML='';};
    input.onkeyup=()=>{grid.innerHTML='';if(input.value!=='United States')return;
      const row=grid.insertRow();row.insertCell().textContent='United States';
      row.onclick=()=>{input.value='United States';grid.innerHTML='';
        input.setAttribute('aria-expanded','false');};};
    </script>""")
    assert reader.select_combobox(
        reader.page.locator("input"), seen["fields"][0], "United States", {}
    )
    assert reader.page.locator("input").get_attribute("aria-expanded") == "false"


def test_email_code_values_and_failure_images_never_leave_the_browser(reader, state):
    seen = reader.read("""<h2>Enter the verification code sent to your email</h2>
        <input aria-label="Verification code" value="593820">""")
    assert seen["code_step"] and not seen["manual_takeover_required"]
    assert all("value" not in field for field in seen["fields"])
    assert "screenshot" not in seen
    path = state / "failure.png"
    path.write_bytes(b"older evidence")
    assert not live_browser.safe_screenshot(reader.page, path)
    assert not path.exists()
    reader.page.set_content('<iframe srcdoc="<input type=password value=synthetic>"></iframe>')
    reader.page.frames[1].wait_for_selector("input")
    assert not live_browser.safe_screenshot(reader.page, path)
    reader.page.set_content("<h1>Application received</h1>")
    assert live_browser.safe_screenshot(reader.page, path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_phone_country_code_has_its_own_accessible_name(reader):
    seen = reader.read("""<label for="dial">Phone Number</label>
    <input id="dial" aria-label="Country code" role="combobox">
    <input type="tel" aria-label="Phone Number">""")
    assert [f["label"] for f in seen["fields"]] == ["Country code", "Phone Number"]
    assert questions.resolve(
        seen["fields"][0], {"identity": {"phone": "+1 202 555 0198", "country": "United States"}}
    ) == ("+1", "identity.phone")


def test_render_counters_do_not_turn_profile_fields_into_new_questions(reader):
    before = reader.read('<label>Last Name<input id="lastName-15" name="lastName"></label>')
    after = reader.read('<label>Last Name<input id="lastName-27" name="lastName"></label>')
    assert before["fields"][0]["key"] == after["fields"][0]["key"]
    other = {**after["fields"][0], "label": "Previous legal last name"}
    assert workflow.field_key(other) != before["fields"][0]["key"]


def test_styled_radio_groups_use_the_visible_label(reader):
    seen = reader.read("""<fieldset><legend>Do you have a disability?</legend>
        <style>input {position:absolute;opacity:0;width:1px;height:1px}</style>
        <input type="radio" id="yes" name="disability" value="yes">
        <label for="yes">Yes</label>
        <input type="radio" id="decline" name="disability" value="decline">
        <label for="decline">I do not want to answer</label></fieldset>""")
    field = next(f for f in seen["fields"] if f["kind"] == "radio_group")
    assert reader.select_choice(field, "I do not want to answer")
    assert reader.page.locator("#decline").is_checked()
    assert not reader.page.locator("#yes").is_checked()


def test_searches_linked_grid_without_selecting_unrelated_table_row(reader):
    seen = reader.read("""<label for="country">Country</label>
    <input id="country" role="combobox" aria-autocomplete="list" aria-controls="countries">
    <table><tr><td>United States</td></tr></table>
    <table id="countries" role="grid"></table>
    <script>
    let chosen='';const input=document.querySelector('input');
    const grid=document.querySelector('#countries');
    const draw=()=>{grid.innerHTML='';if(!input.value)return;
      const row=grid.insertRow();row.insertCell().textContent='United States';
      row.setAttribute('aria-selected',String(chosen==='United States'));
      row.onclick=()=>{chosen='United States';input.value=chosen;grid.innerHTML='';};};
    input.oninput=draw;input.onfocus=draw;input.onkeydown=e=>{if(e.key==='ArrowDown')draw();};
    </script>""")
    field = seen["fields"][0]
    assert reader.select_combobox(reader.page.locator("#country"), field, "United States", {})
    assert reader.page.locator("#country").input_value() == "United States"
