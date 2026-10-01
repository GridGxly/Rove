"""JazzHR career pages ({employer}.applytojob.com).

Facts read from public postings, the confirmation page and the form script:

- Each employer has its own subdomain. A job is `/apply/{code}/{slug}`; the posting and
  its form are the same page. Codes are case-sensitive.
- The form is server-rendered: `form#form_submit_new_resume`, a multipart POST to the
  job's own URL. Its submit control is a link, `a#resumator-submit-resume` ("Submit
  Application"), and many employers add a reCAPTCHA checkbox ("Human Check").
- An accepted POST redirects to `/apply/confirm/{code}`, which shows
  `.page-body.success .page-title` ("Your application has been received.") and no form.
  That page answers a plain GET for anyone, so the form's own POST is part of the
  contract.
- The page's script checks the form before any request leaves: a refused form stays where
  it is with `.form-group.has-error` on the offending questions, and
  `#recaptcha-required-error` ("Please verify.") when the human check was not done.
"""

import re

from .base import BoardAdapter, affirms, parts

NAME = "jazzhr"
DOMAIN = "applytojob.com"
RESERVED_TENANTS = {"www", "app", "api", "info"}
RESERVED_CODES = {"confirm", "jobs", "share", "details"}
FINAL_CONTROL = {"selector": "a#resumator-submit-resume", "label": "Submit Application"}
SUCCESS_TEXT = r"application has been received"
CAPTCHA_TEXT = r"please verify"

SUCCESS_SELECTOR = ".page-body.success .page-title"
FORM_SELECTOR = "form#form_submit_new_resume"
ERROR_SELECTOR = (
    "#form_submit_new_resume .form-group.has-error > label,"
    "#form_submit_new_resume .resumator_label_error,#recaptcha-required-error"
)
MARKERS = {
    "jazzhr_success": ("text", SUCCESS_SELECTOR),
    "jazzhr_form": ("present", FORM_SELECTOR),
    "jazzhr_errors": ("text", ERROR_SELECTOR),
}


def tenant(url: str) -> str | None:
    host = parts(url)[0]
    label, _, domain = host.partition(".")
    if (
        domain == DOMAIN
        and label not in RESERVED_TENANTS
        and re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", label)
    ):
        return label
    return None


def owns(url: str) -> bool:
    return tenant(url) is not None


def job(url: str) -> tuple[str, str, bool] | None:
    """(employer, job code, on the confirmation page)."""
    employer = tenant(url)
    path = parts(url)[1]
    if not employer or len(path) < 2 or path[0].lower() != "apply":
        return None
    confirmation = path[1].lower() == "confirm"
    if confirmation and len(path) != 3:
        return None
    code = path[2] if confirmation else path[1]
    if code.lower() in RESERVED_CODES or not re.fullmatch(r"[A-Za-z0-9]{6,16}", code):
        return None
    return employer, code, confirmation


def scope(url: str) -> tuple | None:
    found = job(url)
    return (NAME, found[0], found[1]) if found else None


def apply_url(url: str) -> str | None:
    """The posting carries its own form, so the form's address is the posting's."""
    found = job(url)
    if not found or found[2]:
        return None
    host, path, _query = parts(url)
    return f"https://{host}/" + "/".join(path)


class JazzHRV1(BoardAdapter):
    name = "jazzhr_v1"
    label = "JazzHR"
    success_marker = "jazzhr_success"
    form_marker = "jazzhr_form"
    error_marker = "jazzhr_errors"
    success_selector = SUCCESS_SELECTOR
    error_selector = ERROR_SELECTOR

    @classmethod
    def scope(cls, url: str) -> tuple | None:
        return scope(url)

    @classmethod
    def confirmation_path(cls, url: str) -> str | None:
        found = job(url)
        return rf"^/apply/confirm/{re.escape(found[1])}/?$" if found else None

    @classmethod
    def confirmed(
        cls, package_url: str, after: dict, responses: list[dict], before: dict | None = None
    ) -> dict:
        employer, code, _ = job(package_url)
        posting = "/apply/" + code
        posts = cls.own_posts(
            package_url, responses, lambda p: p == posting or p.startswith(posting + "/")
        )
        markers = after.get("ats_markers") or {}
        errors = cls.new_errors(after, before)
        open_here = cls.form_open(after) and job(after.get("url", "")) == (employer, code, False)
        checks = {
            # The form's own POST: a redirect to the confirmation page is how success looks.
            "post_status": posts[-1]["status"] if posts else None,
            "post_accepted": any(200 <= r["status"] < 400 for r in posts),
            "post_rejected": any(r["status"] >= 400 for r in posts),
            "confirmation_url": job(after.get("url", "")) == (employer, code, True),
            "confirmation_content": affirms(markers.get(cls.success_marker), SUCCESS_TEXT),
            "form_gone": not after.get("fields")
            and not after.get("final_controls")
            and not cls.form_open(after),
            "error_text": errors,
        }
        # The page's own check stopped the form before any request left the browser.
        checks["rejected_form"] = open_here and bool(errors) and not posts
        # Only the human check is missing: the owner finishes this one in the open tab.
        checks["captcha_rejected"] = checks["rejected_form"] and bool(
            re.fullmatch(CAPTCHA_TEXT, errors.strip().rstrip("."), re.IGNORECASE)
        )
        checks["confirmed"] = (
            checks["post_accepted"]
            and not checks["post_rejected"]
            and checks["confirmation_url"]
            and checks["confirmation_content"]
            and checks["form_gone"]
        )
        return checks


Adapter = JazzHRV1
