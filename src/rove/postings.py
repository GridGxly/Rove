"""A queued posting's text from its board's public API, read before the browser opens.

Greenhouse, Lever and Ashby publish every open posting as JSON. For a queued job on one
of those boards, the worker's idle tick reads the posting this way (one plain GET, no
browser, no cookies, nothing about the owner in the request) so its fit review can be
done before the pass. The pass then reads the same API once more: when the text is
unchanged, the review made in the background stands, however the browser's own page
text differs. Any other host, or a read that fails, leaves the job to be reviewed in its
pass as before.

The text is untrusted data like any page: hidden elements are dropped by the same reader
the company research uses, and it reaches the model only as the posting.
"""

import hashlib
import html
import json
import re
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from . import research, workflow
from .runtime import state_root, write_private

POSTING_FILE = "posting.json"
# A board's JSON is read up to this size; an Ashby board lists every opening in one file.
MAX_BYTES = 5_000_000
TEXT_CHARS = 12_000
# Seconds a pass waits for the board before it reviews the page it has instead.
PASS_TIMEOUT = 5.0
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
NAME = r"[A-Za-z0-9][A-Za-z0-9._%-]{0,99}"
# The board table: posting hosts and the one API host each may be read from.
BOARDS = {
    "greenhouse": {
        "hosts": ("boards.greenhouse.io", "job-boards.greenhouse.io"),
        "path": re.compile(rf"^/({NAME})/jobs/(\d{{1,20}})/?$"),
        "api": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}",
    },
    "lever": {
        "hosts": ("jobs.lever.co",),
        "path": re.compile(rf"^/({NAME})/({UUID})(?:/apply)?/?$"),
        "api": "https://api.lever.co/v0/postings/{board}/{job}",
    },
    "ashby": {
        "hosts": ("jobs.ashbyhq.com",),
        "path": re.compile(rf"^/({NAME})/({UUID})(?:/application)?/?$"),
        "api": "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false",
    },
}


def board_posting(url: str | None) -> tuple[str, str, str] | None:
    """(board kind, board name, job id) for a posting URL in the board table, else None."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        return None
    for kind, board in BOARDS.items():
        if host not in board["hosts"]:
            continue
        match = board["path"].match(parts.path)
        if match:
            return kind, match.group(1), match.group(2)
        if kind == "greenhouse" and parts.path.rstrip("/") == "/embed/job_app":
            query = parse_qs(parts.query)
            name, job = (query.get("for") or [""])[0], (query.get("token") or [""])[0]
            if re.fullmatch(NAME, name) and re.fullmatch(r"\d{1,20}", job):
                return kind, name, job
    return None


def visible(markup: str) -> str:
    """The text a reader of the posting sees, from the board's HTML."""
    text = research.PageText.parse(markup).text()
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def joined(*parts) -> str:
    return "\n".join(str(part).strip() for part in parts if str(part or "").strip())


def greenhouse_text(data: dict, _job: str) -> tuple[str, str]:
    location = (data.get("location") or {}).get("name", "")
    body = visible(html.unescape(str(data.get("content") or "")))
    return joined(data.get("title"), location, body), str(data.get("updated_at") or "")


def lever_text(data: dict, _job: str) -> tuple[str, str]:
    categories = data.get("categories") or {}
    lists = [
        joined(item.get("text"), visible(str(item.get("content") or "")))
        for item in data.get("lists") or []
        if isinstance(item, dict)
    ]
    text = joined(
        data.get("text"),
        categories.get("location"),
        categories.get("commitment"),
        data.get("descriptionPlain"),
        *lists,
        data.get("additionalPlain"),
    )
    return text, ""


def ashby_text(data: dict, job: str) -> tuple[str, str]:
    for posting in data.get("jobs") or []:
        if isinstance(posting, dict) and str(posting.get("id", "")).lower() == job.lower():
            text = joined(
                posting.get("title"),
                posting.get("location"),
                posting.get("employmentType"),
                posting.get("descriptionPlain") or visible(str(posting.get("descriptionHtml"))),
            )
            return text, ""
    return "", ""


READERS = {"greenhouse": greenhouse_text, "lever": lever_text, "ashby": ashby_text}


def read_json(client: httpx.Client, url: str) -> dict | None:
    """One GET of a board API, after the destination check, up to MAX_BYTES; else None."""
    try:
        url = research.validate_destination(url)
    except (PermissionError, OSError):
        return None
    with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
        kind = response.headers.get("content-type", "").lower()
        if response.status_code != 200 or "json" not in kind:
            return None
        size, chunks = 0, []
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                return None
            chunks.append(chunk)
    try:
        data = json.loads(b"".join(chunks))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def fetch(url: str, timeout: float | None = None) -> dict | None:
    """The posting at `url` from its board's API, or None for any other host or failure.

    `identity` is the board, the job and a hash of the text read: the same identity
    means the same posting, word for word.
    """
    found = board_posting(url)
    if not found:
        return None
    kind, board, job = found
    api = BOARDS[kind]["api"].format(board=quote(board, safe="%-._"), job=quote(job, safe="-"))
    try:
        with research.http_client() as client:
            if timeout is not None:
                client.timeout = httpx.Timeout(timeout)
            data = read_json(client, api)
    except httpx.HTTPError:
        return None
    if data is None:
        return None
    text, updated = READERS[kind](data, job)
    text = text[:TEXT_CHARS].strip()
    if len(text) < 200:
        return None  # an empty or placeholder posting is no posting to review
    digest = hashlib.sha256(" ".join(text.split()).casefold().encode()).hexdigest()[:16]
    return {
        "text": text,
        "source": kind,
        "board": board,
        "job_id": job,
        "url": url,
        "updated_at": updated,
        "identity": f"{kind}:{board}:{job}:{digest}",
        "fetched_at": workflow.now(),
    }


def kept(application_id: str) -> dict:
    try:
        data = json.loads(
            (state_root() / "applications" / application_id / POSTING_FILE).read_text()
        )
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def keep(application_id: str, posting: dict):
    write_private(state_root() / "applications" / application_id / POSTING_FILE, posting)


def from_board(posting: dict) -> bool:
    return posting.get("source") in BOARDS and bool(posting.get("identity"))


def still_current(application_id: str, page_url: str | None = None) -> dict | None:
    """The kept board posting when the board still shows exactly it, else None.

    One read of the board's API, bounded to PASS_TIMEOUT. A page the browser reached on a
    board must be the same board posting; a page elsewhere (an employer's own careers
    site around the board's form) is judged by the queued posting alone.
    """
    posting = kept(application_id)
    if not from_board(posting):
        return None
    on_page = board_posting(page_url)
    if on_page and on_page != (posting["source"], posting["board"], posting["job_id"]):
        return None
    now = fetch(posting["url"], timeout=PASS_TIMEOUT)
    return posting if now and now["identity"] == posting["identity"] else None
