import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from rove import submission, worker, workflow
from rove.onboarding import approve, digest, draft, propose, read_approved
from rove.submission import GenericV1, GreenhouseV1, LeverV1, package_digest

URL = "https://job-boards.greenhouse.io/example/jobs/123"
LEVER_POSTING = "0f3c7a1e-5b2d-4c8e-9a6f-1d2e3f4a5b6c"
LEVER = f"https://jobs.lever.co/acme/{LEVER_POSTING}/apply"
FINAL = [{"ref": "0", "label": "Submit application"}]


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    propose("identity", {"legal_first_name": "Alex", "legal_last_name": "Example"}, digest(draft()))
    approve(digest(draft()))
    monkeypatch.setattr(
        workflow,
        "config",
        lambda: {
            "enabled": False,
            "submission_enabled": True,
            "submit_adapters": ["greenhouse_v1"],
        },
    )
    return tmp_path / "state"


def field(key, label, value, **extra):
    base = {
        "key": key,
        "label": label,
        "name": key,
        "kind": "text",
        "role": None,
        "required": True,
        "disabled": False,
        "readonly": False,
        "value": value,
        "checked": False,
        "selected": None,
        "selection_code": None,
        "options": [],
    }
    return {**base, **extra}


def ready_application(state, url=URL):
    application_id = workflow.enqueue(url)["application_id"]
    directory = state / "applications" / application_id
    directory.mkdir(parents=True)
    approved = read_approved()
    (directory / "profile.json").write_text(json.dumps(approved))
    (directory / "resume.pdf").write_bytes(b"%PDF-1.4 synthetic resume")
    sha = hashlib.sha256((directory / "resume.pdf").read_bytes()).hexdigest()
    (directory / "resume-manifest.json").write_text(
        json.dumps({"ready": True, "resume_sha256": sha})
    )
    fields = [
        field("aaaaaaaaaaaa", "First name", "Alex"),
        field("bbbbbbbbbbbb", "Country", "", role="combobox", selected="United States"),
        field("cccccccccccc", "Resume", None, kind="file", required=False),
    ]
    package = {
        "run_id": application_id,
        "url": url,
        "profile_hash": approved["profile_hash"],
        "resume_sha256": sha,
        "filled": [
            {"label": "First name", "value": "Alex", "source": "identity.legal_first_name"},
            {"label": "Resume", "source": "frozen approved base resume", "sha256": sha},
        ],
        "pending": [],
        "form_state": fields,
        "final_controls": FINAL,
        "submission_enabled": False,
    }
    package["package_hash"] = package_digest(package)
    (directory / "package.json").write_text(json.dumps(package))
    workflow.set_state(application_id, "READY_FOR_REVIEW", package_hash=package["package_hash"])
    current = {"url": url, "fields": [dict(f) for f in fields], "final_controls": FINAL}
    return application_id, package, current


def after_page(url, marker=True, fields=()):
    return {
        "url": url,
        "fields": list(fields),
        "final_controls": [],
        "ats_markers": {"greenhouse_confirmation": marker},
    }


def test_greenhouse_confirmation_requires_every_signal():
    accepted = [{"host": "boards.greenhouse.io", "path": "/example/jobs/123", "status": 200}]
    confirmation = URL + "/confirmation?utm=1"
    assert GreenhouseV1.confirmed(URL, after_page(confirmation), accepted)["confirmed"]
    assert not GreenhouseV1.confirmed(URL, after_page(confirmation), [])["confirmed"]
    assert not GreenhouseV1.confirmed(URL, after_page(URL), accepted)["confirmed"]
    assert not GreenhouseV1.confirmed(URL, after_page(confirmation, marker=False), accepted)[
        "confirmed"
    ]
    still_form = after_page(confirmation, fields=[field("a", "Email", "")])
    assert not GreenhouseV1.confirmed(URL, still_form, accepted)["confirmed"]
    other_job = [{"host": "boards.greenhouse.io", "path": "/example/jobs/999", "status": 200}]
    assert not GreenhouseV1.confirmed(URL, after_page(confirmation), other_job)["confirmed"]
    rejected = accepted + [
        {"host": "boards.greenhouse.io", "path": "/example/jobs/123", "status": 422}
    ]
    checks = GreenhouseV1.confirmed(URL, after_page(confirmation), rejected)
    assert checks["post_rejected"] and not checks["confirmed"]
    assert not GreenhouseV1.matches("https://job-boards.greenhouse.io/example")
    assert not GreenhouseV1.matches("https://attacker.example/example/jobs/123")


def generic_page(url, text="", fields=(), status="", errors=""):
    return {
        "url": url,
        "fields": list(fields),
        "final_controls": [],
        "text": text,
        "ats_markers": {"status_region": status, "form_error": errors},
    }


def test_generic_adapter_is_last_and_confirms_only_from_new_signals(monkeypatch):
    monkeypatch.setattr(
        workflow,
        "config",
        lambda: {
            "enabled": False,
            "submission_enabled": True,
            "submit_adapters": ["greenhouse_v1", "generic_v1"],
        },
    )
    form_url = "https://careers.example.com/jobs/42/complete-application"
    assert submission.enabled_adapter(URL) is GreenhouseV1
    assert submission.enabled_adapter(form_url) is GenericV1
    assert not GenericV1.matches("http://careers.example.com/jobs/42")
    # The careers page already thanks the visitor and notes required fields before any click.
    before = generic_page(
        form_url,
        "Thank you for your interest in Example. * Required fields",
        fields=[field("a", "Email", "alex@example.invalid")],
    )

    def confirmed(after, responses=()):
        return GenericV1.confirmed(form_url, after, list(responses), before=before)

    # A confirmation-looking URL is evidence; it never confirms without the page saying so.
    address_only = confirmed(generic_page(form_url + "/thanks", "All set"))
    assert address_only["confirmation_url"] and address_only["form_gone"]
    assert address_only["url_changed"] and not address_only["confirmed"]
    assert "did not" in GenericV1.reason(address_only, {})
    assert confirmed(generic_page(form_url + "/thanks", "Thank you for applying."))["confirmed"]
    # A sentence or a success region alone confirms once the form has left.
    inline = generic_page(form_url, "Thank you for your interest. Application submitted.")
    assert confirmed(inline)["confirmed"] and not confirmed(inline)["url_changed"]
    region = generic_page(form_url, "Thank you for your interest.", status="Application received")
    assert confirmed(region)["confirmation_region"] and confirmed(region)["confirmed"]
    # Wording and URL tokens present before the click never count.
    assert not confirmed(generic_page(form_url, "Thank you for your interest in Example."))[
        "confirmed"
    ]
    step = generic_page(
        form_url + "?step=2", "Thank you for your interest.", fields=[field("b", "Phone", "")]
    )
    assert not confirmed(step)["confirmation_url"] and not confirmed(step)["confirmed"]
    # A thank-you sentence under a form that is still open is not a confirmation.
    under = generic_page(form_url, "Application submitted", fields=[field("a", "Email", "x")])
    assert confirmed(under)["confirmation_text"] and not confirmed(under)["confirmed"]
    # A new validation message outranks every success signal, and names itself.
    error = generic_page(
        form_url + "?submitted=1", "Application submitted", errors="Email is required"
    )
    checks = confirmed(error, [{"host": "careers.example.com", "path": "/x", "status": 200}])
    assert checks["confirmation_url"] and checks["url_changed"] and checks["post_accepted"]
    assert not checks["no_form_error"] and not checks["confirmed"]
    assert "Email is required" in GenericV1.reason(checks, error)
    assert not GenericV1.rejected(checks)  # the page moved on: unknown, not "not sent"
    kept = generic_page(form_url, fields=[field("a", "Email", "x")], errors="Email is required")
    assert GenericV1.rejected(confirmed(kept)) and not GenericV1.rejected(confirmed(under))
    assert not GreenhouseV1.rejected({"confirmed": False, "post_rejected": True})
    # The same note that was already there is not a new rejection.
    before["ats_markers"]["form_error"] = "* Required fields"
    stale = generic_page(
        form_url + "/thanks", "Done. Application received.", errors="* Required fields"
    )
    assert confirmed(stale)["no_form_error"] and confirmed(stale)["confirmed"]


def test_generic_adapter_ignores_negated_url_tokens():
    """T7: a failure address, a negated word and a look-alike word confirm nothing."""
    form_url = "https://careers.example.com/jobs/42/apply"
    before = generic_page(form_url, "Apply here", fields=[field("a", "Email", "x")])

    def confirmed(after_url, text="", status=""):
        after = generic_page(after_url, text, status=status)
        return GenericV1.confirmed(form_url, after, [], before=before)

    base = "https://careers.example.com"
    for failing in (
        base + "/login?redirect=/application/incomplete",
        base + "/error/unsuccessful",
        form_url + "?msg=not+received",
        form_url + "?msg=not%20received",
        base + "/jobs/42/submission-failed",
    ):
        checks = confirmed(failing)
        assert not checks["confirmation_url"] and not checks["confirmed"], failing
        assert checks["url_changed"] and checks["form_gone"]  # neither is a confirmation
    # Whole words only: these contain a token's letters without being the word.
    for look_alike in ("/incomplete", "/unsuccessful", "/thankless", "/receivedx"):
        assert not submission.url_tokens(base + look_alike), look_alike
    assert submission.url_tokens(base + "/jobs/42/thank-you") == {"thank"}
    assert submission.url_tokens(base + "/apply/application_submitted") == {"submitted"}
    assert submission.url_tokens(base + "/apply?status=success") == {"success"}
    assert submission.url_tokens(base + "/apply?status=not_success") == set()
    # A real confirmation address still needs the page's own words, and gets them here.
    assert not confirmed(base + "/jobs/42/thanks")["confirmed"]
    assert confirmed(base + "/jobs/42/thanks", "Thank you for applying.")["confirmed"]
    assert confirmed(base + "/jobs/42/thanks", status="Application received")["confirmed"]
    # A failure address outranks a thank-you sentence: the evidence conflicts, so unknown.
    conflict = confirmed(base + "/error/unsuccessful", "Thank you for applying.")
    assert (
        conflict["confirmation_text"]
        and not conflict["no_failure_url"]
        and not conflict["confirmed"]
    )
    assert not GenericV1.rejected(conflict)
    # Sentences that deny their own success wording, and failure sentences, confirm nothing.
    for text in (
        "Your application was not received.",
        "We could not submit your application. Thank you for your interest.",
        "Thank you for applying, but there was an error saving your answers.",
        "Application incomplete",
        "Thank you for applying. Something went wrong, please try again.",
        "Thank you for your interest; unfortunately this role is no longer open.",
        "Application has not been submitted",
    ):
        checks = confirmed(base + "/jobs/42/done", text)
        assert not checks["confirmed"], text
    assert not confirmed(base + "/jobs/42/done", "Unsuccessfully submitted")["confirmation_text"]
    said = confirmed(base + "/jobs/42/done", "Something went wrong. Please try again.")
    assert not said["no_failure_text"] and "did not go through" in GenericV1.reason(said, {})
    # "Not" about something else does not take a thank-you back.
    for text in (
        "Thank you for applying, we will not share your data.",
        "Application submitted! You will not be able to edit it.",
        "Thank you for applying. If you do not hear from us in two weeks, the role was filled.",
    ):
        assert confirmed(base + "/jobs/42/done", text)["confirmed"], text
    # A posting that reads as closed, or "you already applied", is not this send's receipt.
    thanks = generic_page(base + "/jobs/42/done", "Thank you for your interest.")
    assert GenericV1.confirmed(form_url, thanks, [], before=before)["confirmed"]
    closed = {**thanks, "closed": True}
    assert not GenericV1.confirmed(form_url, closed, [], before=before)["confirmed"]
    already = {**thanks, "ats_markers": {**thanks["ats_markers"], "already_applied": True}}
    assert not GenericV1.confirmed(form_url, already, [], before=before)["confirmed"]


def test_accepted_post_is_never_reported_as_rejected():
    """T8a: a form error next to an accepted or unanswered POST is unknown, not "not sent"."""
    form_url = "https://careers.example.com/jobs/42/apply"
    before = generic_page(form_url, fields=[field("a", "Email", "x")])
    kept = generic_page(
        form_url, fields=[field("a", "Email", "x")], errors="An error occurred. Try again."
    )

    def checks(*statuses, unanswered=0):
        responses = [
            {"host": "careers.example.com", "path": "/apply", "status": s} for s in statuses
        ]
        result = GenericV1.confirmed(form_url, kept, responses, before=before)
        result["posts_answered"] = not unanswered
        return result

    # No request at all, or only a refused one: the form said no and nothing was stored.
    assert GenericV1.rejected(checks()) and GenericV1.rejected(checks(422))
    # The site accepted a POST (or redirected after one): the alert may be about anything.
    for accepted in ((200,), (201,), (302,), (303,), (422, 200)):
        result = checks(*accepted)
        assert result["post_accepted"] and not result["confirmed"]
        assert not GenericV1.rejected(result), accepted
    # A POST still in the air may have been stored too.
    assert not GenericV1.rejected(checks(unanswered=1))
    assert not GenericV1.rejected(checks(422, unanswered=1))
    # A POST to another host is not this site's answer.
    foreign = GenericV1.confirmed(
        form_url,
        kept,
        [{"host": "tracker.example.net", "path": "/b", "status": 200}],
        before=before,
    )
    assert not foreign["post_accepted"] and GenericV1.rejected(foreign)


def test_url_variants_of_one_job_cannot_both_submit(state):
    """T8b: one job is one application and one send, however its link is spelled."""
    first = workflow.enqueue(URL)
    for variant in (
        "https://job-boards.greenhouse.io:443/example/jobs/123",
        URL + "/",
        URL + "?gh_src=feed&utm_source=x",
        URL + "?extra=1",
        "https://boards.greenhouse.io/example/jobs/123",
        "https://boards.greenhouse.io/embed/job_app?for=example&token=123",
        "https://JOB-BOARDS.greenhouse.io/Example/jobs/123#app",
    ):
        again = workflow.enqueue(variant, source="keryx")
        assert again["already_exists"], variant
        assert again["application_id"] == first["application_id"], variant
        assert again["url"] == URL
    other_job = workflow.enqueue("https://job-boards.greenhouse.io/example/jobs/124")
    other_board = workflow.enqueue("https://job-boards.greenhouse.io/other/jobs/123")
    assert (
        len({first["application_id"], other_job["application_id"], other_board["application_id"]})
        == 3
    )
    # Where only a path names the job, the query may be what tells two jobs apart.
    one = workflow.enqueue("https://careers.example.com/job?jobId=1")
    two = workflow.enqueue("https://careers.example.com/job?jobId=2")
    assert one["application_id"] != two["application_id"]
    assert (
        workflow.enqueue("https://careers.example.com:443/job/?jobId=1&utm_medium=x")[
            "application_id"
        ]
        == one["application_id"]
    )
    with workflow.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM application_queue").fetchone()[0] == 5
        assert (
            conn.execute("SELECT COUNT(*) FROM application_jobs WHERE canonical=1").fetchone()[0]
            == 5
        )


def duplicate_row(state, url):
    """A second, later queue row for a job, as rows written before job keys existed can be."""
    application_id, package, _current = ready_application(state, url=URL)
    twin = "b" * 12
    with workflow.db() as conn:
        row = dict(
            conn.execute("SELECT * FROM application_queue WHERE id=?", (application_id,)).fetchone()
        )
        row.update(id=twin, url=url, source_url=url, created_at=workflow.now())
        conn.execute(
            "INSERT INTO application_queue VALUES(" + ",".join("?" * len(row)) + ")",
            tuple(row.values()),
        )
        # Forget the index, as on the first start after the upgrade.
        conn.execute("DELETE FROM job_index_meta")
    return application_id, twin, package


def test_existing_duplicate_rows_are_kept_and_only_one_of_them_can_send(state):
    """The migration tolerates duplicates that already exist; the claim does not."""
    first, twin, package = duplicate_row(state, "https://boards.greenhouse.io/example/jobs/123")
    with workflow.db() as conn:  # the next connection rebuilds the index
        rows = {
            r["application_id"]: r["canonical"]
            for r in conn.execute("SELECT application_id,canonical FROM application_jobs")
        }
        keys = {r[0] for r in conn.execute("SELECT job_key FROM application_jobs")}
    assert rows == {first: 1, twin: 0} and len(keys) == 1  # both kept, one job
    assert workflow.get(twin)["status"] == "READY_FOR_REVIEW"
    for application_id, message in ((first, "msg-1"), (twin, "msg-2")):
        worker.apply_command(
            {
                "kind": "submit",
                "application_id": application_id,
                "package_hash": package["package_hash"],
            },
            message,
        )
    submission.claim_attempt(first, package["package_hash"], "msg-1")
    with pytest.raises(PermissionError, match="already has an application") as refused:
        submission.claim_attempt(twin, package["package_hash"], "msg-2")
    assert worker.owner_words(str(refused.value)).startswith("This job already has")
    assert workflow.get(twin)["status"] == "READY_FOR_REVIEW"
    with workflow.db() as conn:
        attempts = [
            tuple(r) for r in conn.execute("SELECT application_id FROM live_submission_attempts")
        ]
        sends = [tuple(r) for r in conn.execute("SELECT application_id FROM job_sends")]
    assert attempts == [(first,)] and sends == [(first,)]
    # Unknown stays a send; only a verified "nothing was sent" frees the job for the twin.
    submission.finish_attempt(
        first, "UNKNOWN_SUBMISSION", {"package_hash": package["package_hash"]}
    )
    with pytest.raises(PermissionError, match="already has an application"):
        submission.claim_attempt(twin, package["package_hash"], "msg-2")
    submission.reconcile(first, "not-submitted", "msg-3")
    submission.claim_attempt(twin, package["package_hash"], "msg-2")
    assert workflow.get(twin)["status"] == "SUBMITTING"
    submission.finish_attempt(twin, "APPLIED", {"package_hash": package["package_hash"]})
    # The first row can be prepared again, but it can never send this job a second time.
    workflow.set_state(first, "READY_FOR_REVIEW", package_hash=package["package_hash"])
    worker.apply_command(
        {"kind": "submit", "application_id": first, "package_hash": package["package_hash"]},
        "msg-4",
    )
    with pytest.raises(PermissionError, match="already has an application"):
        submission.claim_attempt(first, package["package_hash"], "msg-4")


def test_a_manual_application_takes_the_jobs_one_send(state):
    """`applied` on a hand-finished application blocks a duplicate row from sending."""
    first, twin, package = duplicate_row(state, URL + "?variant=1")
    workflow.set_state(first, "MANUAL_TAKEOVER")
    submission.reconcile(first, "applied", "msg-1")
    assert workflow.get(first)["status"] == "APPLIED"
    worker.apply_command(
        {"kind": "submit", "application_id": twin, "package_hash": package["package_hash"]},
        "msg-2",
    )
    with pytest.raises(PermissionError, match="already has an application"):
        submission.claim_attempt(twin, package["package_hash"], "msg-2")
    with workflow.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM live_submission_attempts").fetchone()[0] == 0


def lever_page(url, success=False, verification=False, fields=(), errors="", challenge=False):
    return {
        "url": url,
        "fields": list(fields),
        "final_controls": [],
        "text": "",
        "ats_markers": {
            "lever_submit_success": success,
            "lever_verification_error": verification,
            "captcha_challenge": challenge,
            "form_error": errors,
            "status_region": "",
        },
    }


def test_lever_adapter_is_listed_before_generic_and_needs_the_thanks_page(monkeypatch):
    monkeypatch.setattr(
        workflow,
        "config",
        lambda: {
            "enabled": False,
            "submission_enabled": True,
            "submit_adapters": ["greenhouse_v1", "lever_v1", "generic_v1"],
        },
    )
    names = list(submission.ADAPTERS)
    assert names.index("greenhouse_v1") < names.index("lever_v1") < names.index("generic_v1")
    assert submission.enabled_adapter(LEVER) is LeverV1
    assert submission.enabled_adapter(URL) is GreenhouseV1
    assert submission.enabled_adapter("https://careers.example.com/jobs/42/apply") is GenericV1
    assert LeverV1.scope(LEVER) == ("jobs.lever.co", "acme", LEVER_POSTING)
    assert LeverV1.scope(LEVER.removesuffix("/apply")) == LeverV1.scope(LEVER)
    assert LeverV1.scope(LEVER.upper().replace("HTTPS://JOBS.LEVER.CO", "https://jobs.lever.co"))
    assert not LeverV1.matches("https://jobs.lever.co/acme")
    assert not LeverV1.matches("https://jobs.lever.co/acme/apply")
    assert not LeverV1.matches(f"https://attacker.example/acme/{LEVER_POSTING}/apply")
    thanks = LEVER.removesuffix("/apply") + "/thanks"
    apply_path = f"/acme/{LEVER_POSTING}/apply"
    sent = [{"host": "jobs.lever.co", "path": apply_path, "status": 303}]
    before = lever_page(LEVER, fields=[field("a", "Email", "alex@example.invalid")])

    def confirmed(after, responses=()):
        return LeverV1.confirmed(LEVER, after, list(responses), before=before)

    ok = confirmed(lever_page(thanks + "?utm=1", success=True), sent)
    assert ok["confirmed"] and not LeverV1.rejected(ok)
    assert ok["post_status"] == 303 and ok["post_accepted"] and not ok["post_rejected"]
    # The POST status is evidence; the thanks URL, its heading and no form are the rule.
    assert confirmed(lever_page(thanks, success=True))["confirmed"]
    assert confirmed(lever_page(thanks, success=True))["post_status"] is None
    assert not confirmed(lever_page(thanks), sent)["confirmed"]
    assert not confirmed(lever_page(LEVER, success=True), sent)["confirmed"]
    still = lever_page(thanks, success=True, fields=[field("a", "Email", "")])
    assert not confirmed(still, sent)["confirmed"]
    other = "https://jobs.lever.co/acme/7b1e9d2c-3a4f-4e5b-8c6d-9e0f1a2b3c4d/thanks"
    assert not confirmed(lever_page(other, success=True), sent)["confirmed"]
    foreign = [{"host": "attacker.example", "path": apply_path, "status": 303}]
    assert confirmed(lever_page(thanks, success=True), foreign)["post_status"] is None
    # The CAPTCHA's verification error: the form came back empty, nothing was sent.
    cleared = lever_page(LEVER, verification=True, fields=[field("a", "Email", "")])
    checks = confirmed(cleared, [{"host": "jobs.lever.co", "path": apply_path, "status": 200}])
    assert checks["captcha_rejected"] and LeverV1.rejected(checks) and not checks["confirmed"]
    assert LeverV1.reason(checks, cleared) == (
        "Lever's CAPTCHA rejected the send; open the recruiting browser, solve it and "
        "press Submit yourself, then reply applied"
    )
    # A form kept open with a new validation message is not sent either; a stale one is not.
    invalid = lever_page(LEVER, fields=[field("a", "Email", "x")], errors="Email is invalid")
    checks = confirmed(invalid)
    assert LeverV1.rejected(checks) and "Email is invalid" in LeverV1.reason(checks, invalid)
    before["ats_markers"]["form_error"] = "Email is invalid"
    assert not LeverV1.rejected(confirmed(invalid))
    # Anything else is unknown, and the reason names the page the owner should look for.
    quiet = lever_page(LEVER, fields=[field("a", "Email", "x")])
    assert not LeverV1.rejected(confirmed(quiet))
    assert "Application submitted!" in LeverV1.reason(confirmed(quiet), quiet)
    challenge = lever_page(LEVER, fields=[field("a", "Email", "x")], challenge=True)
    assert not LeverV1.rejected(confirmed(challenge))
    assert "solve it" in LeverV1.reason(confirmed(challenge), challenge)
    moved = lever_page(LEVER.removesuffix("/apply"), fields=[field("a", "Email", "x")])
    assert confirmed(moved)["url_changed"] and not LeverV1.rejected(confirmed(moved))


def test_preflight_accepts_only_the_reviewed_unchanged_form(state):
    application_id, package, current = ready_application(state)
    assert submission.preflight(application_id, package["package_hash"], current)["url"] == URL
    with pytest.raises(PermissionError, match="package changed"):
        submission.preflight(application_id, "0" * 64, current)
    changed = {
        **current,
        "fields": [{**current["fields"][0], "value": "Injected"}, *current["fields"][1:]],
    }
    with pytest.raises(PermissionError, match="form changed"):
        submission.preflight(application_id, package["package_hash"], changed)
    with pytest.raises(PermissionError, match="destination"):
        submission.preflight(
            application_id, package["package_hash"], {**current, "url": URL + "?x"}
        )
    unselected = {
        **current,
        "fields": [
            current["fields"][0],
            {**current["fields"][1], "selected": None},
            current["fields"][2],
        ],
    }
    with pytest.raises(PermissionError, match="form changed"):
        submission.preflight(application_id, package["package_hash"], unselected)
    (state / "applications" / application_id / "resume.pdf").write_bytes(b"tampered")
    with pytest.raises(PermissionError, match="resume"):
        submission.preflight(application_id, package["package_hash"], current)


def test_claim_needs_exact_owner_approval_and_never_repeats(state):
    application_id, package, _current = ready_application(state)
    package_hash = package["package_hash"]
    with pytest.raises(PermissionError, match="owner approval"):
        submission.claim_attempt(application_id, package_hash, "msg-1")
    with pytest.raises(PermissionError, match="exact package"):
        worker.apply_command(
            {"kind": "submit", "application_id": application_id, "package_hash": "1" * 64}, "msg-0"
        )
    worker.apply_command(
        {"kind": "submit", "application_id": application_id, "package_hash": package_hash}, "msg-1"
    )
    submission.claim_attempt(application_id, package_hash, "msg-1")
    assert workflow.get(application_id)["status"] == "SUBMITTING"
    with pytest.raises(PermissionError):
        submission.claim_attempt(application_id, package_hash, "msg-1")
    submission.finish_attempt(
        application_id, "UNKNOWN_SUBMISSION", {"package_hash": package_hash, "reason": "timeout"}
    )
    assert workflow.get(application_id)["status"] == "UNKNOWN_SUBMISSION"
    with pytest.raises(PermissionError, match="cannot be prepared"):
        worker.apply_command({"kind": "resume", "application_id": application_id}, "msg-2")
    worker.apply_command(
        {"kind": "reconcile", "application_id": application_id, "outcome": "not-submitted"}, "msg-3"
    )
    assert workflow.get(application_id)["status"] == "NEEDS_USER"
    workflow.set_state(application_id, "READY_FOR_REVIEW", package_hash=package_hash)
    worker.apply_command(
        {"kind": "submit", "application_id": application_id, "package_hash": package_hash}, "msg-4"
    )
    submission.claim_attempt(application_id, package_hash, "msg-4")
    submission.finish_attempt(application_id, "APPLIED", {"package_hash": package_hash})
    assert workflow.get(application_id)["status"] == "APPLIED"
    with workflow.db() as conn:
        assert (
            conn.execute("SELECT status FROM live_submission_attempts").fetchone()[0] == "APPLIED"
        )
        kinds = [r[0] for r in conn.execute("SELECT kind FROM application_events ORDER BY id")]
    assert (
        "submission_unknown" in kinds and "submission_confirmed" in kinds and "lifecycle" in kinds
    )
    assert (state / "applications" / application_id / "receipt.json").exists()


def test_owner_commands_for_submission_lifecycle_parse_strictly():
    good = lambda text: {"author": {"id": "owner"}, "content": text}
    parse = lambda text: worker.parse_command(good(text), "owner", "control", {"control"})
    assert parse("submit abcdef012345 " + "a" * 64)["kind"] == "submit"
    assert parse("submit abcdef012345 " + "a" * 7) is None
    assert parse("submit abcdef012345 " + "a" * 12)["package_hash"] == "a" * 12
    assert parse("proceed abcdef012345") == {"kind": "proceed", "application_id": "abcdef012345"}
    assert parse("reconcile abcdef012345 applied")["outcome"] == "applied"
    assert parse("reconcile abcdef012345 maybe") is None
    assert parse("please submit abcdef012345 " + "a" * 64) is None
    # A word reply means nothing outside an application's own thread.
    assert parse("send it") is None and parse("go") is None


def test_send_it_in_the_thread_binds_the_current_package_once(state):
    application_id, package, _current = ready_application(state)
    workflow.set_state(application_id, "READY_FOR_REVIEW", thread_id="t1")
    threads = {"t1": application_id}
    parse = lambda text: worker.parse_command(
        {"author": {"id": "owner"}, "content": text}, "owner", "t1", {"t1"}, threads
    )
    command = parse("send it")
    assert command["package_hash"] == package["package_hash"]
    worker.apply_command(command, "msg-1")
    submission.claim_attempt(application_id, package["package_hash"], "msg-1")
    with pytest.raises(ValueError, match="already in flight"):
        parse("send it")
    with workflow.db() as conn:
        payload = json.loads(
            conn.execute("SELECT payload FROM owner_commands WHERE message_id='msg-1'").fetchone()[
                0
            ]
        )
    assert payload["package_hash"] == package["package_hash"] and payload["word"] == "send it"


def test_tick_executes_one_approved_submission_and_recovers_crashed_runs(state, monkeypatch):
    monkeypatch.setattr(
        workflow,
        "config",
        lambda: {"enabled": True, "submission_enabled": True, "submit_adapters": ["greenhouse_v1"]},
    )
    monkeypatch.setattr(worker, "poll_commands", lambda: None)
    monkeypatch.setattr(worker, "discord", lambda *a, **k: {})
    monkeypatch.setattr(workflow, "discord", lambda *a, **k: {})
    application_id, package, _ = ready_application(state)
    stale = workflow.enqueue("https://jobs.example.com/crashed")["application_id"]
    workflow.set_state(stale, "PREPARING")
    with workflow.db() as conn:
        conn.execute(
            "UPDATE application_queue SET updated_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), stale),
        )
    calls = []
    monkeypatch.setattr(
        worker,
        "browser_call",
        lambda action, **kw: calls.append((action, kw)) or {"status": "APPLIED"},
    )
    worker.apply_command(
        {
            "kind": "submit",
            "application_id": application_id,
            "package_hash": package["package_hash"],
        },
        "msg-1",
    )
    result = worker.tick()
    assert [c[0] for c in calls] == ["submit"]
    assert calls[0][1]["package_hash"] == package["package_hash"]
    assert result["submissions"][0]["outcome"] == "executed"
    assert workflow.get(stale)["status"] == "NEEDS_USER"
    assert workflow.get(stale)["error"] == "preparation_interrupted"
    with workflow.db() as conn:
        assert (
            conn.execute("SELECT status FROM owner_commands WHERE message_id='msg-1'").fetchone()[0]
            == "executed"
        )
    assert worker.tick() == {
        "idle": True
    }  # nothing approved; the recovered run now waits on the owner


def test_failed_submission_request_leaves_the_package_reviewable(state, monkeypatch):
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": True})
    monkeypatch.setattr(worker, "poll_commands", lambda: None)
    monkeypatch.setattr(workflow, "discord", lambda *a, **k: {})
    application_id, package, _ = ready_application(state)
    worker.apply_command(
        {
            "kind": "submit",
            "application_id": application_id,
            "package_hash": package["package_hash"],
        },
        "msg-1",
    )

    def refuse(action, **kw):
        raise RuntimeError("The visible form changed after review")

    monkeypatch.setattr(worker, "browser_call", refuse)
    result = worker.tick()
    assert result["submissions"][0]["outcome"] == "failed"
    assert workflow.get(application_id)["status"] == "READY_FOR_REVIEW"
    with workflow.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM live_submission_attempts").fetchone()[0] == 0
        assert (
            conn.execute("SELECT status FROM owner_commands WHERE message_id='msg-1'").fetchone()[0]
            == "failed"
        )


def test_queue_holds_for_waiting_applications_unless_owner_resumes(state):
    first = workflow.enqueue("https://jobs.example.com/a", source="keryx")["application_id"]
    second = workflow.enqueue("https://jobs.example.com/b", source="keryx")["application_id"]
    workflow.set_state(first, "NEEDS_USER")
    assert worker.next_queued(1) is None
    assert worker.next_queued(2) == second
    # A link the owner pasted is worked next, however many holds are waiting.
    pasted = workflow.enqueue("https://jobs.example.com/pasted")["application_id"]
    assert worker.next_queued(1) == pasted
    workflow.set_state(pasted, "DEFERRED")
    worker.apply_command({"kind": "resume", "application_id": first}, "msg-1")
    assert worker.next_queued(1) == first


def test_erga_confirmation_works_on_a_thread_that_already_runs_an_event_loop(state, monkeypatch):
    """A verified submission is mirrored into Erga from the browser service, whose thread
    already runs the browser library's event loop. The confirmation must go through
    there rather than fail because a second loop cannot start on that thread."""
    import asyncio

    from rove import resumes

    application_id = workflow.enqueue(URL)["application_id"]
    directory = state / "applications" / application_id
    directory.mkdir(parents=True)
    (directory / "resume-manifest.json").write_text(json.dumps({"application_id": "erga-123"}))
    calls = []

    async def erga_call(name, arguments):
        calls.append((name, arguments))
        return {"result": {"status": "applied"}}

    monkeypatch.setattr(resumes, "erga_call", erga_call)
    confirmed = {"synced": True, "application_id": "erga-123", "status": "applied"}

    async def as_the_browser_service_calls_it():
        return submission.erga_confirm(application_id)

    assert asyncio.run(as_the_browser_service_calls_it()) == confirmed
    assert submission.erga_confirm(application_id) == confirmed  # and with no loop running
    sent = {"application_id": "erga-123", "status": "applied", "used_generated_resume": False}
    assert calls == [("confirm_application_submission", sent)] * 2

    async def erga_down(name, arguments):
        raise RuntimeError("Erga could not complete this operation")

    monkeypatch.setattr(resumes, "erga_call", erga_down)
    with pytest.raises(RuntimeError, match="Erga could not complete"):
        asyncio.run(as_the_browser_service_calls_it())
