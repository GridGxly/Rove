"""Workable job boards (apply.workable.com).

Facts read from public postings, the board's public JSON and its careers bundle:

- One host. A job is `/{account}/j/{shortcode}` and its form is the same path plus
  `/apply`. The short link `/j/{shortcode}` redirects to the account's path, and so does a
  valid shortcode under the wrong account, so the shortcode alone names the job and its
  employer.
- The posting's "Apply for this job" link (`a[data-ui="apply-button"]`) opens the form.
  `/api/v1/jobs/{shortcode}/form` publishes the form's fields as JSON.
- The form is one page: `form[data-ui="application-form"]`, sent by
  `button[data-ui="apply-button"]` ("Submit application"). The send is a POST to
  `/api/v1/jobs/{shortcode}/apply` carrying a Cloudflare Turnstile token.
- Success is shown in place, at the same URL: `div[data-ui="successful-submit"]` with
  "Your application has been submitted successfully." in its status region. A voluntary
  survey form may follow it, so the test is that the application form left, not that no
  field is on the page.
- A 400 or 422 keeps the form and flashes "There are some issues with your application.";
  a 409 is Turnstile refusing the send; anything else is "Something went wrong."
"""

import re

from .base import BoardAdapter, affirms, parts

NAME = "workable"
HOSTS = ("apply.workable.com",)
RESERVED = {"api", "j", "oops", "cdn-cgi", "static"}
APPLY_LINK = {"selector": 'a[data-ui="apply-button"]', "label": "Apply for this job"}
FINAL_CONTROL = {"selector": 'button[data-ui="apply-button"]', "label": "Submit application"}
SUCCESS_TEXT = r"application has been submitted successfully"
CAPTCHA_TEXT = r"couldn[’']t process your request|different browser"

SUCCESS_SELECTOR = '[data-ui="successful-submit"] [role="status"]'
FORM_SELECTOR = 'form[data-ui="application-form"]'
ERROR_SELECTOR = '[data-ui="flash-container"]'
MARKERS = {
    "workable_success": ("text", SUCCESS_SELECTOR),
    "workable_form": ("present", FORM_SELECTOR),
    "workable_errors": ("text", ERROR_SELECTOR),
}


def owns(url: str) -> bool:
    return parts(url)[0] in HOSTS


def job(url: str) -> tuple[str, str, bool] | None:
    """(account or "", shortcode, on the apply page) for a posting or form URL."""
    host, path, _query = parts(url)
    if host not in HOSTS:
        return None
    account = ""
    if len(path) >= 3 and path[1] == "j":
        account, path = path[0].lower(), path[1:]
        if account in RESERVED or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", account):
            return None
    if len(path) < 2 or path[0] != "j" or not re.fullmatch(r"[0-9A-Za-z]{6,16}", path[1]):
        return None
    rest = [p.lower() for p in path[2:]]
    if rest not in ([], ["apply"]):
        return None
    return account, path[1].upper(), rest == ["apply"]


def scope(url: str) -> tuple | None:
    found = job(url)
    return (NAME, found[1]) if found else None


def apply_url(url: str) -> str | None:
    found = job(url)
    if not found:
        return None
    account = f"{found[0]}/" if found[0] else ""
    return f"https://{HOSTS[0]}/{account}j/{found[1]}/apply/"


def form_definition_url(url: str) -> str | None:
    """Where the board publishes this job's form fields as JSON."""
    found = job(url)
    return f"https://{HOSTS[0]}/api/v1/jobs/{found[1]}/form" if found else None


class WorkableV1(BoardAdapter):
    name = "workable_v1"
    label = "Workable"
    success_marker = "workable_success"
    form_marker = "workable_form"
    error_marker = "workable_errors"
    success_selector = SUCCESS_SELECTOR
    error_selector = ERROR_SELECTOR
    replaced_form_selector = FORM_SELECTOR

    @classmethod
    def scope(cls, url: str) -> tuple | None:
        return scope(url)

    @classmethod
    def confirmed(
        cls, package_url: str, after: dict, responses: list[dict], before: dict | None = None
    ) -> dict:
        code = job(package_url)[1]
        posts = cls.own_posts(
            package_url, responses, lambda p: p.lower() == f"/api/v1/jobs/{code.lower()}/apply"
        )
        markers = after.get("ats_markers") or {}
        errors = cls.new_errors(after, before)
        statuses = {r["status"] for r in posts}
        checks = {
            "post_status": posts[-1]["status"] if posts else None,
            "post_accepted": any(200 <= s < 300 for s in statuses),
            "post_rejected": any(s >= 400 for s in statuses),
            # Success is drawn in place: the page must still be this job's.
            "same_job": scope(after.get("url", "")) == scope(package_url),
            "confirmation_content": affirms(markers.get(cls.success_marker), SUCCESS_TEXT),
            "form_gone": not cls.form_open(after) and not after.get("final_controls"),
            "error_text": errors,
        }
        refused = bool(statuses) and statuses <= {400, 409, 422} and cls.form_open(after)
        checks["captcha_rejected"] = refused and (
            409 in statuses or bool(re.search(CAPTCHA_TEXT, errors, re.IGNORECASE))
        )
        # The server refused the request and the form is still there: nothing was stored.
        checks["rejected_form"] = refused
        checks["confirmed"] = (
            checks["post_accepted"]
            and not checks["post_rejected"]
            and checks["same_job"]
            and checks["confirmation_content"]
            and checks["form_gone"]
        )
        return checks


Adapter = WorkableV1
