"""Which hosts may receive applicant data, and which employer and job a link names.

Pure functions over URLs: no browser, no database, no DNS. A shared ATS hostname is
never an employer, so every board names its exact hosts (or one anchored pattern for
tenant subdomains) and says how a URL on it spells the tenant and the job. The boards
with a module of their own in `rove.boards` keep their rules there; this table only
points at them, so each board's facts live in one place.

To add a board, add one `Board` to `BOARDS` and raise `KEY_VERSION`: `approved_ats`,
`ats_vendor`, `job_scope`, `job_key`, `tenant_key` and `coverage` pick it up, and the
stored job keys are rebuilt.

A host off the table may still carry a form the owner wants filled. `ineligible` names
the hosts that never qualify, whatever the owner says: addresses instead of names,
private networks, hosting a stranger can rent, link shorteners, and names that are not
public site names at all.
"""

import ipaddress
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from . import boards as board_modules
from .jobs import strip_tracking

# Raise when a board or a scope rule changes: `job_index` rebuilds its derived tables.
KEY_VERSION = 3

Parts = list[str]
Query = dict[str, str]


@dataclass(frozen=True)
class Board:
    """One applicant tracking system, or one employer site that hosts its own form."""

    name: str
    hosts: tuple[str, ...] = ()  # exact hostnames
    pattern: str = ""  # or one anchored hostname pattern, for tenant subdomains
    path: str = ""  # a required path prefix when the host serves more than job pages
    vendor: str = ""  # the ATS vendor's own domain; empty for an employer's own site
    words: str = ""  # how a card names it: "Greenhouse"; empty when the host is the name
    # url -> whether this board serves it; defaults to the hosts, pattern and path above
    owns: Callable[[str], bool] | None = None
    # url -> the job's scope, or None when the URL names no job
    job: Callable[[str], tuple | None] | None = None
    # url -> the employer's tenant, or None when the URL names none
    tenant: Callable[[str], tuple | None] | None = None

    def serves(self, url: str) -> bool:
        if self.owns is not None:
            return self.owns(url)
        if not _plain(url):
            return False
        host, path, _parts, _query, _raw = _split(url)
        on_host = host in self.hosts or bool(self.pattern and re.fullmatch(self.pattern, host))
        return on_host and (not self.path or path == self.path or path.startswith(self.path + "/"))


def _split(url: str) -> tuple[str, str, Parts, Query, str]:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path
    parts = path.strip("/").split("/")
    query: Query = {}
    conflicting = set()
    for name, value in parse_qsl(parsed.query, keep_blank_values=False):
        if query.setdefault(name, value) != value:
            # `?for=a&for=b`: which one the site honours is unknown, so neither names it.
            conflicting.add(name)
    for name in conflicting:
        del query[name]
    return host, path, parts, query, parsed.query


def _plain(url: str) -> bool:
    """Public HTTPS on its default port, with nothing before the host: the only shape a
    board URL has. A look-alike with a port, a user or plain HTTP is nobody's board."""
    try:
        parsed = urlsplit(str(url or ""))
        return (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and not parsed.username
            and bool(parsed.hostname)
        )
    except ValueError:
        return False


def _greenhouse_space(host: str) -> str:
    # The EU data center is a separate set of boards under the same tokens.
    return "greenhouse.eu" if ".eu." in host else "greenhouse"


def _greenhouse_job(url: str) -> tuple | None:
    host, _path, parts, query, _raw = _split(url)
    if len(parts) >= 3 and parts[1] == "jobs" and parts[2].isdigit():
        return (_greenhouse_space(host), parts[0].lower(), parts[2])
    # The embedded form names its board and job in the query, not the path.
    if parts[:2] == ["embed", "job_app"] and query.get("for") and query.get("token", "").isdigit():
        return (_greenhouse_space(host), query["for"].lower(), query["token"])
    return None


def _greenhouse_tenant(url: str) -> tuple | None:
    host, _path, parts, query, _raw = _split(url)
    if parts[:1] == ["embed"]:
        return (_greenhouse_space(host), query["for"].lower()) if query.get("for") else None
    return (_greenhouse_space(host), parts[0].lower()) if parts and parts[0] else None


def _company_job(url: str) -> tuple | None:
    """`/{company}/{posting}`: Lever and Ashby."""
    host, _path, parts, _query, _raw = _split(url)
    return (host, parts[0].lower(), parts[1].lower()) if len(parts) >= 2 else None


def _company_tenant(url: str) -> tuple | None:
    host, _path, parts, _query, _raw = _split(url)
    return (host, parts[0].lower()) if parts and parts[0] else None


def _smartrecruiters_tenant(url: str) -> tuple | None:
    host, _path, parts, _query, _raw = _split(url)
    if parts[:2] == ["oneclick-ui", "company"] and len(parts) >= 3:
        return (host, parts[2].lower())
    return (host, parts[0].lower()) if parts and parts[0] else None


# The host every Eightfold customer shares: there the `domain` parameter names the tenant.
EIGHTFOLD_SHARED = ("app.eightfold.ai",)


def _eightfold_tenant(url: str) -> tuple | None:
    host, _path, _parts, query, _raw = _split(url)
    if host in EIGHTFOLD_SHARED:
        domain = query.get("domain", "").lower()
        return ("eightfold", domain) if domain else None
    return ("eightfold", host)


def _eightfold_job(url: str) -> tuple | None:
    _host, _path, parts, query, _raw = _split(url)
    tenant = _eightfold_tenant(url)
    pid = query.get("pid") or next((p for p in reversed(parts) if p.isdigit()), "")
    return (*tenant, pid) if tenant and pid else None


def _jobvite_segments(url: str) -> Parts:
    """`/{company}/job/{id}` or `/careers/{company}/job/{id}`, with the leading word dropped."""
    _host, _path, parts, _query, _raw = _split(url)
    return parts[1:] if parts[:1] == ["careers"] else parts


def _jobvite_job(url: str) -> tuple | None:
    segments = _jobvite_segments(url)
    if (
        len(segments) >= 3
        and segments[0]
        and segments[1].lower() == "job"
        and re.fullmatch(r"[A-Za-z0-9]{6,16}", segments[2])
    ):
        return ("jobvite", segments[0].lower(), segments[2])
    return None


def _jobvite_tenant(url: str) -> tuple | None:
    segments = _jobvite_segments(url)
    return ("jobvite", segments[0].lower()) if segments and segments[0] else None


def _workable_tenant(url: str) -> tuple | None:
    found = board_modules.workable.job(url)
    if not found:
        return None
    account, shortcode, _on_apply = found
    return ("workable", account or shortcode)


def _module_tenant(module, name: str) -> Callable[[str], tuple | None]:
    def tenant(url: str) -> tuple | None:
        label = module.tenant(url)
        return (name, label) if label else None

    return tenant


def from_module(module, words: str, vendor: str, tenant=None) -> Board:
    """A board whose hosts, job keys and pages live in its own module under rove.boards."""
    return Board(
        module.NAME, vendor=vendor, words=words, owns=module.owns, job=module.scope, tenant=tenant
    )


TENANT_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
NOT_WWW = r"(?!www\.)"  # the vendor's own marketing site is not a tenant

BOARDS: tuple[Board, ...] = (
    Board(
        "greenhouse",
        hosts=(
            "job-boards.greenhouse.io",
            "boards.greenhouse.io",
            "job-boards.eu.greenhouse.io",
            "boards.eu.greenhouse.io",
        ),
        vendor="greenhouse.io",
        words="Greenhouse",
        job=_greenhouse_job,
        tenant=_greenhouse_tenant,
    ),
    Board(
        "lever",
        hosts=("jobs.lever.co", "jobs.eu.lever.co"),
        vendor="lever.co",
        words="Lever",
        job=_company_job,
        tenant=_company_tenant,
    ),
    Board(
        "ashby",
        hosts=("jobs.ashbyhq.com",),
        vendor="ashbyhq.com",
        words="Ashby",
        job=_company_job,
        tenant=_company_tenant,
    ),
    Board(
        "jobvite",
        hosts=("jobs.jobvite.com",),
        vendor="jobvite.com",
        words="Jobvite",
        job=_jobvite_job,
        tenant=_jobvite_tenant,
    ),
    Board(
        "smartrecruiters",
        hosts=("jobs.smartrecruiters.com",),
        vendor="smartrecruiters.com",
        words="SmartRecruiters",
        tenant=_smartrecruiters_tenant,
    ),
    Board(
        "eightfold",
        pattern=NOT_WWW + TENANT_LABEL + r"\.eightfold\.ai",
        vendor="eightfold.ai",
        words="Eightfold",
        job=_eightfold_job,
        tenant=_eightfold_tenant,
    ),
    # The tenant is the host itself on the boards below.
    Board(
        "workday",
        pattern=TENANT_LABEL + r"\.wd\d{1,4}\.myworkdayjobs\.com",
        vendor="myworkdayjobs.com",
    ),
    Board("icims", pattern=NOT_WWW + TENANT_LABEL + r"\.icims\.com", vendor="icims.com"),
    # Oracle Recruiting lives on a customer's Fusion host; other oraclecloud.com hosts
    # (object storage, anything a customer can upload to) are not job boards.
    Board(
        "oracle",
        pattern=TENANT_LABEL + r"\.fa\.(?:[a-z0-9-]+\.)?oraclecloud\.com",
        vendor="oraclecloud.com",
    ),
    # An employer's own careers pages, listed because its form is filled there.
    Board("tesla", hosts=("www.tesla.com",), path="/careers"),
    # Boards with a module of their own: their hosts and job keys are read from there.
    from_module(board_modules.paylocity, "Paylocity", "paylocity.com"),
    from_module(board_modules.workable, "Workable", "workable.com", _workable_tenant),
    from_module(
        board_modules.jazzhr,
        "JazzHR",
        "applytojob.com",
        _module_tenant(board_modules.jazzhr, "jazzhr"),
    ),
    from_module(
        board_modules.bamboohr,
        "BambooHR",
        "bamboohr.com",
        _module_tenant(board_modules.bamboohr, "bamboohr"),
    ),
)


def board_for(url: str) -> Board | None:
    return next((board for board in BOARDS if board.serves(url)), None)


def approved_ats(url: str) -> bool:
    """An exact board host (and path, where the host serves more than jobs), as a plain
    public HTTPS address."""
    return board_for(url) is not None


def ats_vendor(host: str) -> bool:
    """Any host under an ATS vendor's own domain: never an employer's own site.

    Broader than `approved_ats` on purpose. This only keeps research away from vendor
    pages; it never lets applicant data go anywhere.
    """
    host = (host or "").lower().rstrip(".")
    return any(
        board.vendor and (host == board.vendor or host.endswith("." + board.vendor))
        for board in BOARDS
    )


def _residual_query(raw: str) -> str:
    return "&".join(sorted(pair for pair in strip_tracking(raw).split("&") if pair))


def _apply_suffix_dropped(path: str) -> str:
    return re.sub(r"/(apply|application|apply-?now)$", "", path.rstrip("/"), flags=re.IGNORECASE)


def _board_scope(url: str) -> tuple | None:
    """The scope a board's own URL rules give, or None when no such board serves the URL."""
    board = board_for(url)
    if not board or not board.job:
        return None
    scope = board.job(url)
    if scope:
        return scope
    # A shared host and a URL shape the board does not define: nothing is assumed, so two
    # tenants or two jobs can never read as one.
    host, path, _parts, _query, raw = _split(url)
    return (host, path.rstrip("/"), _residual_query(raw))


def job_scope(url: str) -> tuple:
    """Tenant AND job binding: an ATS hostname alone is never employer approval.

    Answers "is this page the job that was opened": on employer sites the posting and its
    apply page share the job number, not the path, so the number stands for the job.
    """
    scope = _board_scope(url)
    if scope:
        return scope
    host, path, _parts, query, _raw = _split(url)
    trimmed = _apply_suffix_dropped(path)
    # The posting's number may move into the query on the apply page ("?jobSeqNo=R12345").
    numbers = re.findall(r"[A-Za-z]{0,3}\d{4,}", trimmed) or re.findall(
        r"[A-Za-z]{0,3}\d{4,}", " ".join(query.values())
    )
    return (host, numbers[-1].upper()) if numbers else (host, trimmed)


def nested_paths(approved: str, form: str) -> bool:
    """On an employer's own site, the form often lives under the posting's own path
    (`/o/software-intern` and `/o/software-intern/c/new`): the same job, by the site's
    own layout. Boards define their own identity and never qualify here."""
    if board_for(approved) or board_for(form):
        return False
    host_a, path_a, _parts, _query, _raw = _split(approved)
    host_b, path_b, _parts, _query, _raw = _split(form)
    path_a, path_b = _apply_suffix_dropped(path_a), _apply_suffix_dropped(path_b)
    if not host_a or host_a != host_b or not path_a.strip("/") or not path_b.strip("/"):
        return False
    return path_b.startswith(path_a + "/") or path_a.startswith(path_b + "/")


def job_names(url: str) -> set[str]:
    """Every word a link could name its job by: its query values, its path segments and
    the numbers in its path ("?gh_jid=4471", "/jobs/4471-intern", "?ashby_jid=<id>")."""
    _host, path, parts, query, _raw = _split(url)
    names = {v.lower() for v in query.values()} | {p.lower() for p in parts if p}
    names |= set(re.findall(r"\d{4,}", path))
    return {name for name in names if len(name) >= 4}


def embedded_job(
    queued: str, page_url: str | None, form: str, host_ok: Callable[[str], bool]
) -> bool:
    """A board's form inside the employer's own page is the queued job.

    The tab shows the employer's page (`page_url`); the form is a frame a board in the
    table serves. It is the job that was opened when the page is on the queued link's
    host (or on a host the owner let a form be filled on, `host_ok`), and the board's job
    id inside the frame is the job the queued link names. The frame's own key is the
    board's key, so the employer page and the board link reach one send.
    """
    if not page_url:
        return False
    board = board_for(form)
    scope = board.job(form) if board and board.job else None
    if not scope:
        return False
    page_host, queued_host = _split(page_url)[0], _split(queued)[0]
    if not page_host or (page_host != queued_host and not host_ok(page_url)):
        return False
    return str(scope[-1]).lower() in job_names(queued)


def job_key(url: str) -> str:
    """One application per job, however its link is spelled.

    Answers "are these two links the same application". The port, a trailing slash, a
    fragment, tracking parameters, an `/apply` suffix and a board's alternate hostname do
    not make a second job. Off the boards that define their own job identity the key
    stays close to the link: the whole path and the rest of the query, because a number
    in a path can be a year and a query can be what tells two jobs apart. Two distinct
    jobs must never share a key; one job under two unusual links may get two.
    """
    scope = _board_scope(url)
    if not scope:
        host, path, _parts, _query, raw = _split(url)
        scope = (host, _apply_suffix_dropped(path), _residual_query(raw))
    return json.dumps([str(part) for part in scope], separators=(",", ":"))


def tenant_scope(url: str) -> tuple:
    """The employer's account on a board. On a shared host with no tenant in the URL, the
    job's own scope stands in, so an unnamed tenant is never mistaken for a known one."""
    board = board_for(url)
    if board and board.tenant:
        return board.tenant(url) or job_scope(url)
    return (_split(url)[0],)


def tenant_key(url: str) -> str:
    return json.dumps([str(part) for part in tenant_scope(url)], separators=(",", ":"))


def tenant_words(url: str) -> str:
    """The employer's board as a card names it: "example on Greenhouse", or the host."""
    board = board_for(url)
    if board and board.tenant and board.words:
        tenant = board.tenant(url)
        if tenant:
            return f"{tenant[-1]} on {board.words}"
    return _split(url)[0] or "this site"


# ---------------------------------------------------------------------------
# Hosts off the table
# ---------------------------------------------------------------------------

# Domains under which anyone can put up a page or a form. A form there names no employer,
# so no reply from the owner makes it a place for applicant data.
SHARED_HOSTS = frozenset(
    {
        # Static sites and app hosting
        "github.io",
        "gitlab.io",
        "pages.dev",
        "workers.dev",
        "vercel.app",
        "netlify.app",
        "web.app",
        "firebaseapp.com",
        "herokuapp.com",
        "onrender.com",
        "railway.app",
        "fly.dev",
        "deno.dev",
        "repl.co",
        "replit.app",
        "replit.dev",
        "glitch.me",
        "surge.sh",
        "azurewebsites.net",
        "azurestaticapps.net",
        "appspot.com",
        "run.app",
        "cloudfunctions.net",
        "elasticbeanstalk.com",
        "cloudfront.net",
        "pythonanywhere.com",
        "000webhostapp.com",
        "trycloudflare.com",
        "ngrok.io",
        "ngrok.app",
        "ngrok-free.app",
        "ngrok-free.dev",
        "loca.lt",
        "serveo.net",
        # Object storage
        "amazonaws.com",
        "s3.amazonaws.com",
        "storage.googleapis.com",
        "blob.core.windows.net",
        "digitaloceanspaces.com",
        "linodeobjects.com",
        "backblazeb2.com",
        "r2.dev",
        # Site builders, documents and form builders
        "notion.site",
        "webflow.io",
        "wixsite.com",
        "squarespace.com",
        "wordpress.com",
        "weebly.com",
        "blogspot.com",
        "substack.com",
        "carrd.co",
        "framer.website",
        "framer.app",
        "typeform.com",
        "airtable.com",
        "medium.com",
        "docs.google.com",
        "forms.gle",
        "sites.google.com",
        "drive.google.com",
        "dropbox.com",
        "jotform.com",
        "tally.so",
        "wufoo.com",
        "formsite.com",
        "formspree.io",
        "hsforms.com",
        "formstack.com",
        "cognitoforms.com",
        "paperform.co",
        "surveymonkey.com",
        "forms.office.com",
    }
)
# Hosts whose only job is to redirect: a link to them says nothing about where it goes.
SHORTENERS = frozenset(
    {
        "bit.ly",
        "t.co",
        "goo.gl",
        "tinyurl.com",
        "lnkd.in",
        "ow.ly",
        "buff.ly",
        "rebrand.ly",
        "cutt.ly",
        "is.gd",
        "t.ly",
        "short.io",
        "shorturl.at",
        "rb.gy",
        "grnh.se",
        "jobvite.me",
    }
)
# Object storage buckets on Oracle and Amazon carry their region in the host.
STORAGE_PATTERNS = (
    re.compile(r"objectstorage\.[a-z0-9-]+\.oraclecloud\.com"),
    re.compile(r"[a-z0-9.-]*s3[.-][a-z0-9-]+\.amazonaws\.com"),
    re.compile(r"[a-z0-9.-]*\.s3\.amazonaws\.com"),
)
# Top-level names that are not on the public internet.
NON_PUBLIC_TLDS = frozenset(
    {
        "local",
        "localhost",
        "localdomain",
        "internal",
        "intranet",
        "lan",
        "home",
        "corp",
        "test",
        "example",
        "invalid",
        "onion",
        "arpa",
    }
)
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# The registry's own second level under a country code ("co.uk", "com.au"): a suffix that
# names nobody, never a site of its own.
REGISTRY_LABELS = frozenset({"co", "com", "net", "org", "ac", "edu", "gov", "gob", "ne", "or"})


def _under(host: str, domains: Iterable[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _bare_suffix(labels: list[str]) -> bool:
    return len(labels) == 2 and len(labels[1]) == 2 and labels[0] in REGISTRY_LABELS


def ineligible(url: str) -> str:
    """Why this host can never take applicant data, in plain words, or "" when it may.

    Boards and employer sites pass. Addresses, private networks, hosting a stranger can
    rent, link shorteners and names that are not public site names never do, whatever
    the owner replies.
    """
    try:
        parsed = urlsplit(str(url or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return "it is not a valid web address"
    if parsed.scheme != "https" or port not in (None, 443) or parsed.username or not host:
        return "it is not a plain HTTPS address"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        mapped = getattr(address, "ipv4_mapped", None) or address
        return (
            "it is a private network address"
            if not mapped.is_global
            else "it is a bare address, not a site name"
        )
    labels = host.split(".")
    if (
        len(labels) < 2
        or len(host) > 253
        or not all(LABEL.fullmatch(label) for label in labels)
        or labels[-1] in NON_PUBLIC_TLDS
        or not (re.fullmatch(r"[a-z]{2,24}", labels[-1]) or labels[-1].startswith("xn--"))
        or _bare_suffix(labels)
    ):
        return "it is not a public site name"
    if any(pattern.fullmatch(host) for pattern in STORAGE_PATTERNS) or _under(host, SHARED_HOSTS):
        return "it is hosting that anyone can rent, not an employer's own site"
    if _under(host, SHORTENERS):
        return "it is a link shortener, not a site"
    return ""


def coverage(urls: Iterable[str]) -> dict:
    """Counts by board for a set of links, and how many fall outside the table. Read-only:
    nothing is fetched, stored or named beyond the host of each link off the table."""
    by_board: Counter = Counter()
    off_table: Counter = Counter()
    unfit = 0
    for url in urls:
        board = board_for(url)
        if board:
            by_board[board.name] += 1
            continue
        host = _split(url)[0] or "(no host)"
        off_table[host] += 1
        if ineligible(url):
            unfit += 1
    return {
        "by_board": dict(sorted(by_board.items())),
        "not_in_table": sum(off_table.values()),
        "hosts_not_in_table": dict(off_table.most_common()),
        "never_eligible": unfit,
    }
