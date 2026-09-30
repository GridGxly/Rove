"""Owner-bound, single-attempt submission of an unchanged prepared package.

Only the trusted browser daemon runs this, after the worker relays an authenticated
owner approval for one exact package hash. The model never receives a submit tool.
The attempt is claimed durably before the click; an unclear result stays
UNKNOWN_SUBMISSION until the owner reconciles it. Nothing here retries a click.
"""

import asyncio
import hashlib
import json
import re
import shutil
from urllib.parse import urlsplit

from patchright.sync_api import Error as PlaywrightError

from . import workflow
from .live_browser import ERROR_SELECTOR, MESSAGES_JS, STATUS_SELECTOR
from .onboarding import digest, read_approved
from .runtime import state_root, write_private

CONFIRMATION_TIMEOUT_MS = 45000

# The generic contract's wording. The same source patterns drive the Python checks and
# the in-page wait, so the two never disagree about what counts as a signal.
GENERIC_URL_TOKENS = r"confirmation|thank|success|submitted|complete|received"
GENERIC_SUCCESS = (
    r"thank you for (applying|your (application|interest))"
    r"|application (has been |was )?(submitted|received|complete)"
    r"|we('ve| have) received your application"
    r"|successfully (submitted|applied)"
)
GENERIC_ERROR = r"required|invalid|error|could not|try again"
GENERIC_URL_RE = re.compile(GENERIC_URL_TOKENS, re.IGNORECASE)
GENERIC_SUCCESS_RE = re.compile(GENERIC_SUCCESS, re.IGNORECASE)
GENERIC_ERROR_RE = re.compile(GENERIC_ERROR, re.IGNORECASE)

# Polled after the click: true once the page left, the form is gone, or a success or
# validation message appeared that was not in the observation taken before the click.
GENERIC_SETTLED_JS = r"""({start, text_hits, status_region, form_error, success, error, status_selector, error_selector}) => {
 try {
  if (location.href !== start) return true;
  const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' && e.getAttribute('aria-hidden')!=='true';
  if (![...document.querySelectorAll('input,textarea,select')].some(e=>e.type!=='hidden' && visible(e))) return true;
  if ((document.body.innerText.slice(0,15000).match(new RegExp(success,'gi'))||[]).length > text_hits) return true;
  const messages=__MESSAGES__;
  const status=messages(status_selector), errors=messages(error_selector);
  return (status!==status_region && new RegExp(success,'i').test(status))
      || (errors!==form_error && new RegExp(error,'i').test(errors));
 } catch (e) { return false; }
}""".replace("__MESSAGES__", MESSAGES_JS)


def normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def host_matches(host: str | None, suffixes: tuple[str, ...]) -> bool:
    host = (host or "").lower()
    return any(host == s or host.endswith("." + s) for s in suffixes)


def url_tokens(url: str) -> set[str]:
    parsed = urlsplit(url)
    return {m.group(0).lower() for m in GENERIC_URL_RE.finditer(parsed.path + "?" + parsed.query)}


def success_phrases(text: str | None) -> set[str]:
    return {normalized(m.group(0)) for m in GENERIC_SUCCESS_RE.finditer(text or "")}


def package_digest(package: dict) -> str:
    body = {k: v for k, v in package.items() if k != "package_hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class GreenhouseV1:
    """Public Greenhouse job board (job-boards.greenhouse.io).

    Its client posts JSON to boards.greenhouse.io/{board}/jobs/{id} and, only on an OK
    response, navigates to /{board}/jobs/{id}/confirmation, which renders the employer's
    confirmation message inside `.confirmation__content`. All four signals are required.
    """

    name = "greenhouse_v1"
    board_hosts = ("job-boards.greenhouse.io",)
    submit_host = "boards.greenhouse.io"
    marker = "greenhouse_confirmation"

    @classmethod
    def scope(cls, url: str) -> tuple[str, str] | None:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        parts = parsed.path.strip("/").split("/")
        if (
            host in cls.board_hosts
            and len(parts) >= 3
            and parts[1] == "jobs"
            and parts[2].isdigit()
        ):
            return parts[0], parts[2]
        return None

    @classmethod
    def matches(cls, url: str) -> bool:
        return cls.scope(url) is not None

    @classmethod
    def response_hosts(cls, package_url: str) -> tuple[str, ...]:
        return (cls.submit_host, *cls.board_hosts)

    @classmethod
    def await_result(cls, page, before: dict, timeout_ms: int):
        try:
            page.wait_for_url(re.compile(r"/confirmation/?(\?.*)?$"), timeout=timeout_ms)
        except PlaywrightError:
            pass

    @classmethod
    def reason(cls, checks: dict, after: dict) -> str:
        if checks["post_rejected"] and not checks["post_accepted"]:
            return (
                "The ATS rejected the submission request and the form is still open. "
                "Check the visible page for validation messages before reconciling."
            )
        return "Independent confirmation is incomplete; investigate before any retry."

    @classmethod
    def confirmed(
        cls, package_url: str, after: dict, responses: list[dict], before: dict | None = None
    ) -> dict:
        # `before` is unused: the board's own contract does not depend on the prior page.
        board, job = cls.scope(package_url)
        post_path = f"/{board}/jobs/{job}"
        posts = [r for r in responses if r["host"] == cls.submit_host and r["path"] == post_path]
        parsed = urlsplit(after.get("url", ""))
        checks = {
            "post_accepted": any(200 <= r["status"] < 300 for r in posts),
            "post_rejected": any(r["status"] >= 400 for r in posts),
            "confirmation_url": (parsed.hostname or "").lower() in cls.board_hosts
            and parsed.path.rstrip("/") == post_path + "/confirmation",
            "confirmation_content": after.get("ats_markers", {}).get(cls.marker) is True,
            "form_gone": not after.get("fields") and not after.get("final_controls"),
        }
        checks["confirmed"] = (
            checks["post_accepted"]
            and not checks["post_rejected"]
            and checks["confirmation_url"]
            and checks["confirmation_content"]
            and checks["form_gone"]
        )
        return checks


class GenericV1:
    """Employer sites without an ATS contract; list it last so specific adapters win.

    There is no known request to watch, so the result is read from the page and compared
    with the observation taken before the click. Confirmed needs a signal that was not
    there before (a confirmation-looking URL, a thank-you sentence, or a success region)
    and a form that left (no fields, or a new URL). A new validation message means the
    form rejected the attempt. Everything else is an unknown submission.
    """

    name = "generic_v1"
    schemes = ("https",)

    @classmethod
    def scope(cls, url: str) -> tuple[str, str] | None:
        parsed = urlsplit(url)
        if parsed.scheme in cls.schemes and parsed.hostname:
            return parsed.hostname.lower(), parsed.path.rstrip("/")
        return None

    @classmethod
    def matches(cls, url: str) -> bool:
        return cls.scope(url) is not None

    @classmethod
    def response_hosts(cls, package_url: str) -> tuple[str, ...]:
        return ((urlsplit(package_url).hostname or "").lower(),)

    @classmethod
    def await_result(cls, page, before: dict, timeout_ms: int):
        markers = before.get("ats_markers") or {}
        try:
            page.wait_for_function(
                GENERIC_SETTLED_JS,
                arg={
                    "start": before["url"],
                    "text_hits": sum(
                        1 for _ in GENERIC_SUCCESS_RE.finditer(before.get("text") or "")
                    ),
                    "status_region": markers.get("status_region") or "",
                    "form_error": markers.get("form_error") or "",
                    "success": GENERIC_SUCCESS,
                    "error": GENERIC_ERROR,
                    "status_selector": STATUS_SELECTOR,
                    "error_selector": ERROR_SELECTOR,
                },
                timeout=timeout_ms,
            )
        except PlaywrightError:
            pass

    @classmethod
    def confirmed(
        cls, package_url: str, after: dict, responses: list[dict], before: dict | None = None
    ) -> dict:
        before = before or {}
        posts = [r for r in responses if host_matches(r["host"], cls.response_hosts(package_url))]
        after_url = after.get("url", "")
        markers = after.get("ats_markers") or {}
        prior = before.get("ats_markers") or {}
        errors = markers.get("form_error") or ""
        checks = {
            # Same-host POST statuses are evidence for the owner, not a confirmation.
            "post_accepted": any(200 <= r["status"] < 300 for r in posts),
            "post_rejected": any(r["status"] >= 400 for r in posts),
            "url_changed": after_url != package_url,
            "confirmation_url": bool(url_tokens(after_url) - url_tokens(package_url)),
            "confirmation_text": bool(
                success_phrases(after.get("text")) - success_phrases(before.get("text"))
            ),
            "confirmation_region": bool(
                success_phrases(markers.get("status_region"))
                - success_phrases(prior.get("status_region"))
            ),
            "form_gone": not after.get("fields") and not after.get("final_controls"),
            "no_form_error": not (
                GENERIC_ERROR_RE.search(errors) and errors != (prior.get("form_error") or "")
            ),
        }
        checks["confirmed"] = (
            (
                checks["confirmation_url"]
                or checks["confirmation_text"]
                or checks["confirmation_region"]
            )
            and (checks["form_gone"] or checks["url_changed"])
            and checks["no_form_error"]
        )
        return checks

    @classmethod
    def reason(cls, checks: dict, after: dict) -> str:
        if not checks["no_form_error"]:
            message = (after.get("ats_markers") or {}).get("form_error") or ""
            excerpt = " ".join(message.split())[:200]
            return (
                f"The form reported a validation error and stayed open: {excerpt} "
                "Check the highlighted field in the recruiting browser; do not click Submit again."
            )
        if checks["post_rejected"] and not checks["post_accepted"]:
            return (
                "The site rejected a submission request and showed no confirmation. "
                "Check the visible page before reconciling."
            )
        return (
            "No confirmation URL, sentence or status region appeared after the click; "
            "check the recruiting browser and employer email before reconciling."
        )


ADAPTERS = {GreenhouseV1.name: GreenhouseV1, GenericV1.name: GenericV1}


def enabled_adapter(url: str):
    settings = workflow.config()
    if not settings.get("submission_enabled"):
        raise PermissionError("Final submission is disabled in the local workflow configuration")
    # The first listed adapter that matches wins, so the catch-all belongs at the end.
    for name in settings.get("submit_adapters", []):
        adapter = ADAPTERS.get(name)
        if adapter and adapter.matches(url):
            return adapter
    raise PermissionError("No enabled submission adapter supports this application page")


def form_state(fields: list[dict]) -> list[dict]:
    # DOM reference numbers are transient; question identities and values are not.
    keys = (
        "key",
        "label",
        "kind",
        "role",
        "required",
        "disabled",
        "readonly",
        "value",
        "checked",
        "selected",
        "selection_code",
        "options",
    )
    return sorted([{k: f.get(k) for k in keys} for f in fields], key=lambda f: str(f["key"]))


def preflight(application_id: str, package_hash: str, current: dict) -> dict:
    """Everything the owner reviewed must still be exactly what the browser shows."""
    item = workflow.get(application_id)
    directory = state_root() / "applications" / application_id
    package = json.loads((directory / "package.json").read_text())
    if package_digest(package) != package_hash or item["package_hash"] != package_hash:
        raise PermissionError("The reviewed package changed; prepare and review again")
    if item["status"] != "READY_FOR_REVIEW":
        raise PermissionError("Application is not ready for submission")
    if package["pending"] or len(package["final_controls"]) != 1:
        raise PermissionError("Resolve every question and verify one final application control")
    approved = read_approved()
    frozen = json.loads((directory / "profile.json").read_text())
    if (
        digest(frozen["profile"]) != package["profile_hash"]
        or approved["profile_hash"] != package["profile_hash"]
    ):
        raise PermissionError("Approved profile changed after preparation")
    if current["url"] != package["url"] or current.get("manual_takeover_required"):
        raise PermissionError("The application destination or authentication state changed")
    if form_state(current["fields"]) != form_state(package["form_state"]):
        raise PermissionError("The visible form changed after review; prepare and review again")
    if current.get("final_controls") != package["final_controls"]:
        raise PermissionError("The final application control changed")
    resume = directory / "resume.pdf"
    actual = hashlib.sha256(resume.read_bytes()).hexdigest() if resume.is_file() else None
    manifest_path = directory / "resume-manifest.json"
    expected = package["resume_sha256"]
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if not manifest.get("ready") or manifest.get("resume_sha256") != expected:
            raise PermissionError("The frozen resume manifest does not match the package")
    uploaded = [f for f in package["filled"] if f.get("sha256")]
    if not expected or actual != expected or not any(f["sha256"] == expected for f in uploaded):
        raise PermissionError("The frozen resume was not uploaded or changed after review")
    # An empty required widget, including a search input with no committed selection,
    # cannot be excused by a generated proposal or a package hash.
    for field in current["fields"]:
        if not field["required"] or field["disabled"]:
            continue
        if field["role"] == "combobox" and not field.get("selected"):
            raise PermissionError("A required dropdown has no committed selection")
        if field["kind"] in {"checkbox", "radio"}:
            if not field.get("checked") and not any(
                f["kind"] == field["kind"] and f.get("checked") and f["name"] == field["name"]
                for f in current["fields"]
            ):
                raise PermissionError("A required choice is not selected")
        elif field["kind"] != "file" and field["role"] != "combobox" and not field.get("value"):
            raise PermissionError("A required answer is empty")
    return package


def claim_attempt(application_id: str, package_hash: str, owner_message_id: str):
    """Commit before the irreversible click; a crash is never permission to retry."""
    with workflow.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        command = conn.execute(
            "SELECT kind,payload,status FROM owner_commands WHERE message_id=? AND application_id=?",
            (owner_message_id, application_id),
        ).fetchone()
        if (
            not command
            or command["kind"] != "submit"
            or command["status"] != "applied"
            or json.loads(command["payload"]).get("package_hash") != package_hash
        ):
            raise PermissionError("No authenticated owner approval for this exact package")
        item = conn.execute(
            "SELECT status,package_hash FROM application_queue WHERE id=?", (application_id,)
        ).fetchone()
        if not item or item["status"] != "READY_FOR_REVIEW" or item["package_hash"] != package_hash:
            raise PermissionError("Application is not ready for this approval")
        prior = conn.execute(
            "SELECT status FROM live_submission_attempts WHERE application_id=?", (application_id,)
        ).fetchone()
        if prior and prior["status"] != "NOT_SUBMITTED":
            raise PermissionError("A submission attempt already exists; do not retry")
        conn.execute(
            "INSERT OR REPLACE INTO live_submission_attempts VALUES(?,?,?,?,?)",
            (application_id, package_hash, owner_message_id, "SUBMITTING", workflow.now()),
        )
        conn.execute(
            "UPDATE application_queue SET status='SUBMITTING',updated_at=? WHERE id=?",
            (workflow.now(), application_id),
        )


def erga_confirm(application_id: str) -> dict:
    """Mirror a verified submission into Erga through its supported confirmation."""
    from .resumes import erga_call

    manifest_path = state_root() / f"applications/{application_id}/resume-manifest.json"
    erga_id = None
    if manifest_path.exists():
        erga_id = json.loads(manifest_path.read_text()).get("application_id")
    if not erga_id:
        return {"synced": False, "warning": "No Erga application is linked to this package"}
    result = asyncio.run(
        erga_call(
            "confirm_application_submission",
            {"application_id": erga_id, "status": "applied", "used_generated_resume": False},
        )
    )
    data = result.get("result", result)
    return {"synced": True, "application_id": erga_id, "status": data.get("status")}


def finish_attempt(application_id: str, status: str, evidence: dict):
    if status not in {"APPLIED", "UNKNOWN_SUBMISSION"}:
        raise ValueError("Invalid submission outcome")
    directory = state_root() / f"applications/{application_id}"
    receipt = directory / "receipt.json"
    if receipt.exists():
        shutil.move(receipt, directory / f"receipt-superseded-{int(receipt.stat().st_mtime)}.json")
    write_private(receipt, evidence)
    with workflow.db() as conn:
        conn.execute(
            "UPDATE live_submission_attempts SET status=? WHERE application_id=?",
            (status, application_id),
        )
    if status == "APPLIED":
        try:
            evidence["erga"] = erga_confirm(application_id)
        except Exception as error:  # noqa: BLE001 -- the submission already happened; record, do not hide
            evidence["erga"] = {"synced": False, "error": type(error).__name__}
        write_private(receipt, evidence)
        workflow.record(application_id, "submission_confirmed", evidence)
        workflow.transition(
            application_id,
            "APPLIED",
            "verified employer confirmation after one owner-approved submit",
            f"receipt {receipt.name}",
        )
    else:
        workflow.record(application_id, "submission_unknown", evidence)
        workflow.transition(
            application_id,
            "UNKNOWN_SUBMISSION",
            "submit clicked once; confirmation incomplete",
            str(evidence.get("reason", "")),
        )
        workflow.action_needed(
            application_id,
            "One submission was attempted but not confirmed. Do not click Submit again. "
            "Check the recruiting browser and any employer email, then tell me the outcome.",
            commands=[
                f"reconcile {application_id} applied",
                f"reconcile {application_id} not-submitted",
            ],
            headline="Submission unclear",
        )


def reconcile(application_id: str, outcome: str, owner_message_id: str):
    """Owner-verified resolution of an unknown attempt; never inferred by code."""
    item = workflow.get(application_id)
    if item["status"] not in {"UNKNOWN_SUBMISSION", "MANUAL_TAKEOVER"}:
        raise PermissionError(
            "Only an unknown submission or a manual application can be reconciled"
        )
    if outcome == "applied":
        finish_attempt(
            application_id,
            "APPLIED",
            {
                "application_id": application_id,
                "package_hash": item["package_hash"],
                "status": "APPLIED",
                "confirmed_at": workflow.now(),
                "reason": "Owner verified the employer confirmation independently"
                if item["status"] == "UNKNOWN_SUBMISSION"
                else "Owner applied manually outside the recruiting browser",
                "owner_message_id": owner_message_id,
            },
        )
        return
    if outcome != "not-submitted":
        raise ValueError("Reconcile outcome must be applied or not-submitted")
    with workflow.db() as conn:
        conn.execute(
            "UPDATE live_submission_attempts SET status='NOT_SUBMITTED' WHERE application_id=?",
            (application_id,),
        )
    workflow.transition(
        application_id,
        "NEEDS_USER",
        "owner verified nothing was submitted",
        f"owner message {owner_message_id}; prepare and review again before another attempt",
    )


def submit(browser, application_id: str, package_hash: str, owner_message_id: str) -> dict:
    browser.check(application_id)
    with browser.guarded(browser.page):
        return _submit(browser, application_id, package_hash, owner_message_id)


def _submit(browser, application_id: str, package_hash: str, owner_message_id: str) -> dict:
    current = browser.observe()
    adapter = enabled_adapter(current["url"])
    package = preflight(application_id, package_hash, current)
    if adapter.scope(current["url"]) != adapter.scope(package["url"]):
        raise PermissionError("The live page is not the reviewed employer job")
    control = package["final_controls"][0]
    locator = browser.page.locator(f'[data-autopilot-submit="{int(control["ref"])}"]')
    if locator.count() != 1 or not locator.is_enabled():
        raise PermissionError("Final application control is unavailable")
    if normalized(locator.inner_text()) != normalized(control["label"]):
        raise PermissionError("Final application control changed")
    directory = state_root() / f"applications/{application_id}"
    if (directory / "browser.png").exists():
        shutil.copyfile(directory / "browser.png", directory / "form-before-submit.png")
        (directory / "form-before-submit.png").chmod(0o600)
    claim_attempt(application_id, package_hash, owner_message_id)
    workflow.record(
        application_id,
        "submit_attempt",
        {
            "package_hash": package_hash,
            "owner_message_id": owner_message_id,
            "adapter": adapter.name,
        },
    )
    workflow.flush_events(application_id)
    responses = []
    hosts = adapter.response_hosts(package["url"])

    def observe_response(response):
        url = urlsplit(response.url)
        if response.request.method == "POST" and host_matches(url.hostname, hosts):
            # No headers, body, query strings, applicant fields or tokens in receipt logs.
            responses.append({"host": url.hostname, "path": url.path, "status": response.status})

    browser.page.on("response", observe_response)
    result = {
        "application_id": application_id,
        "package_hash": package_hash,
        "adapter": adapter.name,
        "status": "UNKNOWN_SUBMISSION",
        "attempted_at": workflow.now(),
        "form_screenshot": str(directory / "form-before-submit.png"),
    }
    try:
        # The code-owned preparation guard is armed for this one observed click only.
        browser.page.evaluate(
            "() => document.documentElement.setAttribute('data-erga-submit-armed', '1')"
        )
        browser.click(locator)
        # Bounded: the adapter waits for its own signal, then the page is read once.
        adapter.await_result(browser.page, current, CONFIRMATION_TIMEOUT_MS)
        browser.page.wait_for_load_state("domcontentloaded", timeout=15000)
        after = browser.observe()
        checks = adapter.confirmed(package["url"], after, responses, before=current)
        result["checks"] = checks
        if checks["confirmed"]:
            result.update(
                status="APPLIED",
                confirmed_at=workflow.now(),
                confirmation_url=after["url"],
                confirmation_text=after.get("text", "")[:4000],
                screenshot=after.get("screenshot"),
            )
        else:
            result["reason"] = adapter.reason(checks, after)
    except Exception as error:  # noqa: BLE001 -- any uncertainty after the claim must stay durable
        result["reason"] = "No independent confirmation: " + type(error).__name__
    finally:
        try:
            browser.page.evaluate(
                "() => document.documentElement.removeAttribute('data-erga-submit-armed')"
            )
        except PlaywrightError:
            pass
        browser.page.remove_listener("response", observe_response)
        result["responses"] = responses
        finish_attempt(application_id, result["status"], result)
    if result["status"] == "APPLIED":
        browser.close_run(application_id)
    return result
