"""Which frame of a tab holds the application form.

Employer career pages often embed their applicant-tracking form in an iframe: a
Greenhouse board at `boards.greenhouse.io/embed/job_app?for=…&token=…`, an Ashby or Lever
embed, an iCIMS frame. The observation script runs in the tab's own document first and
then in each child frame that could hold a form; the frame with the application's
questions is chosen, and every reference that observation hands out (fields, choices,
the final control, navigation, links, sign-in controls) belongs to that frame alone.

This module decides from the readings and addresses only; the browser code runs the
scripts and keeps the chosen frame.
"""

from urllib.parse import urlsplit

# Frames that never hold the application: CAPTCHA widgets, ads, tag managers and video
# players, and the video-interview and assessment sites whose steps the owner takes.
NOT_FORM_HOSTS = (
    "recaptcha.net",
    "hcaptcha.com",
    "challenges.cloudflare.com",
    "doubleclick.net",
    "googlesyndication.com",
    "googletagmanager.com",
    "google-analytics.com",
    "facebook.com",
    "facebook.net",
    "youtube.com",
    "youtube-nocookie.com",
    "vimeo.com",
    "wistia.com",
    "wistia.net",
    "hirevue.com",
    "sparkhire.com",
    "modernhire.com",
    "codility.com",
    "hackerrank.com",
    "codesignal.com",
    "testgorilla.com",
)
# The smallest frame that can show a form; a tracking pixel or a hidden helper is smaller.
MIN_WIDTH, MIN_HEIGHT = 100, 50


def candidate(url: str) -> bool:
    """A frame address that could hold an application form."""
    parsed = urlsplit(url or "")
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if host.endswith("google.com") and parsed.path.startswith("/recaptcha"):
        return False
    return not any(host == s or host.endswith("." + s) for s in NOT_FORM_HOSTS)


def big_enough(box: dict | None) -> bool:
    return bool(box) and box["width"] >= MIN_WIDTH and box["height"] >= MIN_HEIGHT


def weight(reading: dict) -> int:
    """How many questions a reading holds; a group of options counts once."""
    groups, single = set(), 0
    for field in reading.get("fields") or []:
        group = field.get("_group")
        if group:
            groups.add(group["key"])
        else:
            single += 1
    return single + len(groups)


def holds_form(reading: dict) -> bool:
    """Two questions or more, or one with a control that sends, signs in or moves on.

    A chat box's message field or a lone newsletter address is not an application.
    """
    count = weight(reading)
    if count >= 2:
        return True
    return count == 1 and bool(
        reading.get("final_controls") or reading.get("auth_controls") or reading.get("nav_controls")
    )


def needs_frame(reading: dict) -> bool:
    """The tab's own document holds no form and no Apply link: what it has (a job search
    box, a header's sign-in link) is not the way into this application."""
    return not reading.get("application_links") and not holds_form(reading)


def choose(main: dict, children: list[dict]) -> int | None:
    """The index of the child reading that holds the form; None for the tab's own document.

    A child frame wins when it holds a form with more questions than the tab's own
    document has (a search box or a newsletter field on an employer page is not the
    application). When the tab's own document holds no form and no Apply link, a child
    frame with an Apply link or a sign-in control is the way in. Ties go to the earlier
    frame.
    """
    forms = [i for i, reading in enumerate(children) if holds_form(reading)]
    if forms:
        best = max(forms, key=lambda i: (weight(children[i]), -i))
        if weight(children[best]) > weight(main):
            return best
    if needs_frame(main):
        for index, reading in enumerate(children):
            if reading.get("application_links") or reading.get("auth_controls"):
                return index
    return None
