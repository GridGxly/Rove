"""Paylocity recruiting (recruiting.paylocity.com).

Facts read from the public posting, apply and success pages and the apply bundle:

- One host for every employer. A job is `/Recruiting/Jobs/Details/{id}`, its form is
  `/Recruiting/Jobs/Apply/{id}` and its confirmation is `/Recruiting/Jobs/Success/{id}`;
  an optional employer slug may follow the id. Ids are global, so the id is the job.
- The posting's `a.apply-link-marker` ("Apply") leads to the form.
- The form is a React wizard. `button#btn-submit` reads "Next Step" until the last step,
  a review page, where it reads "Submit". A resume modal covers the page on load.
- Submit posts JSON to `/Recruiting/Jobs/Apply/{id}`. The reply is a list of validation
  messages: empty sends the browser to the success page, which shows
  `#appSubmitResponseDiv .success-text` ("Your application has been received!") and no
  form. Anything in the list brings a toast ("Please correct missing or invalid fields.")
  and the wizard back to its first step with the fields marked.
- The success page answers a plain GET for anyone, so the URL and its text prove nothing
  on their own: the accepted POST is part of the contract.
"""

import re

from .base import BoardAdapter, affirms, parts

NAME = "paylocity"
HOSTS = ("recruiting.paylocity.com",)
PAGES = ("details", "apply", "success")
APPLY_LINK = {"selector": "a.apply-link-marker", "label": "Apply"}
FINAL_CONTROL = {"selector": 'button[data-automation-id="btnSubmit"]', "label": "Submit"}
SUCCESS_TEXT = r"application has been received"
INVALID_TEXT = r"correct missing or invalid fields|\bis (required|invalid)\b"

SUCCESS_SELECTOR = "#appSubmitResponseDiv .success-text"
FORM_SELECTOR = (
    '#app #appDetailDiv,#app [data-automation-id="btnSubmit"],#app [data-automation-id="btnNext"]'
)
ERROR_SELECTOR = (
    ".toast-message:not(.hidden-animated):not(.hidden):not(.success),"
    ".form-group.form-error .type-footnote.show"
)
MARKERS = {
    "paylocity_success": ("text", SUCCESS_SELECTOR),
    "paylocity_form": ("present", FORM_SELECTOR),
    "paylocity_errors": ("text", ERROR_SELECTOR),
}


def owns(url: str) -> bool:
    return parts(url)[0] in HOSTS


def page(url: str) -> tuple[str, str] | None:
    """Which page of which job a URL is: ("details" | "apply" | "success", id)."""
    host, path, _query = parts(url)
    if (
        host in HOSTS
        and len(path) >= 4
        and [p.lower() for p in path[:2]] == ["recruiting", "jobs"]
        and path[2].lower() in PAGES
        and re.fullmatch(r"\d{1,12}", path[3])
    ):
        return path[2].lower(), path[3]
    return None


def scope(url: str) -> tuple | None:
    found = page(url)
    return (NAME, found[1]) if found else None


def apply_url(url: str) -> str | None:
    found = page(url)
    return f"https://{HOSTS[0]}/Recruiting/Jobs/Apply/{found[1]}" if found else None


class PaylocityV1(BoardAdapter):
    name = "paylocity_v1"
    label = "Paylocity"
    success_marker = "paylocity_success"
    form_marker = "paylocity_form"
    error_marker = "paylocity_errors"
    success_selector = SUCCESS_SELECTOR
    error_selector = ERROR_SELECTOR

    @classmethod
    def scope(cls, url: str) -> tuple | None:
        return scope(url)

    @classmethod
    def confirmation_path(cls, url: str) -> str | None:
        found = page(url)
        return rf"^/recruiting/jobs/success/{found[1]}(/|$)" if found else None

    @classmethod
    def confirmed(
        cls, package_url: str, after: dict, responses: list[dict], before: dict | None = None
    ) -> dict:
        job = page(package_url)[1]
        posts = cls.own_posts(
            package_url, responses, lambda p: p.lower() == f"/recruiting/jobs/apply/{job}"
        )
        markers = after.get("ats_markers") or {}
        errors = cls.new_errors(after, before)
        checks = {
            "post_status": posts[-1]["status"] if posts else None,
            "post_accepted": any(200 <= r["status"] < 300 for r in posts),
            "post_rejected": any(r["status"] >= 400 for r in posts),
            "confirmation_url": page(after.get("url", "")) == ("success", job),
            "confirmation_content": affirms(markers.get(cls.success_marker), SUCCESS_TEXT),
            "form_gone": not after.get("fields")
            and not after.get("final_controls")
            and not cls.form_open(after),
            "error_text": errors,
        }
        # The reply to an accepted POST can still be a list of validation messages: the
        # page then stays on the form and says so, and nothing was stored.
        checks["rejected_form"] = (
            page(after.get("url", "")) == ("apply", job)
            and cls.form_open(after)
            and bool(re.search(INVALID_TEXT, errors, re.IGNORECASE))
            and not checks["post_rejected"]
        )
        checks["confirmed"] = (
            checks["post_accepted"]
            and not checks["post_rejected"]
            and checks["confirmation_url"]
            and checks["confirmation_content"]
            and checks["form_gone"]
        )
        return checks


Adapter = PaylocityV1
