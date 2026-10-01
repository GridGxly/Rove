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
card in the recruiting channel that the owner confirms with a word or ignores.
"""

import asyncio
import html
import json
import re
from datetime import UTC, datetime
from email.parser import HeaderParser
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

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
    """)
    return conn


# --- Zoho transport ----------------------------------------------------------


def http(base_url: str, headers: dict | None = None) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers=headers or {}, timeout=30, trust_env=False)


class Zoho:
    """The read-only slice of the Zoho Mail API the tracker needs: one folder, a page of
    message headers, one message body. The access token lives in this object only."""

    def __init__(self, creds: dict):
        self.creds = creds
        self.client: httpx.Client | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        if self.client is not None:
            self.client.close()

    def connect(self):
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
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            # The body names the problem (an invalid client or refresh token); it is not
            # printed because the same response can carry an issued token.
            raise ValueError(
                "Zoho issued no access token; check the Zoho values in the private env"
            )
        self.client = http(
            self.creds["api_base"],
            {"Authorization": "Zoho-oauthtoken " + token, "Accept": "application/json"},
        )

    def get(self, path: str, params: dict | None = None):
        if self.client is None:
            self.connect()
        response = self.client.get(path, params=params)
        response.raise_for_status()
        payload = response.json()
        status = payload.get("status") or {}
        if status.get("code") not in (None, 200):
            raise ValueError(
                "Zoho refused the request: " + str(status.get("description", ""))[:120]
            )
        return payload.get("data")

    def inbox_folder(self) -> str:
        folders = self.get(f"/api/accounts/{self.creds['account_id']}/folders") or []
        inbox = next(
            (f for f in folders if str(f.get("folderType", "")).lower() == "inbox"), None
        ) or next((f for f in folders if str(f.get("path", "")).lower() == "/inbox"), None)
        if not inbox:
            raise ValueError("The Zoho account has no Inbox folder")
        return str(inbox["folderId"])

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


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIPPED_TAGS:
            self.skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n" if tag != "td" else " ")

    def handle_endtag(self, tag):
        if tag in SKIPPED_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n" if tag != "td" else " ")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain_text(content: str) -> str:
    """The readable text of an HTML or plain mail body; markup, scripts and styles dropped."""
    parser = _Text()
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
    header, written above the server's own Received line and carrying its name. A header
    of that name further down, or text of that shape in the body, is the sender's and
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
    if "received" not in names or "authentication-results" not in names:
        return verdict(False, "the mail server recorded no check")
    index = names.index("authentication-results")
    if index > names.index("received"):
        return verdict(False, "the only check on record was written by the sender")
    value = headers[index][1]
    while re.search(r"\([^()]*\)", value):
        value = re.sub(r"\([^()]*\)", " ", value)
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


def employer_domain(url: str) -> str | None:
    domain = registrable(urlsplit(str(url or "")).hostname or "")
    return None if not domain or domain in ATS_DOMAINS else domain


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
        match = re.search(r"^(.*?)\s+at\s+([^|–—]+?)\s*(?:[|–—].*)?$", shown)
        company, role = (match[2], match[1]) if match else ("", shown)
    return COMPANY_SUFFIXES.sub("", company.strip()), role.strip()


def company_name(item: dict) -> str:
    name = normalize(split_title(item)[0])
    return name if len(name) >= 3 else ""


def role_hits(item: dict, body: str) -> int:
    words = {
        w
        for w in normalize(split_title(item)[1]).split()
        if len(w) >= 4 and w not in GENERIC_ROLE_WORDS
    }
    return sum(1 for w in words if f" {w} " in f" {body} ")


def match_application(apps: list[dict], sender: str, subject: str, text: str):
    """The application a mail concerns and how surely: the employer's own domain, or a
    known recruiting sender naming the company, is strong; the company named only in the
    subject is weak and counts only when the rules recognise the mail."""
    domain = registrable(sender)
    body = normalize(subject + " " + text)
    head = normalize(subject)
    best = None
    for item in apps:
        employer = employer_domain(item["url"])
        name = company_name(item)
        in_subject = bool(name) and f" {name} " in f" {head} "
        named = in_subject or (bool(name) and f" {name} " in f" {body} ")
        if employer and domain == employer:
            strength, score = "strong", 3
        elif domain in ATS_DOMAINS and named:
            strength, score = "strong", 2
        elif in_subject:
            strength, score = "weak", 1
        else:
            continue
        key = (score + role_hits(item, body), item["updated_at"])
        if best is None or key > best[0]:
            best = (key, item, strength)
    return (best[1], best[2]) if best else None


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
    line = (
        f"→ **{word}** · {workflow.clip(workflow.display_title(item), 120)} · "
        f"from {data['sender_domain']} · “{workflow.clip(data['subject'], 120)}”"
    )
    if data.get("deadline"):
        line += f" · {data['deadline']}"
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
    workflow.recruiting_line(application_id, recruiting_text(item, data))
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


def card_text(row: dict) -> str:
    item = workflow.get(row["application_id"])
    mail = read_evidence(row["message_id"])
    line = (
        f"→ **{LOOKS_LIKE[row['label']]}** · {workflow.clip(workflow.display_title(item), 120)} · "
        f"from {plain(mail['sender_domain'], 80) or 'an unknown sender'} · "
        f"“{plain(mail['subject'], 120)}”"
    )
    if row["deadline"]:
        line += f" · {plain(row['deadline'], 100)}"
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


def hold_for_owner(application: dict, mail: dict, label: str, classifier: str, deadline, why: str):
    """A mail that would move the application but cannot be trusted by itself: nothing
    changes; one card asks the owner."""
    with mail_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO mail_confirmations VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                mail["message_id"],
                application["id"],
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
    workflow.system_line(
        application["id"],
        f"recruiting mail held for the owner · {label} · {mail['sender_domain']} · "
        f"message {mail['message_id']} · {why}",
    )
    post_cards()


def owner_reply(message: dict) -> str | None:
    """The owner's Discord reply on one of the waiting cards: `confirm` records the mail,
    `ignore` drops it. Returns the line to post, or None when the message is not a reply
    to a card. The caller has already checked that the configured owner wrote it."""
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
    if word not in CONFIRM_WORDS | DISMISS_WORDS:
        return "Reply `confirm` if that mail is real, or `ignore`."
    decision = "confirmed" if word in CONFIRM_WORDS else "dismissed"
    with mail_db() as conn:
        changed = conn.execute(
            "UPDATE mail_confirmations SET status=?,owner_message_id=? "
            "WHERE message_id=? AND status='pending'",
            (decision, str(message.get("id") or ""), row["message_id"]),
        ).rowcount
    if not changed:
        return "That one is already settled."
    if decision == "dismissed":
        return "Left as it was."
    mail = read_evidence(row["message_id"])
    mail["evidence_path"] = message_directory(row["message_id"]) / "message.json"
    data = apply_mail(
        row["application_id"],
        mail,
        row["label"],
        row["classifier"],
        row["deadline"],
        confirmed_by=str(message.get("id") or "owner"),
    )
    if data.get("to_state") or data.get("reconciled"):
        return "Recorded."
    return "Noted in its thread. Nothing else moved."


def sender_trust(
    zoho: Zoho, folder_id: str, message_id: str, address: str, strength: str, settings: dict
) -> dict:
    """Whether this mail may change the record by itself, and if not, why, in words."""
    if strength != "strong":
        return {
            "trusted": False,
            "why": "it did not come from the employer or their applicant system",
        }
    try:
        raw = zoho.headers(folder_id, message_id)
    except (httpx.HTTPStatusError, ValueError):
        raw = ""  # no header to read is no proof; a network failure still stops the tick
    check = authentication(raw, address, settings.get("authserv_ids") or ())
    return {
        "trusted": check["passed"],
        "why": "the sender could not be verified",
        "method": check["method"],
        "detail": check["why"],
    }


def handle_message(
    zoho: Zoho, folder_id: str, item: dict, apps: list[dict], settings: dict | None = None
) -> dict:
    """Classify one inbox message. Verified mail from the employer or their applicant
    system is applied; mail that would move an application but is not verified becomes a
    card for the owner; everything else leaves no trace but its id."""
    message_id = str(item.get("messageId") or "")
    # Only the address counts; the display name (`sender`) is whatever the sender typed.
    address = sender_address(item.get("fromAddress"))
    domain = address.rpartition("@")[2]
    subject = squash(item.get("subject"))
    outcome = {"message_id": message_id, "sender_domain": domain, "outcome": "ignored"}
    if not apps or not domain:
        return outcome
    text = plain_text(zoho.content(folder_id, message_id))
    match = match_application(apps, domain, subject, text)
    if not match:
        return outcome
    application, strength = match
    label, classifier, deadline = classify(subject, text), "rule", None
    if label is None and strength != "strong":
        return outcome
    if label is None:
        context = {
            "review_type": "recruiting_mail",
            "labels": list(LABELS),
            "sender_domain": domain,
            "subject": sanitize_for_model(subject, 200),
            "excerpt": sanitize_for_model(text),
            "application": dict(zip(("company", "role"), split_title(application))),
        }
        try:
            label, deadline = classify_with_qwen(message_directory(message_id), context)
            classifier = "qwen"
        except (RuntimeError, ValueError, TypeError) as error:
            from .reasoning import ModelUnavailable

            if isinstance(error, ModelUnavailable):
                raise
            label, classifier = "other", "qwen_failed"
    deadline = stated_deadline(subject + "\n" + text) or deadline
    mail = {
        "message_id": message_id,
        "sender_domain": domain,
        "subject": subject,
        "received_at": datetime.fromtimestamp(received(item) / 1000, UTC).isoformat(),
        "incomplete": incomplete_notice(subject + "\n" + text),
    }
    trust = sender_trust(zoho, folder_id, message_id, address, strength, settings or {})
    if not trust["trusted"] and not would_change(application, mail, label):
        # Unverified and nothing at stake: not worth the owner's attention.
        return outcome
    mail["evidence_path"] = message_directory(message_id) / "message.json"
    write_private(
        mail["evidence_path"],
        {
            **mail,
            "evidence_path": str(mail["evidence_path"]),
            "from": address,
            "text": text,
            "label": label,
            "classifier": classifier,
            "application_id": application["id"],
            "match": strength,
            "sender_check": {k: v for k, v in trust.items() if k != "why"},
        },
    )
    result = {
        **outcome,
        "application_id": application["id"],
        "label": label,
        "classifier": classifier,
    }
    if not trust["trusted"]:
        hold_for_owner(application, mail, label, classifier, deadline, trust["why"])
        return {**result, "outcome": "held", "to_state": None}
    data = apply_mail(application["id"], mail, label, classifier, deadline)
    return {**result, "outcome": "applied", "to_state": data.get("to_state")}


def tick() -> dict:
    """Read inbox mail newer than the checkpoint and apply what concerns a sent application.

    Off unless private `config/mail.json` enables it and the private env holds the four
    Zoho values. A mail is handled once; the checkpoint advances past each handled mail,
    so a stop (the local model down for an ambiguous mail) resumes at that mail.
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
    workflow.ensure_recruiting_channel()
    post_cards()
    result = {"enabled": True, "seen": 0, "applied": 0, "held": 0, "ignored": 0, "events": []}
    account = creds["account_id"]
    db = mail_db()
    try:
        row = db.execute(
            "SELECT received_time FROM mail_checkpoints WHERE account_id=?", (account,)
        ).fetchone()
        if row:
            since = int(row[0])
        else:
            days = max(0, int(settings.get("lookback_days", 3)))
            since = int(datetime.now(UTC).timestamp() * 1000) - days * 86_400_000
        with Zoho(creds) as zoho:
            folder = zoho.inbox_folder()
            for item in new_messages(zoho, folder, since):
                message_id = str(item.get("messageId") or "")
                if (
                    not message_id
                    or db.execute(
                        "SELECT 1 FROM mail_messages WHERE message_id=?", (message_id,)
                    ).fetchone()
                ):
                    continue
                try:
                    outcome = handle_message(zoho, folder, item, tracked_applications(), settings)
                except RuntimeError as error:
                    from .reasoning import ModelUnavailable

                    if not isinstance(error, ModelUnavailable):
                        raise
                    result["waiting"] = "model"
                    break
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
                        (account, received(item), message_id, workflow.now()),
                    )
    finally:
        db.close()
    result["finished_at"] = workflow.now()
    write_private(state_root() / "mail/service.json", result)
    return result


def status() -> dict:
    """What the owner can check without seeing a secret: switches, cursor, counts."""
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
    }
