"""The local solver acts only on its observed provider frame and verifies the result.

The puzzles here are synthetic browser fixtures with a stubbed model, not benchmark
results or evidence of Qwen's accuracy on real challenges.
"""

import base64
import json
import time

import httpx
import test_browser_guards
import test_live_submission

from rove import captcha

board = test_live_submission.board
site = test_browser_guards.site
CHALLENGE = "/hcaptcha.com/challenge"


def challenge(board, site, monkeypatch, html):
    runtime, _, root = board
    test_browser_guards.Site.pages["/acme/jobs/702"] = (
        b'<h1>Apply</h1><input name="email">'
        b'<textarea hidden name="h-captcha-response"></textarea>'
        b'<iframe width="400" height="400" src="' + CHALLENGE.encode() + b'"></iframe>'
    )
    test_browser_guards.Site.pages[CHALLENGE] = html
    monkeypatch.setattr(captcha, "provider", lambda url: url == site + CHALLENGE)
    runtime.open(site + "/acme/jobs/702")
    return runtime.page, runtime.page.frames[-1], root / "captcha.png"


def test_model_receives_only_the_picture_and_bounded_instruction(monkeypatch):
    sent = []

    def reply(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"action":"click","points":[[500,500]]}'}}]},
        )

    monkeypatch.setattr(
        captcha.runtime,
        "client",
        lambda: httpx.Client(base_url="http://localhost/v1", transport=httpx.MockTransport(reply)),
    )
    assert captcha.decide([b"picture1", b"picture2"], 1)["points"] == [(0.5, 0.5)]
    content = sent[0]["messages"][1]["content"]
    assert len(content) == 3
    assert [base64.b64decode(c["image_url"]["url"].split(",")[1]) for c in content[1:]] == [
        b"picture1",
        b"picture2",
    ]
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_cleared_requires_site_evidence_after_real_input(board, site, monkeypatch):
    page, frame, evidence = challenge(
        board,
        site,
        monkeypatch,
        b"""<style>body{margin:0}
    button{width:400px;height:400px}</style><button onclick="parent.postMessage('solved','*')">
    Click this synthetic verification button</button>""",
    )
    page.evaluate("""() => addEventListener('message',e=>{
        if(e.data!=='solved')return;
        document.querySelector('textarea').value='synthetic-response';
        document.querySelector('iframe').remove();
    })""")
    monkeypatch.setattr(captcha, "decide", lambda *_: {"action": "click", "points": [(0.5, 0.5)]})
    assert (
        captcha.solve(page, page.main_frame, lambda: None, rounds=2, evidence=evidence) == "cleared"
    )
    assert frame.is_detached()
    record = evidence.with_suffix(".json").read_text()
    assert "synthetic-response" not in record and "decisions" in record
    assert evidence.stat().st_mode & 0o777 == 0o600


def test_changed_challenge_never_receives_stale_coordinates(board, site, monkeypatch):
    page, frame, evidence = challenge(
        board,
        site,
        monkeypatch,
        b"""<h1>First synthetic question</h1>
    <button onclick="document.body.dataset.clicked='yes'">A</button>""",
    )

    def changed(*_):
        frame.locator("h1").evaluate("e=>e.textContent='A different synthetic question'")
        return {"action": "click", "points": [(0.5, 0.5)]}

    monkeypatch.setattr(captcha, "decide", changed)
    assert (
        captcha.solve(page, page.main_frame, lambda: None, rounds=1, evidence=evidence)
        == "exhausted"
    )
    assert frame.locator("body").get_attribute("data-clicked") is None


def test_page_overlay_blocks_click_through_the_challenge(board, site, monkeypatch):
    page, _, _ = challenge(board, site, monkeypatch, b"<h1>A synthetic picture challenge</h1>")

    def covered(*_):
        page.evaluate("""() => {
            const cover=document.createElement('button');cover.textContent='Unexpected action';
            cover.style='position:fixed;inset:0;z-index:9999';
            cover.onclick=()=>document.body.dataset.clicked='yes';document.body.append(cover);
        }""")
        return {"action": "click", "points": [(0.5, 0.5)]}

    monkeypatch.setattr(captcha, "decide", covered)
    assert captcha.solve(page, page.main_frame, lambda: None, rounds=1) == "interaction_ValueError"
    assert page.locator("body").get_attribute("data-clicked") is None


def test_animated_pictures_keep_temporal_evidence_with_a_deadline(board, site, monkeypatch):
    page, frame, _ = challenge(
        board,
        site,
        monkeypatch,
        b"""<h1>Watch this synthetic animation</h1>
    <style>@keyframes move{from{transform:translateX(0)}to{transform:translateX(200px)}}
    .dot{width:20px;height:20px;background:blue;
      animation:move .4s linear infinite alternate}</style>
    <div class="dot"></div>""",
    )
    _, element = captcha.visible_frame(page)
    pictures, scene = captcha.pictures(page, element, frame, time.monotonic() + 3)
    assert len(pictures) >= 2 and len(set(pictures)) > 1
    assert len(pictures) < 12
    assert frame.evaluate(captcha.SCENE_JS) == scene
