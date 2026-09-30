"""Real browser regression: display text is not necessarily a selected value."""

from playwright.sync_api import sync_playwright

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
