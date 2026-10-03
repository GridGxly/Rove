"""Owner-bound, single-attempt submission of an unchanged prepared package.

Only the trusted browser daemon runs this, after the worker relays an authenticated
owner approval for one exact package hash. The model never receives a submit tool.
The attempt is claimed durably before the click; an unclear result stays
UNKNOWN_SUBMISSION until the owner reconciles it. Nothing here retries a click.
"""

import asyncio
import contextlib
import hashlib
import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote_plus, urlsplit

from patchright.sync_api import Error as PlaywrightError

from . import boards, job_index, live_browser, timing, workflow
from .destinations import ineligible, tenant_words
from .live_browser import ERROR_SELECTOR, MESSAGES_JS, PLAIN_STOP, STATUS_SELECTOR
from .onboarding import digest, read_approved
from .runtime import state_root, write_private

CONFIRMATION_TIMEOUT_MS = 45000
# How long one send can take in the browser service, from its claim to its recorded
# outcome: the confirmation wait, the page load after it, and slack for a service that
# was busy with another request. A claim older than this with no outcome is a send whose
# service died between the click and its record.
STALE_SEND_AFTER = timedelta(milliseconds=CONFIRMATION_TIMEOUT_MS) + timedelta(minutes=5)

# How long a generic success signal has to stand without an error appearing after it.
GENERIC_QUIET_MS = 2500

# The generic contract's wording. The same source patterns drive the Python checks and
# the in-page wait, so the two never disagree about what counts as a signal.
#
# URL words are whole words of the path and query: "incomplete" is not "complete" and
# "unsuccessful" is not "success". A URL that also carries a failure word says nothing.
# A URL is supporting evidence only; it never confirms a submission by itself.
GENERIC_URL_WORDS = frozenset(
    {
        "confirmation",
        "confirmed",
        "thank",
        "thanks",
        "thankyou",
        "success",
        "successful",
        "submitted",
        "complete",
        "completed",
        "received",
    }
)
GENERIC_URL_FAILURE = frozenset(
    {
        "not",
        "error",
        "errors",
        "fail",
        "failed",
        "failure",
        "invalid",
        "unsuccessful",
        "incomplete",
        "denied",
        "rejected",
        "expired",
        "login",
        "signin",
        "retry",
    }
)
GENERIC_SUCCESS = (
    r"\b(?:thank you for (applying|your (application|interest))"
    r"|application (has been |was )?(submitted|received|complete)"
    r"|we('ve| have) received your application"
    r"|successfully (submitted|applied))\b"
)
# A sentence that takes back its own success wording is not a success sentence: "thank
# you for applying, but there was an error", "thank you for your interest; unfortunately".
GENERIC_NEGATION = (
    r"\b(unable|cannot|can't|couldn't|could not|did not|didn't|was not|wasn't|has not|hasn't"
    r"|not been|failed|failure|error|unsuccessful|incomplete|problem|try again|unfortunately"
    r"|no longer|expired)\b"
)
# The site saying, in a sentence, that the send did not go through.
GENERIC_FAILURE = (
    r"\b(?:something went wrong"
    r"|(could not|couldn't|unable to|failed to|cannot|can't) (be )?(submit|process|complete|save|send)\w*"
    r"|submission (failed|error)"
    r"|(was|were|has|have) not (been )?(submitted|received|sent|saved|processed)"
    r"|application (is |was )?(incomplete|not complete)"
    r"|please try again|try again later"
    r"|session (has )?(expired|timed out))\b"
)
GENERIC_ERROR = r"required|invalid|error|could not|try again"
GENERIC_SUCCESS_RE = re.compile(GENERIC_SUCCESS, re.IGNORECASE)
GENERIC_NEGATION_RE = re.compile(GENERIC_NEGATION, re.IGNORECASE)
GENERIC_FAILURE_RE = re.compile(GENERIC_FAILURE, re.IGNORECASE)
GENERIC_ERROR_RE = re.compile(GENERIC_ERROR, re.IGNORECASE)

# Polled after the click: true once the page left, the form is gone, or a success or
# validation message appeared that was not in the observation taken before the click.
GENERIC_SETTLED_JS = r"""({start, text_hits, status_region, form_error, success, error, status_selector, error_selector}) => {
 try {
  if (location.href !== start) return true;
  const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' && e.getAttribute('aria-hidden')!=='true';
  const deep=(r,s)=>[...r.querySelectorAll(s),...[...r.querySelectorAll('*')].filter(e=>e.shadowRoot).flatMap(e=>deep(e.shadowRoot,s))];
  if (!deep(document,'input,textarea,select').some(e=>e.type!=='hidden' && visible(e))) return true;
  if ((document.body.innerText.slice(0,15000).match(new RegExp(success,'gi'))||[]).length > text_hits) return true;
  const messages=__MESSAGES__;
  const status=messages(status_selector), errors=messages(error_selector);
  return (status!==status_region && new RegExp(success,'i').test(status))
      || (errors!==form_error && new RegExp(error,'i').test(errors));
 } catch (e) { return false; }
}""".replace("__MESSAGES__", MESSAGES_JS)

# Polled once the page settled: true when a validation message or a failure sentence
# shows up after all. A page that thanks the applicant and then reports an error did not
# confirm anything.
GENERIC_TROUBLE_JS = r"""({form_error, failure_hits, error, failure, error_selector}) => {
 try {
  const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' && e.getAttribute('aria-hidden')!=='true';
  const messages=__MESSAGES__;
  const errors=messages(error_selector);
  if (errors!==form_error && new RegExp(error,'i').test(errors)) return true;
  return (document.body.innerText.slice(0,15000).match(new RegExp(failure,'gi'))||[]).length > failure_hits;
 } catch (e) { return false; }
}""".replace("__MESSAGES__", MESSAGES_JS)

# Lever's apply page posts its form natively once hCaptcha hands it a token. Polled after
# the click: true on the thanks page, its success heading, the CAPTCHA's verification
# error, or a form error that was not there before the click.
LEVER_VERIFICATION_ERROR = r"there was an error verifying your application"
LEVER_POSTING_RE = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)
LEVER_SETTLED_JS = r"""({form_error, error_selector, verification}) => {
 try {
  if (/\/thanks\/?$/.test(location.pathname)) return true;
  if (document.querySelector('h3[data-qa="msg-submit-success"]')) return true;
  if (new RegExp(verification, 'i').test(document.body.innerText)) return true;
  const messages=__MESSAGES__;
  return messages(error_selector) !== form_error;
 } catch (e) { return false; }
}""".replace("__MESSAGES__", MESSAGES_JS)


def normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def host_matches(host: str | None, suffixes: tuple[str, ...]) -> bool:
    host = (host or "").lower()
    return any(host == s or host.endswith("." + s) for s in suffixes)


def url_words(url: str) -> set[str]:
    parsed = urlsplit(url)
    return set(re.findall(r"[a-z0-9]+", unquote_plus(parsed.path + " " + parsed.query).lower()))


def url_tokens(url: str) -> set[str]:
    """Whole confirmation words in the path or query; none when the URL also says it failed."""
    words = url_words(url)
    return set() if words & GENERIC_URL_FAILURE else words & GENERIC_URL_WORDS


def sentences(text: str | None) -> list[str]:
    return re.split(r"(?<=[.!?])\s+|\n+", text or "")


def success_phrases(text: str | None) -> set[str]:
    """Success wording, sentence by sentence; a sentence that negates itself has none."""
    return {
        normalized(m.group(0))
        for sentence in sentences(text)
        if not GENERIC_NEGATION_RE.search(sentence)
        for m in GENERIC_SUCCESS_RE.finditer(sentence)
    }


def failure_phrases(text: str | None) -> set[str]:
    return {normalized(m.group(0)) for m in GENERIC_FAILURE_RE.finditer(text or "")}


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
    def rejected(cls, checks: dict) -> bool:
        # The board's contract has no rejected-form reading: short of confirmation, unknown.
        return False

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
    with the observation taken before the click. Confirmed needs all of: the form gone
    (no fields, no final control), a thank-you sentence or a success region that was not
    there before, no new validation message, no failure sentence and no failure word in
    the URL. A confirmation-looking URL is recorded as evidence and never confirms on its
    own, and neither does a changed URL. A form that stayed where it was and named its
    own validation error rejected the attempt, but only when no POST to the site was
    accepted or left unanswered. Everything else is an unknown submission.
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
            return
        try:
            # The page settled. Give an error that follows a thank-you a bounded moment
            # to show itself; the timeout is the good case and is not an error.
            page.wait_for_function(
                GENERIC_TROUBLE_JS,
                arg={
                    "form_error": markers.get("form_error") or "",
                    "failure_hits": sum(
                        1 for _ in GENERIC_FAILURE_RE.finditer(before.get("text") or "")
                    ),
                    "error": GENERIC_ERROR,
                    "failure": GENERIC_FAILURE,
                    "error_selector": ERROR_SELECTOR,
                },
                timeout=min(GENERIC_QUIET_MS, timeout_ms),
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
        said = f"{after.get('text') or ''}\n{markers.get('status_region') or ''}"
        said_before = f"{before.get('text') or ''}\n{prior.get('status_region') or ''}"
        checks = {
            # Same-host POST statuses are evidence for the owner, not a confirmation. A
            # redirect after a POST is how an accepted native form looks.
            "post_accepted": any(200 <= r["status"] < 400 for r in posts),
            "post_rejected": any(r["status"] >= 400 for r in posts),
            "url_changed": after_url != package_url,
            "confirmation_url": bool(url_tokens(after_url) - url_tokens(package_url)),
            "no_failure_url": not (
                (url_words(after_url) & GENERIC_URL_FAILURE) - url_words(package_url)
            ),
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
            # A failure sentence, a posting that reads as closed, or "you already applied":
            # none of them says this send was received.
            "no_failure_text": not (failure_phrases(said) - failure_phrases(said_before))
            and not after.get("closed")
            and not (markers.get("already_applied") and not prior.get("already_applied")),
        }
        checks["confirmed"] = (
            checks["form_gone"]
            and (checks["confirmation_text"] or checks["confirmation_region"])
            and checks["no_form_error"]
            and checks["no_failure_text"]
            and checks["no_failure_url"]
        )
        return checks

    @classmethod
    def reason(cls, checks: dict, after: dict) -> str:
        if not checks["no_form_error"]:
            message = (after.get("ats_markers") or {}).get("form_error") or ""
            excerpt = " ".join(message.split())[:200]
            if checks.get("form_gone"):
                return (
                    f"The form closed and the page then showed an error: {excerpt} "
                    "It may still have stored something. Check the recruiting browser and "
                    "your email before reconciling; do not click Submit again."
                )
            return (
                f"The form reported a validation error and stayed open: {excerpt} "
                "Check the highlighted field in the recruiting browser; do not click Submit again."
            )
        if not checks.get("no_failure_text", True) or not checks.get("no_failure_url", True):
            return (
                "After the click the site said the send did not go through, but it may have "
                "stored something. Check the recruiting browser and your email before "
                "reconciling; do not click Submit again."
            )
        if checks["post_rejected"] and not checks["post_accepted"]:
            return (
                "The site rejected a submission request and showed no confirmation. "
                "Check the visible page before reconciling."
            )
        if checks.get("confirmation_url") and not (
            checks["confirmation_text"] or checks["confirmation_region"]
        ):
            return (
                "The page address looks like a confirmation, but the page itself did not "
                "say the application was received. Check the recruiting browser and your "
                "email before reconciling."
            )
        return (
            "No confirmation sentence or status region appeared after the click; "
            "check the recruiting browser and employer email before reconciling."
        )

    @classmethod
    def rejected(cls, checks: dict) -> bool:
        """The form stayed where it was and named its own validation error: nothing was sent.

        Not when the site accepted a POST, or a POST is still unanswered: the form's
        message could be about anything, and the application may already be stored.
        """
        return (
            checks.get("no_form_error") is False
            and not checks.get("url_changed")
            and not checks.get("form_gone")
            and not checks.get("post_accepted")
            and checks.get("posts_answered", True)
        )


class LeverV1:
    """Public Lever postings (jobs.lever.co/{company}/{posting}/apply).

    The apply page holds `form#application-form`, posted as multipart to its own path once
    hCaptcha hands the page a token: `button[data-qa="btn-submit"]` ("Submit application")
    runs the CAPTCHA, then clicks a hidden submit control. Success navigates to
    `/{company}/{posting}/thanks`, which shows `h3[data-qa="msg-submit-success"]`
    ("Application submitted!") and no form; both are required, and the POST status is
    recorded as evidence only. A send the CAPTCHA rejected comes back as the emptied form
    under "There was an error verifying your application": nothing was stored, so the
    owner finishes that one by hand. The form, button and thanks page were checked against
    the live public DOM; the verification error is untested until a live run shows it.
    """

    name = "lever_v1"
    hosts = ("jobs.lever.co",)
    success_marker = "lever_submit_success"
    error_marker = "lever_verification_error"

    @classmethod
    def scope(cls, url: str) -> tuple[str, str, str] | None:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        parts = parsed.path.strip("/").split("/")
        if host in cls.hosts and len(parts) >= 2 and LEVER_POSTING_RE.match(parts[1]):
            return host, parts[0], parts[1].lower()
        return None

    @classmethod
    def matches(cls, url: str) -> bool:
        return cls.scope(url) is not None

    @classmethod
    def response_hosts(cls, package_url: str) -> tuple[str, ...]:
        return cls.hosts

    @classmethod
    def await_result(cls, page, before: dict, timeout_ms: int):
        markers = before.get("ats_markers") or {}
        try:
            page.wait_for_function(
                LEVER_SETTLED_JS,
                arg={
                    "form_error": markers.get("form_error") or "",
                    "error_selector": ERROR_SELECTOR,
                    "verification": LEVER_VERIFICATION_ERROR,
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
        host, company, posting = cls.scope(package_url)
        posting_path = f"/{company}/{posting}"
        posts = [
            r
            for r in responses
            if host_matches(r["host"], cls.hosts)
            and r["path"].rstrip("/") in {posting_path, posting_path + "/apply"}
        ]
        after_url = after.get("url", "")
        parsed = urlsplit(after_url)
        markers = after.get("ats_markers") or {}
        prior = before.get("ats_markers") or {}
        errors = markers.get("form_error") or ""
        checks = {
            # The form's own POST, kept as evidence: a 303 to /thanks is how success looks.
            "post_status": posts[-1]["status"] if posts else None,
            "post_accepted": any(200 <= r["status"] < 400 for r in posts),
            "post_rejected": any(r["status"] >= 400 for r in posts),
            "confirmation_url": cls.scope(after_url) == (host, company, posting)
            and parsed.path.rstrip("/") == posting_path + "/thanks",
            "confirmation_content": markers.get(cls.success_marker) is True,
            "form_gone": not after.get("fields") and not after.get("final_controls"),
            "url_changed": (parsed.hostname or "").lower() != host
            or parsed.path.rstrip("/") != urlsplit(package_url).path.rstrip("/"),
            "captcha_rejected": markers.get(cls.error_marker) is True,
            "no_form_error": not (
                GENERIC_ERROR_RE.search(errors) and errors != (prior.get("form_error") or "")
            ),
        }
        checks["confirmed"] = (
            checks["confirmation_url"] and checks["confirmation_content"] and checks["form_gone"]
        )
        return checks

    @classmethod
    def rejected(cls, checks: dict) -> bool:
        if checks["confirmed"]:
            return False
        return checks["captcha_rejected"] or (
            not checks["no_form_error"] and not checks["url_changed"] and not checks["form_gone"]
        )

    @classmethod
    def reason(cls, checks: dict, after: dict) -> str:
        markers = after.get("ats_markers") or {}
        if checks["captcha_rejected"]:
            return (
                "Lever's CAPTCHA rejected the send; open the recruiting browser, solve it and "
                "press Submit yourself, then reply applied"
            )
        if not checks["no_form_error"]:
            excerpt = " ".join(str(markers.get("form_error") or "").split())[:200]
            return f"Lever kept the form open and said: {excerpt}"
        if markers.get("captcha_challenge"):
            return (
                "Lever's CAPTCHA is showing a challenge and nothing confirmed; solve it in the "
                "recruiting browser, watch for 'Application submitted!', then reconcile."
            )
        if checks["post_rejected"] and not checks["post_accepted"]:
            return (
                "Lever answered the send with an error and showed no 'Application submitted!' "
                "page; check the recruiting browser before reconciling."
            )
        return (
            "No 'Application submitted!' page appeared after the click; check the recruiting "
            "browser and your email before reconciling."
        )


ADAPTERS = {
    GreenhouseV1.name: GreenhouseV1,
    LeverV1.name: LeverV1,
    GenericV1.name: GenericV1,
}


def enabled_adapter(url: str):
    settings = workflow.config()
    if not settings.get("submission_enabled"):
        raise PermissionError("Final submission is disabled in the local workflow configuration")
    # The first listed adapter that matches wins, so the catch-all belongs at the end.
    for name in settings.get("submit_adapters", []):
        # Board adapters (paylocity_v1, workable_v1, jazzhr_v1, bamboohr_v1) are used only
        # when listed here; none is on by default.
        adapter = ADAPTERS.get(name) or boards.ADAPTERS.get(name)
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
    if current.get("ats_markers", {}).get("captcha_challenge"):
        raise PermissionError(
            "A CAPTCHA is visible; solve it in the recruiting browser, then reply go"
        )
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


# Approvals the owner typed, as opposed to the ones code writes under a policy: the
# auto-submit approval and the re-prepare that follows a `not sent` reconciliation.
OWN_WORD_KINDS = ("resume", "proceed", "submit", "account")
# The `first_send_hold` policy: which forms wait for the owner's go before they are filled.
#   unfamiliar  (default) a feed job whose form is on a host off the board table waits
#               once per host; boards in the table never wait
#   all         also the first application to each employer on a board
#   off         nothing waits; a host still has to be one a form may be filled on at all
FIRST_SEND_HOLD = ("unfamiliar", "all", "off")


def unattended(owner_message_id: str) -> bool:
    return str(owner_message_id).startswith("auto-submit:")


def first_send_policy() -> str:
    value = str(workflow.config().get("first_send_hold", "unfamiliar")).strip().lower()
    return value if value in FIRST_SEND_HOLD else "unfamiliar"


def owner_went_ahead(conn, application_id: str) -> bool:
    """The owner typed go, proceed, send it or create account on this application."""
    kinds = ",".join("?" * len(OWN_WORD_KINDS))
    return bool(
        conn.execute(
            f"SELECT 1 FROM owner_commands WHERE application_id=? AND kind IN ({kinds}) "
            "AND message_id NOT LIKE 'auto-submit:%' AND message_id NOT LIKE '%:resume' LIMIT 1",
            (application_id, *OWN_WORD_KINDS),
        ).fetchone()
    )


def _card(status: str, headline: str, reason: str, commands: list[str]) -> dict:
    return {"status": status, "headline": headline, "reason": reason, "commands": commands}


def fill_hold(application_id: str, url: str, conn=None) -> dict | None:
    """Whether a form at `url` may be filled for this application now.

    None means go ahead; a host the owner just vouched for is remembered on the way. A
    dict is the card to show instead: `status`, `headline`, `reason` and `commands`, in
    plain words. Decided before any applicant data is typed, and checked again by the
    browser daemon and before the one click, so no path around the worker skips it.
    """
    if conn is None:
        with workflow.db() as own:
            return fill_hold(application_id, url, own)
    host = job_index.host_of(url) or "this site"
    never = ineligible(url)
    if never:
        return _card(
            "MANUAL_TAKEOVER",
            "This site cannot take the application",
            f"The form lives on `{host}`, and {never}. Rove will not enter your details "
            "there. If you still want this one, apply in your own browser and reply "
            "`applied`; otherwise `park it`.",
            ["applied", "park it"],
        )
    item = conn.execute(
        "SELECT source FROM application_queue WHERE id=?", (application_id,)
    ).fetchone()
    source = item["source"] if item else "agent"
    policy = first_send_policy()
    on_board = live_browser.approved_ats(url)  # the daemon's own name for the host table
    if workflow.source_policy(source)["owner_decided"]:
        basis = "the owner's own link"
    elif owner_went_ahead(conn, application_id):
        basis = "the owner's go"
    else:
        basis = ""
    if policy == "off" or basis:
        if basis:
            job_index.familiarize(conn, url, application_id, basis)
            if on_board:
                job_index.approve_tenant(conn, url, application_id, basis)
        return None
    if on_board:
        if policy == "all" and not job_index.tenant_seen(conn, url):
            return _card(
                "NEEDS_USER",
                "First application to this employer",
                f"I have not sent an application to {tenant_words(url)} before, so this "
                "first one waits for you. Nothing was entered. Reply `go` and I fill and "
                "send it; later ones to the same employer go without asking.",
                ["go", "park it"],
            )
        return None
    if job_index.familiar(conn, url):
        return None
    return _card(
        "NEEDS_USER",
        "First time on this site",
        f"Rove has not applied on `{host}` before, and it is not one of the job boards I "
        "know. This first application there waits for you; nothing was entered. Reply "
        "`go` to fill and send it (later jobs on this site go on their own), or `park it`.",
        ["go", "park it"],
    )


def fill_hold_words(application_id: str, url: str, conn=None) -> str:
    """The hold as one plain-word error, or "" when the form may be filled."""
    hold = fill_hold(application_id, url, conn)
    return f"{PLAIN_STOP}{hold['reason']}" if hold else ""


def link_hold(application_id: str, page_url: str, link_url: str | None) -> dict | None:
    """The card for an Apply link that leaves the page's site for one the owner has not
    let a form be filled on; None when the link stays on the site, goes to a board, or
    the browser will decide once it sees the form (a link with no address)."""
    if not link_url:
        return None
    if job_index.host_of(link_url) == job_index.host_of(page_url) or live_browser.approved_ats(
        link_url
    ):
        return None
    return fill_hold(application_id, link_url)


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
            "SELECT status,package_hash,url,source FROM application_queue WHERE id=?",
            (application_id,),
        ).fetchone()
        if not item or item["status"] != "READY_FOR_REVIEW" or item["package_hash"] != package_hash:
            raise PermissionError("Application is not ready for this approval")
        prior = conn.execute(
            "SELECT status FROM live_submission_attempts WHERE application_id=?", (application_id,)
        ).fetchone()
        if prior and prior["status"] != "NOT_SUBMITTED":
            raise PermissionError("A submission attempt already exists; do not retry")
        # The same job under another spelling of its link is the same application: one send.
        key = job_index.key_of(conn, application_id, item["url"])
        if job_index.sent_elsewhere(conn, application_id, key):
            raise PermissionError(
                f"{PLAIN_STOP}This job already has an application that was sent from another "
                "link to the same posting, so this one stays unsent. Nothing is sent twice. "
                "Reply `park it` to drop it."
            )
        # The form's host must be one the owner let a form be filled on (the daemon
        # checked before filling; this is the last check before the one click).
        package = state_root() / f"applications/{application_id}/package.json"
        form_url = json.loads(package.read_text())["url"] if package.is_file() else item["url"]
        hold = fill_hold_words(application_id, form_url, conn)
        if hold:
            raise PermissionError(hold)
        job_index.claim_send(conn, application_id, key)
        job_index.approve_tenant(
            conn,
            form_url,
            application_id,
            "unattended send" if unattended(owner_message_id) else "the owner's send",
        )
        # Named columns, and never OR REPLACE: a uniqueness conflict must fail, not delete.
        if prior:
            conn.execute(
                "UPDATE live_submission_attempts SET package_hash=?,owner_message_id=?,"
                "status='SUBMITTING',created_at=? WHERE application_id=?",
                (package_hash, owner_message_id, workflow.now(), application_id),
            )
        else:
            conn.execute(
                "INSERT INTO live_submission_attempts"
                "(application_id,package_hash,owner_message_id,status,created_at) VALUES(?,?,?,?,?)",
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
    arguments = {"application_id": erga_id, "status": "applied", "used_generated_resume": False}

    def confirm() -> dict:
        return asyncio.run(erga_call("confirm_application_submission", arguments))

    # The browser service calls this on a thread that already runs the browser library's
    # event loop, where a second loop cannot start: the Erga call gets a thread of its own.
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(confirm).result()
    data = result.get("result", result)
    return {"synced": True, "application_id": erga_id, "status": data.get("status")}


def finish_attempt(application_id: str, status: str, evidence: dict):
    if status not in {"APPLIED", "UNKNOWN_SUBMISSION", "NOT_SUBMITTED"}:
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
        if status == "NOT_SUBMITTED":
            # Nothing went out: the job is free to be sent once by a later attempt.
            job_index.release_send(conn, application_id)
        elif status == "APPLIED":
            # Also the manual and mail-settled ways here, which never claimed an attempt:
            # the job has its one application and the employer's board has been sent to.
            item = conn.execute(
                "SELECT url FROM application_queue WHERE id=?", (application_id,)
            ).fetchone()
            job_index.keep_send(conn, application_id, item["url"])
            job_index.approve_tenant(conn, item["url"], application_id, "applied")
    if status == "NOT_SUBMITTED":
        workflow.record(application_id, "submission_rejected", evidence)
        if evidence.get("owner_finishes"):
            # The attempt stays NOT_SUBMITTED. The owner solves the CAPTCHA, presses Submit
            # and replies `applied`, which records the application the manual way.
            workflow.transition(
                application_id,
                "MANUAL_TAKEOVER",
                "the site's CAPTCHA rejected the send and kept the form open",
                str(evidence.get("reason", "")),
            )
            workflow.action_needed(
                application_id,
                str(evidence.get("reason", ""))[:300],
                commands=["applied", "park it"],
                headline="The site's CAPTCHA rejected the send",
            )
            return
        workflow.transition(
            application_id,
            "NEEDS_USER",
            "the site rejected the form and kept it open",
            str(evidence.get("reason", "")),
        )
        workflow.action_needed(
            application_id,
            "The site rejected the form and kept it open; nothing was sent. "
            + str(evidence.get("reason", ""))[:300],
            commands=[f"resume {application_id}", f"defer {application_id}"],
            headline="The site rejected the form",
        )
        return
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
            str(evidence.get("reason") or "verified employer confirmation after one submit"),
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


def own_receipt(application_id: str, package_hash: str, claimed_at: str) -> dict | None:
    """The receipt the browser service wrote for this attempt, if it got that far."""
    path = state_root() / f"applications/{application_id}/receipt.json"
    try:
        receipt = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if (
        isinstance(receipt, dict)
        and receipt.get("package_hash") == package_hash
        and str(receipt.get("attempted_at") or "") >= str(claimed_at)
        and receipt.get("status") in {"APPLIED", "UNKNOWN_SUBMISSION", "NOT_SUBMITTED"}
    ):
        return receipt
    return None


def settle_stale_sends(now: datetime | None = None) -> list[str]:
    """Sends whose record never came, made unknown so they stop holding the queue.

    The claim commits before the click and the outcome is written after it; a browser
    service that died in between leaves the application SUBMITTING for good. Once the
    claim is older than any send can take, the attempt becomes an unknown submission
    with its usual card: the owner checks the site and his mail, nothing is retried. An
    attempt whose service did write its receipt gets the outcome the receipt holds.
    """
    cutoff = ((now or datetime.now(UTC)) - STALE_SEND_AFTER).isoformat()
    with workflow.db() as conn:
        rows = conn.execute(
            "SELECT q.id,a.package_hash,a.owner_message_id,a.created_at "
            "FROM application_queue q JOIN live_submission_attempts a ON a.application_id=q.id "
            "WHERE q.status='SUBMITTING' AND a.created_at<?",
            (cutoff,),
        ).fetchall()
    settled = []
    for row in rows:
        receipt = own_receipt(row["id"], row["package_hash"], row["created_at"])
        if receipt is not None:
            workflow.system_line(row["id"], f"send settled from its receipt · {receipt['status']}")
            finish_attempt(row["id"], receipt["status"], receipt)
        else:
            workflow.system_line(
                row["id"], f"send never reported back · claimed {row['created_at']} · now unknown"
            )
            finish_attempt(
                row["id"],
                "UNKNOWN_SUBMISSION",
                {
                    "application_id": row["id"],
                    "package_hash": row["package_hash"],
                    "owner_message_id": row["owner_message_id"],
                    "status": "UNKNOWN_SUBMISSION",
                    "attempted_at": row["created_at"],
                    "reason": "The send was started, but the browser service stopped before "
                    "it recorded what the site answered. Check the recruiting browser and "
                    "your email before reconciling; do not click Submit again.",
                },
            )
        settled.append(row["id"])
    return settled


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
        job_index.release_send(conn, application_id)
    workflow.transition(
        application_id,
        "NEEDS_USER",
        "owner verified nothing was submitted",
        f"owner message {owner_message_id}; prepare and review again before another attempt",
    )
    if workflow.config().get("auto_submit"):
        # The owner said nothing went out: prepare it again without another reply.
        with workflow.db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO owner_commands VALUES(?,?,?,?,?,?)",
                (
                    f"{owner_message_id}:resume",
                    application_id,
                    "resume",
                    json.dumps({"kind": "resume", "application_id": application_id}),
                    "applied",
                    workflow.now(),
                ),
            )
        workflow.set_state(application_id, "QUEUED")
        return
    workflow.action_needed(
        application_id,
        "You confirmed nothing was sent. Reply `go` to prepare and review it again.",
        commands=["go", "park it"],
        headline="Ready to try again",
    )


def submit(browser, application_id: str, package_hash: str, owner_message_id: str) -> dict:
    browser.check(application_id)
    with browser.guarded(browser.page):
        return _submit(browser, application_id, package_hash, owner_message_id)


@timing.stage(None, "submission")
def _submit(browser, application_id: str, package_hash: str, owner_message_id: str) -> dict:
    timing.lap("submit")
    current = browser.observe()
    adapter = enabled_adapter(current["url"])
    package = preflight(application_id, package_hash, current)
    if adapter.scope(current["url"]) != adapter.scope(package["url"]):
        raise PermissionError("The live page is not the reviewed employer job")
    control = package["final_controls"][0]
    locator = browser.form.locator(f'[data-rove-submit="{int(control["ref"])}"]')
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

    sent = []

    def observe_request(request):
        # A POST the page started may never be answered; it still may have been stored.
        if request.method == "POST" and host_matches(urlsplit(request.url).hostname, hosts):
            sent.append(urlsplit(request.url).path)

    browser.page.on("request", observe_request)
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
        browser.form.evaluate(
            "() => document.documentElement.setAttribute('data-rove-submit-armed', '1')"
        )
        browser.click(locator)
        timing.lap("verify")
        # Bounded: the adapter waits for its own signal, then the page is read once.
        adapter.await_result(browser.form, current, CONFIRMATION_TIMEOUT_MS)
        browser.form.wait_for_load_state("domcontentloaded", timeout=15000)
        after = browser.observe()
        checks = adapter.confirmed(package["url"], after, responses, before=current)
        checks["posts_answered"] = len(sent) <= len(responses)
        result["checks"] = checks
        if checks["confirmed"]:
            result.update(
                status="APPLIED",
                confirmed_at=workflow.now(),
                confirmation_url=after["url"],
                confirmation_text=after.get("text", "")[:4000],
                screenshot=after.get("screenshot"),
            )
        elif adapter.rejected(checks):
            # The site kept the form and said no: nothing was sent.
            result.update(status="NOT_SUBMITTED", reason=adapter.reason(checks, after))
            if checks.get("captcha_rejected"):
                # Only a person can satisfy the CAPTCHA: the owner finishes this one.
                result["owner_finishes"] = True
            labels = re.findall(
                r"required field:?\s*([^\n.;]+)", str(result["reason"]), re.IGNORECASE
            )
            if labels:
                # The site knows which fields it requires: the next preparation treats them so.
                write_private(directory / "required-overrides.json", [x.strip() for x in labels])
        else:
            result["reason"] = adapter.reason(checks, after)
    except Exception as error:  # noqa: BLE001 -- any uncertainty after the claim must stay durable
        result["reason"] = "No independent confirmation: " + type(error).__name__
    finally:
        try:
            browser.form.evaluate(
                "() => document.documentElement.removeAttribute('data-rove-submit-armed')"
            )
        except PlaywrightError:
            pass
        browser.page.remove_listener("response", observe_response)
        with contextlib.suppress(Exception):  # never between the click and its record
            browser.page.remove_listener("request", observe_request)
        result["responses"] = responses
        finish_attempt(application_id, result["status"], result)
    if result["status"] == "APPLIED":
        browser.close_run(application_id)
    else:
        workflow.attach_file(
            application_id, directory / "form-before-submit.png", "→ The form just before the click"
        )
        workflow.attach_file(
            application_id, directory / "browser.png", "→ What the page showed after"
        )
    return result
