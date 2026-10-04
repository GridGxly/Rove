"""Real browser regression: display text is not necessarily a selected value."""

from patchright.sync_api import sync_playwright

from rove.live_browser import RecruitingBrowser


def test_country_dial_code_does_not_confuse_canada_and_us(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("""<div class="select__container"><div class="select__single-value">+1</div>
        <span aria-live="polite"></span><input role="combobox" data-rove-field="1"></div>
        <button>Next field</button><script>
        let selected='Canada';const input=document.querySelector('input');
        const announce=()=>document.querySelector('[aria-live]').textContent='option '+selected+', selected.';
        const close=()=>document.querySelectorAll('[role=option]').forEach(e=>e.remove());
        input.onfocus=announce;input.onkeydown=e=>{if(e.key==='Escape')close();if(e.key==='ArrowDown'){
        close(); for(const name of ['United States','Canada']) {const o=document.createElement('div');o.setAttribute('role','option');o.textContent=name+' +1';o.onclick=()=>{selected=name;close();input.blur();};document.body.append(o);}}};
        </script>""")
        runtime = RecruitingBrowser()
        runtime.page = page
        runtime.run = {"id": "abcdef012345"}
        field = {"label": "Country*", "ref": "1", "key": "123456abcdef"}
        assert runtime.select_combobox(page.get_by_role("combobox"), field, "United States", {})
        page.get_by_role("button").click()
        page.get_by_role("combobox").click()
        assert page.locator("[aria-live]").text_content() == "option United States, selected."
        browser.close()


def test_uploaded_file_can_be_verified_after_widget_removes_input(tmp_path):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<input type="file" onchange="this.remove()">')
        control = page.locator("input").element_handle()
        page.locator("input").set_input_files(str(pdf))
        assert page.locator("input").count() == 0
        assert control.evaluate("e=>e.files.length===1 && e.files[0].name==='resume.pdf'")
        browser.close()


def test_block_pages_are_recognized_and_never_treated_as_forms(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path))
    from rove import live_browser

    monkeypatch.setattr(live_browser.workflow, "config", lambda: {"human_pacing": False})
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            "<title>Access Denied</title><h1>Access Denied</h1><p>You don't have permission. Reference #18.4f1c</p>"
        )
        runtime = RecruitingBrowser(headless=True)
        runtime.page = page
        runtime.run = {"id": "abcdef012345", "profile_hash": "x"}
        seen = runtime.observe()
        assert seen["blocked"] and seen["block_marker"].lower() == "access denied"
        page.set_content("<title>Apply</title><label for=a>Email</label><input id=a>")
        assert not runtime.observe()["blocked"]
        page.set_content(
            "<title>Intern</title><p>This job is no longer accepting applications.</p>"
        )
        gone = runtime.observe()
        assert gone["closed"] and not gone["blocked"]
        browser.close()


def test_phone_formatting_does_not_hide_a_different_value_or_country_code():
    from rove.live_browser import same_field_value, same_value

    field = {"kind": "tel", "label": "Phone"}
    assert same_field_value(field, "2025550198", "+1 (202) 555-0198")
    assert same_field_value(field, "+1 202-555-0198", "2025550198")
    assert not same_field_value(field, "2025550198", "+44 2025550198")
    assert not same_field_value(field, "2025550198", "2025550199")
    assert not same_value("Reference 2025550198", "Wrong 2025550198")
    assert not same_value("1234567", "9991234567")
    assert same_value("Alex", "Alex ")
    assert not same_value("Alex", "Ale")


def test_place_labels_are_recognised_with_or_without_qualifiers():
    from rove.live_browser import is_place_label

    assert is_place_label("Current location")
    assert is_place_label("Location (Optional)")
    assert is_place_label("City *")
    assert not is_place_label("Office location preference")
    assert not is_place_label("Phone Number")
