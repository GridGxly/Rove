"""What every board module shares: URL parsing, page markers, and the adapter hooks.

A board module holds facts read from the board's public pages: its exact hosts, how a
job is named in a URL, where the form lives, and what the page shows after a send. The
adapter turns those facts into the hooks `submission._submit` calls. Nothing here
imports the browser runtime, so both it and the submission code can import a board.
"""

import json
import re
from urllib.parse import urlsplit

from patchright.sync_api import Error as PlaywrightError

# A confirmation sentence that also says any of this is not a confirmation.
NEGATION_RE = re.compile(
    r"\b(not|cannot|\w+n[’']t|unable|unsuccessful\w*|incomplete|errors?|failed|failure"
    r"|invalid|problem|try again|rejected|declined)\b",
    re.IGNORECASE,
)

# Visible text of every element a selector matches, each line once.
TEXT_JS = (
    "sel=>[...new Set([...document.querySelectorAll(sel)]"
    ".filter(e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden')"
    ".map(e=>(e.innerText||'').trim()).filter(Boolean))].join('\\n').slice(0,1000)"
)

# Polled after the click: true on the board's confirmation path, once its confirmation
# element has text (the given line, where the element has no selector of its own), once
# a form that is replaced in place has left, or once its error elements say something
# they did not say before.
SETTLED_JS = (
    "({path, success, line, form, errors, baseline}) => {\n"
    " try {\n"
    "  const text=" + TEXT_JS + ";\n"
    "  if (path && new RegExp(path,'i').test(location.pathname)) return true;\n"
    "  if (success && (line ? [...document.querySelectorAll(success)]"
    ".some(e=>new RegExp(line,'i').test((e.innerText||'').trim())) : text(success)))"
    " return true;\n"
    "  if (form && !document.querySelector(form)) return true;\n"
    "  return !!errors && text(errors) !== baseline;\n"
    " } catch (e) { return false; }\n"
    "}"
)


def parts(url: str) -> tuple[str, list[str], str]:
    """Host, path segments and query of a public HTTPS URL; an empty host otherwise."""
    try:
        parsed = urlsplit(str(url or ""))
        plain = parsed.scheme == "https" and parsed.port in (None, 443) and not parsed.username
    except ValueError:  # a malformed address is nobody's job page
        return "", [], ""
    if not plain:
        return "", [], ""
    host = (parsed.hostname or "").lower()
    return host, [p for p in parsed.path.split("/") if p], parsed.query


def affirms(text, pattern: str) -> bool:
    """The text says the confirmation sentence and nothing that takes it back."""
    text = " ".join(str(text or "").split())
    return bool(re.search(pattern, text, re.IGNORECASE)) and not NEGATION_RE.search(text)


def markers_js(boards) -> str:
    """One object-literal fragment adding every board's markers to an observation.

    The fragment is a spread of a guarded function, so a board whose markup changed
    yields no markers instead of breaking the whole observation.
    """
    entries = []
    for board in boards:
        for name, (kind, *args) in board.MARKERS.items():
            call = f"{kind}({','.join(json.dumps(a) for a in args)})"
            entries.append(f"{json.dumps(name)}:{call}")
    return (
        "...(()=>{try{const text=" + TEXT_JS + ";"
        "const present=sel=>!!document.querySelector(sel);"
        "const line=(sel,re)=>[...document.querySelectorAll(sel)]"
        ".filter(e=>!!e.getClientRects().length)"
        ".map(e=>(e.innerText||'').trim()).find(t=>new RegExp(re,'i').test(t))||'';"
        "return {" + ",".join(entries) + "};}catch(e){return {};}})(),"
    )


def excerpt(text, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[:limit]


class BoardAdapter:
    """Hooks shared by the board adapters; each board supplies its own contract."""

    name = ""
    label = ""
    success_marker = ""
    form_marker = ""
    error_marker = ""
    success_selector = ""
    success_line = ""
    error_selector = ""
    # Set where the result replaces the form at the same address.
    replaced_form_selector = ""

    @classmethod
    def scope(cls, url: str) -> tuple | None:
        raise NotImplementedError

    @classmethod
    def matches(cls, url: str) -> bool:
        return cls.scope(url) is not None

    @classmethod
    def response_hosts(cls, package_url: str) -> tuple[str, ...]:
        return (parts(package_url)[0],)

    @classmethod
    def confirmation_path(cls, url: str) -> str | None:
        """A regular expression for the confirmation path, where the board has one."""
        return None

    @classmethod
    def await_result(cls, page, before: dict, timeout_ms: int):
        markers = before.get("ats_markers") or {}
        try:
            page.wait_for_function(
                SETTLED_JS,
                arg={
                    "path": cls.confirmation_path(before.get("url", "")),
                    "success": cls.success_selector,
                    "line": cls.success_line,
                    "form": cls.replaced_form_selector,
                    "errors": cls.error_selector,
                    "baseline": markers.get(cls.error_marker) or "",
                },
                timeout=timeout_ms,
            )
        except PlaywrightError:
            pass

    @classmethod
    def own_posts(cls, package_url: str, responses: list[dict], path_ok) -> list[dict]:
        """The form's own POSTs on the board's host; other requests are not evidence."""
        host = parts(package_url)[0]
        return [
            r
            for r in responses
            if (r.get("host") or "").lower() == host and path_ok((r.get("path") or "").rstrip("/"))
        ]

    @classmethod
    def new_errors(cls, after: dict, before: dict | None, marker: str | None = None) -> str:
        """Error text the page shows now and did not show before the click."""
        marker = marker or cls.error_marker
        now = (after.get("ats_markers") or {}).get(marker) or ""
        prior = ((before or {}).get("ats_markers") or {}).get(marker) or ""
        return now if now != prior else ""

    @classmethod
    def form_open(cls, after: dict) -> bool:
        return (after.get("ats_markers") or {}).get(cls.form_marker) is True

    @classmethod
    def rejected(cls, checks: dict) -> bool:
        return not checks["confirmed"] and bool(checks.get("rejected_form"))

    @classmethod
    def reason(cls, checks: dict, after: dict) -> str:
        if checks.get("captcha_rejected"):
            return (
                f"{cls.label} wants its human check before it takes the application; open "
                "the recruiting browser, complete the check and press Submit yourself, then "
                "reply applied"
            )
        if checks.get("rejected_form"):
            said = excerpt(checks.get("error_text"))
            return (
                f"{cls.label} kept the form open and said: {said}"
                if said
                else (
                    f"{cls.label} refused the form and kept it open; check the marked fields "
                    "in the recruiting browser."
                )
            )
        if checks.get("post_rejected") and not checks.get("post_accepted"):
            return (
                f"{cls.label} answered the send with an error and showed no confirmation; "
                "check the recruiting browser before reconciling."
            )
        return (
            f"{cls.label} showed no confirmation after the click; check the recruiting "
            "browser and your email before reconciling."
        )
