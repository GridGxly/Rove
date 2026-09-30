"""Real browser regression: display text is not necessarily a selected value."""

from patchright.sync_api import sync_playwright

from erga_autopilot.live_browser import RecruitingBrowser


def test_country_dial_code_does_not_confuse_canada_and_us(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_STATE_DIR", str(tmp_path))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("""<div class="select__container"><div class="select__single-value">+1</div>
        <span aria-live="polite"></span><input role="combobox" data-autopilot-field="1"></div>
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
    monkeypatch.setenv("AUTOPILOT_STATE_DIR", str(tmp_path))
    from erga_autopilot import live_browser

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


def test_same_value_accepts_a_phone_in_any_national_or_international_form():
    from erga_autopilot.live_browser import same_value

    assert same_value("2025550123", "+1 (202) 555-0123")
    assert same_value("+1 202-555-0123", "2025550123")
    assert same_value("12025550123", "202-555-0123")
    assert not same_value("2025550123", "8632589846")
    assert same_value("Ralph", "Ralph ")
    assert not same_value("Ralph", "Ralp")
