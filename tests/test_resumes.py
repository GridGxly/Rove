"""Erga resume preparation: the failure pause, one notice per reason, the background
intake beside the job-fit review, and one Erga process per pass."""

import asyncio
import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from rove import erga_session, resumes, worker, workflow
from rove.onboarding import approve, digest, draft, propose

URL = "https://jobs.example.com/synthetic/{}"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    propose("identity", {"legal_first_name": "Alex", "legal_last_name": "Example"}, digest(draft()))
    approve(digest(draft()))
    pdf = tmp_path / "approved.pdf"
    pdf.write_bytes(b"%PDF-1.4 approved base")
    propose("evidence", {"resume_path": str(pdf)}, digest(draft()))
    approve(digest(draft()))
    return tmp_path / "state"


def application(state, number: int) -> str:
    application_id = workflow.enqueue(URL.format(number))["application_id"]
    (state / "applications" / application_id).mkdir(parents=True, exist_ok=True)
    return application_id


def failing(calls: list, cause: str = "ValueError: expected exactly one section"):
    async def call(name, arguments):
        calls.append((name, arguments))
        raise erga_session.ErgaError(name, cause)

    return call


def events(application_id: str, kind: str) -> list[dict]:
    with workflow.db() as conn:
        rows = conn.execute(
            "SELECT data FROM application_events WHERE application_id=? AND kind=?",
            (application_id, kind),
        ).fetchall()
    return [json.loads(row["data"]) for row in rows]


def test_identical_failures_pause_intake_and_tell_the_owner_once(state, monkeypatch):
    calls: list = []
    monkeypatch.setattr(resumes, "erga_call", failing(calls))
    manifests, ids = [], []
    for number in range(4):
        ids.append(application(state, number))
        manifests.append(resumes.prepare_resume(ids[-1], URL.format(number)))

    # Three calls fail the same way; the fourth application never calls Erga.
    assert [name for name, _ in calls] == ["intake_job_url"] * 3
    assert all(m["ready"] and not m["tailored"] for m in manifests)
    assert [m["warning_is_new"] for m in manifests] == [True, False, False, False]
    assert "1 in a row" in manifests[0]["system_note"]
    assert "paused" not in manifests[1]["system_note"]
    assert "3 in a row" in manifests[2]["system_note"]
    assert "intake paused for 60 min" in manifests[2]["system_note"]
    assert manifests[3]["system_note"] == ""  # the pause already has its one log line
    health = resumes.intake_health()
    until = datetime.fromisoformat(health["paused_until"])
    assert timedelta(minutes=59) < until - datetime.now(UTC) <= timedelta(minutes=60)
    skipped = json.loads((state / "applications" / ids[3] / "erga-error.json").read_text())
    assert skipped["skipped"] == "intake paused"


def test_a_different_failure_starts_a_new_count_and_is_news(state, monkeypatch):
    calls: list = []
    monkeypatch.setattr(resumes, "erga_call", failing(calls, "ValueError: first kind"))
    for number in range(2):
        resumes.prepare_resume(application(state, number), URL.format(number))
    monkeypatch.setattr(resumes, "erga_call", failing(calls, "OSError: second kind"))
    changed = resumes.prepare_resume(application(state, 2), URL.format(2))

    assert changed["warning_is_new"] is True
    assert "1 in a row" in changed["system_note"]
    assert resumes.intake_health()["count"] == 1
    assert "paused_until" not in resumes.intake_health()


def test_intake_is_tried_again_when_the_pause_ends(state, monkeypatch):
    calls: list = []
    monkeypatch.setattr(resumes, "erga_call", failing(calls))
    monkeypatch.setattr(workflow, "config", lambda: {"erga_failure_limit": 2})
    for number in range(3):
        resumes.prepare_resume(application(state, number), URL.format(number))
    assert len(calls) == 2  # the configured limit of two paused the third
    health = resumes.intake_health()
    health["paused_until"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    resumes.write_private(resumes.health_path(), health)

    again = resumes.prepare_resume(application(state, 3), URL.format(3))
    assert len(calls) == 3
    # Still failing the same way: the pause starts over, with one more log line.
    assert "intake paused for 60 min" in again["system_note"]
    assert again["warning_is_new"] is False


def tailored_erga(state, calls: list, slug: str, *, passes: bool = True):
    """A stand-in Erga whose intake wrote a package under a relative output root."""
    (state / "erga").mkdir(exist_ok=True)
    (state / "erga" / "config.toml").write_text('[resume]\noutput_root = "output"\n')
    artifacts = state / "erga" / "output" / "summer-2027" / slug / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "proposal.tex").write_text("\\documentclass{article}")
    pdf = artifacts / "Alex_Example_Resume.pdf"
    if passes:
        pdf.write_bytes(b"%PDF-1.4 tailored")

    async def call(name, arguments):
        calls.append((name, arguments))
        if name == "intake_job_url":
            return {
                "proposal_tex": str(artifacts / "proposal.tex"),
                "validation": {
                    "pdf": str(pdf) if passes else None,
                    "returncode": 0 if passes else 1,
                    "page_fill_ratio": 0.93 if passes else 0.81,
                    "skipped": None if passes else "Resume fills 81.0% of the page",
                },
                "tailoring_meaningful_change": True,
                "application_id": "erga-synthetic-1",
            }
        return {"returncode": 0, "pdf": str(artifacts / "proposal.pdf"), "skipped": None}

    return call


def test_a_tailored_pdf_is_validated_from_its_package_source(state, monkeypatch):
    calls: list = []
    monkeypatch.setattr(resumes, "erga_call", tailored_erga(state, calls, "role-1"))
    application_id = application(state, 1)
    manifest = resumes.prepare_resume(application_id, URL.format(1))

    assert manifest["ready"] and manifest["tailored"] and manifest["announced"] is False
    # Erga names the PDF after the candidate; the source it validates is proposal.tex.
    assert calls[1] == (
        "validate_tailored_resume",
        {"proposal_tex": str(state / "erga/output/summer-2027/role-1/artifacts/proposal.tex")},
    )
    assert (state / "applications" / application_id / "resume.pdf").read_bytes() == (
        b"%PDF-1.4 tailored"
    )


def test_layout_rejections_are_news_once_until_a_tailored_resume_is_used(state, monkeypatch):
    calls: list = []
    attached: list = []
    monkeypatch.setattr(workflow, "attach_file", lambda *args: attached.append(args[2]))
    rendered: list = []
    monkeypatch.setattr("subprocess.run", lambda *a, **k: rendered.append(a))
    results = []
    for number, passes in ((1, False), (2, False), (3, True), (4, False)):
        monkeypatch.setattr(
            resumes, "erga_call", tailored_erga(state, calls, f"role-{number}", passes=passes)
        )
        results.append(resumes.prepare_resume(application(state, number), URL.format(number)))

    first, second, tailored, after = results
    assert first["warning"] == resumes.LAYOUT_REJECTED and first["warning_is_new"]
    assert "Resume fills 81.0% of the page" in first["system_note"]
    assert second["warning_is_new"] is False
    assert tailored["tailored"] is True
    assert after["warning_is_new"] is True  # a tailored resume came in between

    # The draft rejected for a reason the owner already saw is not posted again.
    worker.attach_resume(workflow.enqueue(URL.format(2))["application_id"], second)
    assert attached == ["→ Resume as sent · your approved base PDF"]
    assert rendered == []


def test_the_background_intake_is_announced_once_with_the_captured_posting(state, monkeypatch):
    calls: list = []
    monkeypatch.setattr(resumes, "erga_call", failing(calls))
    application_id = application(state, 1)
    url = URL.format(1)

    preparing = resumes.start_preparation(
        worker.prepare_resume, application_id, url, "Synthetic posting the browser read"
    )
    resume = worker.ready_resume(application_id, url, preparing)

    assert resume["ready"]
    assert calls[0][1]["job_text"] == "Synthetic posting the browser read"
    prepared = events(application_id, "resume_prepared")
    assert len(prepared) == 1 and prepared[0]["warning"] == resumes.INTAKE_FAILED
    assert events(application_id, "resume_preparation_started") == []
    manifest_path = state / "applications" / application_id / "resume-manifest.json"
    assert json.loads(manifest_path.read_text())["announced"] is True

    # A later pass finds the resume settled: no new intake, no second announcement.
    assert resumes.start_preparation(worker.prepare_resume, application_id, url, "x") is None
    worker.ready_resume(application_id, url, None)
    assert len(events(application_id, "resume_prepared")) == 1
    assert len(calls) == 1


def test_a_pass_that_stops_early_still_finishes_its_intake(state, monkeypatch):
    calls: list = []
    monkeypatch.setattr(resumes, "erga_call", failing(calls))
    application_id = application(state, 1)
    url = URL.format(1)

    def slow(application_id, url):
        time.sleep(0.2)
        return resumes.prepare_resume(application_id, url)

    @resumes.one_erga_pass
    def held_on_fit():
        resumes.start_preparation(slow, application_id, url, "Synthetic posting")
        raise RuntimeError("held before the resume step")

    with pytest.raises(RuntimeError):
        held_on_fit()
    manifest_path = state / "applications" / application_id / "resume-manifest.json"
    assert json.loads(manifest_path.read_text())["announced"] is False
    assert not resumes._running

    # The pass after the owner's `go` announces the resume that was waiting.
    worker.ready_resume(application_id, url, None)
    assert len(events(application_id, "resume_prepared")) == 1


FAKE_ERGA = """#!{python}
import os
import anyio
from mcp.server.mcpserver import MCPServer

with open(os.environ["FAKE_ERGA_SPAWNS"], "a") as spawns:
    spawns.write(f"{{os.getpid()}}\\n")
server = MCPServer("fake-erga")


@server.tool()
def list_evidence() -> list[dict]:
    return [{{"id": "e1", "approved": True, "text": "Built a synthetic parser."}}]


@server.tool()
async def intake_job_url(job_url: str, application_slug: str = "", job_text: str = "") -> dict:
    await anyio.sleep(float(os.environ.get("FAKE_ERGA_DELAY", "0")))
    raise ValueError("expected exactly one section matching 'Skills'")


server.run()
"""


@pytest.fixture
def fake_erga(state, tmp_path, monkeypatch):
    executable = erga_session.Path.home() / ".local/bin/erga-mcp"
    executable.parent.mkdir(parents=True)
    executable.write_text(FAKE_ERGA.format(python=sys.executable))
    executable.chmod(0o755)
    spawns = tmp_path / "spawns.txt"
    spawns.write_text("")
    monkeypatch.setenv("FAKE_ERGA_SPAWNS", str(spawns))
    yield lambda: len(spawns.read_text().split())
    erga_session.end_pass()


def test_a_pass_shares_one_erga_process_and_keeps_the_cause(fake_erga):
    erga_session.begin_pass()
    for _ in range(2):
        assert asyncio.run(erga_session.call("list_evidence", {}))["result"][0]["id"] == "e1"
    with pytest.raises(erga_session.ErgaError) as failure:
        asyncio.run(erga_session.call("intake_job_url", {"job_url": URL.format(1)}))
    assert failure.value.cause == "ValueError: expected exactly one section matching 'Skills'"
    assert failure.value.signature.startswith("intake_job_url · ValueError")
    assert fake_erga() == 1
    erga_session.end_pass()

    # Outside a pass each call starts its own process, as mail and submission do.
    for _ in range(2):
        asyncio.run(erga_session.call("list_evidence", {}))
    assert fake_erga() == 3


def test_evidence_is_answered_while_the_intake_is_running(fake_erga, monkeypatch):
    monkeypatch.setenv("FAKE_ERGA_DELAY", "1.5")
    erga_session.begin_pass()
    finished: dict = {}

    def intake():
        try:
            asyncio.run(erga_session.call("intake_job_url", {"job_url": URL.format(1)}))
        except erga_session.ErgaError:
            finished["intake"] = time.monotonic()

    asyncio.run(erga_session.call("list_evidence", {}))  # the process is up
    thread = threading.Thread(target=intake)
    thread.start()
    time.sleep(0.2)
    asyncio.run(erga_session.call("list_evidence", {}))
    finished["evidence"] = time.monotonic()
    thread.join()
    assert finished["evidence"] < finished["intake"]
    assert fake_erga() == 1
