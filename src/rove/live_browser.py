"""Visible recruiting browser, owned by a local daemon with a narrow Unix-socket API.

The agent can open a job, follow an observed application link, observe, and prepare
known fields. It cannot execute JS, choose a file, invent an answer or submit.
"""

import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import socketserver
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from patchright.sync_api import Error as PlaywrightError
from patchright.sync_api import sync_playwright

from . import boards, form_reading, questions, workflow
from .jobs import lookup_job_link, public_link
from .onboarding import read_approved
from .runtime import state_root, write_private

ATS_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "tesla.com",
    "oraclecloud.com",
    "icims.com",
    "smartrecruiters.com",
    "eightfold.ai",
    "workable.com",
    "jobs.ashbyhq.com",
)


def approved_ats(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if boards.approved(url):  # Paylocity, Workable, JazzHR, BambooHR: exact host patterns
        return True
    return any(host == suffix or host.endswith("." + suffix) for suffix in ATS_HOSTS)


def same_value(expected: str, actual: str) -> bool:
    """A typed value counts when the site kept it, reformatted it, or prefixed its country code."""
    expected, actual = str(expected or ""), str(actual or "")
    if expected == actual or " ".join(expected.split()) == " ".join(actual.split()):
        return True
    digits_expected, digits_actual = re.sub(r"\D", "", expected), re.sub(r"\D", "", actual)
    if len(digits_expected) >= 10 and len(digits_actual) >= 10:
        return digits_expected[-10:] == digits_actual[-10:]
    return len(digits_expected) >= 7 and digits_actual.endswith(digits_expected)


def job_scope(url: str) -> tuple:
    """Tenant AND job binding: an ATS hostname alone is never employer approval."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    parts = parsed.path.strip("/").split("/")
    board_scope = boards.scope(url)
    if board_scope:  # a posting, its form and its confirmation share one key per board
        return board_scope
    if (
        host in {"job-boards.greenhouse.io", "boards.greenhouse.io"}
        and len(parts) >= 3
        and parts[1] == "jobs"
        and parts[2].isdigit()
    ):
        return ("greenhouse", parts[0], parts[2])
    if host == "jobs.lever.co" and len(parts) >= 2:
        return (host, *parts[:2])
    if host == "jobs.ashbyhq.com" and len(parts) >= 2:
        return (host, *parts[:2])
    # Employer sites: the posting and its apply page share the job number, not the path.
    path = re.sub(
        r"/(apply|application|apply-?now)$", "", parsed.path.rstrip("/"), flags=re.IGNORECASE
    )
    numbers = re.findall(r"[A-Za-z]{0,3}\d{4,}", path)
    return (host, numbers[-1].upper()) if numbers else (host, path)


def validate_destination(url: str) -> str:
    safe = public_link(url)
    if not safe:
        raise PermissionError("Only public HTTPS job pages are supported")
    host = urlsplit(safe).hostname
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise PermissionError("Private/local network destinations are forbidden")
    return safe


def normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


COUNTRY_ALIASES = questions.COUNTRY_ALIASES


def option_matches(option_label: str, value) -> bool:
    """Exact option text, or the same country written differently."""
    return questions.option_matches(option_label, value)


def is_place_label(label) -> bool:
    return questions.is_place_label(label)


# Questions that are several controls, or buttons: they have no input of their own under
# `data-rove-field`, so account forms never type into them.
GROUP_KINDS = ("radio_group", "checkbox_group", "choice")


def phone_variants(value) -> list[str]:
    """National digits first (sites with their own +1 selector reject a repeated code),
    then the international form; a non-US number is left as written."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        return [digits[1:], "+" + digits]
    if len(digits) == 10:
        return [digits, "+1" + digits]
    return [str(value or "")]


def resolve_known(label: str, profile: dict) -> tuple[str | None, str | None]:
    """A free-text value for a label from the approved profile, with its source.

    The rules live in questions.py: a label is matched by its meaning, word for word,
    and anything it does not know (dates, demographics, another person's contact
    details, a custom question) is never guessed.
    """
    return questions.known_fact(label, profile)


def decline_self_identification(label: str, option_labels: list[str]) -> str | None:
    """Voluntary self-identification is answered with the form's own decline option."""
    return questions.decline_self_identification(label, option_labels)


def resolve_choice(label: str, options: list[dict], profile: dict) -> tuple[str | None, str | None]:
    """Pick exactly one option from what code may answer without the owner's memory: an
    approved fact, the form's decline option, or a standing default for a plain question.
    Never a guess among options."""
    return questions.resolve({"label": label, "options": options}, profile)


# Visible status and validation messages, read the same way by the observation below and
# by the generic submission wait in submission.py. Both scripts define `visible` first.
# An invalid field has no text of its own, so its described-by message or its parent's
# text stands in for it.
STATUS_SELECTOR = "[role=alert],[role=status],.confirmation,.success"
ERROR_SELECTOR = "[role=alert],.error,[aria-invalid=true]"
MESSAGES_JS = (
    "sel=>[...new Set([...document.querySelectorAll(sel)].filter(visible).map(e=>"
    "e.matches('input,select,textarea')?((e.getAttribute('aria-describedby')||'').split(' ')"
    ".map(id=>document.getElementById(id)?.innerText||'').join(' ').trim()"
    "||e.parentElement?.innerText||''):e.innerText).map(t=>t.trim()).filter(Boolean))]"
    ".join('\\n').slice(0,2000)"
)

OBSERVE = (
    r"""() => {
 const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' && e.getAttribute('aria-hidden')!=='true';
 const messages=__MESSAGES__;
__READING__
 const controls=[...document.querySelectorAll('input,textarea,select')].filter(shown);
 const fields=controls.map((e,i)=>{
   e.setAttribute('data-rove-field',String(i));
   // A grouped radio or checkbox is one option of its group's question, never a question itself.
   const g=groupFor(e,controls);const read=g?readOption(e):readLabel(e);
   return {ref:String(i),label:read.text,group:g?g.label:'',name:e.name,id:e.id,kind:e.type,tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),placeholder:e.getAttribute('placeholder')||'',autocomplete:e.getAttribute('aria-autocomplete')||'',
    selected:e.closest('.select__container')?.querySelector('.select__single-value')?.innerText||null,
    selection_code:e.closest('.select__container')?.querySelector('.select__single-value .iti__flag')?.className.match(/\biti__([a-z]{2})\b/)?.[1]||null,
    required:e.required || e.getAttribute('aria-required')==='true' || requiredBy(e) || !!read.required,disabled:e.disabled,readonly:e.readOnly,checked:e.checked,
    value:(['password','hidden','file'].includes(e.type)?null:e.value),maxlength:(e.maxLength>0?e.maxLength:null),
    options:e.tagName==='SELECT'?[...e.options].map(o=>({label:o.text,value:o.value})).slice(0,300):[],
    ...(read.missing?{label_missing:true}:{}),...(g?{_group:{key:g.key,required:g.required,missing:g.missing}}:{})};
 }).filter(e=>e.kind!=='hidden');
 const boxes=[...new Set([...document.querySelectorAll('button[aria-pressed]')].filter(visible).map(b=>b.parentElement))].filter(c=>c.querySelectorAll(':scope > button[aria-pressed]').length>=2);
 const choices=boxes.map((c,i)=>{c.setAttribute('data-rove-choice',String(i));const buttons=[...c.querySelectorAll(':scope > button[aria-pressed]')];const box=c.querySelector('input');const read=readContainer(c);
   return {ref:String(i),label:read.text,group:'',name:box?.name||'',id:box?.id||'',kind:'choice',tag:'buttons',role:'choice',selected:null,selection_code:null,
    required:!!read.required||!!(c.parentElement&&[...c.parentElement.querySelectorAll('label')].some(l=>/required/i.test(l.className)||/\*\s*$/.test(l.innerText))),disabled:false,readonly:false,checked:false,
    value:buttons.find(b=>b.getAttribute('aria-pressed')==='true')?.innerText.trim()||'',options:buttons.map(b=>({label:b.innerText.trim(),value:b.getAttribute('data-option')||b.innerText.trim()})),
    ...(read.missing?{label_missing:true}:{})};});
 fields.push(...choices);
 const auth=[...document.querySelectorAll('button,input[type=submit],a[href],[role="button"]')].filter(visible).filter(e=>
   /^(create (an )?account|create (my )?profile|sign ?up|register|sign ?in|log ?in)$/i.test((e.innerText||e.value||'').trim())).map((e,i)=>{
   e.setAttribute('data-rove-auth',String(i));const t=(e.innerText||e.value||'').trim();return {ref:String(i),label:t,intent:/sign ?in|log ?in/i.test(t)?'login':'register'};});
 const nav=[...document.querySelectorAll('button,input[type=submit],[role="button"]')].filter(visible).filter(e=>
   /^(next|continue|save (and|&) continue|next step)$/i.test((e.innerText||e.value||'').trim())).map((e,i)=>{
   e.setAttribute('data-rove-nav',String(i));return {ref:String(i),label:(e.innerText||e.value||'').trim()};});
 const links=[...document.querySelectorAll('a[href],button,[role="button"]')].filter(visible).filter(e=>
   /^(apply( now| for this (job|position))?|apply on (the )?(employer|company) (site|website)|apply for this job|start application|continue application)$/i.test(e.innerText.trim())).map((e,i)=>{
   e.setAttribute('data-rove-link',String(i));return {ref:String(i),label:e.innerText.trim(),url:e.href||null,kind:e.tagName.toLowerCase()};
 });
 return {title:document.title,text:document.body.innerText.slice(0,15000),fields,application_links:links,auth_controls:auth,nav_controls:nav,
 final_controls:[...document.querySelectorAll('button,input[type=submit]')].filter(visible).filter(e=>/^(submit|submit application|submit my application|submit your application|submit now|send application|complete application|finish application)$/i.test((e.innerText||e.value).trim())).map((e,i)=>{e.setAttribute('data-rove-submit',String(i));return {ref:String(i),label:(e.innerText||e.value).trim()};}),
 ats_markers:{captcha_challenge:[...document.querySelectorAll('iframe[src*="recaptcha/api2/bframe"],iframe[src*="hcaptcha.com"],iframe[src*="challenges.cloudflare.com"],iframe[src*="turnstile"],.g-recaptcha,.h-captcha,.cf-turnstile')].some(e=>{const r=e.getBoundingClientRect();return visible(e)&&r.width>=200&&r.height>=60;}),
 already_applied:/\b(you have |you've )?already (applied|submitted an application)\b|application already exists/i.test(document.body.innerText),
 greenhouse_confirmation:!!document.querySelector('div.confirmation div.confirmation__content'),
 lever_submit_success:!!document.querySelector('h3[data-qa="msg-submit-success"]'),
 lever_verification_error:/there was an error verifying your application/i.test(document.body.innerText),
 __BOARDS__
  status_region:messages('__STATUS__'),form_error:messages('__ERROR__')}};
}""".replace("__MESSAGES__", MESSAGES_JS)
    .replace("__READING__", form_reading.READING_JS)
    .replace("__STATUS__", STATUS_SELECTOR)
    .replace("__ERROR__", ERROR_SELECTOR)
    .replace("__BOARDS__", boards.MARKERS_JS)
)

# Ordinary form submission is blocked in the recruiting browser unless trusted
# submission code arms this flag for one observed click. It stops accidental
# native/React submits during preparation; it is not a network-level guarantee
# against page scripts that post on their own.
# The arm flag lives on the DOM, which every JavaScript world shares; Patchright
# evaluates scripts in an isolated world, so a window variable would never be seen.
PREPARE_GUARD = (
    "document.addEventListener('submit',e=>{"
    "if(document.documentElement.getAttribute('data-rove-submit-armed')!=='1'){"
    "e.preventDefault();e.stopImmediatePropagation();}},true)"
)

BLOCK_MARKERS = re.compile(
    r"access denied|pardon our interruption|request unsuccessful|verify (?:that )?you are (?:a )?human"
    r"|just a moment\.\.\.|attention required|are you a robot|checking your browser"
    r"|blocked by (?:the )?(?:site|website) administrator|reference #\s?[0-9a-f.]{6,}",
    re.IGNORECASE,
)


CLOSED_MARKERS = re.compile(
    r"no longer accepting applications|this job is no longer available|position has been filled"
    r"|this job has been closed|job posting has expired|this posting has expired"
    r"|is no longer open|applications are closed",
    re.IGNORECASE,
)


def pacing_enabled() -> bool:
    return bool(workflow.config().get("human_pacing", True))


def pace(low: float = 0.35, high: float = 1.2):
    """Randomized, human-scale pauses between actions; off in tests."""
    if pacing_enabled():
        time.sleep(random.uniform(low, high))


def front_app() -> tuple[str, str]:
    """(process serial, bundle id) of the frontmost macOS app, without any permission."""
    try:
        asn = subprocess.check_output(["lsappinfo", "front"], timeout=5).decode().strip()
        info = subprocess.check_output(["lsappinfo", "info", "-only", "bundleid", asn], timeout=5)
        match = re.search(r'bundleID="([^"]+)"', info.decode())
        return asn, (match.group(1) if match else "")
    except (OSError, subprocess.SubprocessError):
        return "", ""


def restore_front(previous: tuple[str, str], watch_seconds: float = 6.0) -> dict:
    """Give focus back to whatever the owner was using; a launching app activates itself.

    Chrome activates a moment after its DevTools port answers, so watch for a few
    seconds and hand focus back each time it is taken. Nothing here needs a permission.
    """
    asn, bundle = previous
    outcome = {"previous": bundle, "restored": 0}
    if not asn or not bundle:
        return outcome
    deadline = time.monotonic() + watch_seconds
    try:
        while time.monotonic() < deadline:
            front = subprocess.check_output(["lsappinfo", "front"], timeout=5).decode().strip()
            if front != asn:
                subprocess.run(["open", "-b", bundle], check=False, capture_output=True, timeout=10)
                outcome["restored"] += 1
            time.sleep(0.5)
    except (OSError, subprocess.SubprocessError):
        pass
    return outcome


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def devtools_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as reply:
            return reply.status == 200
    except (OSError, ValueError):
        return False


class ChromeLauncher:
    """Start the recruiting browser as its own app instance and reach it over local CDP.

    Launching it ourselves means no automation flag is ever set (navigator.webdriver stays
    false), the window survives daemon restarts, and new tabs open in the background.
    The DevTools port binds to localhost only; the local account is trusted by design.
    """

    def __init__(self):
        self.session = state_root() / "browser/session.json"

    def app(self, playwright) -> tuple[str, str]:
        preference = workflow.config().get("browser_app", "chrome")
        chrome = Path("/Applications/Google Chrome.app")
        if preference == "chrome" and chrome.is_dir():
            return str(chrome), "com.google.Chrome"
        executable = Path(playwright.chromium.executable_path)
        bundle = next((p for p in executable.parents if p.suffix == ".app"), None)
        if bundle is None:
            raise RuntimeError("No launchable Chrome application bundle was found")
        return str(bundle), "com.google.chrome.for.testing"

    def running_port(self) -> int | None:
        if not self.session.is_file():
            return None
        try:
            port = int(json.loads(self.session.read_text())["port"])
        except (ValueError, KeyError, TypeError):
            return None
        return port if devtools_alive(port) else None

    def ensure_running(self, profile: Path, playwright) -> int:
        port = self.running_port()
        if port:
            return port
        app, bundle = self.app(playwright)
        port = free_port()
        previous = front_app()
        subprocess.run(
            [
                "open",
                "-n",
                "-a",
                app,
                "--args",
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={port}",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-startup-window",
                "--disable-background-networking",
                "--window-size=1280,900",
                "--lang=en-US",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        for _ in range(60):
            if devtools_alive(port):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("The recruiting browser did not expose its local DevTools port")
        focus = restore_front(previous)
        write_private(
            self.session,
            {
                "port": port,
                "app": app,
                "bundle": bundle,
                "started_at": workflow.now(),
                "focus": focus,
            },
        )
        return port


FIELDS_JS = "() => !!document.querySelector('input:not([type=hidden]),select,textarea')"
# The visible suggestion that names the typed place, in a dropdown without ARIA roles.
SUGGESTION_JS = """(city) => {
  const norm = s => (s || '').toLowerCase();
  const wanted = norm(city);
  const candidates = [...document.querySelectorAll(
    '[role=option],[role=listbox] *,li,[class*="option" i],[class*="suggestion" i],[class*="result" i],[class*="menu" i] *')]
    .filter(e => e.getClientRects().length && !['INPUT','TEXTAREA'].includes(e.tagName))
    .filter(e => { const t = (e.innerText || '').trim(); return t.length > 0 && t.length < 160 && norm(t).includes(wanted); });
  if (!candidates.length) return false;
  candidates.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
  candidates[0].setAttribute('data-rove-suggestion', '1');
  return true;
}"""
RENDERED_JS = FIELDS_JS + " || document.body.innerText.trim().length > 200"
# A visible loading indicator means the shell painted before the form: keep waiting.
BUSY_JS = """() => {
  if (document.querySelector('input:not([type=hidden]),select,textarea')) return true;
  const busy = document.querySelectorAll(
    '[role=progressbar],[aria-busy=true],[class*="spinner" i],[class*="loading" i],[class*="loader" i]');
  return ![...busy].some(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
}"""
# The declining control of a cookie banner, recognised by the banner's own wording. Accept is never chosen.
CONSENT_JS = """() => {
  const label = el => (el.innerText || el.textContent || '').trim();
  const decline = /^(reject( all)?( cookies)?|decline( all)?( cookies)?|refuse( all)?|deny( all)?|necessary( cookies)? only|only (necessary|essential|required)( cookies)?|essential( cookies)? only|reject (non-essential|optional)( cookies)?)$/i;
  for (const button of document.querySelectorAll('button,[role=button],a')) {
    if (!decline.test(label(button))) continue;
    const box = button.getBoundingClientRect();
    if (!box.width || !box.height) continue;
    let holder = button.parentElement;
    for (let depth = 0; holder && depth < 8; depth++, holder = holder.parentElement) {
      const text = label(holder);
      if (/cookie|consent/i.test(text) && text.length < 2000) {
        button.setAttribute('data-rove-consent', '1');
        return label(button);
      }
    }
  }
  return null;
}"""


class RecruitingBrowser:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.playwright = None
        self.context = None
        self.page = None
        self.run = None
        self.observation = None
        self.dns = {}
        self.runs = {}
        self.pages = {}
        self.browser = None
        self.cdp = None
        self.warmed = set()
        self.launcher = ChromeLauncher()

    def _route(self, route):
        url = route.request.url
        try:
            host = urlsplit(url).hostname
            if host not in self.dns or time.monotonic() - self.dns[host] > 60:
                validate_destination(url)
                self.dns[host] = time.monotonic()
            elif not public_link(url):
                raise PermissionError("Unsupported URL")
            route.continue_()
        except (ValueError, PermissionError, OSError):
            route.abort()

    def connected(self) -> bool:
        if self.browser is not None:
            return self.browser.is_connected()
        return bool(self.context and self.context.browser and self.context.browser.is_connected())

    def ensure(self):
        if self.context and self.connected():
            return
        if self.playwright:
            self.playwright.stop()
        self.playwright = sync_playwright().start()
        directory = state_root() / "browser/recruiting-profile"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.browser = self.cdp = None
        if self.headless:
            self.context = self.playwright.chromium.launch_persistent_context(
                str(directory),
                headless=True,
                viewport=None,
                args=["--disable-background-networking"],
                accept_downloads=False,
            )
        else:
            port = self.launcher.ensure_running(directory, self.playwright)
            self.browser = self.playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=20000
            )
            self.context = (
                self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
            )
            self.cdp = self.browser.new_browser_cdp_session()
        self.context.set_default_timeout(12000)
        self.context.add_init_script(PREPARE_GUARD)
        if self.cdp is not None:
            self.adopt_pages()

    def adopt_pages(self):
        """After a reconnect, map the tabs still open in Chrome back to their applications.

        A tab whose URL belongs to an application waiting on the owner is kept and
        re-registered; anything else left over (blank tabs, finished runs) is closed.
        """
        with workflow.db() as conn:
            waiting = [
                dict(r)
                for r in conn.execute(
                    "SELECT id,url FROM application_queue WHERE run_id IS NOT NULL AND status IN "
                    "('NEEDS_USER','READY_FOR_REVIEW','MANUAL_TAKEOVER','PREPARING')"
                )
            ]
        for page in list(self.context.pages):
            if page.is_closed():
                continue
            owner = next(
                (
                    item
                    for item in waiting
                    if job_scope(page.url) == job_scope(item["url"])
                    or page.url.startswith(item["url"].rstrip("/"))
                ),
                None,
            )
            run_file = state_root() / f"applications/{owner['id']}/run.json" if owner else None
            if owner and run_file and run_file.is_file() and owner["id"] not in self.pages:
                self.pages[owner["id"]] = page
                self.runs[owner["id"]] = json.loads(run_file.read_text())
            else:
                with contextlib.suppress(PlaywrightError):
                    page.close()

    def new_page(self):
        """A background tab (or a background window when none exists): never steals focus."""
        if self.cdp is None:
            return self.context.new_page()
        first = not any(not page.is_closed() for page in self.context.pages)
        with self.context.expect_page(timeout=15000) as created:
            self.cdp.send(
                "Target.createTarget",
                {"url": "about:blank", "newWindow": first, "background": True},
            )
        return created.value

    @contextlib.contextmanager
    def guarded(self, page):
        """Destination policy only while this daemon drives the page.

        A route handler runs only while the daemon is inside a browser call, so a
        context-wide route would stall every tab the owner browses by hand whenever the
        daemon sits idle. The guard is attached per operation and removed afterwards.
        """
        page.route("**/*", self._route)
        try:
            yield page
        finally:
            with contextlib.suppress(PlaywrightError):
                page.unroute("**/*")

    def close_run(self, run_id: str) -> dict:
        page = self.pages.pop(run_id, None)
        self.runs.pop(run_id, None)
        if page is not None and not page.is_closed():
            page.close()
        if self.run and self.run["id"] == run_id:
            self.page, self.run, self.observation = None, None, None
        return {"closed": run_id}

    def enforce_tab_limit(self, keep: str):
        """Keep the recruiting window uncluttered; a closed tab reopens on resume."""
        limit = int(workflow.config().get("max_open_tabs", 5))
        open_runs = [r for r, page in self.pages.items() if not page.is_closed() and r != keep]
        while len(open_runs) + 1 > limit and open_runs:
            self.close_run(open_runs.pop(0))

    def click(self, locator, timeout: int = 12000):
        """Move to the element first, like a person would, then click."""
        if pacing_enabled():
            try:
                locator.hover(timeout=3000)
            except PlaywrightError:
                pass
            pace(0.2, 0.7)
        locator.click(timeout=timeout)

    def type_value(self, locator, value: str):
        if pacing_enabled() and len(value) <= 80:
            locator.click()
            locator.fill("")
            locator.press_sequentially(value, delay=random.uniform(25, 65))
        else:
            locator.fill(value)

    def warm(self, url: str, force: bool = False):
        """Visit the site's front door first; a cold deep link is what bot managers flag."""
        host = urlsplit(url).hostname or ""
        if not pacing_enabled() or (host in self.warmed and not force):
            return
        self.warmed.add(host)
        try:
            self.page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=30000)
            self.settle(5000)
            self.page.mouse.move(random.randint(200, 900), random.randint(200, 600), steps=12)
            pace(1.0, 2.2)
        except PlaywrightError:
            pass

    def reopen(self, run_id: str) -> dict:
        """One patient retry after a block: pause, enter through the front door, observe."""
        self.check(run_id)
        pace(12, 30)
        with self.guarded(self.page):
            self.warm(self.run["target_url"], force=True)
            try:
                self.page.goto(self.run["target_url"], wait_until="domcontentloaded", timeout=45000)
                self.settle()
            except PlaywrightError as error:
                self.run["navigation_error"] = type(error).__name__
            result = self.observe()
        workflow.record(
            run_id,
            "browser_retry",
            {"url": result["url"], "blocked": result.get("blocked", False)},
        )
        workflow.flush_events(run_id)
        return result

    def save(self):
        write_private(state_root() / f"applications/{self.run['id']}/run.json", self.run)

    def settle(self, timeout: int = 10000):
        """Bounded wait for a rendered page.

        Single-page job boards paint a shell, a cookie banner and a loading indicator
        before the form; the banner alone reads as "rendered" text, so the wait also
        declines the banner and waits out a visible indicator.
        """
        try:
            self.page.wait_for_function(RENDERED_JS, timeout=timeout)
        except PlaywrightError:
            pass
        self.dismiss_consent()
        try:
            self.page.wait_for_function(BUSY_JS, timeout=timeout)
        except PlaywrightError:
            pass

    def picker_diagnostic(self, field: dict, typed: str, seen: list, locator):
        """Private evidence when a picker refuses our value: what it listed and kept."""
        with contextlib.suppress(Exception):
            directory = state_root() / f"applications/{self.run['id']}"
            write_private(
                directory / f"picker-{field['key']}.json",
                {
                    "label": field["label"],
                    "typed": typed,
                    "options_seen": [str(t)[:120] for t in seen[:25]],
                    "committed": locator.input_value()[:200],
                    "at": workflow.now(),
                },
            )
            self.page.screenshot(path=str(directory / f"picker-{field['key']}.png"))

    def retype_phone(self, observation: dict) -> bool:
        """The site rejected the phone: try the other format once."""
        for field in observation["fields"]:
            if (
                field["kind"] in ("text", "tel")
                and re.search(r"phone|mobile", field["label"] or "", re.IGNORECASE)
                and field.get("value")
            ):
                current = re.sub(r"\D", "", field["value"])
                alternative = next(
                    (v for v in phone_variants(field["value"]) if re.sub(r"\D", "", v) != current),
                    None,
                )
                if not alternative:
                    return False
                locator = self.page.locator(f'[data-rove-field="{int(field["ref"])}"]')
                self.type_value(locator, alternative)
                return True
        return False

    def wait_for_fields(self, timeout: int = 8000):
        try:
            self.page.wait_for_function(FIELDS_JS, timeout=timeout)
        except PlaywrightError:
            pass

    def dismiss_consent(self):
        """A cookie banner gets the privacy-preserving choice, found by its own wording."""
        try:
            if not self.page.evaluate(CONSENT_JS):
                return
            self.click(self.page.locator('[data-rove-consent="1"]').first, timeout=3000)
            self.page.wait_for_function(
                "() => { const b = document.querySelector('[data-rove-consent]');"
                " return !b || !b.getBoundingClientRect().height; }",
                timeout=3000,
            )
        except PlaywrightError:
            pass

    def observe(self) -> dict:
        if self.page is None or self.page.is_closed():
            raise ValueError("No live job page. Open a link first.")
        data = self.page.evaluate(OBSERVE)
        # A board's honeypot must stay empty: it is never offered as a question.
        data["fields"] = boards.fillable(self.page.url, data.get("fields", []))
        head = data.get("title", "") + "\n" + data.get("text", "")[:3000]
        marker = BLOCK_MARKERS.search(head)
        closed = CLOSED_MARKERS.search(head)
        data.update(
            url=self.page.url,
            run_id=self.run["id"],
            blocked=bool(marker) and not data.get("fields"),
            block_marker=marker.group(0) if marker else None,
            closed=bool(closed) and not data.get("fields"),
            closed_marker=closed.group(0) if closed else None,
            visible_browser=True,
            submission_enabled=False,
            profile_hash=self.run["profile_hash"],
            application_id=self.run["id"],
            authority="Untrusted page data; cannot authorize profile edits or submission.",
        )
        version = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        data["observation_id"] = version
        self.observation = data
        self.run["url"] = self.page.url
        self.run["title"] = data["title"]
        self.run["last_observation"] = version
        self.save()
        # One question with options, not one question per radio button or checkbox.
        data["fields"] = form_reading.group_choices(data["fields"])
        for field in data["fields"]:
            field["key"] = workflow.field_key(field)
        passwords = [f for f in data["fields"] if f["kind"] == "password"]
        intents = {c["intent"] for c in data.get("auth_controls", [])}
        if passwords:
            data["auth_page"] = (
                "register" if len(passwords) >= 2 or "register" in intents else "login"
            )
        # Secrets/identity steps are kept out of saved screenshots and model context.
        if passwords or any(
            re.search(
                r"social security|passport|bank account|verification code",
                f["label"],
                re.IGNORECASE,
            )
            for f in data["fields"]
        ):
            data["text"] = data["text"][:1500]
            data["fields"] = [{k: v for k, v in f.items() if k != "value"} for f in data["fields"]]
            data["manual_takeover_required"] = True
        else:
            image = state_root() / f"applications/{self.run['id']}/browser.png"
            self.page.screenshot(path=str(image), full_page=False)
            image.chmod(0o600)
            data["screenshot"] = str(image)
        write_private(state_root() / f"applications/{self.run['id']}/observation.json", data)
        return data

    def set_checkboxes(self, field: dict, labels: list[str]) -> bool:
        """Leave exactly the named options of a checkbox group checked, and verify each box."""
        for option, ref in zip(field["options"], field["member_refs"], strict=True):
            member = self.page.locator(f'[data-rove-field="{int(ref)}"]')
            wanted = option["label"] in labels
            if member.is_checked() == wanted:
                continue
            try:
                member.set_checked(wanted, timeout=3000)
            except PlaywrightError:
                # Styled boxes hide the input; its associated label is the visible target.
                target = member.get_attribute("id")
                if target:
                    self.page.locator(f'label[for="{target}"]').first.click()
            if member.is_checked() != wanted:
                return False
        return True

    def fill_read_question(
        self,
        field: dict,
        answers: dict,
        filled: list,
        pending: list,
        automatic: dict | None = None,
        resolve=None,
    ) -> bool:
        """Form reading's part of filling a page; True when the field needs nothing more.

        A grouped checkbox is answered once, as its group. A checkbox group takes this
        application's own answer, else what `resolve(field)` finds: the approved profile,
        an answer the owner gave before, the form's own decline option for voluntary
        self-identification, or a standing default. A question whose text could not be
        read is never answered from a guess, a default or memory: it goes to the owner as
        unreadable. `automatic` is what Rove recorded itself (a used draft, a blank).
        """
        if field["kind"] in {"checkbox", "radio"} and field.get("in_group"):
            return True
        answer = questions.application_answer(field, answers, automatic or {})
        if answer and answer["value"].lower() == "skip":
            if field["kind"] == "checkbox_group" and not field["required"]:
                return True
            # A blank recorded while the question looked optional is not an answer now.
            answer = None if field["required"] else answer
        if field.get("label_missing") and answer and "auto-draft:" in answer["source"]:
            answer = None  # only the owner's own words answer a question nobody could read
        if field.get("label_missing") and not answer and field["kind"] not in {"file", "password"}:
            pending.append(form_reading.unreadable_question(field))
            return True
        if field["kind"] != "checkbox_group":
            return False
        options = [o["label"] for o in field["options"]]
        if answer:
            value, source = answer["value"], answer["source"]
        else:
            value, source = resolve(field) if resolve else (None, None)
        # Every part of the answer must name a box; a lone box is ticked by a plain yes.
        chosen = questions.match_many(options, value) if value is not None else None
        if chosen is not None and self.set_checkboxes(field, chosen):
            filled.append(
                {
                    "label": field["label"],
                    "value": ", ".join(chosen),
                    "source": source,
                    "key": field["key"],
                    "control": "checkbox_group",
                }
            )
            return True
        pending.append(
            {
                "label": field["label"],
                "key": field["key"],
                "required": field["required"],
                "options": options,
                "reason": "Needs reviewed answer or supported control adapter"
                if chosen is None
                else "Selection could not be verified",
            }
        )
        return True

    def open(self, url: str) -> dict:
        target = validate_destination(url.strip().strip("<>\"'"))
        approved = read_approved()
        self.ensure()
        item = workflow.enqueue(target, source="agent")
        run_id = item["application_id"]
        target = item["url"]
        existing = workflow.get(run_id)
        if existing["status"] in {"APPLIED", "SUBMITTING", "UNKNOWN_SUBMISSION"}:
            raise PermissionError("Existing submission or uncertain attempt blocks reopening")
        if run_id in self.pages and not self.pages[run_id].is_closed():
            # A reopened application starts from a fresh load of its page: no half-filled
            # form, no toggled choices, no attached file the site hid its input for.
            self.page, self.run = self.pages[run_id], self.runs[run_id]
            with self.guarded(self.page):
                try:
                    self.page.goto(target, wait_until="domcontentloaded", timeout=45000)
                    self.page.locator("body").wait_for()
                    self.settle()
                except PlaywrightError as error:
                    self.run["navigation_error"] = type(error).__name__
                return self.observe()
        self.run = {
            "id": run_id,
            "source_url": existing["source_url"],
            "target_url": target,
            "profile_hash": approved["profile_hash"],
            "status": "BROWSING",
            "filled": [],
            "pending": [],
            "submitted": False,
        }
        directory = state_root() / f"applications/{run_id}"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        prior_profile = directory / "profile.json"
        if prior_profile.exists():
            frozen = json.loads(prior_profile.read_text())
            if frozen["profile_hash"] != approved["profile_hash"]:
                raise PermissionError(
                    "Existing application profile changed; explicit rebuild required"
                )
        else:
            write_private(prior_profile, approved)
        self.enforce_tab_limit(keep=run_id)
        self.page = self.new_page()
        manifest_path = directory / "resume-manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            self.run.update(
                resume_sha256=manifest.get("resume_sha256"),
                resume_is_tailored=manifest.get("tailored", False),
            )
        self.runs[run_id], self.pages[run_id] = self.run, self.page
        workflow.set_state(run_id, "PREPARING", run_id=run_id)
        workflow.ensure_forum(run_id)
        self.save()
        with self.guarded(self.page):
            self.warm(target)
            try:
                self.page.goto(target, wait_until="domcontentloaded", timeout=45000)
                self.page.locator("body").wait_for()
                self.settle()
            except PlaywrightError as error:
                self.run["navigation_error"] = type(error).__name__
            result = self.observe()
        result["feed_lookup"] = lookup_job_link(target)
        if not existing["title"]:
            workflow.set_state(run_id, "PREPARING", title=result["title"][:300])
        workflow.record(run_id, "opened", {"url": self.page.url, "title": result["title"]})
        workflow.flush_events(run_id)
        return result

    def follow(self, run_id: str, observation_id: str, ref: str) -> dict:
        self.check(run_id)
        if not self.observation or observation_id != self.observation["observation_id"]:
            raise ValueError("Page observation changed; inspect again before following a link")
        if self.page.url != self.observation["url"]:
            raise ValueError("Page URL changed; inspect again")
        item = next((x for x in self.observation["application_links"] if x["ref"] == ref), None)
        if not item:
            raise PermissionError("Only an observed application-start link may be followed")
        locator = self.page.locator(f'[data-rove-link="{int(ref)}"]')
        if normalized(locator.inner_text()) != normalized(item["label"]):
            raise ValueError("Application link changed")
        if item["url"]:
            validate_destination(item["url"])
            if urlsplit(item["url"]).hostname != urlsplit(
                self.page.url
            ).hostname and not approved_ats(item["url"]):
                raise PermissionError("Unexpected application destination; owner review needed")
        old_pages = list(self.context.pages)
        with self.guarded(self.page):
            self.click(locator)
            try:
                self.page.wait_for_function(
                    "old => location.href !== old || !!document.querySelector('input:not([type=hidden]),select,textarea,[role=dialog]')",
                    arg=self.observation["url"],
                    timeout=2500,
                )
            except PlaywrightError:
                pass
        fresh = [p for p in self.context.pages if p not in old_pages]
        if fresh:
            self.page = fresh[-1]
            self.pages[run_id] = self.page
        with self.guarded(self.page):
            self.page.wait_for_load_state("domcontentloaded", timeout=30000)
            self.settle()
            result = self.observe()
            if not (
                result["fields"]
                or result["application_links"]
                or result["auth_controls"]
                or result.get("blocked")
            ):
                # The application page painted its shell first: one bounded chance for the form.
                self.wait_for_fields()
                result = self.observe()
        workflow.record(
            run_id, "application_link", {"clicked": item["label"], "url": result["url"]}
        )
        workflow.flush_events(run_id)
        return result

    def check(self, run_id: str):
        page = self.pages.get(run_id)
        if page is None or page.is_closed() or run_id not in self.runs:
            # Never act on another application's tab: a missing tab is reopened, not reused.
            raise ValueError("This application's tab is not open; reopen it before continuing")
        self.page, self.run = page, self.runs[run_id]

    def select_combobox(self, locator, field, value, profile) -> bool:
        """Select a unique visible exact option; text input alone is not selection."""
        locator.click()
        place = normalized(field["label"]) in {
            "location city",
            "location",
            "current location",
            "city",
        }
        identity = profile.get("identity", {})
        city = str(identity.get("city") or "") if place else ""
        if place:
            # Pickers list "City, ST, Country": typing the city alone surfaces it.
            locator.fill(city or value)
        else:
            locator.press("ArrowDown")
        options = self.page.get_by_role("option")
        try:
            options.first.wait_for(state="visible", timeout=4000)
        except PlaywrightError:
            self.picker_diagnostic(field, city or value, [], locator)
            if place:
                # A custom suggestion list without ARIA roles: click the suggestion that
                # names our city, else take the first one; accept only when the committed
                # text still names the city.
                city = str(profile["identity"].get("city") or "")
                try:
                    found = False
                    for _ in range(12):
                        # Place pickers geocode after a pause: poll for the suggestion.
                        self.page.wait_for_timeout(500)
                        if city and self.page.evaluate(SUGGESTION_JS, city):
                            found = True
                            break
                    if found:
                        self.click(
                            self.page.locator('[data-rove-suggestion="1"]').first,
                            timeout=4000,
                        )
                    else:
                        locator.press("ArrowDown")
                        locator.press("Enter")
                except PlaywrightError:
                    pass
                committed = normalized(locator.input_value())
                if city and normalized(city) in committed:
                    return True
                if not committed:
                    # No suggestion list at all: a plain input keeps what we type.
                    locator.fill(value)
                    locator.press("Tab")
                    if city and normalized(city) in normalized(locator.input_value()):
                        return True
                with contextlib.suppress(Exception):
                    # Private evidence for the next fix: what the picker showed.
                    self.page.screenshot(
                        path=str(
                            state_root() / f"applications/{self.run['id']}/typeahead-failed.png"
                        )
                    )
            locator.press("Escape")
            return False
        wanted = {normalized(value)}
        if normalized(field["label"]) == "country":
            wanted |= {normalized(value + " +1"), normalized(value + " (+1)")}
        if place:
            identity = profile["identity"]
            parts = [identity[k] for k in ("city", "state_region", "country") if identity.get(k)]
            wanted |= {normalized(", ".join(parts)), normalized(", ".join(parts[:2]))}
        texts = options.all_text_contents()
        matches = [i for i, text in enumerate(texts) if normalized(text) in wanted]
        if not matches and place and city:
            # A place picker geocodes after a pause and may list "Loading" first: poll until
            # an option starts with our city, preferring one that also names our state.
            state = normalized(str(identity.get("state_region") or ""))
            for _ in range(12):
                texts = options.all_text_contents()
                starts = [
                    i
                    for i, text in enumerate(texts)
                    if normalized(text).startswith(normalized(city))
                ]
                with_state = [i for i in starts if state and state in normalized(texts[i])]
                matches = (with_state or starts)[:1]
                if matches:
                    break
                self.page.wait_for_timeout(500)
        if not matches:
            locator.fill(value)
            try:
                options.filter(has_text=value).first.wait_for(timeout=5000)
            except PlaywrightError:
                self.picker_diagnostic(field, value, texts, locator)
                locator.press("Escape")
                return False
            texts = options.all_text_contents()
            matches = [i for i, text in enumerate(texts) if normalized(text) in wanted]
        if len(matches) != 1:
            self.picker_diagnostic(field, city or value, texts, locator)
            locator.press("Escape")
            return False
        expected = texts[matches[0]]
        options.nth(matches[0]).click()
        if place and city:
            # A place picker commits the suggestion into the input: that text is the proof.
            self.page.wait_for_timeout(300)
            committed = normalized(locator.input_value())
            if normalized(city) in committed:
                locator.press("Escape")
                return True
            self.picker_diagnostic(field, city, texts, locator)
            return False
        # Refocusing emits React Select's current selected country (its visual
        # single-value label contains only a dial code shared by many countries).
        locator.press("Tab")
        locator.click()
        if normalized(field["label"]) == "country":
            try:
                self.page.wait_for_function(
                    "({ref,value})=>{const e=document.querySelector('[data-rove-field=\"'+ref+'\"]');return e?.closest('.select__container')?.querySelector('[aria-live]')?.textContent.includes('option '+value+', selected.')}",
                    arg={"ref": field["ref"], "value": value},
                    timeout=3000,
                )
            except PlaywrightError:
                pass
            announcement = locator.evaluate(
                "e=>e.closest('.select__container')?.querySelector('[aria-live]')?.textContent||''"
            )
            if normalized("option " + value + " selected") in normalized(announcement):
                locator.press("Escape")
                return True
        # The selected native ARIA option proves commitment; typing into a search
        # input or seeing a shared country dial code does not.
        locator.click()
        locator.press("ArrowDown")
        selected = self.page.get_by_role("option", name=expected, exact=True)
        try:
            selected.wait_for(state="visible", timeout=3000)
            verified = (
                selected.get_attribute("aria-selected") == "true"
                or "select__option--is-selected" in (selected.get_attribute("class") or "").split()
            )
        except PlaywrightError:
            verified = False
        # React Select exposes some committed values only in its selected-value
        # label or accessible live announcement, not aria-selected on options.
        evidence = locator.evaluate(
            "e=>{const c=e.closest('.select__container'); return {value:c?.querySelector('.select__single-value')?.innerText||'', announcement:c?.querySelector('[aria-live]')?.textContent||''}}"
        )
        verified = verified or normalized(evidence["value"]) == normalized(expected)
        if normalized(field["label"]) == "country":
            verified = verified or normalized("option " + value + " selected") in normalized(
                evidence["announcement"]
            )
        write_private(
            state_root() / f"applications/{self.run['id']}/dropdown-{field['key']}.json",
            {"expected": expected, "selected_evidence": evidence, "verified": verified},
        )
        locator.press("Escape")
        return verified

    def select_choice(self, field: dict, label: str) -> bool:
        """Select one observed option of a radio group or button group and verify it."""
        if field["kind"] == "radio_group":
            index = next(i for i, o in enumerate(field["options"]) if o["label"] == label)
            member = self.page.locator(f'[data-rove-field="{int(field["member_refs"][index])}"]')
            try:
                member.check(timeout=3000)
            except PlaywrightError:
                # Styled radios hide the input; its associated label is the visible target.
                target = member.get_attribute("id")
                if target:
                    self.page.locator(f'label[for="{target}"]').first.click()
            return member.is_checked()
        container = self.page.locator(f'[data-rove-choice="{int(field["ref"])}"]')
        button = container.get_by_role("button", name=label, exact=True)
        if button.count() != 1:
            return False
        self.click(button)
        try:
            self.page.wait_for_function(
                "({ref,label})=>[...document.querySelector('[data-rove-choice=\"'+ref+'\"]').querySelectorAll('button[aria-pressed]')].some(b=>b.innerText.trim()===label&&b.getAttribute('aria-pressed')==='true')",
                arg={"ref": field["ref"], "label": label},
                timeout=3000,
            )
        except PlaywrightError:
            return False
        return True

    def _auth_control(self, observation: dict, intent: str):
        control = next(
            (c for c in observation.get("auth_controls", []) if c["intent"] == intent), None
        )
        if control is None:
            raise ValueError(f"No {intent} control is visible on this page")
        return self.page.locator(f'[data-rove-auth="{int(control["ref"])}"]'), control

    def register(self, run_id: str) -> dict:
        """Create the employer account the owner approved: email, generated password, nothing else."""
        from . import credentials

        self.check(run_id)
        with self.guarded(self.page):
            before = self.observe()
            if before.get("auth_page") != "register":
                raise ValueError("This page is not an account-creation form")
            if (
                not approved_ats(before["url"])
                and job_scope(before["url"])[0] != urlsplit(self.run["target_url"]).hostname
            ):
                raise PermissionError("Account creation is limited to the verified employer site")
            profile = json.loads(
                (state_root() / f"applications/{run_id}/profile.json").read_text()
            )["profile"]
            email = profile["identity"]["email"]
            host = credentials.account_host(before["url"])
            existing = credentials.lookup(host)
            password = existing["password"] if existing else credentials.generate_password()
            filled = []
            for field in before["fields"]:
                if (
                    field["disabled"]
                    or field.get("readonly")
                    or field["kind"] in {"file", "hidden", *GROUP_KINDS}
                ):
                    continue
                locator = self.page.locator(f'[data-rove-field="{int(field["ref"])}"]')
                label = normalized(field["label"] + " " + field["name"])
                if field["kind"] == "password":
                    locator.fill(password)
                    filled.append("password" if "confirm" not in label else "password confirmation")
                elif field["kind"] == "email" or "email" in label:
                    locator.fill(email)
                    filled.append(field["label"] or "email")
                elif field["kind"] == "checkbox" and re.search(
                    r"terms|privacy|agree|consent", label
                ):
                    locator.check()
                    filled.append("accepted: " + (field["label"] or "terms")[:80])
                elif field["kind"] in {"text", "tel"} and not field.get("label_missing"):
                    value, _source = resolve_known(field["label"], profile)
                    if value:
                        self.type_value(locator, value)
                        filled.append(field["label"])
                pace(0.2, 0.6)
            locator, control = self._auth_control(before, "register")
            self.click(locator)
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=30000)
            except PlaywrightError:
                pass
            self.settle()
            after = self.observe()
            credentials.store(host, email, password, run_id)
            workflow.record(
                run_id,
                "account_created",
                {
                    "host": host,
                    "username": email,
                    "filled": filled,
                    "clicked": control["label"],
                    "storage": "encrypted local credential store",
                },
            )
            workflow.flush_events(run_id)
            return after

    def login(self, run_id: str) -> dict:
        """Sign in with a stored account for this host; never with anything typed by the model."""
        from . import credentials

        self.check(run_id)
        with self.guarded(self.page):
            before = self.observe()
            if before.get("auth_page") != "login":
                raise ValueError("This page is not a sign-in form")
            host = credentials.account_host(before["url"])
            account = credentials.lookup(host)
            if not account:
                raise PermissionError("No stored account for this site")
            for field in before["fields"]:
                if field["disabled"] or field["kind"] in {"file", "hidden", *GROUP_KINDS}:
                    continue
                locator = self.page.locator(f'[data-rove-field="{int(field["ref"])}"]')
                label = normalized(field["label"] + " " + field["name"])
                if field["kind"] == "password":
                    locator.fill(account["password"])
                elif field["kind"] == "email" or re.search(r"email|user ?name", label):
                    locator.fill(account["username"])
            locator, control = self._auth_control(before, "login")
            self.click(locator)
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=30000)
            except PlaywrightError:
                pass
            self.settle()
            after = self.observe()
            workflow.record(
                run_id,
                "signed_in" if after.get("auth_page") != "login" else "sign_in_failed",
                {"host": host, "username": account["username"], "clicked": control["label"]},
            )
            workflow.flush_events(run_id)
            return after

    def prepare(self, run_id: str) -> dict:
        self.check(run_id)
        with self.guarded(self.page):
            return self._prepare(run_id)

    def _verify_batch(self, result: dict, filled: list):
        after = {f["key"]: f for f in result["fields"]}
        for entry in filled:
            if entry.get("key"):
                field = after.get(entry["key"])
                if field is None:
                    raise ValueError("Filled field disappeared; re-inspect before continuing")
                if (
                    field["kind"] in ("text", "email", "tel", "url", "textarea")
                    and entry.get("control") != "combobox"
                    and not same_value(entry["value"], field["value"])
                ):
                    raise ValueError(f"Field verification failed: {field.get('label', '')[:80]}")
                if (
                    entry.get("control") in {"radio_group", "choice"}
                    and field.get("value") != entry["value"]
                ):
                    raise ValueError(f"Field verification failed: {field.get('label', '')[:80]}")

    def _fill_page(self, run_id: str, before: dict, approved: dict, answers: dict):
        """Fill one page of a form from approved facts; returns (filled, pending)."""
        directory = state_root() / f"applications/{run_id}"
        overrides = directory / "required-overrides.json"
        if overrides.exists():
            # The site named these as required when it rejected the form: treat them so.
            wanted = {normalized(x) for x in json.loads(overrides.read_text())}
            for field in before["fields"]:
                if field["kind"] != "file" and normalized(field["label"]) in wanted:
                    field["required"] = True
        pending = []
        filled = []
        # What Rove recorded itself (a used draft, a blank), and whose employer this is.
        automatic = workflow.automatic_answers(run_id)
        employer = questions.employer_key(self.run.get("target_url") or before["url"])

        def resolve(field: dict, picker: bool = False):
            # Approved profile, then the owner's earlier answer, then a standing default.
            return questions.resolve(
                field,
                approved["profile"],
                recall=workflow.recall_answer,
                employer=employer,
                picker=picker,
            )

        grouped = {f["name"] for f in before["fields"] if f["kind"] == "radio_group"}
        for field in before["fields"]:
            if field["disabled"] or field["readonly"]:
                continue
            if self.page.url != before["url"]:
                raise PermissionError("Page changed before fill")
            pace()
            if field["kind"] == "radio" and field.get("name") in grouped:
                continue  # handled once as its group
            if self.fill_read_question(field, answers, filled, pending, automatic, resolve):
                continue  # a grouped checkbox, a checkbox group, or an unreadable question
            if field["kind"] in {"radio_group", "choice"}:
                owner_answer = questions.application_answer(field, answers, automatic)
                if owner_answer and owner_answer["value"].lower() == "skip":
                    if not field["required"]:
                        continue
                    owner_answer = None
                if owner_answer:
                    value, source = owner_answer["value"], owner_answer["source"]
                else:
                    value, source = resolve(field)
                if value is not None and normalized(field.get("value") or "") == normalized(
                    str(value)
                ):
                    # Already selected on this page: record it, never toggle it off.
                    filled.append(
                        {
                            "label": field["label"],
                            "value": str(value),
                            "source": source,
                            "key": field["key"],
                            "control": field["kind"],
                        }
                    )
                    continue
                chosen = next(
                    (
                        o
                        for o in field["options"]
                        if value is not None and normalized(o["label"]) == normalized(str(value))
                    ),
                    None,
                )
                if chosen is not None and self.select_choice(field, chosen["label"]):
                    filled.append(
                        {
                            "label": field["label"],
                            "value": chosen["label"],
                            "source": source,
                            "key": field["key"],
                            "control": field["kind"],
                        }
                    )
                    continue
                pending.append(
                    {
                        "label": field["label"],
                        "key": field["key"],
                        "required": field["required"],
                        "options": [o["label"] for o in field["options"]],
                        "reason": "Needs reviewed answer or supported control adapter"
                        if chosen is None
                        else "Selection could not be verified",
                        **questions.unlabeled(field),
                    }
                )
                continue
            locator = self.page.locator(f'[data-rove-field="{int(field["ref"])}"]')
            if field["kind"] == "file":
                # Resume uploads are a separate, explicit preparation action, using a
                # frozen file only. Other requested files always remain unresolved.
                if not re.search(
                    r"resume|curriculum vitae|\bcv\b",
                    field["label"] + " " + field["name"] + " " + field["id"],
                    re.IGNORECASE,
                ):
                    if field["required"]:
                        pending.append(
                            {
                                "label": field["label"],
                                "key": field["key"],
                                "reason": "Unapproved required file requested",
                            }
                        )
                    continue
                resume = directory / "resume.pdf"
                manifest_path = directory / "resume-manifest.json"
                if manifest_path.exists():
                    manifest = json.loads(manifest_path.read_text())
                    if not manifest.get("ready") or not resume.is_file():
                        raise PermissionError("Resume preparation is incomplete")
                    self.run["resume_sha256"] = manifest["resume_sha256"]
                    self.run["resume_is_tailored"] = manifest.get("tailored", False)
                if not resume.exists():
                    source = Path(approved["profile"]["evidence"]["resume_path"])
                    if source.suffix.lower() != ".pdf":
                        raise ValueError("Approved resume must be a PDF")
                    shutil.copyfile(source, resume)
                    resume.chmod(0o600)
                    self.run["resume_sha256"] = hashlib.sha256(resume.read_bytes()).hexdigest()
                    self.save()
                if hashlib.sha256(resume.read_bytes()).hexdigest() != self.run["resume_sha256"]:
                    raise PermissionError("Frozen resume changed")
                upload_control = locator.element_handle()
                locator.set_input_files(str(resume))
                if (
                    upload_control.evaluate(
                        "e=>e.files.length===1 && e.files[0].name==='resume.pdf'"
                    )
                    is not True
                ):
                    raise ValueError("Resume attachment verification failed")
                filled.append(
                    {
                        "label": field["label"],
                        "source": "frozen Erga job resume"
                        if self.run.get("resume_is_tailored")
                        else "frozen approved base resume",
                        "sha256": self.run["resume_sha256"],
                    }
                )
                continue
            owner_answer = questions.application_answer(field, answers, automatic)
            if owner_answer and owner_answer["value"].lower() == "skip":
                if not field["required"]:
                    continue
                # A blank recorded while the field looked optional is not an answer now.
                owner_answer = None
            if field["kind"] in ("text", "search") and (
                field.get("autocomplete") in ("list", "both")
                or re.search(
                    r"start typing|type to search|search",
                    field.get("placeholder") or "",
                    re.IGNORECASE,
                )
                or is_place_label(field["label"])
            ):
                # Place fields are pickers nearly everywhere, even without ARIA hints.
                field = {**field, "role": "combobox"}
            # A picker shows its options only once opened: until then only a value that
            # does not depend on them (a profile fact, an earlier answer) is resolved.
            unread_picker = field["role"] == "combobox" and not field["options"]
            if owner_answer:
                value, source = owner_answer["value"], owner_answer["source"]
            else:
                value, source = resolve(field, picker=unread_picker)
            if (
                value is not None
                and re.search(r"phone|mobile", field["label"], re.IGNORECASE)
                and not re.search(r"country code", field["label"], re.IGNORECASE)
            ):
                value = phone_variants(value)[0]
            choices = [o["label"] for o in field["options"]]
            if field["role"] == "combobox" and value is None:
                locator.click()
                locator.press("ArrowDown")
                try:
                    options = self.page.get_by_role("option")
                    options.first.wait_for(state="visible", timeout=3000)
                    choices = options.all_text_contents()[:300]
                except PlaywrightError:
                    pass
                locator.press("Escape")
            if value is None and choices and unread_picker:
                # The options are known now: resolve again against them.
                value, source = resolve({**field, "options": [{"label": c} for c in choices]})
            if field["role"] == "combobox" and value is not None:
                if self.select_combobox(locator, field, value, approved["profile"]):
                    filled.append(
                        {
                            "label": field["label"],
                            "value": value,
                            "source": source,
                            "key": field["key"],
                            "control": "combobox",
                        }
                    )
                    continue
                pending.append(
                    {
                        "label": field["label"],
                        "key": field["key"],
                        "required": field["required"],
                        "reason": "No unique matching dropdown option",
                    }
                )
                continue
            if field["tag"] == "select" and value is not None:
                options = [x for x in field["options"] if option_matches(x["label"], value)]
                if len(options) == 1:
                    locator.select_option(value=options[0]["value"])
                    if locator.input_value() != options[0]["value"]:
                        raise ValueError("Selection verification failed")
                    filled.append(
                        {
                            "label": field["label"],
                            "value": value,
                            "source": source,
                            "key": field["key"],
                        }
                    )
                    continue
            if (
                field["kind"] in ("checkbox", "radio")
                and value is not None
                and str(value).lower() in {"yes", "no", "true", "false"}
            ):
                desired = str(value).lower() in {"yes", "true"}
                locator.set_checked(desired)
                if locator.is_checked() != desired:
                    raise ValueError("Selection verification failed")
                filled.append(
                    {"label": field["label"], "value": value, "source": source, "key": field["key"]}
                )
                continue
            if (
                field["kind"] not in ("text", "email", "tel", "url", "textarea")
                or (
                    field["kind"] == "textarea"
                    and not owner_answer
                    and not questions.fills_long_text(source)
                )
            ) or value is None:
                pending.append(
                    {
                        "label": field["label"],
                        "required": field["required"],
                        "key": field["key"],
                        "options": choices,
                        "max_chars": field.get("maxlength"),
                        "reason": "Needs reviewed answer or supported control adapter",
                        **questions.unlabeled(field),
                    }
                )
                continue
            if field["value"] and not same_value(value, field["value"]) and not owner_answer:
                pending.append(
                    {
                        "label": field["label"],
                        "key": field["key"],
                        "required": field["required"],
                        "reason": "Existing value differs; preserved for review",
                    }
                )
                continue
            if field["value"] and same_value(value, field["value"]):
                filled.append(
                    {"label": field["label"], "value": value, "source": source, "key": field["key"]}
                )
                continue
            self.type_value(locator, value)
            kept = locator.input_value()
            if not same_value(value, kept) and kept and value.startswith(kept):
                # The field silently keeps only its first N characters: fit the text to it.
                value = workflow.brief(value, len(kept))
                self.type_value(locator, value)
                kept = locator.input_value()
                source = f"{source} (shortened to {len(kept)} characters)"
            if not same_value(value, kept):
                raise ValueError(f"Field verification failed: {field['label'][:80]}")
            filled.append(
                {"label": field["label"], "value": value, "source": source, "key": field["key"]}
            )
        return filled, pending

    def _prepare(self, run_id: str) -> dict:
        before = self.observe()
        if not approved_ats(before["url"]) or job_scope(before["url"]) != job_scope(
            self.run.get("target_url", workflow.get(run_id)["url"])
        ):
            return {
                **before,
                "status": "NEEDS_EMPLOYER_LINK",
                "reason": "The form must match the verified employer and job destination before entering candidate data. A shared ATS hostname is insufficient.",
            }
        directory = state_root() / f"applications/{run_id}"
        approved = json.loads((directory / "profile.json").read_text())
        from .onboarding import digest

        if digest(approved["profile"]) != self.run["profile_hash"]:
            raise PermissionError("Frozen profile bytes changed")
        if read_approved()["profile_hash"] != approved["profile_hash"]:
            raise ValueError("Approved profile changed; reopen the job to freeze the new version")
        if before.get("manual_takeover_required"):
            return {**before, "status": "MANUAL_LOGIN_REQUIRED"}
        if not before["fields"]:
            return {
                **before,
                "status": "APPLICATION_FORM_NOT_OPEN",
                "reason": "Follow the application-start link first.",
            }
        self.run["status"] = "PREPARING"
        answers = workflow.approved_answers(run_id)
        filled, pending, pages = [], [], []
        result = before
        for _step in range(4):
            pages.append(before["url"])
            page_filled, page_pending = self._fill_page(run_id, before, approved, answers)
            filled.extend(page_filled)
            pending.extend(page_pending)
            self.run.update(status="NEEDS_REVIEW", filled=filled, pending=pending)
            self.save()
            result = self.observe()
            self._verify_batch(result, page_filled)
            seen = {f["key"] for f in before["fields"]}
            pending.extend(
                {
                    "label": f["label"],
                    "key": f["key"],
                    "required": f["required"],
                    "reason": "New field appeared after filling",
                }
                for f in result["fields"]
                if f["key"] not in seen
            )
            nav = result.get("nav_controls", [])
            if pending or result.get("final_controls") or not nav:
                break
            # A complete step of a multi-page form: continue once, then keep filling.
            self.click(self.page.locator(f'[data-rove-nav="{int(nav[0]["ref"])}"]'))
            self.settle()
            self.wait_for_fields()
            before = self.observe()
            workflow.record(run_id, "form_step", {"clicked": nav[0]["label"], "url": before["url"]})
            stuck = before["url"] in pages and before["fields"] == result["fields"]
            error = str(before.get("ats_markers", {}).get("form_error") or "")
            if (
                stuck
                and error
                and re.search(r"phone", error, re.IGNORECASE)
                and self.retype_phone(before)
            ):
                workflow.record(
                    run_id, "form_step", {"clicked": "retyped phone", "url": before["url"]}
                )
                before = self.observe()
                stuck = False
            if stuck and error:
                self.run["form_error"] = error[:300]
            if not before["fields"] or stuck:
                break
        self.run["pending"] = pending
        self.save()
        package = {
            "run_id": run_id,
            "url": self.page.url,
            "profile_hash": approved["profile_hash"],
            "resume_sha256": self.run.get("resume_sha256"),
            "filled": filled,
            "pending": pending,
            "form_state": result["fields"],
            "final_controls": result.get("final_controls", []),
            "pages": pages,
            "submission_enabled": False,
            "resume_is_tailored": self.run.get("resume_is_tailored", False),
        }
        package["package_hash"] = hashlib.sha256(
            json.dumps(package, sort_keys=True).encode()
        ).hexdigest()
        write_private(directory / "package.json", package)
        ready = not pending and len(package["final_controls"]) == 1
        workflow.set_state(
            run_id,
            "READY_FOR_REVIEW" if ready else "NEEDS_USER",
            package_hash=package["package_hash"],
        )
        workflow.record(run_id, "fields_prepared", {"filled": filled, "pending": pending})
        workflow.flush_events(run_id)
        status = "READY_FOR_REVIEW" if ready else "NEEDS_USER"
        if not pending and not ready:
            error = self.run.pop("form_error", "")
            result = {
                **result,
                "reason": (
                    f"The site rejected a value on this step: {error}. Check it in the "
                    "recruiting browser, then resume."
                    if error
                    else "The form's last step with its Submit control was not reached. Check "
                    "the recruiting browser, then resume."
                ),
            }
        return {**result, **package, "status": status}


def socket_path() -> Path:
    return state_root() / "browser.sock"


def serve():
    os.umask(0o077)
    lock = open(state_root() / "browser-daemon.lock", "a")  # noqa: SIM115 -- lifetime of service
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    socket_path().unlink(missing_ok=True)
    browser = RecruitingBrowser()
    try:
        # Launch at service start (login), when the one-time activation bothers no one.
        browser.ensure()
    except Exception as error:  # noqa: BLE001 -- the first request retries with a clear error
        write_private(state_root() / "browser/launch-error.json", {"error": str(error)[:500]})

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                raw = self.rfile.readline(32769)
                if len(raw) > 32768:
                    raise ValueError("Request too large")
                request = json.loads(raw)
                action = request["action"]
                if action == "open":
                    result = browser.open(request["url"])
                elif action == "observe":
                    if request.get("run_id"):
                        browser.check(request["run_id"])
                    result = browser.observe()
                elif action == "follow":
                    result = browser.follow(
                        request["run_id"], request["observation_id"], request["ref"]
                    )
                elif action == "prepare":
                    result = browser.prepare(request["run_id"])
                elif action == "close":
                    result = browser.close_run(request["run_id"])
                elif action == "register":
                    result = browser.register(request["run_id"])
                elif action == "login":
                    result = browser.login(request["run_id"])
                elif action == "reopen":
                    result = browser.reopen(request["run_id"])
                elif action == "submit":
                    # Only the worker calls this, with an authenticated owner approval
                    # for one exact package; the model has no submit tool.
                    from .submission import submit

                    result = submit(
                        browser,
                        request["run_id"],
                        request["package_hash"],
                        request["owner_message_id"],
                    )
                elif action == "status":
                    result = {
                        "daemon_running": True,
                        "browser_connected": browser.connected(),
                        "open_tabs": sorted(
                            r for r, page in browser.pages.items() if not page.is_closed()
                        ),
                        "run_id": browser.run["id"] if browser.run else None,
                    }
                else:
                    raise PermissionError("Unsupported browser action")
                response = {"result": result}
            except Exception as error:  # noqa: BLE001 -- serialize failures at the IPC boundary
                response = {"error": str(error)[:800], "error_type": type(error).__name__}
                run_id = request.get("run_id") if isinstance(request, dict) else None
                if run_id and browser.page and not browser.page.is_closed():
                    with contextlib.suppress(Exception):
                        # Private evidence: what the tab showed when the action failed.
                        browser.page.screenshot(
                            path=str(state_root() / f"applications/{run_id}/failure.png")
                        )
            self.wfile.write((json.dumps(response) + "\n").encode())

    with socketserver.UnixStreamServer(str(socket_path()), Handler) as server:
        socket_path().chmod(0o600)
        server.serve_forever()


def browser_call(action: str, **kwargs) -> dict:
    def connect():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(150)
        try:
            client.connect(str(socket_path()))
        except Exception:
            client.close()
            raise
        return client

    try:
        client = connect()
    except (FileNotFoundError, ConnectionRefusedError):
        subprocess.run(
            ["launchctl", "kickstart", f"gui/{os.getuid()}/dev.rove.browser"],
            check=True,
            capture_output=True,
        )
        for _ in range(50):
            try:
                client = connect()
                break
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.1)
        else:
            raise RuntimeError("Visible browser service did not start")
    with client:
        client.sendall((json.dumps({"action": action, **kwargs}) + "\n").encode())
        result = json.loads(client.makefile("rb").readline(200000))
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["result"]
