"""Recruiting mail from Zoho Mail: rules first, Qwen only to pick a label, never obeyed.

An employer's mail can move an application that was already sent forward (online
assessment, interview, offer, rejected) or prove that an unclear submission was
received. It cannot queue, prepare or submit anything, and it never becomes a
candidate fact. The body stays in a private file; Discord gets the sender's domain,
the subject and the label. Qwen reads a sanitized excerpt and only chooses among the
fixed labels, and only when the rules cannot settle a mail that is clearly from the
employer.

Anyone can write a From line, so a mail changes the record by itself only when its
sender is the employer's or an applicant system's domain and Zoho's own
Authentication-Results header says the domain really sent it (DMARC, or a DKIM
signature aligned with the From domain). Anything else that looks like a step is a
card in the recruiting channel that the owner confirms with a word or ignores. So is
anything from the Spam folder, however well it is signed, and a mail that could be about
several applications at one company: that card names them and the owner picks by number.
Automatic replies are dropped before any of this.
"""

import asyncio
import fcntl
import html
import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from email.parser import HeaderParser
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import workflow
from .discord_feed import private_env
from .jobs import database
from .mail_prompt import MAIL_PROMPT  # noqa: F401 -- part of this module's surface
from .runtime import state_root, write_private

LABELS = ("acknowledgement", "oa", "interview", "offer", "rejection", "other")
LABEL_STATES = {"oa": "OA", "interview": "INTERVIEW", "offer": "OFFER", "rejection": "REJECTED"}
# Erga's own status words for the labels its `update_application_status` accepts.
ERGA_STATUS = {"oa": "oa", "interview": "interview", "offer": "offer", "rejection": "rejected"}
ENV_KEYS = ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "ZOHO_ACCOUNT_ID")
# Applications a mail may concern: sent ones, and an unclear submission a mail can settle.
TRACKED = ("APPLIED", "OA", "INTERVIEW", "OFFER", "REJECTED", "UNKNOWN_SUBMISSION")
PAGE = 50
MAX_PAGES = 10


# --- configuration -----------------------------------------------------------


def config() -> dict:
    path = state_root() / "config/mail.json"
    return json.loads(path.read_text()) if path.exists() else {"enabled": False}


def accounts_base(api_base: str) -> str:
    """The Zoho accounts server for a mail API host: mail.zoho.eu pairs with accounts.zoho.eu."""
    host = urlsplit(api_base).hostname or "mail.zoho.com"
    return "https://" + re.sub(r"^mail\.", "accounts.", host)


def credentials() -> dict | None:
    """The four Zoho values from the private env, or None; values are never printed."""
    env = private_env()
    if not all(env.get(key) for key in ENV_KEYS):
        return None
    api_base = (env.get("ZOHO_API_BASE") or "https://mail.zoho.com").rstrip("/")
    return {
        "client_id": env["ZOHO_CLIENT_ID"],
        "client_secret": env["ZOHO_CLIENT_SECRET"],
        "refresh_token": env["ZOHO_REFRESH_TOKEN"],
        "account_id": env["ZOHO_ACCOUNT_ID"],
        "api_base": api_base,
        "accounts_base": (env.get("ZOHO_ACCOUNTS_BASE") or accounts_base(api_base)).rstrip("/"),
    }


def mail_db():
    conn = database()
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS mail_checkpoints(
        account_id TEXT PRIMARY KEY, received_time INTEGER NOT NULL, message_id TEXT NOT NULL,
        updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS mail_messages(
        message_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, received_time INTEGER NOT NULL,
        sender_domain TEXT NOT NULL, outcome TEXT NOT NULL, label TEXT, classifier TEXT,
        application_id TEXT, created_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS mail_confirmations(
        message_id TEXT PRIMARY KEY, application_id TEXT NOT NULL, label TEXT NOT NULL,
        classifier TEXT NOT NULL, deadline TEXT, reason TEXT NOT NULL, card_message_id TEXT,
        status TEXT NOT NULL, created_at TEXT NOT NULL, owner_message_id TEXT);
      CREATE TABLE IF NOT EXISTS mail_candidates(
        message_id TEXT PRIMARY KEY, candidates TEXT NOT NULL);
    """)
    return conn


# --- Zoho transport ----------------------------------------------------------


def http(base_url: str, headers: dict | None = None) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers=headers or {}, timeout=30, trust_env=False)


class ZohoFailure(Exception):
    """Zoho could not be used this run. `kind` is one of:

    auth     the refresh token or the access token was refused (revoked, expired, wrong)
    quota    Zoho's request or mailbox limit was reached
    network  Zoho could not be reached
    refused  any other refusal (a missing folder, a message Zoho no longer has)

    The text never carries a token or a response body that could hold one.
    """

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        super().__init__(f"{kind}: {detail}" if detail else kind)


LIMIT_WORDS = re.compile(r"limit|quota|exceed|throttl|too many", re.IGNORECASE)


def refusal(status: int, code: str = "", description: str = "") -> ZohoFailure:
    """The kind of a Zoho refusal from its HTTP status and its own error words."""
    words = f"{code} {description}"
    if status == 429 or LIMIT_WORDS.search(words):
        return ZohoFailure("quota", f"HTTP {status}")
    if status == 401 or re.search(r"oauth|token|unauthori", words, re.IGNORECASE):
        return ZohoFailure("auth", f"HTTP {status}")
    return ZohoFailure("refused", f"HTTP {status} {str(code)[:60]}".strip())


class Zoho:
    """The read-only slice of the Zoho Mail API the tracker needs: the folder list, a page
    of message headers, one message body, its header block and its calendar attachment.
    The access token lives in this object only."""

    def __init__(self, creds: dict):
        self.creds = creds
        self.client: httpx.Client | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        if self.client is not None:
            self.client.close()

    def connect(self):
        try:
            with http(self.creds["accounts_base"]) as accounts:
                response = accounts.post(
                    "/oauth/v2/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": self.creds["client_id"],
                        "client_secret": self.creds["client_secret"],
                        "refresh_token": self.creds["refresh_token"],
                    },
                )
        except httpx.TransportError as error:
            raise ZohoFailure("network", type(error).__name__) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        token = body.get("access_token") if isinstance(body, dict) else None
        if response.status_code >= 400 or not token:
            # The body names the problem (an invalid client or refresh token); only its
            # error word is read, because the same response can carry an issued token.
            error = str(body.get("error", "")) if isinstance(body, dict) else ""
            described = str(body.get("error_description", "")) if isinstance(body, dict) else ""
            if response.status_code == 429 or LIMIT_WORDS.search(described):
                raise ZohoFailure("quota", "token refresh limited")
            raise ZohoFailure("auth", f"no access token ({error[:40] or response.status_code})")
        self.client = http(
            self.creds["api_base"],
            {"Authorization": "Zoho-oauthtoken " + token, "Accept": "application/json"},
        )

    def fetch(self, path: str, params: dict | None = None) -> httpx.Response:
        if self.client is None:
            self.connect()
        try:
            response = self.client.get(path, params=params)
        except httpx.TransportError as error:
            raise ZohoFailure("network", type(error).__name__) from None
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            data = payload.get("data") if isinstance(payload, dict) else None
            status = payload.get("status") if isinstance(payload, dict) else None
            raise refusal(
                response.status_code,
                str((data or {}).get("errorCode", "")) if isinstance(data, dict) else "",
                str((status or {}).get("description", "")) if isinstance(status, dict) else "",
            )
        return response

    def get(self, path: str, params: dict | None = None):
        payload = self.fetch(path, params).json()
        status = payload.get("status") or {}
        if status.get("code") not in (None, 200):
            data = payload.get("data")
            try:
                code = int(status.get("code"))
            except (TypeError, ValueError):
                code = 0
            raise refusal(
                code,
                str(data.get("errorCode", "")) if isinstance(data, dict) else "",
                str(status.get("description", ""))[:120],
            )
        return payload.get("data")

    def folders(self) -> list[dict]:
        return self.get(f"/api/accounts/{self.creds['account_id']}/folders") or []

    def inbox_folder(self, folders: list | None = None) -> str:
        folders = self.folders() if folders is None else folders
        # Custom folders also report the Inbox type, so the Inbox's own path comes first.
        inbox = next(
            (f for f in folders if str(f.get("path", "")).lower() == "/inbox"), None
        ) or next((f for f in folders if str(f.get("folderType", "")).lower() == "inbox"), None)
        if not inbox:
            raise ZohoFailure("refused", "the account has no Inbox folder")
        return str(inbox["folderId"])

    def spam_folder(self, folders: list) -> str | None:
        """The Spam (or Junk) folder, read with less trust than the Inbox; None if absent."""
        spam = next(
            (
                f
                for f in folders
                if str(f.get("folderType", "")).lower() in ("spam", "junk")
                or str(f.get("path", "")).lower() in ("/spam", "/junk")
            ),
            None,
        )
        return str(spam["folderId"]) if spam else None

    def attachments(self, folder_id: str, message_id: str) -> list[dict]:
        data = (
            self.get(
                f"/api/accounts/{self.creds['account_id']}/folders/{folder_id}"
                f"/messages/{message_id}/attachmentinfo"
            )
            or {}
        )
        found = data.get("attachments") if isinstance(data, dict) else None
        return [a for a in found or [] if isinstance(a, dict)]

    def attachment(self, folder_id: str, message_id: str, attachment_id: str) -> bytes:
        response = self.fetch(
            f"/api/accounts/{self.creds['account_id']}/folders/{folder_id}"
            f"/messages/{message_id}/attachments/{attachment_id}"
        )
        return response.content[:INVITE_LIMIT]

    def messages(self, folder_id: str, start: int, limit: int) -> list[dict]:
        return (
            self.get(
                f"/api/accounts/{self.creds['account_id']}/messages/view",
                {
                    "folderId": folder_id,
                    "start": start,
                    "limit": limit,
                    "sortBy": "date",
                    "sortorder": "false",
                },
            )
            or []
        )

    def content(self, folder_id: str, message_id: str) -> str:
        data = (
            self.get(
                f"/api/accounts/{self.creds['account_id']}/folders/{folder_id}"
                f"/messages/{message_id}/content"
            )
            or {}
        )
        return str(data.get("content") or "")

    def headers(self, folder_id: str, message_id: str) -> str:
        """The raw header block as Zoho stored it; "" when Zoho returns anything else."""
        data = (
            self.get(
                f"/api/accounts/{self.creds['account_id']}/folders/{folder_id}"
                f"/messages/{message_id}/header"
            )
            or {}
        )
        content = data.get("headerContent") if isinstance(data, dict) else None
        return content if isinstance(content, str) else ""


def received(item: dict) -> int:
    try:
        return int(item.get("receivedTime") or 0)
    except (TypeError, ValueError):
        return 0


def new_messages(zoho: Zoho, folder_id: str, since: int) -> list[dict]:
    """Inbox messages received after `since` (ms since the epoch), oldest first, bounded."""
    found = []
    for page in range(MAX_PAGES):
        batch = zoho.messages(folder_id, start=page * PAGE + 1, limit=PAGE)
        if not batch:
            break
        found.extend(item for item in batch if received(item) > since)
        if received(batch[-1]) <= since or len(batch) < PAGE:
            break
    found.sort(key=received)
    return found


# --- mail text ---------------------------------------------------------------


BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "ul",
        "ol",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
        "hr",
        "td",
        "th",
    }
)
SKIPPED_TAGS = frozenset({"script", "style", "head", "title"})


VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "wbr"})
# Inline styles that keep an element's words off the reader's screen.
HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)"
    r"|font-size\s*:\s*0(?:px|pt|em|rem|%)?\s*(?:;|$)|max-height\s*:\s*0(?:px)?\s*(?:;|$)",
    re.IGNORECASE,
)


class _Text(HTMLParser):
    def __init__(self, visible_only: bool = False):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.visible_only = visible_only
        self.hidden_tag = ""  # the element whose words the reader never sees
        self.hidden_depth = 0

    def hides(self, attrs) -> bool:
        found = dict(attrs)
        return "hidden" in found or bool(HIDDEN_STYLE.search(str(found.get("style") or "")))

    def handle_starttag(self, tag, attrs):
        if self.hidden_depth:
            self.hidden_depth += tag == self.hidden_tag
            return
        if self.visible_only and self.hides(attrs):
            if tag not in VOID_TAGS:
                self.hidden_tag, self.hidden_depth = tag, 1
            return
        if tag in SKIPPED_TAGS:
            self.skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n" if tag != "td" else " ")

    def handle_endtag(self, tag):
        if self.hidden_depth:
            self.hidden_depth -= tag == self.hidden_tag
            return
        if tag in SKIPPED_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n" if tag != "td" else " ")

    def handle_data(self, data):
        if not self.skip and not self.hidden_depth:
            self.parts.append(data)


def plain_text(content: str, visible_only: bool = False) -> str:
    """The readable text of an HTML or plain mail body; markup, scripts and styles dropped.
    With `visible_only`, words inside an element styled to stay off the screen are
    dropped too: that is the text shown to the owner."""
    parser = _Text(visible_only)
    parser.feed(str(content or ""))
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def squash(value) -> str:
    return " ".join(str(value or "").split())


ADDRESS = re.compile(r"[^@\s<>\"(),;:]+@((?:[a-z0-9-]+\.)+[a-z0-9-]+)")


def sender_address(value) -> str:
    """The one address a From value names, lower-cased, or "" when it names none or
    several. The display name is the sender's own text and is never read as an address:
    `"careers@acme.example" <x@evil.example>` is x@evil.example."""
    pairs = [pair for pair in getaddresses([html.unescape(str(value or ""))]) if any(pair)]
    if len(pairs) != 1:
        return ""
    address = pairs[0][1].strip().lower()
    return address if ADDRESS.fullmatch(address) else ""


def sender_domain(value) -> str:
    return sender_address(value).rpartition("@")[2]


# The receiving servers whose verdict is believed: Zoho's own mail exchangers. A private
# `authserv_ids` list in config/mail.json adds exact names for other Zoho regions.
# Where the receiving server writes its verdict: the plain header, or the copy it seals.
VERDICT_HEADERS = ("authentication-results", "arc-authentication-results")
ZOHO_AUTHSERV = re.compile(r"mx\.zoho(?:mail)?\.(?:com|eu|in|jp|sa|ca|com\.au|com\.cn)")


def header_block(raw) -> list[tuple[str, str]]:
    """(lower-cased name, unfolded value) for each header, top to bottom; nothing below
    the first blank line is a header."""
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\n")
    parsed = HeaderParser().parsestr(text.split("\n\n", 1)[0] + "\n\n")
    return [(name.lower(), " ".join(str(value).split())) for name, value in parsed.items()]


def aligned(signing: str, domain: str) -> bool:
    """The signing domain is the From domain, its parent or its child. Two unrelated
    names under one shared suffix (two tenants of a hosting domain) are not aligned."""
    signing = signing.strip().strip("<>").rpartition("@")[2].lower().strip(".")
    if not signing or "." not in signing:
        return False
    return (
        signing == domain
        or (domain.endswith("." + signing) and registrable(domain) == registrable(signing))
        or signing.endswith("." + domain)
    )


def authentication(raw_headers, address: str, extra_ids=()) -> dict:
    """Whether Zoho's own check says the From domain really sent this mail.

    Only the receiving server's verdict counts: the topmost Authentication-Results
    header, or the topmost ARC-Authentication-Results (the copy Zoho seals on receipt),
    written above the first Received line and carrying the server's name. A header of
    either name further down, or text of that shape in the body, may be the sender's and
    proves nothing. A pass is DMARC for the From domain, or a DKIM signature whose
    domain is the From domain's.
    """
    domain = address.rpartition("@")[2]
    headers = header_block(raw_headers)

    def verdict(passed: bool, why: str, method: str | None = None) -> dict:
        return {"passed": passed, "method": method, "why": why}

    if not domain or not headers:
        return verdict(False, "no headers to check")
    senders = [value for name, value in headers if name == "from"]
    if len(senders) != 1 or sender_address(senders[0]) != address:
        return verdict(False, "the From line is missing, repeated or different")
    names = [name for name, _ in headers]
    if "received" not in names or not set(names) & set(VERDICT_HEADERS):
        return verdict(False, "the mail server recorded no check")
    on_top = [i for i in range(names.index("received")) if names[i] in VERDICT_HEADERS]
    if not on_top:
        return verdict(False, "the only check on record was written by the sender")
    value = headers[on_top[0]][1]
    while re.search(r"\([^()]*\)", value):
        value = re.sub(r"\([^()]*\)", " ", value)
    if names[on_top[0]] == "arc-authentication-results":
        value = re.sub(r"^\s*i=\d+\s*;", "", value)  # the seal's number comes first
    server, _, results = value.partition(";")
    server = (server.split() or [""])[0].lower()
    trusted = {str(name).strip().lower() for name in extra_ids or ()}
    if not ZOHO_AUTHSERV.fullmatch(server) and server not in trusted:
        return verdict(False, "the check was not written by the mail server")
    for part in results.split(";"):
        tokens = part.split()
        if not tokens or "=" not in tokens[0]:
            continue
        method, _, result = tokens[0].lower().partition("=")
        if result != "pass":
            continue
        properties = dict(token.lower().split("=", 1) for token in tokens[1:] if "=" in token)
        if method == "dmarc":
            claimed = properties.get("header.from", "").strip("<>").rpartition("@")[2]
            if claimed.strip(".") == domain:
                return verdict(True, "DMARC passed for the sender's domain", "dmarc")
        if method == "dkim" and any(
            aligned(properties.get(key, ""), domain) for key in ("header.d", "header.i")
        ):
            return verdict(True, "a DKIM signature of the sender's domain passed", "dkim")
    return verdict(False, "the sender's domain did not pass DMARC or DKIM")


SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}


def registrable(host: str) -> str:
    """example.com for mail.example.com; example.co.uk for jobs.example.co.uk."""
    labels = [label for label in str(host or "").lower().strip(".").split(".") if label]
    if len(labels) >= 3 and labels[-2] in SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# Hosts that send mail for many employers: the company name, not the domain, names one.
ATS_DOMAINS = {
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "myworkday.com", "workday.com", "smartrecruiters.com", "icims.com", "jobvite.com",
    "bamboohr.com", "workable.com", "workablemail.com", "successfactors.com", "taleo.net",
    "breezy.hr", "rippling.com", "dover.com", "jobright.ai", "linkedin.com", "indeed.com",
    "hackerrank.com", "hackerrankforwork.com", "codesignal.com", "codility.com",
    "wellfound.com", "gem.com", "recruitee.com", "jazzhr.com", "applytojob.com",
    "eightfold.ai", "phenom.com", "avature.net", "oraclecloud.com", "ultipro.com",
    "paylocity.com", "adp.com", "calendly.com", "goodtime.io", "modernloop.io",
}  # fmt: skip


# Domains under which anyone can get a site or a mailbox of their own. A posting hosted
# there does not make the rest of the domain the employer.
SHARED_SITE_DOMAINS = {
    "github.io", "gitlab.io", "pages.dev", "vercel.app", "netlify.app", "web.app",
    "firebaseapp.com", "herokuapp.com", "notion.site", "webflow.io", "wixsite.com",
    "squarespace.com", "wordpress.com", "blogspot.com", "substack.com", "carrd.co",
    "framer.website", "typeform.com", "airtable.com", "medium.com", "gmail.com",
    "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "proton.me", "zohomail.com",
}  # fmt: skip


def employer_domain(url: str) -> str | None:
    domain = registrable(urlsplit(str(url or "")).hostname or "")
    if not domain or domain in ATS_DOMAINS or domain in SHARED_SITE_DOMAINS:
        return None
    return domain


def normalize(text) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


COMPANY_SUFFIXES = re.compile(
    r"(?:,?\s+(?:inc|llc|ltd|corp|corporation|co|company|plc|gmbh|limited)\.?)+$", re.IGNORECASE
)
GENERIC_ROLE_WORDS = {
    "intern", "internship", "software", "engineer", "engineering", "summer", "developer",
    "development", "student", "program", "remote", "hybrid", "onsite", "year", "application",
    "position", "role", "team", "senior", "junior", "associate", "analyst", "full", "time",
}  # fmt: skip


def split_title(item: dict) -> tuple[str, str]:
    """(company, role) from the application's title: the feed writes "Company — Role",
    a page title reads "Role at Company"."""
    title = str(item.get("title") or "")
    company, sep, role = title.partition(" — ")
    if not sep:
        shown = workflow.display_title(item)
        match = re.search(r"^(.*?)\s+(?:at|@)\s+([^|–—]+?)\s*(?:[|–—].*)?$", shown)
        company, role = (match[2], match[1]) if match else ("", shown)
    return COMPANY_SUFFIXES.sub("", company.strip()), role.strip()


def company_name(item: dict) -> str:
    name = normalize(split_title(item)[0])
    return name if len(name) >= 3 else ""


def board_name(item: dict) -> str:
    """The employer's own name on its job board, which its mail is written under:
    "example" for a posting at jobs.board.example/example/…; "" off the boards."""
    from .destinations import board_for

    for url in (item.get("url"), item.get("source_url")):
        board = board_for(str(url or ""))
        tenant = board.tenant(str(url)) if board and board.tenant else None
        name = normalize(tenant[-1]) if tenant else ""
        if len(name) >= 3 and name not in VENDOR_NAMES:
            return name
    return ""


def role_hits(item: dict, body: str) -> int:
    words = {
        w
        for w in normalize(split_title(item)[1]).split()
        if len(w) >= 4 and w not in GENERIC_ROLE_WORDS
    }
    return sum(1 for w in words if f" {w} " in f" {body} ")


HREF = re.compile(r"""href\s*=\s*["']([^"'<>]{1,2000})["']""", re.IGNORECASE)
BARE_LINK = re.compile(r"https?://[^\s<>\"')\]]{1,2000}", re.IGNORECASE)
# A job posting's own id in its link: a Lever or Ashby UUID, a long Greenhouse or
# SmartRecruiters number, or a Workday-style requisition (R12345, JR-0012345).
JOB_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?<![a-z0-9])(?:[a-z]{1,3}[-_]?\d{4,}|\d{6,})(?![a-z0-9])",
    re.IGNORECASE,
)


def mail_links(content) -> list[str]:
    """Every web link a mail body names, read as text and never fetched."""
    raw = html.unescape(str(content or ""))
    found = [*HREF.findall(raw), *BARE_LINK.findall(raw)]
    return list(dict.fromkeys(link for link in found if link.lower().startswith("http")))


def job_ids(*urls) -> set[str]:
    """The posting ids in an application's links (path segments and query values)."""
    ids = set()
    for url in urls:
        parts = urlsplit(str(url or ""))
        haystack = unquote(parts.path) + " " + " ".join(v for _, v in parse_qsl(parts.query))
        ids.update(match[0].lower() for match in JOB_ID.finditer(haystack))
    return ids


def link_hit(item: dict, links, text: str) -> bool:
    """Whether the mail names this application's posting: its id in a link or in the
    text, or a link to the posting page itself. The ATS sender's own redirect links are
    read as text; nothing is opened."""
    haystack = (" ".join(unquote(link) for link in links) + " " + str(text or "")).lower()
    for job_id in job_ids(item.get("url"), item.get("source_url")):
        if re.search(rf"(?<![0-9a-z]){re.escape(job_id)}(?![0-9a-z])", haystack):
            return True
    targets = set()
    for url in (item.get("url"), item.get("source_url")):
        parts = urlsplit(str(url or ""))
        if parts.hostname and parts.path.strip("/"):
            targets.add((parts.hostname.lower(), parts.path.rstrip("/").lower()))
    for link in links:
        parts = urlsplit(link)
        host, path = (parts.hostname or "").lower(), parts.path.rstrip("/").lower()
        if any(host == h and (path == p or path.startswith(p + "/")) for h, p in targets):
            return True
    return False


def match_application(
    apps: list[dict], sender: str, subject: str, text: str, links=(), aliases=None
) -> dict | None:
    """Which application a mail concerns, and how surely.

    Strong: the employer's own domain, or a known recruiting sender that names the
    company (its name in the title, its name on its job board, or a brand learned for
    it) or the posting's id or link. Weak: the company
    named in the subject, or the posting's id or link, from anyone else; a weak match
    counts only when the rules recognise the mail. The strongest kind of match wins,
    then the posting's id or link, then the role's own words. Applications still level
    after that are all returned as candidates: the mail does not say which one it means.
    """
    domain = registrable(sender)
    body = normalize(subject + " " + text)
    head = normalize(subject)
    # A brand learned on one application names its company: every application there
    # answers to it, so a mail under that brand never picks one of them by itself.
    brands: dict[str, set[str]] = {}
    for item in apps:
        company = company_name(item) or item["id"]
        brands.setdefault(company, set()).update((aliases or {}).get(item["id"], ()))
    scored = []
    for item in apps:
        employer = employer_domain(item["url"])
        known = sorted(brands.get(company_name(item) or item["id"], ()))
        names = list(dict.fromkeys(n for n in (company_name(item), board_name(item), *known) if n))
        in_subject = any(f" {n} " in f" {head} " for n in names)
        named = in_subject or any(f" {n} " in f" {body} " for n in names)
        hit = link_hit(item, links, text)
        if employer and domain == employer:
            base, strength = 3, "strong"
        elif domain in ATS_DOMAINS and (named or hit):
            base, strength = 2, "strong"
        elif in_subject or hit:
            base, strength = 1, "weak"
        else:
            continue
        scored.append(((base, hit, role_hits(item, body)), item, strength))
    if not scored:
        return None
    top = max(key for key, _, _ in scored)
    level = sorted(
        ((item, strength) for key, item, strength in scored if key == top),
        key=lambda pair: pair[0]["updated_at"],
        reverse=True,
    )
    return {
        "candidates": [item for item, _ in level],
        "strength": level[0][1],
        "id_hit": top[1],
    }


# --- brands an ATS mail uses for a company ------------------------------------

# Words a sender adds to the company's name in its display name.
SENDER_WORDS = re.compile(
    r"(?:[\s,|·:-]+(?:university|campus|early careers?|emerging talent|technical|global)?\s*"
    r"(?:recruiting|recruitment|recruiters?|talent(?: acquisition)?(?: team)?|careers?"
    r"|hiring(?: team)?|team|hr|people(?: team| ops)?|jobs|no[- ]?reply|notifications?))+\s*$",
    re.IGNORECASE,
)
VENDOR_NAMES = {
    "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "icims", "jobvite",
    "bamboohr", "workable", "taleo", "successfactors", "breezy", "rippling", "dover", "gem",
    "recruitee", "jazzhr", "eightfold", "phenom", "avature", "hackerrank", "codesignal",
    "codility", "linkedin", "indeed", "calendly", "goodtime", "modernloop", "paylocity",
    "no reply", "noreply", "recruiting", "careers", "talent", "team", "notifications", "hr",
}  # fmt: skip
SUBJECT_BRAND = re.compile(
    r"\b(?:applying|application|applied|interest)\s+(?:to|at|in|with)\s+"
    r"([A-Z][\w&.'’ -]{1,40}?)(?=\s*(?:[!.,:;|(]|-\s|$))"
)
MAX_ALIASES = 5


def brand_name(display, subject: str = "") -> str:
    """The company name an ATS mail goes by: its sender's display name without the
    recruiting words, else the name after "applying to" in the subject; "" if neither
    names a company."""
    name = html.unescape(str(display or "")).strip().strip("\"'")
    name = re.sub(r"\s+(?:via|through|on behalf of)\s+.*$", "", name, flags=re.IGNORECASE)
    for candidate in (SENDER_WORDS.sub("", name), *(m for m in SUBJECT_BRAND.findall(subject))):
        words = normalize(COMPANY_SUFFIXES.sub("", candidate.strip()))
        if len(words) >= 3 and "@" not in candidate and words not in VENDOR_NAMES:
            return words
    return ""


def aliases_db(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mail_aliases(application_id TEXT NOT NULL, alias TEXT NOT "
        "NULL, learned_from TEXT NOT NULL, created_at TEXT NOT NULL, "
        "PRIMARY KEY(application_id,alias))"
    )
    return conn


def aliases_for(apps: list[dict]) -> dict[str, list[str]]:
    with aliases_db(workflow.db()) as conn:
        rows = conn.execute("SELECT application_id,alias FROM mail_aliases").fetchall()
    wanted = {item["id"] for item in apps}
    found: dict[str, list[str]] = {}
    for row in rows:
        if row["application_id"] in wanted:
            found.setdefault(row["application_id"], []).append(row["alias"])
    return found


def learn_alias(item: dict, mail: dict, learned_from: str) -> str:
    """Keep the brand a confirmed mail used for this application's company, so the next
    mail under that brand matches without a job link. Returns the brand, or ""."""
    brand = brand_name(mail.get("sender_name"), mail.get("subject", ""))
    if not brand or brand == company_name(item):
        return ""
    with aliases_db(workflow.db()) as conn:
        known = conn.execute(
            "SELECT COUNT(*) FROM mail_aliases WHERE application_id=?", (item["id"],)
        ).fetchone()[0]
        if known >= MAX_ALIASES:
            return ""
        conn.execute(
            "INSERT OR IGNORE INTO mail_aliases VALUES(?,?,?,?)",
            (item["id"], brand, learned_from, workflow.now()),
        )
    return brand


# --- auto-replies --------------------------------------------------------------

AUTO_SUBJECT = re.compile(
    r"^\s*(?:automatic reply|auto[- ]?reply|autoreply|auto[- ]?response|out of (?:the )?office"
    r"|ooo\b|away from (?:the |my )?office|on (?:vacation|leave|holiday)\b)",
    re.IGNORECASE,
)
AWAY_TEXT = re.compile(
    r"\bout of (?:the )?office\b|\bon (?:vacation|leave|holiday|pto)\b|\baway from (?:the|my) "
    r"(?:office|desk)\b|\blimited access to (?:e-?mail|my inbox)\b",
    re.IGNORECASE,
)


def auto_reply(subject: str, raw_headers, text: str = "") -> bool:
    """An automatic reply (out of office, vacation), never a recruiting step.

    `Auto-Submitted: auto-replied`, `Precedence: auto-reply` and the X-Autoreply family
    say so outright. `Precedence: bulk` alone does not: applicant systems send real
    rejections as bulk mail, so it counts only next to out-of-office wording.
    """
    if AUTO_SUBJECT.search(str(subject or "")):
        return True
    headers = header_block(raw_headers)
    values = {}
    for name, value in headers:
        values.setdefault(name, value.lower().strip())
    if values.get("auto-submitted", "").startswith("auto-replied"):
        return True
    if values.get("precedence") == "auto-reply" or {"x-autoreply", "x-autorespond"} & set(values):
        return True
    away = AWAY_TEXT.search(str(text or "")[:600])
    return values.get("precedence") in ("bulk", "junk", "list") and bool(away)


# --- calendar invites ------------------------------------------------------------

INVITE_LIMIT = 256 * 1024
# The time zones Outlook names in invites by their Windows names.
WINDOWS_ZONES = {
    "eastern standard time": "America/New_York",
    "central standard time": "America/Chicago",
    "mountain standard time": "America/Denver",
    "us mountain standard time": "America/Phoenix",
    "pacific standard time": "America/Los_Angeles",
    "alaskan standard time": "America/Anchorage",
    "hawaiian standard time": "Pacific/Honolulu",
    "gmt standard time": "Europe/London",
    "utc": "UTC",
    "coordinated universal time": "UTC",
}


def ics_start(text, zone: ZoneInfo | None = None) -> datetime | date | None:
    """The first event's start in a calendar block (an .ics file or a VCALENDAR block in
    the body): an aware datetime, or a date for an all-day event. A floating time (no
    zone named) is the owner's own: `zone`, or this Mac's zone when None. None for a
    cancelled invite, an unreadable date, or a time zone that cannot be resolved."""
    unfolded = re.sub(r"\r?\n[ \t]", "", str(text or ""))
    if "BEGIN:VEVENT" not in unfolded.upper():
        return None
    flags = re.MULTILINE | re.IGNORECASE
    if re.search(r"^(?:METHOD:CANCEL|STATUS:CANCELLED)\s*$", unfolded, flags):
        return None
    event = re.split(r"BEGIN:VEVENT", unfolded, maxsplit=1, flags=re.IGNORECASE)[1]
    found = re.search(r"^DTSTART((?:;[^:\r\n]*)?):(\d{8})(?:T(\d{6})(Z?))?\s*$", event, flags)
    if not found:
        return None
    params, day, clock, utc = found.groups()
    parts = [int(day[:4]), int(day[4:6]), int(day[6:8])]
    if clock:
        parts += [int(clock[:2]), int(clock[2:4]), int(clock[4:6])]
    try:
        if not clock:
            return date(*parts)
        zone_name = re.search(r"TZID=\"?([^;\":]+)", params or "", re.IGNORECASE)
        if utc:
            where = UTC
        elif zone_name:
            name = zone_name[1].strip()
            where = ZoneInfo(WINDOWS_ZONES.get(name.lower(), name))
        elif zone is not None:
            where = zone
        else:
            # Floating, and no zone configured: this Mac's own rules for that day.
            stamp = time.mktime((*parts, 0, 0, -1))
            return datetime.fromtimestamp(stamp, UTC)
        return datetime(*parts, tzinfo=where)
    except (ZoneInfoNotFoundError, ValueError, OverflowError):
        return None


def owner_zone(settings: dict) -> ZoneInfo | None:
    """The owner's time zone from `time_zone` in config/mail.json; None means this Mac's
    own zone, with its daylight-saving rules for the day of the interview."""
    name = settings.get("time_zone")
    try:
        return ZoneInfo(str(name)) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def when_words(start, zone: ZoneInfo | None = None) -> str:
    """An invite's start in the owner's time zone, as a person writes it."""
    if not isinstance(start, datetime):
        return f"{start:%a} {start.day} {start:%b} (all day)"
    local = start.astimezone(zone)
    hour = local.hour % 12 or 12
    return (
        f"{local:%a} {local.day} {local:%b}, {hour}:{local:%M} "
        f"{'AM' if local.hour < 12 else 'PM'} {local.tzname()}"
    )


def invite_time(zoho, folder_id: str, item: dict, text: str, settings: dict) -> str | None:
    """When an interview invite says the interview starts, from a calendar block in the
    body or an .ics attachment; None when the mail carries no readable invite."""
    message_id = str(item.get("messageId") or "")
    zone = owner_zone(settings)
    start = ics_start(text, zone)
    if start is None and str(item.get("hasAttachment", "1")).lower() not in ("0", "false"):
        try:
            for attachment in zoho.attachments(folder_id, message_id):
                name = str(attachment.get("attachmentName") or "").lower()
                size = int(attachment.get("attachmentSize") or 0)
                if not name.endswith((".ics", ".vcs")) or size > INVITE_LIMIT:
                    continue
                data = zoho.attachment(folder_id, message_id, str(attachment["attachmentId"]))
                start = ics_start(data.decode("utf-8", "replace"), zone)
                if start is not None:
                    break
        except ZohoFailure as failure:
            if failure.kind != "refused":
                raise
        except (KeyError, TypeError, ValueError):
            start = None
    return when_words(start, zone) if start is not None else None


# --- classification ----------------------------------------------------------

RULES = {
    "rejection": (
        r"\bnot (?:be )?(?:moving|going) forward\b",
        r"\bother (?:candidates|applicants)\b",
        r"\bnot (?:been )?selected\b",
        r"\bunable to (?:offer|move|proceed|extend)\b",
        r"\bregret to inform\b",
        r"\bno longer (?:under consideration|being considered)\b",
        r"\b(?:will not|won't|not) be (?:proceeding|progressing|advancing)\b",
        r"\bdecided (?:not to|to not) (?:move|proceed|go) (?:forward|ahead)\b",
        r"\b(?:move|moving|proceed|go|going) (?:forward|ahead) with (?:other|another|a different)\b",
        r"\bposition has been filled\b",
        r"\bnot (?:a |the right )?(?:match|fit) (?:at this time|for this role)\b",
        r"\bunfortunately\b[^.!?\n]{0,80}\b(?:not|won't|unable|cannot|other|no longer)\b",
    ),
    "offer": (
        r"\boffer letter\b",
        r"\b(?:pleased|excited|happy|delighted|thrilled) to (?:extend|offer|make you)\b",
        r"\bextend (?:you )?an offer\b",
        r"\boffer of employment\b",
        r"\b(?:job|formal|written|official) offer\b",
        r"\baccept (?:the|this|your|our) offer\b",
    ),
    "interview": (
        r"\binterview",
        r"\bphone screen\b",
        r"\bschedule (?:a |some |your )?(?:call|time|chat|conversation|meeting|screen)\b",
        r"\byour availability\b",
        r"\bbook a (?:time|slot|call)\b",
        r"\bcalendly\.com\b",
        r"\bmeet (?:with )?the team\b",
        r"\bnext step\w* (?:is|will be|would be) (?:a |an )?(?:call|conversation|chat|screen)\b",
    ),
    "oa": (
        r"\bonline assessment\b",
        r"\bhackerrank\b",
        r"\bcodesignal\b",
        r"\bcodility\b",
        r"\bcoding (?:challenge|assessment|test|exercise)\b",
        r"\btake[- ]home\b",
        r"\btechnical assessment\b",
        r"\bassessment (?:invitation|invite|link)\b",
        r"\bcomplete (?:the|your|this) assessment\b",
        r"\bskills? (?:assessment|test)\b",
    ),
    "acknowledgement": (
        r"\bthank(?:s| you) for (?:applying|your application|your interest|submitting)\b",
        r"\bwe(?:'ve| have)? received your application\b",
        r"\bapplication (?:has been |was |is )?(?:received|submitted|complete|under review)\b",
        r"\bapplication confirmation\b",
        r"\bwe(?:'ll| will) (?:review|be in touch|reach out|get back)\b",
        r"\bsuccessfully (?:applied|submitted)\b",
        r"\bconfirm(?:ing|s|ation of)? (?:receipt of )?your application\b",
    ),
}


# A reminder that an application was started and not sent. It thanks the applicant in
# the same words as a receipt, and is the opposite of one.
INCOMPLETE = re.compile(
    r"\b(?:application|submission)\b[^.!?\n]{0,60}\b(?:incomplete|unfinished|still in progress"
    r"|in draft|saved as a draft|not (?:yet )?(?:been )?(?:complete|completed|finished"
    r"|submitted|received|processed))\b"
    r"|\b(?:incomplete|unfinished|unsubmitted|draft) (?:job )?application\b"
    r"|\b(?:finish|complete|continue|resume|submit) (?:your|the|this) (?:job )?application\b"
    r"|\b(?:haven't|have not|didn't|did not|yet to) (?:yet )?(?:finish|finished|complete"
    r"|completed|submit|submitted)\b"
    r"|\bpick up where you left off\b"
    r"|\b(?:could not|couldn't|unable to) (?:be )?(?:process|processed|receive|received"
    r"|submit|submitted|complete|completed)\b",
    re.IGNORECASE,
)


def incomplete_notice(text: str) -> bool:
    return bool(INCOMPLETE.search(" ".join(str(text or "").split())))


def rule_hits(text: str) -> set[str]:
    return {
        label
        for label, patterns in RULES.items()
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
    }


def classify(subject: str, text: str) -> str | None:
    """A label from the fixed rules, or None when they cannot settle it.

    A rejection outranks everything it mentions (an interview it followed, an offer it
    withholds); an offer outranks the interviews before it. An interview invitation that
    mentions the assessment it followed, or an assessment that promises interviews, is
    settled by the subject line; when the subject names neither, the mail is ambiguous.
    An acknowledgement is the weakest reading.
    """
    found = rule_hits(subject + "\n" + text)
    if "acknowledgement" in found and incomplete_notice(subject + "\n" + text):
        # "Thanks for your interest ... your application is incomplete" is no receipt.
        found.discard("acknowledgement")
        if not found:
            return "other"
    if "rejection" in found:
        return "rejection"
    if "offer" in found:
        return "offer"
    if {"interview", "oa"} <= found:
        in_subject = rule_hits(subject) & {"interview", "oa"}
        return in_subject.pop() if len(in_subject) == 1 else None
    for label in ("interview", "oa", "acknowledgement"):
        if label in found:
            return label
    return None


MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE = (
    r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?"
    rf"(?:{MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTH}(?:,?\s+\d{{4}})?"
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{4}-\d{2}-\d{2})"
)
CLOCK = (
    r"(?:,?\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)\b"
    r"(?:\s+(?:PT|PST|PDT|ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|UTC|GMT|BST|CET|CEST|IST)\b)?)?"
)
DEADLINE = re.compile(
    r"\b(?:by|before|no later than|due(?: on| by)?|deadline(?: is|:)?|expires?(?: on)?"
    r"|until|closes? on|within)\s+"
    rf"(?:{DATE}{CLOCK}|\d+\s+(?:hours?|days?|business days?|weeks?))",
    re.IGNORECASE,
)


def stated_deadline(text: str) -> str | None:
    """The deadline phrase as the mail states it, or None; never a computed date."""
    match = DEADLINE.search(" ".join(str(text or "").split()))
    return workflow.clip(match[0], 100) if match else None


# --- Qwen fallback -----------------------------------------------------------

# The system prompt is MAIL_PROMPT in mail_prompt.py, which the reasoning script can
# load without this module's dependencies.

INSTRUCTION_LIKE = re.compile(
    r"\b(?:ignore|disregard|forget|override)\b[^.!?\n]{0,40}\b(?:instruction|prompt|rule|previous|above)"
    r"|\bsystem prompt\b|\byou are (?:an?|the|now) (?:ai|assistant|model|agent|bot)\b|\bas an ai\b"
    r"|\b(?:assistant|agent|model|rove|qwen|hermes|claude|gpt)\b[^.!?\n]{0,30}"
    r"\b(?:must|should|shall|will now|need to|have to)\b"
    r"|\b(?:run|execute|upload|download|delete|forward|paste|print|reveal|send)\b[^.!?\n]{0,40}"
    r"\b(?:command|script|file|password|token|credential|secret|key|ssh|resume|profile|address|phone)\b"
    r"|\b(?:password|passcode|one[- ]time code|verification code|token|api key|secret)\b",
    re.IGNORECASE,
)


def sanitize_for_model(text: str, limit: int = 2500) -> str:
    """What Qwen may read: the mail's sentences without links, addresses, markup, or any
    sentence that talks to a machine or about secrets."""
    text = re.sub(r"https?://\S+|www\.\S+", "(link)", str(text or ""))
    text = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "(address)", text)
    text = re.sub(r"<[^>\n]{0,200}>", " ", text)
    text = re.sub(r"[`{}\[\]]", " ", text)
    kept = []
    for line in text.splitlines():
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            sentence = " ".join(sentence.split())
            if sentence and not INSTRUCTION_LIKE.search(sentence):
                kept.append(sentence)
    return workflow.clip(" ".join(kept), limit)


def classify_with_qwen(directory: Path, context: dict) -> tuple[str, str | None]:
    """Qwen picks one of the fixed labels for a sanitized excerpt; anything else is an error.
    Raises ModelUnavailable when the local model server is down (the tick waits)."""
    from . import reasoning

    generated = reasoning.generate(directory, context, "classify", attempts=2)
    parsed = reasoning.load_json(reasoning.completed_response(generated))
    if not isinstance(parsed, dict):
        raise TypeError("Qwen returned no object")
    label = str(parsed.get("label") or "").strip().lower()
    if label not in LABELS:
        raise ValueError("Qwen returned an unknown label")
    deadline = parsed.get("deadline")
    quoted = (
        isinstance(deadline, str)
        and deadline.strip()
        and normalize(deadline) in normalize(context.get("excerpt", ""))
    )
    return label, (workflow.clip(squash(deadline), 100) if quoted else None)


# --- verification codes -----------------------------------------------------------

CODE_WORDS = re.compile(
    r"\b(?:verification|security|one[- ]time|confirmation|access|login|log-in|sign[- ]in"
    r"|authentication|application)\s+(?:code|pin|passcode)\b|\bpasscode\b|\botp\b"
    r"|\bone[- ]time password\b|\bcode\b",
    re.IGNORECASE,
)
CODE_TOKEN = re.compile(r"(?<![\w-])(?:\d{3}[ -]\d{3}|[A-Za-z0-9]{4,10})(?![\w-])")


def code_like(token: str, digits: tuple[int, int]) -> bool:
    """A one-time code: `digits[0]`–`digits[1]` digits, or a short mix of letters and digits
    (or of upper- and lower-case letters, which ordinary words are not)."""
    if token.isdigit():
        return digits[0] <= len(token) <= digits[1]
    if not 5 <= len(token) <= 10 or not token.isalnum():
        return False
    has_digit = any(c.isdigit() for c in token)
    has_letter = any(c.isalpha() for c in token)
    upper = sum(c.isupper() for c in token)
    mixed_case = upper >= 2 and any(c.islower() for c in token[1:])
    return (has_digit and has_letter) or (mixed_case and not token.istitle())


def find_code(text, digits: tuple[int, int] = (4, 8)) -> str | None:
    """The one-time code a message states next to words such as "verification code",
    read from the message's own text only; links and addresses are removed first and
    never opened. A token alone on its line or right after "is" or ":" is preferred; a
    bare year is never a code unless it stands alone."""
    clean = re.sub(r"https?://\S+|www\.\S+", " ", str(text or ""))
    clean = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", " ", clean)
    lines = [line.strip() for line in clean.splitlines()]
    for index, line in enumerate(lines):
        hint = CODE_WORDS.search(line)
        if not hint:
            continue
        window = [line[hint.end() :], *lines[index + 1 : index + 3]]
        found = []
        for position, part in enumerate(window):
            for match in CODE_TOKEN.finditer(part):
                token = re.sub(r"[ -]", "", match[0])
                if not code_like(token, digits):
                    continue
                alone = part.strip() == match[0]
                led = bool(re.search(r"(?:\bis|:)\s*$", part[: match.start()]))
                year = bool(re.fullmatch(r"(?:19|20)\d\d", token))
                if year and not (alone or led):
                    continue
                found.append((not (alone or led), position, match.start(), token))
        if found:
            return min(found)[3]
    return None


def redact_codes(text: str) -> str:
    """The text with any one-time code masked, for the private evidence copy."""
    code = find_code(text, (4, 8))
    return text.replace(code, "[code]") if code else text


def verification_code(sender_hosts, since, digits: tuple[int, int] = (4, 8)) -> str | None:
    """The newest one-time code mailed by an expected sender after `since`, or None.

    `sender_hosts` are the employer's or applicant system's domains for the account being
    made. A message counts only when its sender's domain is one of them (or under one of
    them) and Zoho's own authentication verdict says that domain really sent it, the same
    rule that lets a mail change the record. Only Inbox mail received after `since` (an
    aware datetime, or milliseconds since the epoch) is read, newest first; Spam is never
    read for codes. The code comes from that message's text alone: no link is followed,
    nothing is written, and the code is never logged.
    """
    hosts = [str(host or "").lower().strip(".") for host in sender_hosts or ()]
    if not hosts or any(
        "." not in host or registrable(host) in SHARED_SITE_DOMAINS for host in hosts
    ):
        raise ValueError("Name the employer's or the applicant system's own mail domains")
    since_ms = int(since.timestamp() * 1000) if isinstance(since, datetime) else int(since)
    settings, creds = config(), credentials()
    if not settings.get("enabled") or not creds:
        return None
    with Zoho(creds) as zoho:
        folder = zoho.inbox_folder()
        for item in reversed(new_messages(zoho, folder, since_ms)):
            address = sender_address(item.get("fromAddress"))
            domain = address.rpartition("@")[2]
            if not domain or not any(
                domain == host
                or domain.endswith("." + host)
                or (registrable(domain) == registrable(host) and host not in ATS_DOMAINS)
                for host in hosts
            ):
                continue
            message_id = str(item.get("messageId") or "")
            try:
                raw = zoho.headers(folder, message_id)
            except ZohoFailure as failure:
                if failure.kind != "refused":
                    raise
                continue
            if not authentication(raw, address, settings.get("authserv_ids") or ())["passed"]:
                continue
            code = find_code(plain_text(zoho.content(folder, message_id)), digits)
            if code:
                return code
    return None


# --- applying a mail to the record --------------------------------------------


def tracked_applications() -> list[dict]:
    marks = ",".join("?" * len(TRACKED))
    with workflow.db() as conn:
        rows = conn.execute(
            f"SELECT * FROM application_queue WHERE status IN ({marks}) ORDER BY updated_at DESC",
            TRACKED,
        ).fetchall()
    return [dict(r) for r in rows]


def safe_id(message_id) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(message_id))[:64] or "message"


def message_directory(message_id) -> Path:
    return state_root() / "mail/messages" / safe_id(message_id)


def erga_status(application_id: str, label: str) -> dict:
    """Best effort: mirror the new status into Erga when it is linked and accepts the word."""
    status = ERGA_STATUS.get(label)
    if not status:
        return {"synced": False, "reason": "no Erga status for this label"}
    manifest = state_root() / f"applications/{application_id}/resume-manifest.json"
    erga_id = json.loads(manifest.read_text()).get("application_id") if manifest.exists() else None
    if not erga_id:
        return {"synced": False, "warning": "No Erga application is linked to this application"}
    from .resumes import erga_call

    try:
        asyncio.run(
            erga_call("update_application_status", {"application_id": erga_id, "status": status})
        )
    except Exception as error:  # noqa: BLE001 -- the local record is authoritative; note it
        return {"synced": False, "status": status, "error": type(error).__name__}
    return {"synced": True, "status": status}


def recruiting_text(item: dict, data: dict) -> str:
    word = workflow.MAIL_LABEL_WORDS.get(data["label"], "Recruiting mail")
    line = f"→ **{word}** · {workflow.clip(workflow.display_title(item), 120)}"
    if data.get("deadline"):
        line += f" · {data['deadline']}"
    if data.get("interview_time"):
        line += f" · invite for {data['interview_time']}"
    if data.get("reconciled"):
        line += " · the unclear submission went through"
    if data.get("unsettled"):
        line += " · the submission is still unclear, so nothing moved"
    if data.get("label") == "rejection" and data.get("to_state"):
        line += " · reply `not rejected` in its thread if that is wrong"
    return line


def attempt_time(application_id: str) -> datetime | None:
    """When the one submit click for this application was made, if it was."""
    with workflow.db() as conn:
        row = conn.execute(
            "SELECT created_at FROM live_submission_attempts WHERE application_id=?",
            (application_id,),
        ).fetchone()
    try:
        moment = datetime.fromisoformat(row[0]) if row else None
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment and moment.tzinfo is None else moment


def settles(application_id: str, mail: dict, label: str) -> bool:
    """Whether this mail shows an unclear submission went through: it reads as a step in
    the process, it is not a reminder to finish an application, and it arrived after the
    submit click. Mail from before the click is about something else."""
    if label == "other" or mail.get("incomplete"):
        return False
    attempted = attempt_time(application_id)
    try:
        arrived = datetime.fromisoformat(str(mail.get("received_at")))
    except ValueError:
        return False
    return attempted is not None and arrived > attempted


def would_change(item: dict, mail: dict, label: str) -> bool:
    """Whether recording this mail would move the application at all."""
    if item["status"] == "UNKNOWN_SUBMISSION":
        return settles(item["id"], mail, label)
    target = LABEL_STATES.get(label)
    return bool(target and workflow.advances(item["status"], target))


def apply_mail(
    application_id: str,
    mail: dict,
    label: str,
    classifier: str,
    deadline,
    confirmed_by: str | None = None,
) -> dict:
    """Record one classified mail against its application: the thread card, the lifecycle
    step when the mail is a step forward, the recruiting line, and Erga best effort.

    Callers pass only mail whose sender was verified, or mail the owner confirmed
    (`confirmed_by` is the owner's Discord message)."""
    item = workflow.get(application_id)
    current = item["status"]
    data = {
        "label": label,
        "sender_domain": mail["sender_domain"],
        "subject": workflow.clip(mail["subject"], 150),
        "deadline": deadline,
        "message_id": mail["message_id"],
        "received_at": mail["received_at"],
        "classifier": classifier,
        "from_state": current,
    }
    if mail.get("interview_time"):
        data["interview_time"] = mail["interview_time"]
    if confirmed_by:
        data["confirmed_by_owner"] = True
    if current == "UNKNOWN_SUBMISSION" and settles(application_id, mail, label):
        # The employer answered after the click, so the attempt went through: the owner's
        # reconciliation step is settled by the mail, with the private copy as evidence.
        from .submission import finish_attempt

        word = workflow.MAIL_LABEL_WORDS[label].lower()
        receipt = {
            "application_id": application_id,
            "package_hash": item["package_hash"],
            "status": "APPLIED",
            "confirmed_at": workflow.now(),
            "reason": "The employer's mail acknowledged the application"
            if label == "acknowledgement"
            else f"The employer's mail ({word}) shows the application was received",
            "mail": {
                "message_id": mail["message_id"],
                "sender_domain": mail["sender_domain"],
                "subject": workflow.clip(mail["subject"], 150),
                "received_at": mail["received_at"],
                "evidence": str(mail["evidence_path"]),
            },
        }
        if confirmed_by:
            receipt["owner_message_id"] = confirmed_by
        finish_attempt(application_id, "APPLIED", receipt)
        current = "APPLIED"
        data["reconciled"] = True
    elif current == "UNKNOWN_SUBMISSION" and label != "other":
        data["unsettled"] = True
    target = LABEL_STATES.get(label)
    to_state = target if target and workflow.advances(current, target) else None
    if to_state:
        data["erga"] = erga_status(application_id, label)
    data["to_state"] = to_state
    workflow.record(application_id, "recruiting_mail", data)
    if to_state:
        workflow.transition(
            application_id,
            to_state,
            f"{MAIL_TRIGGER}: {label}",
            f"from {mail['sender_domain']}: {workflow.clip(mail['subject'], 150)}"
            + (" · you confirmed it" if confirmed_by else ""),
        )
    else:
        workflow.flush_events(application_id)
        workflow.sync_note(application_id)
    workflow.recruiting_line(application_id, recruiting_text(item, data), mail=data)
    return data


MAIL_TRIGGER = "recruiting mail"
UNDO_TRIGGER = "you said that mail was wrong"
# The state words Erga accepts when a mail-driven step is taken back.
ERGA_STATES = {"APPLIED": "applied", "OA": "oa", "INTERVIEW": "interview", "OFFER": "offer"}


def undo_last_step(application_id: str, owner_message_id: str) -> str:
    """The owner says the mail that moved this application was wrong (a rejection that
    was not one): put it back where it stood before that mail. Returns the line to post."""
    item = workflow.get(application_id)
    with workflow.db() as conn:
        row = conn.execute(
            "SELECT data FROM application_events WHERE application_id=? AND kind='lifecycle' "
            "ORDER BY id DESC LIMIT 1",
            (application_id,),
        ).fetchone()
    last = json.loads(row["data"]) if row else {}
    previous = last.get("from")
    if (
        last.get("to") != item["status"]
        or not str(last.get("trigger") or "").startswith(MAIL_TRIGGER)
        or previous not in workflow.POST_APPLICATION
        or previous == item["status"]
    ):
        raise ValueError("Nothing here was changed by a mail, so there is nothing to undo.")
    workflow.transition(application_id, previous, UNDO_TRIGGER, f"owner message {owner_message_id}")
    manifest = state_root() / f"applications/{application_id}/resume-manifest.json"
    erga_id = json.loads(manifest.read_text()).get("application_id") if manifest.exists() else None
    if erga_id and previous in ERGA_STATES:
        from .resumes import erga_call

        try:
            asyncio.run(
                erga_call(
                    "update_application_status",
                    {"application_id": erga_id, "status": ERGA_STATES[previous]},
                )
            )
        except Exception as error:  # noqa: BLE001 -- the local record is authoritative; note it
            workflow.system_line(
                application_id, f"erga status not restored · {type(error).__name__}"
            )
    return "Put back: " + workflow.STATE_WORDS.get(previous, "as it was") + "."


# --- mail the owner has to confirm ---------------------------------------------

LOOKS_LIKE = {
    "acknowledgement": "Looks like they received your application",
    "oa": "Looks like an online assessment",
    "interview": "Looks like an interview",
    "offer": "Looks like an offer",
    "rejection": "Looks like a rejection",
}
CONFIRM_WORDS = {"confirm", "confirmed", "yes", "real", "it is real", "its real", "it's real"}
DISMISS_WORDS = {"ignore", "no", "not real", "fake", "wrong", "dismiss", "spam"}


def plain(text, limit: int) -> str:
    """A sender's words as plain text in Discord: no markup, masked link, link or mention."""
    text = re.sub(r"\b(?:https?://|www\.)\S+", "(link)", str(text or ""))
    return workflow.clip(" ".join(re.sub(r"[`*_~|<>\[\]\\@#]", " ", text).split()), limit)


def read_evidence(message_id) -> dict:
    return json.loads((message_directory(message_id) / "message.json").read_text())


def candidate_ids(row: dict) -> list[str]:
    """The applications a held mail could be about when it did not say which; [] for a
    card about one application."""
    with mail_db() as conn:
        found = conn.execute(
            "SELECT candidates FROM mail_candidates WHERE message_id=?", (row["message_id"],)
        ).fetchone()
    try:
        ids = json.loads(found[0]) if found else []
    except ValueError:
        return []
    return [str(value) for value in ids if value] if isinstance(ids, list) else []


def candidate_titles(ids: list[str]) -> list[str]:
    titles = []
    for application_id in ids:
        try:
            titles.append(workflow.clip(workflow.display_title(workflow.get(application_id)), 100))
        except ValueError:
            titles.append("an application no longer on record")
    return titles


def card_text(row: dict) -> str:
    mail = read_evidence(row["message_id"])
    sender = plain(mail["sender_domain"], 80) or "an unknown sender"
    subject = plain(mail["subject"], 120)
    extra = f" · {plain(row['deadline'], 100)}" if row["deadline"] else ""
    if mail.get("interview_time"):
        extra += f" · invite for {plain(mail['interview_time'], 60)}"
    ids = candidate_ids(row)
    if ids:
        # The mail does not say which of several applications at one company it means.
        listed = "\n".join(
            f"{number}. {plain(title, 100)}"
            for number, title in enumerate(candidate_titles(ids), start=1)
        )
        return (
            f"→ **{LOOKS_LIKE[row['label']]}** · from {sender} · “{subject}”{extra}\n"
            f"It could be about any of these:\n{listed}\n"
            f"Nothing changed: {row['reason']}. Reply to this message with the number of the "
            "right one, or `ignore`."
        )
    item = workflow.get(row["application_id"])
    line = (
        f"→ **{LOOKS_LIKE[row['label']]}** · {workflow.clip(workflow.display_title(item), 120)} · "
        f"from {sender} · “{subject}”{extra}"
    )
    line += (
        f"\nNothing changed: {row['reason']}. Reply to this message with `confirm` if the "
        "mail is real, or `ignore`."
    )
    link = workflow.forum_url(row["application_id"])
    return line + (f" · <{link}>" if link else "")


def post_cards():
    """Post every waiting card that has not reached the recruiting channel yet. The row is
    written first, so a Discord outage delays a card and never loses it."""
    settings = workflow.config()
    channel = settings.get("recruiting_channel_id")
    if not settings.get("enabled") or not channel:
        return
    with mail_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM mail_confirmations WHERE status='pending' "
                "AND card_message_id IS NULL ORDER BY created_at"
            )
        ]
    for row in rows:
        try:
            sent = workflow.discord(
                "POST",
                f"/channels/{channel}/messages",
                {
                    "content": workflow.clip(card_text(row), 1900),
                    "allowed_mentions": {"parse": []},
                    "nonce": "mail:" + safe_id(row["message_id"])[:20],
                    "enforce_nonce": True,
                },
            )
        except (httpx.HTTPError, OSError, ValueError) as error:
            workflow.delivery_failed("mail-card", row["message_id"], error, row["application_id"])
            return
        with mail_db() as conn:
            conn.execute(
                "UPDATE mail_confirmations SET card_message_id=? WHERE message_id=?",
                (str(sent.get("id") or ""), row["message_id"]),
            )


def hold_card(
    application_id: str,
    mail: dict,
    label: str,
    classifier: str,
    deadline,
    why: str,
    candidates: list[str] | None = None,
):
    with mail_db() as conn:
        if candidates:
            conn.execute(
                "INSERT OR IGNORE INTO mail_candidates VALUES(?,?)",
                (mail["message_id"], json.dumps(candidates)),
            )
        conn.execute(
            "INSERT OR IGNORE INTO mail_confirmations VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                mail["message_id"],
                application_id,
                label,
                classifier,
                deadline,
                why,
                None,
                "pending",
                workflow.now(),
                None,
            ),
        )


def hold_for_owner(application: dict, mail: dict, label: str, classifier: str, deadline, why: str):
    """A mail that would move the application but cannot be trusted by itself: nothing
    changes; one card asks the owner."""
    hold_card(application["id"], mail, label, classifier, deadline, why)
    workflow.system_line(
        application["id"],
        f"recruiting mail held for the owner · {label} · {mail['sender_domain']} · "
        f"message {mail['message_id']} · {why}",
    )
    post_cards()


AMBIGUOUS = "it names no role, job number or posting link that tells them apart"


def hold_ambiguous(candidates: list[dict], mail: dict, label: str, classifier: str, deadline):
    """A mail about one of several applications at the same company that does not say
    which: nothing changes; one card names the candidates and asks the owner."""
    ids = [item["id"] for item in candidates][:6]
    hold_card("", mail, label, classifier, deadline, AMBIGUOUS, ids)
    workflow.system_line(
        "mail",
        f"recruiting mail held for the owner · {label} · {mail['sender_domain']} · message "
        f"{mail['message_id']} · matches {len(ids)} applications: {', '.join(ids)}",
    )
    post_cards()


PICK = re.compile(r"(?:#|number |no\.? )?(\d{1,2})(?: (?:confirm|confirmed|yes|it is))?")


def owner_reply(message: dict) -> str | None:
    """The owner's Discord reply on one of the waiting cards: `confirm` records the mail,
    `ignore` drops it, and on a card naming several applications the number picks one.
    Returns the line to post, or None when the message is not a reply to a card. The
    caller has already checked that the configured owner wrote it."""
    referenced = str((message.get("message_reference") or {}).get("message_id") or "")
    if not referenced:
        return None
    with mail_db() as conn:
        row = conn.execute(
            "SELECT * FROM mail_confirmations WHERE card_message_id=?", (referenced,)
        ).fetchone()
    if not row:
        return None
    row = dict(row)
    if row["status"] != "pending":
        return "That one is already settled."
    word = " ".join(str(message.get("content") or "").strip().strip("`").rstrip(".!?").split())
    word = word.lower()
    ids = candidate_ids(row)
    application_id = row["application_id"]
    if ids:
        picked = PICK.fullmatch(word)
        if word in DISMISS_WORDS:
            decision = "dismissed"
        elif picked and 1 <= int(picked[1]) <= len(ids):
            decision, application_id = "confirmed", ids[int(picked[1]) - 1]
        else:
            return f"Reply with the number of the right one (1 to {len(ids)}), or `ignore`."
    elif word in CONFIRM_WORDS | DISMISS_WORDS:
        decision = "confirmed" if word in CONFIRM_WORDS else "dismissed"
    else:
        return "Reply `confirm` if that mail is real, or `ignore`."
    mail = None
    if decision == "confirmed":
        try:
            mail = read_evidence(row["message_id"])
        except (OSError, ValueError):
            raise ValueError("I no longer have that mail on file, so nothing changed.") from None
        mail["evidence_path"] = message_directory(row["message_id"]) / "message.json"

    def settle(status: str, expected: str, chosen: str) -> bool:
        with mail_db() as conn:
            return bool(
                conn.execute(
                    "UPDATE mail_confirmations SET status=?,owner_message_id=?,application_id=? "
                    "WHERE message_id=? AND status=?",
                    (status, str(message.get("id") or ""), chosen, row["message_id"], expected),
                ).rowcount
            )

    if not settle(decision, "pending", application_id):
        return "That one is already settled."
    if mail is None:
        return "Left as it was."
    try:
        data = apply_mail(
            application_id,
            mail,
            row["label"],
            row["classifier"],
            row["deadline"],
            confirmed_by=str(message.get("id") or "owner"),
        )
    except Exception:
        # Nothing was recorded, so the card is still open for another reply.
        settle("pending", "confirmed", row["application_id"])
        raise
    # The owner tied this mail to the application: its brand is remembered for next time.
    brand = learn_alias(workflow.get(application_id), mail, "owner")
    if brand:
        workflow.system_line(application_id, f"mail brand learned · {brand} · from the owner")
    if data.get("to_state") or data.get("reconciled"):
        return "Recorded."
    return "Noted in its thread. Nothing else moved."


def read_headers(zoho: Zoho, folder_id: str, message_id: str) -> str:
    """The raw header block, or "" when Zoho has none for this message. A Zoho outage, a
    refused token or a limit still stops the tick."""
    try:
        return zoho.headers(folder_id, message_id)
    except ZohoFailure as failure:
        if failure.kind != "refused":
            raise
        return ""  # no header to read is no proof


def sender_trust(raw_headers: str, address: str, strength: str, settings: dict) -> dict:
    """Whether this mail may change the record by itself, and if not, why, in words."""
    if strength != "strong":
        return {
            "trusted": False,
            "why": "it did not come from the employer or their applicant system",
        }
    check = authentication(raw_headers, address, settings.get("authserv_ids") or ())
    return {
        "trusted": check["passed"],
        "why": "the sender could not be verified",
        "method": check["method"],
        "detail": check["why"],
    }


SPAM_WHY = "it landed in your spam folder"


def display_name(item: dict, raw_headers="") -> str:
    """The sender's display name, read only to learn the brand a confirmed mail used.
    Zoho's message list carries the bare address; the name is in the From header."""
    written = [value for name, value in header_block(raw_headers) if name == "from"]
    pairs = getaddresses([html.unescape(str(item.get("fromAddress") or "")), *written[:1]])
    named = next((name for name, _ in pairs if name.strip()), "")
    return squash(named or (item.get("sender") if "@" not in str(item.get("sender")) else ""))


# An acknowledgement belongs to a send for this long after its click.
RECEIPT_WINDOW = timedelta(days=2)
# A board's acknowledgement that names no company this record knows is about the one
# application sent through that board this shortly before it arrived.
JUST_SENT = timedelta(minutes=30)
# Boards whose mail comes from a second domain of theirs.
BOARD_MAIL = {"greenhouse-mail.io": "greenhouse.io"}


def after_click(application_id: str, received_at: str, window: timedelta) -> bool:
    """Whether a mail arrived within `window` after this application's one submit click."""
    clicked = attempt_time(application_id)
    try:
        arrived = datetime.fromisoformat(str(received_at))
    except ValueError:
        return False
    return clicked is not None and timedelta(0) <= arrived - clicked <= window


def sent_just_before(apps: list[dict], sender: str, received_at: str) -> dict | None:
    """The one application sent through the sender's own job board in the half hour
    before its acknowledgement arrived; None when none or several were. This is how a
    board's mail under a brand the record does not know yet still finds its application."""
    from . import job_index

    board = registrable(sender)
    board = BOARD_MAIL.get(board, board)
    if board not in ATS_DOMAINS:
        return None
    found = []
    for item in apps:
        links = (item.get("url"), item.get("source_url"), job_index.form_url(item["id"]))
        hosts = {registrable(urlsplit(str(link or "")).hostname or "") for link in links}
        if board in hosts and after_click(item["id"], received_at, JUST_SENT):
            found.append(item)
    if len(found) != 1:
        return None
    return {"candidates": found, "strength": "strong", "id_hit": False, "timed": True}


def is_receipt(application: dict, mail: dict, label: str, classifier: str, checked: dict) -> bool:
    """A plain "thank you for applying" that changes nothing may be recorded from a
    sender that is not the employer's known domain or board: the rules read it as an
    acknowledgement, the mail server verified the sender's own domain, and it arrived in
    the two days after this application's one submit click."""
    return (
        label == "acknowledgement"
        and classifier == "rule"
        and not mail.get("incomplete")
        and mail.get("folder") != "spam"
        and bool(checked.get("passed"))
        and after_click(application["id"], mail["received_at"], RECEIPT_WINDOW)
    )


def model_label(message_id: str, context: dict) -> tuple[str, str | None, str]:
    """(label, deadline, classifier) from the local model for a mail the rules could not
    settle; `other` when its answer is unusable. An outage of the model is raised."""
    try:
        label, deadline = classify_with_qwen(message_directory(message_id), context)
    except (RuntimeError, ValueError, TypeError) as error:
        from .reasoning import ModelUnavailable

        if isinstance(error, ModelUnavailable):
            raise
        return "other", None, "qwen_failed"
    return label, deadline, "qwen"


def handle_message(
    zoho: Zoho,
    folder_id: str,
    item: dict,
    apps: list[dict],
    settings: dict | None = None,
    *,
    folder: str = "inbox",
) -> dict:
    """Classify one message. Verified mail from the employer or their applicant system
    is applied; mail that would move an application but is not verified, mail from the
    Spam folder, and mail that could be about several applications become a card for the
    owner; automatic replies and everything else leave no trace but their id."""
    settings = settings or {}
    message_id = str(item.get("messageId") or "")
    # Only the address counts; the display name (`sender`) is whatever the sender typed.
    address = sender_address(item.get("fromAddress"))
    domain = address.rpartition("@")[2]
    subject = squash(item.get("subject"))
    outcome = {"message_id": message_id, "sender_domain": domain, "outcome": "ignored"}
    if not apps or not domain:
        return outcome
    content = zoho.content(folder_id, message_id)
    text = plain_text(content)
    received_at = datetime.fromtimestamp(received(item) / 1000, UTC).isoformat()
    label, classifier, deadline = classify(subject, text), "rule", None
    match = match_application(apps, domain, subject, text, mail_links(content), aliases_for(apps))
    if not match and label == "acknowledgement":
        match = sent_just_before(apps, domain, received_at)
    if not match:
        return outcome
    candidates, strength = match["candidates"], match["strength"]
    application = candidates[0]
    if label is None and strength != "strong":
        return outcome
    raw = "" if AUTO_SUBJECT.search(subject) else read_headers(zoho, folder_id, message_id)
    if auto_reply(subject, raw, text):
        return {**outcome, "why": "auto_reply"}
    if label is None:
        company, role = split_title(application)
        context = {
            "review_type": "recruiting_mail",
            "labels": list(LABELS),
            "sender_domain": domain,
            "subject": sanitize_for_model(subject, 200),
            "excerpt": sanitize_for_model(text),
            "application": {"company": company, "role": role if len(candidates) == 1 else ""},
        }
        label, deadline, classifier = model_label(message_id, context)
    deadline = stated_deadline(subject + "\n" + text) or deadline
    mail = {
        "message_id": message_id,
        "sender_domain": domain,
        "sender_name": display_name(item, raw),
        "subject": subject,
        "received_at": received_at,
        "incomplete": incomplete_notice(subject + "\n" + text),
        "folder": folder,
    }
    if label == "interview":
        when = invite_time(zoho, folder_id, item, text, settings)
        if when:
            mail["interview_time"] = when
    if folder == "spam":
        # Spam is read with less trust: at most a card, never a change by itself.
        trust = {"trusted": False, "why": SPAM_WHY}
    else:
        trust = sender_trust(raw, address, strength, settings)
    at_stake = [c for c in candidates if would_change(c, mail, label)]
    ambiguous = len(candidates) > 1
    if not trust["trusted"] and not ambiguous and not at_stake and folder != "spam":
        # A receipt changes nothing, so a verified sender that names the company is enough.
        checked = authentication(raw, address, settings.get("authserv_ids") or ())
        if is_receipt(application, mail, label, classifier, checked):
            trust = {"trusted": True, "why": "", "method": checked["method"], "receipt": True}
    if (ambiguous or not trust["trusted"]) and not at_stake:
        # Nothing at stake, or no one application to record it on: not worth a card.
        return outcome
    mail["evidence_path"] = message_directory(message_id) / "message.json"
    write_private(
        mail["evidence_path"],
        {
            **mail,
            "evidence_path": str(mail["evidence_path"]),
            "from": address,
            "text": redact_codes(text),
            "shown": redact_codes(plain_text(content, visible_only=True)),
            "label": label,
            "classifier": classifier,
            "application_id": None if ambiguous else application["id"],
            "candidates": [c["id"] for c in candidates] if ambiguous else None,
            "match": strength,
            "sender_check": {k: v for k, v in trust.items() if k != "why"},
        },
    )
    result = {
        **outcome,
        "application_id": None if ambiguous else application["id"],
        "label": label,
        "classifier": classifier,
    }
    if ambiguous:
        hold_ambiguous(candidates, mail, label, classifier, deadline)
        return {**result, "outcome": "held", "to_state": None, "candidates": len(candidates)}
    if not trust["trusted"]:
        hold_for_owner(application, mail, label, classifier, deadline, trust["why"])
        return {**result, "outcome": "held", "to_state": None}
    data = apply_mail(application["id"], mail, label, classifier, deadline)
    if strength == "strong" and (match["id_hit"] or match.get("timed")):
        # A verified sender named this posting's own id or link, or its board answered
        # the send within minutes: the brand it writes under is kept.
        how = "job link" if match["id_hit"] else "sent just before"
        brand = learn_alias(application, mail, how)
        if brand:
            workflow.system_line(application["id"], f"mail brand learned · {brand} · {how}")
    return {**result, "outcome": "applied", "to_state": data.get("to_state")}


# --- mail tracking health -----------------------------------------------------------

MAIL_ALERT = "mail"
FAILURES_BEFORE_CARD = 3
RECONNECT = {
    "auth": (
        "Zoho no longer accepts Rove's sign-in; the refresh token was revoked or has "
        "expired. Make a new refresh token the way the setup guide shows (Zoho API Console, "
        "your Self Client, the three read scopes) and put it in the private env file in "
        "place of the old one. Mail tracking starts again on its own on the next run."
    ),
    "quota": (
        "Zoho is refusing requests because a mail or API limit was reached. It usually "
        "clears by itself within a day; if it does not, check the Zoho account's storage "
        "and plan. Mail tracking starts again on its own once Zoho answers."
    ),
    "network": (
        "I could not reach Zoho. If the internet or Zoho was down there is nothing to do; "
        "mail tracking starts again on its own once Zoho answers."
    ),
    "refused": (
        "Zoho refused the requests. Check that the Zoho mail account still exists and that "
        "Rove's Self Client still has the three read scopes. Mail tracking starts again on "
        "its own once Zoho answers."
    ),
}
TROUBLE_WORDS = {
    "auth": "Zoho refused the sign-in",
    "quota": "Zoho's request limit was reached",
    "network": "Zoho could not be reached",
    "refused": "Zoho refused a request",
}


def mail_health(failure: "ZohoFailure | None") -> int:
    """One run's verdict on mail tracking: one system-log line when it starts failing,
    one owner card in the recruiting channel after three failures in a row, both said
    once; the card leaves on the first run that works. Returns the streak."""
    from . import alerts

    words = TROUBLE_WORDS.get(failure.kind if failure else "", "Zoho refused a request")
    return alerts.check(
        MAIL_ALERT,
        failure is None,
        error=failure.kind if failure else "",
        after=FAILURES_BEFORE_CARD,
        channel="recruiting",
        headline="Mail tracking stopped",
        text=(
            f"I could not read your recruiting mail the last {FAILURES_BEFORE_CARD} times: "
            f"{words.lower()}. " + RECONNECT.get(failure.kind if failure else "", "")
        ),
        first_line=f"{words.lower()} · {failure} · trying again next run" if failure else "",
        card_line=f"{FAILURES_BEFORE_CARD} failures in a row · owner card posted",
        back_line="reading mail works again",
        log_name="mail",
    )


def read_folder(
    zoho: Zoho, db, account: str, folder_id: str, kind: str, settings: dict, result: dict
) -> bool:
    """Handle one folder's new messages; False when the local model is down and the
    tick stops at that mail. Each folder keeps its own checkpoint."""
    checkpoint = account if kind == "inbox" else f"{account}:{kind}"
    row = db.execute(
        "SELECT received_time FROM mail_checkpoints WHERE account_id=?", (checkpoint,)
    ).fetchone()
    if row:
        since = int(row[0])
    else:
        days = max(0, int(settings.get("lookback_days", 3)))
        since = int(datetime.now(UTC).timestamp() * 1000) - days * 86_400_000
    for item in new_messages(zoho, folder_id, since):
        message_id = str(item.get("messageId") or "")
        if (
            not message_id
            or db.execute(
                "SELECT 1 FROM mail_messages WHERE message_id=?", (message_id,)
            ).fetchone()
        ):
            continue
        try:
            outcome = handle_message(
                zoho, folder_id, item, tracked_applications(), settings, folder=kind
            )
        except RuntimeError as error:
            from .reasoning import ModelUnavailable

            if not isinstance(error, ModelUnavailable):
                raise
            result["waiting"] = "model"
            return False
        except ZohoFailure as failure:
            if failure.kind != "refused":
                raise  # the whole run stops and is counted; this mail is read next time
            # Zoho no longer has this one message (moved or deleted since the listing):
            # it is passed over, so it cannot hold up every later mail.
            workflow.system_line("mail", f"message {message_id} could not be read · {failure}")
            address = sender_address(item.get("fromAddress"))
            outcome = {
                "message_id": message_id,
                "sender_domain": address.rpartition("@")[2],
                "outcome": "ignored",
            }
        result["seen"] += 1
        result[outcome["outcome"]] += 1
        if outcome["outcome"] in {"applied", "held"}:
            result["events"].append(outcome)
        with db:
            db.execute(
                "INSERT OR IGNORE INTO mail_messages VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    message_id,
                    account,
                    received(item),
                    outcome["sender_domain"],
                    outcome["outcome"],
                    outcome.get("label"),
                    outcome.get("classifier"),
                    outcome.get("application_id"),
                    workflow.now(),
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO mail_checkpoints VALUES(?,?,?,?)",
                (checkpoint, received(item), message_id, workflow.now()),
            )
    return True


def tick() -> dict:
    """Read new Inbox and Spam mail and apply what concerns a sent application.

    Off unless private `config/mail.json` enables it and the private env holds the four
    Zoho values. A mail is handled once; each folder's checkpoint advances past each
    handled mail, so a stop (the local model down for an ambiguous mail) resumes at that
    mail. A Zoho failure (a refused token, a limit, an outage) ends the run quietly: one
    system-log line, an owner card after three in a row, no traceback.
    """
    settings = config()
    creds = credentials()
    if not settings.get("enabled") or not creds:
        return {
            "enabled": False,
            "reason": "config/mail.json does not enable it"
            if not settings.get("enabled")
            else "the Zoho values are not in the private env",
        }
    # One reader at a time: the scheduled run and the look right after a send never
    # handle the same mail twice.
    lock_path = state_root() / "mail/tick.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"enabled": True, "busy": True}
        return read_mailbox(settings, creds)


def read_mailbox(settings: dict, creds: dict) -> dict:
    workflow.ensure_recruiting_channel()
    post_cards()
    result = {"enabled": True, "seen": 0, "applied": 0, "held": 0, "ignored": 0, "events": []}
    account = creds["account_id"]
    db = mail_db()
    try:
        with Zoho(creds) as zoho:
            folders = zoho.folders()
            for kind, folder_id in (
                ("inbox", zoho.inbox_folder(folders)),
                ("spam", zoho.spam_folder(folders)),
            ):
                if folder_id and not read_folder(
                    zoho, db, account, folder_id, kind, settings, result
                ):
                    break
    except ZohoFailure as failure:
        result["error"] = failure.kind
        result["failures_in_a_row"] = mail_health(failure)
    else:
        mail_health(None)
    finally:
        db.close()
    result["finished_at"] = workflow.now()
    write_private(state_root() / "mail/service.json", result)
    return result


# After a send, its receipt is looked for early instead of at the next scheduled run.
RECEIPT_WATCH = timedelta(minutes=12)  # how long after the click
RECEIPT_WAIT = timedelta(seconds=45)  # the first look
RECEIPT_EVERY = timedelta(seconds=75)  # between looks


def receipt_due(now: datetime | None = None) -> bool:
    """Whether a send of the last minutes has no mail on record yet and the mailbox was
    not read in the last minute. The employer's receipt usually arrives within a minute
    or two of the click."""
    now = now or datetime.now(UTC)
    with workflow.db() as conn:
        rows = conn.execute(
            "SELECT a.application_id FROM live_submission_attempts a JOIN application_queue q "
            "ON q.id=a.application_id WHERE q.status IN ('APPLIED','UNKNOWN_SUBMISSION') AND "
            "NOT EXISTS (SELECT 1 FROM application_events e WHERE "
            "e.application_id=a.application_id AND e.kind='recruiting_mail')"
        ).fetchall()
    waiting = False
    for row in rows:
        clicked = attempt_time(row["application_id"])
        waiting = waiting or (
            clicked is not None and RECEIPT_WAIT <= now - clicked <= RECEIPT_WATCH
        )
    if not waiting:
        return False
    try:
        last = datetime.fromisoformat(
            json.loads((state_root() / "mail/service.json").read_text())["finished_at"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return True
    return now - last >= RECEIPT_EVERY


def follow_up() -> dict | None:
    """The worker's look for a receipt right after a send; None when none is due."""
    return tick() if receipt_due() else None


def recheck(days: int = 7) -> dict:
    """Read the last `days` of mail again, for mail that was passed over before its
    application was on record or before the matching knew its sender. Only mail that
    left no trace is read again; mail already recorded or shown on a card never is."""
    days = max(1, min(int(days), 30))
    cutoff = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)
    db = mail_db()
    try:
        with db:
            forgotten = db.execute(
                "DELETE FROM mail_messages WHERE outcome='ignored' AND received_time>=?",
                (cutoff,),
            ).rowcount
            db.execute(
                "UPDATE mail_checkpoints SET received_time=? WHERE received_time>?",
                (cutoff, cutoff),
            )
    finally:
        db.close()
    return {**tick(), "read_again": forgotten}


def status() -> dict:
    """What the owner can check without seeing a secret: switches, cursor, counts."""
    from . import alerts

    settings = config()
    creds = credentials()
    db = mail_db()
    try:
        checkpoint = (
            db.execute(
                "SELECT received_time,message_id,updated_at FROM mail_checkpoints WHERE account_id=?",
                (creds["account_id"],),
            ).fetchone()
            if creds
            else None
        )
        counts = {
            r[0]: r[1]
            for r in db.execute("SELECT outcome,COUNT(*) FROM mail_messages GROUP BY outcome")
        }
    finally:
        db.close()
    return {
        "enabled": bool(settings.get("enabled")),
        "credentials": bool(creds),
        "api_base": creds["api_base"] if creds else None,
        "recruiting_channel": bool(workflow.config().get("recruiting_channel_id")),
        "checkpoint": {
            "received_at": datetime.fromtimestamp(checkpoint[0] / 1000, UTC).isoformat(),
            "updated_at": checkpoint[2],
        }
        if checkpoint
        else None,
        "messages": counts,
        "failures_in_a_row": alerts.streak(MAIL_ALERT),
    }
