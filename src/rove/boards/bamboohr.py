"""BambooHR career sites ({employer}.bamboohr.com/careers).

Facts read from public postings, the site's public JSON and its careers bundle:

- Each employer has its own subdomain. A job is `/careers/{number}`; the old
  `/jobs/view.php?id={number}` redirects there. Numbers are small and per employer, so the
  employer is part of the job's name. `/careers/{number}/detail` publishes the posting and
  its form fields as JSON.
- "Apply for This Job" is a button that swaps the posting for the form in place, at the
  same URL: `form#job-application-form`, sent by "Submit Application".
- The form holds a honeypot (`input#nickname_…`, "Please leave this field blank") that
  must stay empty, and the send may ask for reCAPTCHA when the employer set a site key.
- The send is a POST to `/careers/{number}/add`; only a 200 is success. The page then
  shows "Thank You" and "Your application was submitted successfully" in place of the
  form.
- A refused form stays open with `aria-invalid` fields ("Please fill in this field.") or
  the human-check prompt ("Please confirm you're not a robot to continue.").
"""

import re

from .base import BoardAdapter, affirms, parts

NAME = "bamboohr"
DOMAIN = "bamboohr.com"
RESERVED_TENANTS = re.compile(
    r"www|api|app|staticfe|resources|images\d*|documentation|help|partners|marketplace"
    r"|status|content|blog|support"
)
APPLY_LINK = {"selector": "button", "label": "Apply for This Job"}
# "Cancel" is a submit button too; only this one names the form.
FINAL_CONTROL = {
    "selector": 'button[type="submit"][form="job-application-form"]',
    "label": "Submit Application",
}
HONEYPOT = 'input[id^="nickname_"]'
HONEYPOT_LABEL = "please leave this field blank"
SUCCESS_TEXT = r"application was submitted successfully"
SUCCESS_LINE = r"^your application was submitted successfully\.?$"
CAPTCHA_TEXT = r"confirm you[’']re not a robot|error occurred with recaptcha"

# The result card has no hook of its own: it is found by its exact line.
SUCCESS_SELECTOR = '[data-fabric-component="BodyText"],[data-fabric-component="Headline"],h3,p'
FORM_SELECTOR = "form#job-application-form"
ERROR_SELECTOR = FORM_SELECTOR + ' [data-fabric-component="InlineMessage"]'
MARKERS = {
    "bamboohr_success": ("line", SUCCESS_SELECTOR, SUCCESS_LINE),
    "bamboohr_form": ("present", FORM_SELECTOR),
    "bamboohr_errors": ("text", ERROR_SELECTOR),
}


def tenant(url: str) -> str | None:
    host = parts(url)[0]
    label, _, domain = host.partition(".")
    if (
        domain == DOMAIN
        and not RESERVED_TENANTS.fullmatch(label)
        and re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", label)
    ):
        return label
    return None


def owns(url: str) -> bool:
    return tenant(url) is not None


def job(url: str) -> tuple[str, str] | None:
    """(employer, job number) for a posting, whose form opens at the same address."""
    employer = tenant(url)
    _host, path, query = parts(url)
    if not employer:
        return None
    lowered = [p.lower() for p in path]
    if len(path) == 2 and lowered[0] == "careers" and re.fullmatch(r"\d{1,9}", path[1]):
        return employer, path[1]
    legacy = re.fullmatch(r"id=(\d{1,9})", query)
    if lowered == ["jobs", "view.php"] and legacy:
        return employer, legacy.group(1)
    return None


def scope(url: str) -> tuple | None:
    found = job(url)
    return (NAME, *found) if found else None


def apply_url(url: str) -> str | None:
    found = job(url)
    return f"https://{found[0]}.{DOMAIN}/careers/{found[1]}" if found else None


def form_definition_url(url: str) -> str | None:
    """Where the site publishes this job's posting and form fields as JSON."""
    found = job(url)
    return f"https://{found[0]}.{DOMAIN}/careers/{found[1]}/detail" if found else None


def is_trap(field: dict) -> bool:
    """The form's honeypot: an answer in it marks the application as a bot's."""
    label = " ".join(str(field.get("label") or "").lower().split())
    names = (str(field.get("id") or ""), str(field.get("name") or ""))
    return label == HONEYPOT_LABEL or any(re.fullmatch(r"nickname_\w+", n) for n in names)


class BambooHRV1(BoardAdapter):
    name = "bamboohr_v1"
    label = "BambooHR"
    success_marker = "bamboohr_success"
    form_marker = "bamboohr_form"
    error_marker = "bamboohr_errors"
    success_selector = SUCCESS_SELECTOR
    success_line = SUCCESS_LINE
    error_selector = ERROR_SELECTOR
    replaced_form_selector = FORM_SELECTOR

    @classmethod
    def scope(cls, url: str) -> tuple | None:
        return scope(url)

    @classmethod
    def confirmed(
        cls, package_url: str, after: dict, responses: list[dict], before: dict | None = None
    ) -> dict:
        employer, number = job(package_url)
        posts = cls.own_posts(
            package_url, responses, lambda p: p.lower() == f"/careers/{number}/add"
        )
        markers = after.get("ats_markers") or {}
        # The form's own messages, or failing those the invalid fields every observation reads.
        invalid_fields = cls.new_errors(after, before, "form_error")
        errors = cls.new_errors(after, before) or invalid_fields
        statuses = {r["status"] for r in posts}
        open_here = cls.form_open(after) and job(after.get("url", "")) == (employer, number)
        checks = {
            "post_status": posts[-1]["status"] if posts else None,
            # The site's own client treats a 200 as the only success.
            "post_accepted": 200 in statuses,
            "post_rejected": any(s >= 400 for s in statuses),
            # Success is drawn in place: the page must still be this employer's job.
            "same_job": job(after.get("url", "")) == (employer, number),
            "confirmation_content": affirms(markers.get(cls.success_marker), SUCCESS_TEXT),
            "form_gone": not cls.form_open(after) and not after.get("final_controls"),
            "error_text": errors,
        }
        # Either the page's own check stopped the form before a request left, or the
        # server refused the data: the form is still open and nothing was stored.
        checks["rejected_form"] = open_here and (
            (not posts and bool(errors)) or (bool(statuses) and statuses <= {400, 422})
        )
        # The form asked for its human check and marked no field: the owner finishes it.
        checks["captcha_rejected"] = (
            checks["rejected_form"]
            and bool(re.search(CAPTCHA_TEXT, errors, re.IGNORECASE))
            and not invalid_fields
        )
        checks["confirmed"] = (
            checks["post_accepted"]
            and not checks["post_rejected"]
            and checks["same_job"]
            and checks["confirmation_content"]
            and checks["form_gone"]
        )
        return checks


Adapter = BambooHRV1
