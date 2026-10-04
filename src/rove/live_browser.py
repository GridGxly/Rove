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
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from patchright.sync_api import Error as PlaywrightError
from patchright.sync_api import sync_playwright

from . import (
    boards,
    browser_app,
    dates,
    form_frames,
    form_reading,
    gates,
    overlays,
    questions,
    recovery,
    timing,
    workflow,
)
from .browser_app import devtools_alive

# The board table and the job and tenant rules live in destinations.py; the names stay
# importable from here for the code and tests that already use them.
from .destinations import approved_ats, job_scope, nested_paths
from .jobs import lookup_job_link, posting_gone, public_link
from .onboarding import read_approved
from .runtime import state_root, write_private

# An error the browser already wrote in plain words for the owner starts with this; the
# worker shows the rest as the card's reason instead of naming an exception.
PLAIN_STOP = "Stopped: "
UNSAFE_REDIRECT = (
    PLAIN_STOP + "The page sent the recruiting browser to an address that is not a public "
    "HTTPS site, so I closed the tab. Nothing was typed or sent there. Reply `park it` to "
    "drop this one, or `go` to try the posting again."
)
# The same stop after the send's click: whether the application went through is not
# known, so nothing here may say that nothing was sent.
UNSAFE_AFTER_SEND = (
    PLAIN_STOP + "After the send, the page went to an address that is not a public HTTPS "
    "site, so I closed the tab without reading it. Whether the application went through is "
    "unclear: check your email before you tell me the outcome, and do not click Submit again."
)
LOOKUP_FAILED = (
    PLAIN_STOP + "I could not look up the address this page is on, so I stopped before "
    "reading or typing anything there. The tab is still open. Reply `go` to try again."
)
LOOKUP_FAILED_AFTER_SEND = (
    PLAIN_STOP + "After the send I could not look up the address the page went to, so I "
    "could not read what it said. Whether the application went through is unclear: check "
    "your email and the recruiting browser, and do not click Submit again."
)
BROWSER_GONE = (
    PLAIN_STOP + "The recruiting browser was closed, so this application's page is gone. I "
    "started the browser again; reply `go` and I open the page afresh."
)
TAB_GONE = (
    PLAIN_STOP + "The recruiting browser stopped answering for a while, and this "
    "application's page was not open any more when it came back. Nothing was sent; reply "
    "`go` and I open the page afresh."
)
BROWSER_DOWN = (
    PLAIN_STOP + "The recruiting browser was closed and I could not start it again. Nothing "
    "was typed or sent. Open it, or restart the browser service, then reply `go`."
)
BROWSER_UNSTEADY = (
    PLAIN_STOP + "The recruiting browser closed again while I was working on this one, "
    "right after I had started it again. Nothing was sent. Reply `go` to try once more."
)
STEP_CUT = (
    PLAIN_STOP + "The recruiting browser closed in the middle of this step. A click there "
    "may already have reached the site, so I did not repeat it on my own. The browser is "
    "running again; check the application in it, then reply `go`."
)
# A send stopped before its click is recorded: the claim comes first, and everything
# after the claim ends as a recorded outcome, never as an error.
SEND_CUT = (
    PLAIN_STOP + "The recruiting browser closed before the send, so nothing was sent. It "
    "is running again; reply `go` and I prepare this one afresh."
)
RESTARTED = "The recruiting browser was closed; I restarted it"
RECONNECTED = (
    "The recruiting browser stopped answering (a sleep or a dropped connection); I reconnected"
)
# Operations run a second time when the browser died under the first try. A send and an
# account creation are not: their click may already have reached the site.
RETRIED_ACTIONS = frozenset({"open", "observe", "follow", "prepare", "login", "reopen"})
# A failed address lookup is tried once more after this pause before the run stops.
LOOKUP_RETRY_SECONDS = 0.5
# Pages the browser shows on its own: a new tab, a navigation that failed.
BROWSER_PAGES = ("about:blank", "chrome-error://")
RUN_ID = re.compile(r"[a-f0-9]{12}")


def owner_words(detail: str) -> str | None:
    """The plain-word reason carried by a browser error, or None for any other error."""
    text = str(detail)
    return text[len(PLAIN_STOP) :] if text.startswith(PLAIN_STOP) else None


class BrowserError(RuntimeError):
    """A failure the browser service reported. `error_type` is the service's own name for
    it, kept for the system log; the owner reads plain words built from the message."""

    def __init__(self, text: str, error_type: str = ""):
        super().__init__(text)
        self.error_type = error_type


def same_job(approved: str, form: str) -> bool:
    """The form is the job that was opened: the same job scope, or, on an employer's own
    site, a page under the posting's own path (its apply step)."""
    return job_scope(approved) == job_scope(form) or nested_paths(approved, form)


def route_unhandled(route) -> bool:
    """Whether nobody has continued, aborted or fulfilled this route yet.

    Rove's own mark comes first; the driver's own state (the future it keeps while a
    handler may still answer) covers a route something else already answered.
    """
    if getattr(route, "_rove_handled", False):
        return False
    impl = getattr(route, "_impl_obj", None)
    if impl is not None and hasattr(impl, "_handling_future"):
        return impl._handling_future is not None
    return True


def mark_handled(route):
    with contextlib.suppress(AttributeError):
        route._rove_handled = True


def same_value(expected: str, actual: str) -> bool:
    """A typed value counts when the site kept it, reformatted it, or prefixed its country code."""
    expected, actual = str(expected or ""), str(actual or "")
    if expected == actual or " ".join(expected.split()) == " ".join(actual.split()):
        return True
    digits_expected, digits_actual = re.sub(r"\D", "", expected), re.sub(r"\D", "", actual)
    if len(digits_expected) >= 10 and len(digits_actual) >= 10:
        return digits_expected[-10:] == digits_actual[-10:]
    return len(digits_expected) >= 7 and digits_actual.endswith(digits_expected)


def public_address(value: str) -> bool:
    """A routable public address: not loopback, private, link-local, or one mapped onto them."""
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    return (getattr(address, "ipv4_mapped", None) or address).is_global


def validate_destination(url: str) -> str:
    safe = public_link(url)
    if not safe:
        raise PermissionError("Only public HTTPS job pages are supported")
    host = urlsplit(safe).hostname
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not public_address(a[4][0]) for a in addresses):
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


# How many pages of one form are filled before Rove stops and says so (Workday has six).
FORM_PAGES = 8

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


# What `accept` may say for a PDF to be taken: its extension, its type, or anything at all.
PDF_TYPES = {".pdf", "application/pdf", "application/*", "*/*", "*"}


def accepts_pdf(accept: str | None) -> bool:
    """The upload's `accept` attribute lets a PDF through (no attribute lets anything)."""
    kinds = {k.strip().lower() for k in str(accept or "").split(",") if k.strip()}
    return not kinds or bool(kinds & PDF_TYPES)


def accepted_words(accept: str) -> str:
    """The file types an upload takes, as extensions where a type has a common one."""
    names = {
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "text/plain": ".txt",
        "application/rtf": ".rtf",
    }
    kinds = [names.get(k.strip().lower(), k.strip().lower()) for k in accept.split(",")]
    return ", ".join(dict.fromkeys(k for k in kinds if k))


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


# An account form's own terms box, as opposed to anything it would like permission for.
TERMS_BOX = re.compile(
    r"\b(terms|conditions|privacy (policy|notice|statement)|user agreement|terms of (use|service))\b"
)
TERMS_ASSENT = re.compile(r"\b(agree|accept|acknowledge|read)\b")
MARKETING_BOX = re.compile(
    r"market|newsletter|promot|\boffers?\b|\bupdates?\b|subscri|\balerts?\b|\bsms\b"
    r"|text messages?|talent (community|network|pool)|future (opportunit|opening|role|position)"
    r"|contact(ed)? (me|you)|third part|\bpartners?\b|share (my|your)|survey|keep me|notify me"
    r"|recommend|opt in|\bnews\b"
)


def terms_box(field: dict) -> bool:
    """The terms checkbox an account form requires; never a marketing or contact consent.

    It must name the terms or the privacy policy and either be required or be worded as
    the applicant's assent. A box that also asks for marketing is left for the owner.
    """
    if field.get("kind") != "checkbox":
        return False
    label = normalized(str(field.get("label") or "") + " " + str(field.get("name") or ""))
    if MARKETING_BOX.search(label) or not TERMS_BOX.search(label):
        return False
    return bool(field.get("required") or TERMS_ASSENT.search(label))


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
 // References belong to this observation only: an element that left the form, or stopped
 // being a control the observation names, keeps none.
 for(const name of ['data-rove-field','data-rove-choice','data-rove-submit','data-rove-nav','data-rove-link','data-rove-auth'])
   deepAll('['+name+']').forEach(e=>e.removeAttribute(name));
 // A submit inside a shadow root never reaches the document's own guard: each root gets
 // the same guard, armed by the same flag.
 if(SHADOW)for(const r of ROOTS)if(!r.host.hasAttribute('data-rove-guard')){r.host.setAttribute('data-rove-guard','1');
   r.addEventListener('submit',e=>{if(document.documentElement.getAttribute('data-rove-submit-armed')!=='1'){e.preventDefault();e.stopImmediatePropagation();}},true);}
 const controls=deepAll(CONTROLS).filter(shown);
 const fields=controls.map((e,i)=>{
   // The reference goes on what a person would click: the control, or the visible face of a hidden or covered one.
   const native=e.matches(NATIVE);const rich=!native&&e.matches(RICH);const face=faceOf(e);const styled=!!wrapperOf(e);
   const widget=rich?null:!native?'listbox':face&&!styled?(e.tagName==='SELECT'?'toggle':'shell'):null;
   (face||e).setAttribute('data-rove-field',String(i));
   // A grouped radio or checkbox is one option of its group's question, never a question itself.
   const g=groupFor(e,controls);const column=g&&g.column?g.column(e):'';const read=g?(column?{text:column}:readOption(e)):readLabel(e);const chosen=widget?picked(e,face):null;
   const options=e.tagName==='SELECT'?[...e.options].map(o=>({label:o.text,value:o.value})).slice(0,300):(native||rich?[]:listed(e));
   return {ref:String(i),label:read.text,group:g?g.label:'',name:e.name||e.getAttribute('name')||'',id:e.id,kind:native?e.type:rich?'textarea':'combobox',tag:(widget==='toggle'?face:e).tagName.toLowerCase(),role:rich?'textbox':widget?'combobox':e.getAttribute('role'),placeholder:e.getAttribute('placeholder')||'',autocomplete:e.getAttribute('aria-autocomplete')||'',
    selected:e.closest('.select__container')?.querySelector('.select__single-value')?.innerText||chosen,
    selection_code:e.closest('.select__container')?.querySelector('.select__single-value .iti__flag')?.className.match(/\biti__([a-z]{2})\b/)?.[1]||null,
    required:!!e.required || e.getAttribute('aria-required')==='true' || requiredBy(e) || !!read.required || (!!face&&(face.hasAttribute('required')||face.getAttribute('aria-required')==='true')),
    disabled:native?e.disabled:e.getAttribute('aria-disabled')==='true',readonly:native?e.readOnly:e.getAttribute('aria-readonly')==='true',
    checked:styled?(e.checked||face.getAttribute('aria-checked')==='true'):(native?e.checked:false),
    value:native?(['password','hidden','file'].includes(e.type)?null:e.value):rich?e.innerText.trim():(chosen||''),maxlength:(e.maxLength>0?e.maxLength:null),
    options:widget==='toggle'?options.filter(o=>o.label.trim()):options,
    ...(widget?{widget}:{}),...(rich?{rich:true}:{}),...(read.missing?{label_missing:true}:{}),...(g?{_group:{key:g.key,required:g.required,missing:g.missing}}:{}),_section:sectionOf(e)};
 }).filter(e=>e.kind!=='hidden');
 const boxes=[...new Set(deepAll('button[aria-pressed]').filter(visible).map(b=>b.parentElement))].filter(c=>c.querySelectorAll(':scope > button[aria-pressed]').length>=2);
 const choices=boxes.map((c,i)=>{c.setAttribute('data-rove-choice',String(i));const buttons=[...c.querySelectorAll(':scope > button[aria-pressed]')];const box=c.querySelector('input');const read=readContainer(c);
   return {ref:String(i),label:read.text,group:'',name:box?.name||'',id:box?.id||'',kind:'choice',tag:'buttons',role:'choice',selected:null,selection_code:null,
    required:!!read.required||!!(c.parentElement&&[...c.parentElement.querySelectorAll('label')].some(l=>/required/i.test(l.className)||/\*\s*$/.test(l.innerText))),disabled:false,readonly:false,checked:false,
    value:buttons.find(b=>b.getAttribute('aria-pressed')==='true')?.innerText.trim()||'',options:buttons.map(b=>({label:b.innerText.trim(),value:b.getAttribute('data-option')||b.innerText.trim()})),
    ...(read.missing?{label_missing:true}:{}),_section:sectionOf(c)};});
 fields.push(...choices);
 const auth=deepAll('button,input[type=submit],a[href],[role="button"]').filter(visible).filter(e=>
   /^(create (an )?account|create (my )?profile|sign ?up|register|sign ?in|log ?in)$/i.test((e.innerText||e.value||'').trim())).map((e,i)=>{
   e.setAttribute('data-rove-auth',String(i));const t=(e.innerText||e.value||'').trim();return {ref:String(i),label:t,intent:/sign ?in|log ?in/i.test(t)?'login':'register'};});
 // The control that moves a multi-page form on: Next, Continue, Proceed, Review, "Next:
 // Experience", "Save & Continue", "Continue to step 3". A "continue to" that leaves the
 // form for another site or a sign-in never counts.
 const NAV=/^(?:next|continue|proceed|review|next step|go to (?:the )?next step|save (?:and|&) (?:continue|next)|review (?:and|&) submit|review (?:my |your |the )?application|(?:continue|proceed|next|go) to (?:step \d+|the next step)|(?:continue|proceed) to (?!.*\b(?:linkedin|indeed|google|facebook|apple|site|website|home|home ?page|careers?|jobs?|search|sign ?in|log ?in|dashboard|profile|account)\b).{1,40}|next ?[:\-–—] ?.{1,40}|next \(?\d+ ?(?:of|\/) ?\d+\)?)$/i;
 const navText=t=>squash(t).replace(/^[›»→>\s]+|[\s›»→>]+$/g,'');
 const nav=deepAll('button,input[type=submit],[role="button"]').filter(visible).filter(e=>
   NAV.test(navText(e.innerText||e.value||''))).map((e,i)=>{
   e.setAttribute('data-rove-nav',String(i));return {ref:String(i),label:(e.innerText||e.value||'').trim()};});
 const links=deepAll('a[href],button,[role="button"]').filter(visible).filter(e=>
   /^(apply( now| for this (job|position))?|apply on (the )?(employer|company) (site|website)|apply for this job|start application|continue application)$/i.test(e.innerText.trim())).map((e,i)=>{
   e.setAttribute('data-rove-link',String(i));return {ref:String(i),label:e.innerText.trim(),url:e.href||null,kind:e.tagName.toLowerCase()};
 });
 // A step Rove does not take: a recorded video interview or an online assessment, embedded,
 // linked, or the page itself.
 const ASSESS=/(?:^|\.)(?:hirevue\.com|sparkhire\.com|modernhire\.com|willo\.video|myinterview\.com|vidcruiter\.com|interviewstream\.com|talview\.com|codility\.com|hackerrank\.com|codesignal\.com|testgorilla\.com|karat\.(?:com|io)|pymetrics\.(?:ai|com)|harver\.com|vervoe\.com|shl\.com|mettl\.com|criteriacorp\.com|coderbyte\.com|qualified\.io|devskiller\.com|wonderlic\.com|hackerearth\.com|imocha\.io|testdome\.com)$/i;
 const VIDEO_HOST=/hirevue|sparkhire|modernhire|willo|myinterview|vidcruiter|interviewstream|talview/i;
 const VIDEO_WORDS=/^(?:(?:start|begin|record|take)(?: (?:my|your|the|a))? (?:one-way |recorded )?video (?:interview|answers?|responses?|questions?)|record (?:my |your )?(?:answer|response)s?|start recording)$/i;
 const TEST_WORDS=/^(?:start|begin|take|launch)(?: (?:my|your|the|an?))? (?:online |coding |technical |skills? )?(?:assessment|test|coding (?:test|challenge|exercise))$/i;
 const address=u=>{try{return new URL(u,location.href);}catch(_){return null;}};
 const assessment=(()=>{
   for(const f of deepAll('iframe[src],embed[src]').filter(visible)){const u=address(f.getAttribute('src'));
     if(u&&ASSESS.test(u.hostname))return {kind:VIDEO_HOST.test(u.hostname)?'video':'assessment',host:u.hostname,link:u.href};}
   if(ASSESS.test(location.hostname))return {kind:VIDEO_HOST.test(location.hostname)?'video':'assessment',host:location.hostname,link:location.href};
   for(const a of deepAll('a[href],button,[role=button]').filter(visible)){const t=squash(a.innerText||a.value||'');const u=a.href?address(a.href):null;const h=u&&ASSESS.test(u.hostname)?u.hostname:'';
     const video=VIDEO_WORDS.test(t)||(!!h&&VIDEO_HOST.test(h));const test=TEST_WORDS.test(t)||(!!h&&!VIDEO_HOST.test(h));
     if(video||test)return {kind:video?'video':'assessment',host:h||location.hostname,link:u?u.href:location.href};}
   return null;})();
 const shadowText=SHADOW?ROOTS.map(r=>[...r.children].filter(visible).map(c=>c.innerText||'').join('\n')).join('\n'):'';
 return {title:document.title,http_status:(performance.getEntriesByType('navigation')[0]||{}).responseStatus||0,text:(shadowText?document.body.innerText+'\n'+shadowText:document.body.innerText).slice(0,15000),fields,application_links:links,auth_controls:auth,nav_controls:nav,
 final_controls:deepAll('button,input[type=submit],a,[role=button]').filter(visible).filter(e=>FINAL.test((e.innerText||e.value||'').trim())).map((e,i)=>{e.setAttribute('data-rove-submit',String(i));return {ref:String(i),label:(e.innerText||e.value||'').trim()};}),
 ats_markers:{captcha_challenge:[...document.querySelectorAll('iframe[src*="recaptcha/api2/bframe"],iframe[src*="hcaptcha.com"],iframe[src*="challenges.cloudflare.com"],iframe[src*="turnstile"],.g-recaptcha,.h-captcha,.cf-turnstile')].some(e=>{const r=e.getBoundingClientRect();return visible(e)&&r.width>=200&&r.height>=60;}),
 already_applied:/\b(you have |you've )?already (applied|submitted an application)\b|application already exists/i.test(document.body.innerText),
 greenhouse_confirmation:!!document.querySelector('div.confirmation div.confirmation__content'),
 lever_submit_success:!!document.querySelector('h3[data-qa="msg-submit-success"]'),
 lever_verification_error:/there was an error verifying your application/i.test(document.body.innerText),
 __BOARDS__
  status_region:messages('__STATUS__'),form_error:messages('__ERROR__'),...(assessment?{assessment}:{})}};
}""".replace("__MESSAGES__", MESSAGES_JS)
    .replace("__READING__", form_reading.READING_JS)
    .replace("__STATUS__", STATUS_SELECTOR)
    .replace("__ERROR__", ERROR_SELECTOR)
    .replace("__BOARDS__", boards.MARKERS_JS)
)

# Text fields a person cannot see: fully transparent (itself or an ancestor), positioned
# off the page, or squeezed to a pixel. Sites plant them to catch bots, and a hostile page
# can hide instructions in their labels. Widgets that hide their own input on purpose are
# not judged: styled radios, checkboxes and file inputs, a dropdown's search input (it
# turns transparent once a value is chosen) and read-only inputs. Returns the
# `data-rove-field` references.
TRAPS_JS = r"""() => {
 const textlike=e=>e.tagName==='TEXTAREA'||(e.tagName==='INPUT'&&['text','email','tel','url','search','number','password'].includes((e.getAttribute('type')||'text').toLowerCase()));
 const widget=e=>e.readOnly||e.getAttribute('role')==='combobox'||e.hasAttribute('aria-autocomplete');
 const transparent=e=>{for(let n=e;n&&n.nodeType===1;n=n.parentElement){if(parseFloat(getComputedStyle(n).opacity)===0)return true;}return false;};
 const offpage=e=>{const r=e.getBoundingClientRect();if(r.width<=1||r.height<=1)return true;
  const x=r.left+scrollX,y=r.top+scrollY,w=Math.max(document.documentElement.scrollWidth,innerWidth),h=Math.max(document.documentElement.scrollHeight,innerHeight);
  return x+r.width<=0||y+r.height<=0||x>=w||y>=h;};
 return [...document.querySelectorAll('[data-rove-field]')].filter(e=>textlike(e)&&!widget(e)&&(transparent(e)||offpage(e))).map(e=>e.getAttribute('data-rove-field'));
}"""

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

ARM_JS = "() => document.documentElement.setAttribute('data-rove-submit-armed', '1')"
DISARM_JS = "() => document.documentElement.removeAttribute('data-rove-submit-armed')"

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
            # Short steps: a stolen focus is handed back before a keystroke can land wrong.
            time.sleep(0.1)
    except (OSError, subprocess.SubprocessError):
        pass
    return outcome


def watch_front(previous: tuple[str, str], watch_seconds: float = 6.0) -> dict:
    """Hand focus back in the background, so the launch or the new window it guards is
    not held up while the watch runs."""
    threading.Thread(
        target=restore_front, args=(previous, watch_seconds), name="rove-focus", daemon=True
    ).start()
    return {"previous": previous[1], "watching_seconds": watch_seconds}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# The flags every launch gets.
LAUNCH_FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--no-startup-window",
    "--disable-background-networking",
    "--window-size=1280,900",
    "--lang=en-US",
)
# The Rove Browser is a bundle the Keychain has never seen, so without this flag it would
# ask for "Chrome Safe Storage" at every start. With it, the cookies and passwords of its
# profile are encrypted with a fixed key, which the profile's own permissions protect.
MOCK_KEYCHAIN = "--use-mock-keychain"


class ChromeLauncher:
    """Start the recruiting browser as its own app instance and reach it over local CDP.

    Launching it ourselves means no automation flag is ever set (navigator.webdriver stays
    false), the window survives daemon restarts, and new tabs open in the background.
    The DevTools port binds to localhost only; the local account is trusted by design.

    The app is the Rove Browser that `rove browser install` built under the state root:
    the owner's Chrome, copied, under its own bundle id. The shared /Applications/Google
    Chrome.app is used only when the private config says `browser_app: "shared-chrome"`,
    and nothing falls back from one to the other.
    """

    def __init__(self):
        self.session = state_root() / "browser/session.json"

    def app(self) -> dict:
        return browser_app.chosen(workflow.config())

    def running_port(self) -> int | None:
        port = browser_app.session_info().get("port")
        try:
            port = int(port)
        except (ValueError, TypeError):
            return None
        return port if devtools_alive(port) else None

    def running_app(self) -> str | None:
        """The path of the app behind the live session, or None when nothing answers."""
        if self.running_port() is None:
            return None
        return str(browser_app.session_info().get("app") or "")

    def retire_other_apps(self, app_path: str) -> list[dict]:
        """Stop a recruiting browser that is not the configured app, so the profile is free.

        That is the shared Chrome from before the switch. The holder is read from the
        profile's own lock, so a stale session file does not matter. SIGTERM is Chrome's
        clean shutdown.
        """
        stopped = []
        for name in ("recruiting-profile", "recruiting-profile-rove"):
            holder = browser_app.profile_holder(state_root() / f"browser/{name}")
            if holder is None:
                continue
            pid, executable = holder
            if executable.startswith(app_path.rstrip("/") + "/"):
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue
            for _ in range(30):
                time.sleep(0.5)
                if not process_alive(pid):
                    break
            else:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
            stopped.append({"pid": pid, "executable": executable, "profile": name})
        if stopped:
            apps = ", ".join(sorted({Path(s["executable"]).name for s in stopped}))
            workflow.system_line(
                "browser",
                f"closed the previous recruiting browser ({apps}) so the configured app "
                f"can open the profile",
            )
        return stopped

    def command(self, app: dict, profile: Path, port: int) -> list[str]:
        keychain = [] if app["shared_chrome"] else [MOCK_KEYCHAIN]
        return [
            "open",
            "-g",  # never bring it to the front
            "-j",  # launch hidden: its first window does not appear over the owner's work
            "-n",
            "-a",
            app["path"],
            "--args",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            *LAUNCH_FLAGS,
            *keychain,
        ]

    def ensure_running(self) -> int:
        app = self.app()
        port = self.running_port()
        if port and self.running_app() == app["path"]:
            return port
        self.retire_other_apps(app["path"])
        if not app["shared_chrome"]:
            # Chrome may have updated since the copy was made; the copy follows it.
            rebuilt = browser_app.refresh()
            if rebuilt:
                workflow.system_line("browser", rebuilt)
                app = self.app()
        profile = browser_app.profile_dir(app["shared_chrome"])
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        port = free_port()
        previous = front_app()
        subprocess.run(
            self.command(app, profile, port), check=True, capture_output=True, timeout=30
        )
        for _ in range(60):
            if devtools_alive(port):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("The recruiting browser did not expose its local DevTools port")
        focus = watch_front(previous)
        write_private(
            self.session,
            {
                "port": port,
                "app": app["path"],
                "bundle": app["bundle_id"],
                "name": app["name"],
                "version": app["version"],
                "shared_chrome": app["shared_chrome"],
                "profile": str(profile),
                "started_at": workflow.now(),
                "focus": focus,
            },
        )
        return port


# A form control in the document, or inside one of its open shadow roots.
FIELDS_JS = """() => {
  const q = 'input:not([type=hidden]),select,textarea,[contenteditable][role=textbox]';
  if (document.querySelector(q)) return true;
  const deep = r => { for (const e of r.querySelectorAll('*'))
    if (e.shadowRoot && (e.shadowRoot.querySelector(q) || deep(e.shadowRoot))) return true;
    return false; };
  return deep(document);
}"""
# How many controls a person can fill in the document shows.
COUNT_JS = """() => [...document.querySelectorAll(
  'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]),select,textarea,[contenteditable][role=textbox]')]
  .filter(e => e.getClientRects().length).length"""
# A page that moved to its next step: a form control or a final control is showing.
STEP_JS = (
    """() => ("""
    + FIELDS_JS
    + """)() || [...document.querySelectorAll(
  'button,input[type=submit],a,[role=button]')].some(e => e.getClientRects().length
  && /^(submit|submit application|submit my application|submit your application|submit now|send application|complete application|finish application)$/i
     .test((e.innerText || e.value || '').trim()))"""
)
# An upload control that asks for the resume.
RESUME_UPLOAD = re.compile(r"resume|curriculum vitae|\bcv\b", re.IGNORECASE)
# The page that took the resume carries a mark until it is loaded again.
UPLOAD_MARK_JS = "t => document.documentElement.setAttribute('data-rove-upload', t)"
UPLOAD_MARKED_JS = "t => document.documentElement.getAttribute('data-rove-upload') === t"
# What a select-style picker shows as its chosen value; its input is emptied on a choice.
CHOSEN_JS = (
    "e => e.closest('.select__container')?.querySelector('.select__single-value')?.innerText || ''"
)
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
RENDERED_JS = "() => (" + FIELDS_JS + ")() || document.body.innerText.trim().length > 200"
# A button group shows the option as pressed: the one named by `label`.
CHOICE_PRESSED_JS = (
    "({ref,label})=>{\n"
    + form_reading.DEEP_ONE_JS
    + " const box=deepOne('[data-rove-choice=\"'+ref+'\"]');\n"
    " return !!box&&[...box.querySelectorAll('button[aria-pressed]')]"
    ".some(b=>b.innerText.trim()===label&&b.getAttribute('aria-pressed')==='true');}"
)
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


# How long a code the site mails may take to arrive.
CODE_WAIT_SECONDS = 120
# A picture check that is on screen: its challenge frame is shown at a size to be worked.
CAPTCHA_SHOWING_JS = """() => [...document.querySelectorAll(
  'iframe[src*="recaptcha/api2/bframe"],iframe[src*="hcaptcha.com"],' +
  'iframe[src*="challenges.cloudflare.com"],iframe[src*="turnstile"]')]
  .some(e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return !!e.getClientRects().length && s.visibility !== 'hidden' && r.width >= 200
      && r.height >= 60 && r.bottom > 0 && r.top < innerHeight; })"""
# Addresses that are the page's own content, not a place on the network.
LOCAL_CONTENT = ("blob:", "data:")
# The page's address and how many controls and dialogs it shows, and whether that changed.
PAGE_STATE = (
    "[location.href, document.querySelectorAll("
    "'input:not([type=hidden]),select,textarea,[role=dialog],dialog[open]').length]"
)
WHERE_JS = f"() => {PAGE_STATE}"
MOVED_JS = (
    f"before => {{ const now = {PAGE_STATE}; "
    "return now[0] !== before[0] || now[1] !== before[1]; }"
)


class RecruitingBrowser:
    # What a school picker chose instead of the name ("Other"), or why it chose nothing.
    picked_label: str | None = None
    picker_reason: str | None = None

    def __init__(self, headless: bool = False):
        self.headless = headless
        # The driver's objects and the current run are set once the browser is reached.
        self.playwright: Any = None
        self.context: Any = None
        self.page: Any = None
        self.run: Any = None
        self.observation: Any = None
        self.dns: dict[str, float] = {}
        self.runs: dict[str, dict] = {}
        self.pages: dict[str, Any] = {}
        self.browser: Any = None
        self.cdp: Any = None
        self.warmed: set[str] = set()
        self.hops: list[str] = []
        self.secrets: set[str] = set()
        # Applications whose tab went with a session that was dropped, and the plain words
        # that say what happened to it.
        self.lost: dict[str, str] = {}
        self.port = None
        self.launcher = ChromeLauncher()
        # Per application: the child frame its form lives in, and the tab address it was
        # chosen on. No entry means the tab's own document.
        self.frames = {}
        self.frames_waited = None
        self.sending = False  # between a send's click and its recorded outcome

    def destination(self, url: str) -> str:
        """ "public", "private" (anything that is not public HTTPS), or "unresolved" when
        the host's address could not be looked up. A host's addresses are looked up again
        once a minute."""
        try:
            host = urlsplit(url).hostname
            if host not in self.dns or time.monotonic() - self.dns[host] > 60:
                validate_destination(url)
                self.dns[host] = time.monotonic()
            elif not public_link(url):
                raise PermissionError("Unsupported URL")
            return "public"
        except (ValueError, PermissionError):
            return "private"
        except OSError:  # the lookup itself failed: nothing is known about the address
            return "unresolved"

    def allowed(self, url: str) -> bool:
        """Public HTTPS only."""
        return self.destination(url) == "public"

    def _route(self, route):
        """Continue a public HTTPS request, abort anything else; each route once.

        A route this handler (or the driver) already continued or aborted is left alone:
        calling it again is what the driver reports as "Route is already handled". A
        driver error, such as the page closing mid-request, ends the handler quietly;
        the request is then the driver's to finish.
        """
        if not route_unhandled(route):
            return
        mark_handled(route)
        try:
            url = route.request.url
            # A blob: or data: address is content the page already holds (a worker's own
            # script, an inline image): it names no destination, so nothing is reached.
            allowed = url.startswith(LOCAL_CONTENT) or self.allowed(url)
        except Exception:  # noqa: BLE001 -- an unreadable request is never let through
            allowed = False
        with contextlib.suppress(PlaywrightError):
            if allowed:
                route.continue_()
            else:
                route.abort()

    def require_public_page(self):
        """Stop when the tab, or any hop that led to it, is not a public HTTPS page.

        The driver continues redirected requests without asking `_route`, so a posting
        can answer with a redirect to a loopback or private address. This runs after
        every navigation and before anything on the page is read: the tab is closed, and
        the error carries the owner's plain-word reason.
        """
        if self.page is None or self.page.is_closed():
            return
        hops, self.hops = self.hops, []
        landed = self.page.url
        if landed and not landed.startswith(BROWSER_PAGES):
            self.require_public(landed)
        for url in hops:
            self.require_public(url, hop=True)

    def require_public(self, url: str, hop: bool = False):
        """Stop on an address that is not public HTTPS (the tab is closed), and on one whose
        lookup failed twice (the tab stays: nothing is known against it)."""
        verdict = self.public_hop(url) if hop else self.destination(url)
        if verdict == "unresolved":
            time.sleep(LOOKUP_RETRY_SECONDS)  # one more try: a lookup can fail for a moment
            verdict = self.public_hop(url) if hop else self.destination(url)
        if verdict == "private":
            self.stop_unsafe(url)
        if verdict == "unresolved":
            self.stop_unresolved(url)

    def public_hop(self, url: str) -> str:
        """A redirect hop may be plain HTTP on its way to HTTPS; it may never be a private,
        loopback or link-local address, another port or another scheme."""
        verdict = self.destination(url)
        if verdict != "private":
            return verdict
        try:
            parsed = urlsplit(url)
            if parsed.scheme != "http" or parsed.port not in (None, 80) or not parsed.hostname:
                return "private"
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        except ValueError:
            return "private"
        return self.destination(f"https://{host}{parsed.path or '/'}")

    def stop_unsafe(self, url: str):
        run_id = self.run["id"] if self.run else None
        with contextlib.suppress(PlaywrightError):
            self.page.close()
        # The closed page stays referenced so code already holding it fails cleanly.
        self.observation = None
        after_send = bool(self.sending)
        if run_id:
            self.pages.pop(run_id, None)
            self.runs.pop(run_id, None)
            parsed = urlsplit(url)
            where = f"{parsed.scheme}://{parsed.hostname or ''}"[:200]
            with contextlib.suppress(Exception):  # the stop itself must not depend on Discord
                workflow.record(
                    run_id,
                    "redirect_blocked",
                    {"destination": where, **({"after_send": True} if after_send else {})},
                )
                workflow.system_line(run_id, f"redirect blocked · tab closed · {where}")
        raise PermissionError(UNSAFE_AFTER_SEND if after_send else UNSAFE_REDIRECT)

    def stop_unresolved(self, url: str):
        """The address could not be looked up: stop before anything on the page is read,
        typed or clicked, and leave the tab as it is."""
        run_id = self.run["id"] if self.run else None
        host = (urlsplit(url).hostname or "")[:200]
        if run_id:
            with contextlib.suppress(Exception):
                workflow.system_line(run_id, f"address lookup failed twice · {host}")
        raise PermissionError(LOOKUP_FAILED_AFTER_SEND if self.sending else LOOKUP_FAILED)

    def mark_traps(self, data: dict, frame=None):
        """Mark optional text fields nobody can see as `hidden_trap`; nothing fills or asks them.

        A field the form requires is never marked, however it is styled: skipping it
        silently could send an incomplete application, so it stays an ordinary question.
        """
        try:
            refs = set((frame or self.page).evaluate(TRAPS_JS))
        except PlaywrightError:
            return
        for field in data.get("fields", []):
            if (
                field.get("tag") in {"input", "textarea"}
                and field.get("ref") in refs
                and not field.get("required")
            ):
                field["hidden_trap"] = True

    def fill_secret(self, locator, secret: str):
        """Type a credential; a failure never carries its value into an error or a log."""
        self.secrets.add(secret)
        try:
            locator.fill(secret)
        except PlaywrightError as error:
            raise RuntimeError(
                "The account password could not be typed into the form: " + type(error).__name__
            ) from None

    def connected(self) -> bool:
        return self.alive()

    def attach_if_running(self) -> bool:
        """Reconnect to a running recruiting browser of the configured app; never launch one."""
        if self.connected():
            return True
        if self.headless:
            return False
        try:
            wanted = self.launcher.app()["path"]
        except RuntimeError:
            return False
        if self.launcher.running_app() != wanted:
            return False
        self.ensure()
        return True

    def alive(self) -> bool:
        """The browser, its context and the daemon's CDP session all still answer.

        A browser the owner quit by hand leaves a connected-looking object behind; the
        DevTools port and a round trip on the session are what say it is really there.
        """
        if self.context is None:
            return False
        try:
            if self.browser is not None:
                if not self.browser.is_connected():
                    return False
                if self.port and not devtools_alive(self.port):
                    return False
                if self.cdp is not None:
                    self.cdp.send("Browser.getVersion")
                return True
            return bool(self.context.browser and self.context.browser.is_connected())
        except Exception:  # noqa: BLE001 -- any failure to answer means not alive
            return False

    def reset(self):
        """Drop a dead or stale session; every tab it held is gone with it."""
        for closer in (
            lambda: self.cdp.detach(),
            lambda: self.browser.close(),
            lambda: self.context.close(),
            lambda: self.playwright.stop(),
        ):
            with contextlib.suppress(Exception):
                closer()
        self.playwright = self.browser = self.context = self.cdp = None
        self.port = None
        self.page = self.run = self.observation = None
        self.lost.update(dict.fromkeys(self.pages, BROWSER_GONE))
        self.frames.clear()
        self.pages.clear()
        self.runs.clear()
        self.dns.clear()
        self.warmed.clear()

    def ensure(self) -> bool:
        """A live browser to drive. Returns True when a dead session had to be replaced.

        The owner quitting the browser, or a DevTools session that died (a laptop sleep,
        a crash), leaves a stale session behind: it is dropped and the browser reached
        again through the launcher, which reuses a browser that is still running and
        starts one otherwise. The tabs still open are adopted back by their applications.
        One system-log line says which of the two it was.
        """
        if self.context and self.alive():
            return False
        restarted = self.context is not None
        port, dropped = self.port, list(self.pages)
        self.reset()
        try:
            self.launch()
        except Exception as error:
            if not restarted:
                raise  # the first launch says in its own words what is missing
            raise RuntimeError(BROWSER_DOWN) from error
        if restarted:
            same = bool(port) and self.port == port  # the browser itself never went away
            if same:
                # Not adopted back although the browser stayed up: the tab itself is gone.
                self.lost.update({r: TAB_GONE for r in dropped if r in self.lost})
            with contextlib.suppress(Exception):  # a log line never blocks the recovery
                workflow.system_line("browser", RECONNECTED if same else RESTARTED)
        return restarted

    def recovering(self, operation, retry: bool = True, words=lambda: STEP_CUT):
        """Run one browser operation. When it fails because the browser died under it,
        the browser is reached again once and, if `retry`, the operation runs once more.

        A failure on a live browser is the operation's own and is raised as it is. An
        operation that is not retried (`words` says why in plain words), or a retry that
        dies too, ends in plain words.
        """
        try:
            return operation()
        except Exception as error:
            if self.alive():
                raise
            self.ensure()  # raises the plain BROWSER_DOWN when it cannot
            if not retry:
                raise RuntimeError(words()) from error
        try:
            return operation()
        except Exception as error:
            if self.alive():
                raise
            raise RuntimeError(BROWSER_UNSTEADY) from error

    def launch(self):
        self.playwright = sync_playwright().start()
        self.browser = self.cdp = None
        if self.headless:
            # Tests and the synthetic fixture: Patchright's Chromium, no app bundle at all.
            directory = state_root() / "browser/recruiting-profile"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.context = self.playwright.chromium.launch_persistent_context(
                str(directory),
                headless=True,
                viewport=None,
                args=["--disable-background-networking"],
                accept_downloads=False,
            )
        else:
            port = self.launcher.ensure_running()
            self.browser = self.playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=20000
            )
            self.port = port
            self.context = (
                self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
            )
            self.cdp = self.browser.new_browser_cdp_session()
        self.context.set_default_timeout(12000)
        self.context.add_init_script(PREPARE_GUARD)
        if self.cdp is not None:
            self.adopt_pages()

    def adopt_pages(self, only: str | None = None):
        """Map the tabs still open in the browser back to their applications.

        After a reconnect every tab is looked at: one showing a page of an application
        still being worked on (its posting, or the form it last observed) is kept and
        registered again; anything else left over (blank tabs, finished runs) is closed.
        With `only`, just that application's tab is looked for among the tabs no
        application holds, and nothing is closed.
        """
        with workflow.db() as conn:
            waiting = [
                dict(r)
                for r in conn.execute(
                    "SELECT id,url FROM application_queue WHERE run_id IS NOT NULL AND status IN "
                    "('NEEDS_USER','READY_FOR_REVIEW','MANUAL_TAKEOVER','PREPARING',"
                    "'SUBMITTING','UNKNOWN_SUBMISSION')"
                )
                if only is None or r["id"] == only
            ]
        mine = set(self.own_tabs()) if only is None else set()
        runs = {}
        for item in waiting:
            run_file = state_root() / f"applications/{item['id']}/run.json"
            with contextlib.suppress(OSError, ValueError):
                runs[item["id"]] = json.loads(run_file.read_text())
        free = [
            page
            for page in self.context.pages
            if not page.is_closed() and not any(page is held for held in self.pages.values())
        ]
        # A tab at an application's own address first; one that only shows the same job
        # (another spelling of its link) after, so it never takes another tab's place.
        for exact in (True, False):
            for page in list(free):
                owner = next(
                    (
                        item
                        for item in waiting
                        if item["id"] in runs
                        and item["id"] not in self.pages
                        and self.shows(page.url, item["url"], runs[item["id"]], exact)
                    ),
                    None,
                )
                if owner:
                    free.remove(page)
                    self.pages[owner["id"]] = page
                    self.runs[owner["id"]] = runs[owner["id"]]
                    self.lost.pop(owner["id"], None)
                    self.watch_dialogs(page)
        for page in free:
            if only is None and self.tab_id(page) in mine:
                # Only a tab Rove itself opened, for a run that is over or a blank one;
                # the owner's own tabs and one an unclear send left open stay.
                with contextlib.suppress(PlaywrightError):
                    page.close()

    TABS = "browser/tabs.json"

    def own_tabs(self) -> list[str]:
        try:
            tabs = json.loads((state_root() / self.TABS).read_text())
        except (OSError, ValueError):
            return []
        return [str(t) for t in tabs] if isinstance(tabs, list) else []

    def tab_id(self, page) -> str | None:
        """The browser's own id for a tab, which survives a reconnect."""
        try:
            session = self.context.new_cdp_session(page)
            try:
                return session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
            finally:
                with contextlib.suppress(Exception):
                    session.detach()
        except Exception:  # noqa: BLE001 -- an unknown tab is treated as the owner's
            return None

    def remember_tab(self, page, target: str | None = None):
        """Note that Rove opened this tab, so a later reconnect may close it when its run
        is over. A tab missing from the list is never closed by Rove."""
        target = target or self.tab_id(page)
        if not target:
            return
        tabs = [t for t in self.own_tabs() if t != target][-199:] + [target]
        with contextlib.suppress(OSError):
            write_private(state_root() / self.TABS, tabs)

    @staticmethod
    def shows(url: str, queued: str, run: dict, exact: bool = False) -> bool:
        """The tab's address is a page of this application: its posting, its target, or
        the page it last observed (a form on another host after an Apply link). `exact`
        asks for that address itself (or a page under it), not only the same job."""
        if not str(url or "").startswith(("https://", "http://")):
            return False  # a blank tab or the browser's own error page is nobody's
        for known in (queued, run.get("target_url"), run.get("url")):
            if not known or not str(known).startswith(("https://", "http://")):
                continue
            if url.startswith(str(known).rstrip("/")) or (not exact and same_job(known, url)):
                return True
        return False

    def new_page(self):
        """A background tab (or a background window when none exists): never steals focus."""
        if self.cdp is None:
            page = self.context.new_page()
            self.watch_dialogs(page)
            self.remember_tab(page)
            return page
        first = not any(not page.is_closed() for page in self.context.pages)
        previous = front_app() if first else None
        with self.context.expect_page(timeout=15000) as created:
            target = self.cdp.send(
                "Target.createTarget",
                {"url": "about:blank", "newWindow": first, "background": True},
            )
        if previous:
            # A browser's first window activates the app; give the owner his window back.
            watch_front(previous, watch_seconds=3.0)
        self.watch_dialogs(created.value)
        self.remember_tab(created.value, (target or {}).get("targetId"))
        return created.value

    @contextlib.contextmanager
    def guarded(self, page):
        """Destination policy only while this daemon drives the page.

        A route handler runs only while the daemon is inside a browser call, so a
        context-wide route would stall every tab the owner browses by hand whenever the
        daemon sits idle. The guard is attached per operation and removed afterwards.

        Redirects are continued by the driver without a route call, so every main-frame
        navigation response and every redirect target is also noted here;
        `require_public_page` checks them before the page is read.

        Re-entrant: a page carries one handler however many guarded operations nest on
        it, counted on the page itself, and the handler leaves on the outermost exit,
        error or not. Two handlers on one page would each try to continue the same
        request ("Route is already handled").
        """
        depth = getattr(page, "_rove_guard", 0)
        if depth == 0:
            self.take_back(page)
            self.hops = []
            note = self._hop_noter(page)
            page.route("**/*", self._route)
            page.on("response", note)
            page._rove_note = note
        page._rove_guard = depth + 1
        self.operating = getattr(self, "operating", 0) + 1  # a leave-page warning is Rove's own
        try:
            yield page
        finally:
            self.operating -= 1
            page._rove_guard = max(getattr(page, "_rove_guard", 1) - 1, 0)
            if page._rove_guard == 0:
                self.unguard(page)

    def hand_over(self, run_id: str) -> dict:
        """Leave this application's tab to the owner for a step only he takes.

        The guard that stops an accidental submit while Rove fills a form would also
        swallow his own press of the page's Next, Sign in or Verify. It is lifted on the
        page as it stands; a page loaded afterwards has the guard again. `take_back` puts
        it on again before Rove touches the tab.
        """
        page = self.pages.get(run_id)
        if page is None or page.is_closed():
            return {"handed_over": False}
        armed = 0
        for frame in page.frames:
            with contextlib.suppress(PlaywrightError):
                frame.evaluate(ARM_JS)
                armed += 1
        return {"handed_over": armed > 0}

    @staticmethod
    def take_back(page):
        """Rove drives this tab again: no page in it lets a submit through unarmed."""
        for frame in page.frames:
            with contextlib.suppress(PlaywrightError):
                frame.evaluate(DISARM_JS)

    def _hop_noter(self, page):
        def note(response):
            try:
                request = response.request
                if not request.is_navigation_request():
                    return
                try:
                    if request.frame != page.main_frame:
                        return
                except PlaywrightError:
                    pass  # no frame yet: treat it as the page's own navigation
                if len(self.hops) < 50:
                    self.hops.append(response.url)
                    location = response.headers.get("location")
                    if location and 300 <= response.status < 400:
                        self.hops.append(urljoin(response.url, location))
            except PlaywrightError:
                pass

        return note

    @staticmethod
    def unguard(page):
        """Take the guard off a page whatever state it is in."""
        note = getattr(page, "_rove_note", None)
        page._rove_guard = 0
        page._rove_note = None
        if note is not None:
            with contextlib.suppress(Exception):
                page.remove_listener("response", note)
        with contextlib.suppress(Exception):
            page.unroute("**/*")

    @staticmethod
    def guard_depth(page) -> int:
        return getattr(page, "_rove_guard", 0)

    def close_run(self, run_id: str) -> dict:
        page = self.pages.pop(run_id, None)
        self.runs.pop(run_id, None)
        self.frames.pop(run_id, None)
        self.lost.pop(run_id, None)  # closed on purpose, not lost
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
        self.beat()
        if pacing_enabled():
            try:
                locator.hover(timeout=3000)
            except PlaywrightError:
                pass
            pace(0.2, 0.7)
        locator.click(timeout=timeout)

    def type_value(self, locator, value: str):
        self.beat()
        if pacing_enabled() and len(value) <= 80:
            locator.click()
            locator.fill("")
            locator.press_sequentially(value, delay=random.uniform(25, 65))
        else:
            locator.fill(value)

    def write(self, locator, field: dict, value: str) -> str:
        """Type one value the way its control takes it; what the control kept.

        A rich-text editor is typed with the keyboard and read through its text; a date
        or month input takes its ISO value whole; a masked date box gets the digits first,
        and the separators too when it does not insert them itself.
        """
        if field.get("rich"):
            locator.click()
            self.page.keyboard.press("ControlOrMeta+A")
            self.page.keyboard.press("Backspace")
            slow = pacing_enabled() and len(value) <= 80
            self.page.keyboard.type(value, delay=random.uniform(25, 65) if slow else 0)
            return locator.evaluate("e=>e.innerText.trim()")
        if field["kind"] in dates.DATE_KINDS:
            locator.fill(value)
            return locator.input_value()
        if dates.is_date_box(field):
            digits = re.sub(r"\D", "", value)
            self.type_value(locator, digits)
            kept = locator.input_value()
            if kept == digits and digits != value:
                self.type_value(locator, value)
                kept = locator.input_value()
            return kept
        self.type_value(locator, value)
        return locator.input_value()

    def warm(self, url: str, force: bool = False):
        """Visit the site's front door first; a cold deep link is what bot managers flag."""
        host = urlsplit(url).hostname or ""
        if not pacing_enabled() or (host in self.warmed and not force):
            return
        self.warmed.add(host)
        try:
            self.page.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=30000)
            self.require_public_page()
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
            self.navigate(self.run["target_url"])
            result = self.observe()
        workflow.record(
            run_id,
            "browser_retry",
            {"url": result["url"], "blocked": result.get("blocked", False)},
        )
        workflow.flush_events(run_id)
        return result

    def navigate(self, target: str):
        """Load a page and settle it. Where it landed is checked before the page is touched.

        The body only has to exist: a page whose whole content is a full-screen fixed
        layer (an interstitial in front of the form) has a body with no height, which
        Playwright never calls visible, and waiting for that would skip the settle and
        the pop-up step that clears the layer.
        """
        try:
            self.page.goto(target, wait_until="domcontentloaded", timeout=45000)
        except PlaywrightError as error:
            self.run["navigation_error"] = type(error).__name__
            self.require_public_page()
            return
        self.require_public_page()
        try:
            self.page.locator("body").wait_for(state="attached", timeout=5000)
        except PlaywrightError as error:
            self.run["navigation_error"] = type(error).__name__
        try:
            self.settle()
        except PlaywrightError as error:
            self.run["navigation_error"] = type(error).__name__

    def save(self):
        write_private(state_root() / f"applications/{self.run['id']}/run.json", self.run)
        self.beat()

    def beat(self):
        """A sign of life for the worker while this application is observed and filled."""
        if self.run and self.run.get("id"):
            recovery.beat(self.run["id"])

    def settle(self, timeout: int = 10000):
        """Bounded wait for a rendered page.

        Single-page job boards paint a shell, a cookie banner and a loading indicator
        before the form; the banner alone reads as "rendered" text, so the wait also
        declines the banner and waits out a visible indicator.
        """
        self.require_public_page()  # no banner is clicked on a page a redirect led off-site
        try:
            self.page.wait_for_function(RENDERED_JS, timeout=timeout)
        except PlaywrightError:
            pass
        # The wait may have ended on another page: checked again before anything is
        # clicked, and again before each click below.
        self.require_public_page()
        self.dismiss_consent()
        self.clear_overlays()
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
                locator = self.form.locator(f'[data-rove-field="{int(field["ref"])}"]')
                self.type_value(locator, alternative)
                return True
        return False

    def wait_for_fields(self, timeout: int = 8000, script: str = FIELDS_JS) -> bool:
        """Bounded wait for a form in the tab: its own document, or a frame that may hold one.

        A tab without child frames waits on its document alone, as it always has.
        """
        main = self.page.main_frame
        if len(self.page.frames) < 2:
            try:
                main.wait_for_function(script, timeout=timeout)
                return True
            except PlaywrightError:
                return False
        deadline = time.monotonic() + timeout / 1000
        while True:
            for frame in self.form_frames():
                with contextlib.suppress(PlaywrightError):
                    if frame.evaluate(script):
                        return True
            if time.monotonic() >= deadline:
                return False
            self.page.wait_for_timeout(200)

    def wait_for_reading(self, seconds: float = 10.0) -> bool:
        """Bounded wait until the page reads as something to act on: a field, an Apply
        link or a sign-in control, by the same reading an observation uses. A page may
        hold its inputs in the document for a while before it shows them."""
        deadline = time.monotonic() + seconds
        while True:
            with contextlib.suppress(PlaywrightError):
                _frame, data = self.read_form()
                if any(data.get(k) for k in ("fields", "application_links", "auth_controls")):
                    return True
            if time.monotonic() >= deadline:
                return False
            self.page.wait_for_timeout(400)

    def wait_for_child_form(self, timeout_ms: int) -> bool:
        """Bounded wait for a child frame that may hold a form to show a control."""
        main = self.page.main_frame
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for frame in self.page.frames:
                if frame is main or frame.is_detached() or not form_frames.candidate(frame.url):
                    continue
                with contextlib.suppress(PlaywrightError):
                    if frame.evaluate(FIELDS_JS):
                        return True
            if time.monotonic() >= deadline:
                return False
            self.page.wait_for_timeout(200)

    # --- the frame that holds the form ------------------------------------------------

    @property
    def form(self):
        """Where the observed form lives: the child frame chosen for this application, or
        the tab's own document. Every reference an observation hands out belongs to it."""
        held = self.frames.get(self.run["id"]) if self.run else None
        if held and not held[0].is_detached() and held[0].page is self.page:
            return held[0]
        return self.page.main_frame

    def form_frames(self) -> list:
        """The frames that may hold a form, the current form's frame first."""
        main, form = self.page.main_frame, self.form
        others = [
            f
            for f in self.page.frames
            if f is not form
            and (f is main or (not f.is_detached() and form_frames.candidate(f.url)))
        ]
        return [form, *others]

    def child_readings(self) -> list:
        """(frame, reading) for every child frame big enough to show a form."""
        main, found = self.page.main_frame, []
        for frame in self.page.frames:
            if frame is main or frame.is_detached() or not form_frames.candidate(frame.url):
                continue
            try:
                element = frame.frame_element()
                box = element.bounding_box()
                element.dispose()
                if form_frames.big_enough(box):
                    found.append((frame, frame.evaluate(OBSERVE)))
            except PlaywrightError:
                continue
        return found

    def mark_frame(self, frame, alone: bool = True):
        """The iframe that holds the form stands for it in the tab's own document, so a
        pop-up around it is known to be the application and one on top of it is not.

        `alone` drops the references the tab's own document had: the form is the frame's.
        """
        outer = frame
        while outer.parent_frame is not None and outer.parent_frame is not self.page.main_frame:
            outer = outer.parent_frame
        with contextlib.suppress(PlaywrightError):
            element = outer.frame_element()
            element.evaluate(
                "(e,alone)=>{if(alone)e.ownerDocument.querySelectorAll('[data-rove-field]')"
                ".forEach(x=>x.removeAttribute('data-rove-field'));"
                "e.setAttribute('data-rove-field','frame');}",
                alone,
            )
            element.dispose()

    def read_form(self) -> tuple:
        """(frame, reading): the observation script run where the application form is.

        The tab's own document is read first, as it always was; child frames are read only
        when the tab has some. A chosen child frame stays chosen while the tab stays on the
        same address and the frame is still there, so its confirmation page is read from
        it after the send.
        """
        page, main = self.page, self.page.main_frame
        run_id = self.run["id"] if self.run else None
        held = self.frames.pop(run_id, None)
        if held and not held[0].is_detached() and held[1] == page.url:
            try:
                reading = held[0].evaluate(OBSERVE)
                self.frames[run_id] = held
                return held[0], reading
            except PlaywrightError:
                pass
        reading = main.evaluate(OBSERVE)
        if len(page.frames) < 2:
            return main, reading
        children = self.child_readings()
        index = form_frames.choose(reading, [r for _f, r in children])
        if index is None:
            return main, reading
        frame, chosen = children[index]
        self.frames[run_id] = (frame, page.url)
        self.mark_frame(frame)
        return frame, chosen

    def dismiss_consent(self):
        """A cookie banner gets the privacy-preserving choice, found by its own wording."""
        try:
            if not self.page.evaluate(CONSENT_JS):
                return
            self.require_public_page()
            self.click(self.page.locator('[data-rove-consent="1"]').first, timeout=3000)
            self.page.wait_for_function(
                "() => { const b = document.querySelector('[data-rove-consent]');"
                " return !b || !b.getBoundingClientRect().height; }",
                timeout=3000,
            )
        except PlaywrightError:
            pass

    @timing.call("observe", lambda self: self.run["id"])
    def observe(self) -> dict:
        if self.page is None or self.page.is_closed():
            raise ValueError("No live job page. Open a link first.")
        self.require_public_page()  # nothing is read from a page a redirect led off-site
        frame, data = self.read_form()
        main = self.page.main_frame
        if (
            frame is main
            and form_frames.needs_frame(data)
            and len(self.page.frames) > 1
            and self.frames_waited != (self.run["id"], self.page.url)
        ):
            # An employer page whose embedded form is still loading: one bounded wait.
            self.frames_waited = (self.run["id"], self.page.url)
            if self.wait_for_child_form(5000):
                frame, data = self.read_form()
        if frame is not main:
            if frame.url.startswith(("https://", "http://")):
                # Data is typed into the frame: its own address must be public too. A
                # frame written by the page itself (about:srcdoc) is the page's own.
                self.require_public(frame.url)
            # The tab shows the employer's page; the form, its address and everything the
            # observation names belong to the frame.
            data["page_url"] = self.page.url
            data["title"] = self.page.title() or data["title"]
        # A board's honeypot must stay empty: it is never offered as a question.
        data["fields"] = boards.fillable(frame.url, data.get("fields", []))
        self.mark_traps(data, frame)  # optional text fields nobody can see are never filled
        head = data.get("title", "") + "\n" + data.get("text", "")[:3000]
        marker = BLOCK_MARKERS.search(head)
        closed = CLOSED_MARKERS.search(head)
        gone = posting_gone(
            data.get("title"),
            data.get("http_status"),
            data.get("fields"),
            data.get("application_links"),
        )
        data.update(
            url=frame.url,
            run_id=self.run["id"],
            blocked=bool(marker) and not data.get("fields"),
            block_marker=marker.group(0) if marker else None,
            closed=(bool(closed) and not data.get("fields")) or (bool(gone) and not marker),
            closed_marker=closed.group(0) if closed else gone,
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
        form_reading.distinct_keys(data["fields"], workflow.field_key)
        passwords = [f for f in data["fields"] if f["kind"] == "password"]
        intents = {c["intent"] for c in data.get("auth_controls", [])}
        if passwords:
            data["auth_page"] = (
                "register" if len(passwords) >= 2 or "register" in intents else "login"
            )
        # A code the site mailed to the owner's address is Rove's to fetch and type.
        with contextlib.suppress(PlaywrightError):
            data["code_step"] = gates.code_step(
                data.get("text", ""), frame.evaluate(gates.CODE_BOXES_JS)
            )
        # Secrets/identity steps are kept out of saved screenshots and model context.
        if passwords or (
            not data.get("code_step")
            and any(questions.manual_only(f["label"]) for f in data["fields"])
        ):
            data["text"] = data["text"][:1500]
            data["fields"] = [{k: v for k, v in f.items() if k != "value"} for f in data["fields"]]
            data["manual_takeover_required"] = True
        else:
            image = state_root() / f"applications/{self.run['id']}/browser.png"
            try:
                self.page.screenshot(path=str(image), full_page=False)
                image.chmod(0o600)
                data["screenshot"] = str(image)
            except PlaywrightError as error:
                # Evidence, not a precondition: an error page or a renderer that is gone
                # cannot be captured, and the observation is still what the run needs.
                data["screenshot_error"] = type(error).__name__
        write_private(state_root() / f"applications/{self.run['id']}/observation.json", data)
        return data

    def set_checkboxes(self, field: dict, labels: list[str]) -> bool:
        """Leave exactly the named options of a checkbox group checked, and verify each box."""
        for option, ref in zip(field["options"], field["member_refs"], strict=True):
            member = self.form.locator(f'[data-rove-field="{int(ref)}"]')
            wanted = option["label"] in labels
            if member.is_checked() == wanted:
                continue
            self.set_box(member, wanted)
            if member.is_checked() != wanted:
                return False
        return True

    def set_box(self, box, wanted: bool):
        """Tick or clear one checkbox or radio. A styled box hides its input (no size, no
        opacity): its label is the visible target then."""
        try:
            box.set_checked(wanted, timeout=3000)
        except PlaywrightError as error:
            target = box.get_attribute("id")
            label = self.form.locator(f'label[for="{target}"]') if target else None
            if label is not None and label.count() and label.first.is_visible():
                # What sits on top of a styled box is its own label. Its text may hold
                # links (terms, a policy): the click goes to its edge, not to a link.
                label.first.click(position={"x": 4, "y": 4}, timeout=5000)
            elif overlays.blocked_click(error):
                raise  # something else sits on top of the form: cleared, then tried again
            else:
                box.evaluate("e => e.click()")

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

    def open(self, url: str, in_place: bool = False) -> dict:
        """Open an application's posting, or with `in_place` read the tab as it stands
        when it is still open inside that job's application: the owner finished a step
        there himself (a picture check), and a fresh load would undo it."""
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
            self.page, self.run = self.pages[run_id], self.runs[run_id]
            with self.guarded(self.page):
                if in_place and self.inside_application(target):
                    self.settle()
                    return self.observe()
                # A reopened application starts from a fresh load of its page: no
                # half-filled form, no toggled choices, no attached file the site hid
                # its input for.
                self.navigate(target)
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
        self.lost.pop(run_id, None)
        workflow.set_state(run_id, "PREPARING", run_id=run_id)
        workflow.ensure_forum(run_id)
        self.save()
        with self.guarded(self.page):
            self.warm(target)
            self.navigate(target)
            result = self.observe()
        result["feed_lookup"] = lookup_job_link(target)
        if not existing["title"]:
            workflow.set_state(run_id, "PREPARING", title=result["title"][:300])
        workflow.record(run_id, "opened", {"url": self.page.url, "title": result["title"]})
        workflow.flush_events(run_id)
        return result

    def where(self) -> list:
        """The page as it stands before a click: its address and how many controls and
        dialogs it shows. A page that swaps its view changes one of them."""
        return self.form.evaluate(WHERE_JS)

    def inside_application(self, target: str) -> bool:
        """Whether the tab is past the posting and still inside this job's application:
        the same site, an address under the posting's own (its apply steps)."""
        here, there = urlsplit(self.page.url), urlsplit(target)
        return (
            (here.scheme, here.hostname, here.port) == (there.scheme, there.hostname, there.port)
            and here.path.rstrip("/") != there.path.rstrip("/")
            and here.path.startswith(there.path.rstrip("/") + "/")
        )

    def challenge(self, run_id: str) -> dict:
        """Whether the application's tab still shows a picture check, and where the page
        stands, without touching it. For the worker's watch after a CAPTCHA card."""
        page = self.pages.get(run_id)
        if page is None or page.is_closed():
            return {"open": False, "showing": False, "moved": False}
        try:
            frame = page.main_frame
            showing = bool(frame.evaluate(CAPTCHA_SHOWING_JS))
            state = frame.evaluate(WHERE_JS)
        except PlaywrightError:
            return {"open": True, "showing": True, "moved": False}
        held_at = (self.runs.get(run_id) or {}).get("captcha_state")
        return {"open": True, "showing": showing, "moved": bool(held_at) and state != held_at}

    def enter_mailed_code(self, run_id: str) -> bool:
        """Type the code the site just mailed to the owner's application address.

        The code is read from his own mailbox, from the site's own sender, and only mail
        that arrived after this run asked for it. It is typed into the boxes that were
        read as the code's, never logged and never kept. False when no code came, or the
        site did not take it: the step is then the owner's.
        """
        from . import mail

        boxes = self.form.evaluate(gates.CODE_BOXES_JS)
        if not boxes.get("count"):
            return False
        asked = self.run.get("code_asked_at")
        since = (
            datetime.fromisoformat(asked) if asked else datetime.now(UTC) - timedelta(minutes=15)
        )
        senders = gates.code_senders(self.page.url, self.run.get("target_url", ""))
        digits = (boxes["count"], boxes["count"]) if boxes["segmented"] else (4, 8)
        # The employer as this application knows it, for mail from a domain of its own.
        names = [mail.company_name(workflow.get(run_id))]
        code = None
        deadline = time.monotonic() + CODE_WAIT_SECONDS
        while code is None:
            self.beat()
            with contextlib.suppress(mail.ZohoFailure, OSError):
                code = mail.verification_code(senders, since, digits, names)
            if code is not None or time.monotonic() >= deadline:
                break
            self.page.wait_for_timeout(5000)  # the page keeps running while Rove waits
        if not code:
            return False
        state = self.form.evaluate(form_reading.FORM_STATE_JS)
        url = self.form.url
        if boxes["segmented"]:
            for index, digit in enumerate(code[: boxes["count"]]):
                box = self.form.locator(f'[data-rove-code="{index}"]')
                box.click()
                box.press_sequentially(digit, delay=90)
        else:
            self.type_value(self.form.locator('[data-rove-code="0"]'), code)
        go = self.form.evaluate(gates.CODE_GO_JS)
        with self.step_armed():
            if go:
                self.click(self.form.locator('[data-rove-code-go="1"]'))
            self.next_step(state, url)
        self.run["code_asked_at"] = None
        workflow.record(run_id, "mailed_code", {"host": urlsplit(self.page.url).hostname})
        after = self.form.evaluate(gates.CODE_BOXES_JS)
        return not after.get("count")

    def moved_on(self, before: list) -> bool:
        """Whether the page is no longer the one a click was made on."""
        try:
            return bool(self.form.evaluate(MOVED_JS, before))
        except PlaywrightError:
            return True  # the document itself was replaced

    def follow(self, run_id: str, observation_id: str, ref: str) -> dict:
        self.check(run_id)
        if not self.observation or observation_id != self.observation["observation_id"]:
            raise ValueError("Page observation changed; inspect again before following a link")
        if self.form.url != self.observation["url"]:
            raise ValueError("Page URL changed; inspect again")
        item = next((x for x in self.observation["application_links"] if x["ref"] == ref), None)
        if not item:
            raise PermissionError("Only an observed application-start link may be followed")
        locator = self.form.locator(f'[data-rove-link="{int(ref)}"]')
        if locator.count() == 0:
            # The page drew itself again since it was read and dropped the mark. It is
            # read once more, and only the one link of the same words and kind is taken.
            again = self.observe()
            same = [
                x
                for x in again["application_links"]
                if normalized(x["label"]) == normalized(item["label"])
                and x.get("kind") == item.get("kind")
                and x.get("url") == item.get("url")
            ]
            if len(same) != 1:
                raise ValueError("Application link changed")
            item = same[0]
            locator = self.form.locator(f'[data-rove-link="{int(item["ref"])}"]')
        if normalized(locator.inner_text()) != normalized(item["label"]):
            raise ValueError("Application link changed")
        if item["url"]:
            validate_destination(item["url"])
            if urlsplit(item["url"]).hostname != urlsplit(
                self.form.url
            ).hostname and not approved_ats(item["url"]):
                # Another site: only one the owner has let a form be filled on.
                from .submission import fill_hold_words

                hold = fill_hold_words(run_id, item["url"], link=True)
                if hold:
                    raise PermissionError(hold)
        old_pages = list(self.context.pages)
        with self.guarded(self.page):
            before = self.where()
            try:
                self.click(locator)
            except PlaywrightError:
                # A page that swaps itself the moment it is clicked may never tell the
                # driver the click landed. It landed when the page is no longer the one
                # that was clicked: another address, or other controls.
                if not self.moved_on(before):
                    raise
            # A single-page site takes a moment to swap its view: bounded wait for the
            # address or the controls to change. A page with a search box of its own does
            # not count as changed.
            with contextlib.suppress(PlaywrightError):
                self.form.wait_for_function(MOVED_JS, arg=before, timeout=10000)
            self.require_public_page()
        fresh = [p for p in self.context.pages if p not in old_pages]
        if fresh:
            self.remember_tab(fresh[-1])
            # The link opened its own tab, which loaded outside the route guard.
            self.page = fresh[-1]
            self.pages[run_id] = self.page
        with self.guarded(self.page):
            self.page.wait_for_load_state("domcontentloaded", timeout=30000)
            self.require_public_page()
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
                self.wait_for_reading()
                result = self.observe()
        workflow.record(
            run_id, "application_link", {"clicked": item["label"], "url": result["url"]}
        )
        workflow.flush_events(run_id)
        return result

    def check(self, run_id: str):
        """Make this application's own tab the one being driven.

        A tab the daemon lost sight of while the browser lived on (a laptop sleep, a
        dropped DevTools session) is found again: a dead session is replaced, and the
        tab is adopted back from the tabs still open. Only a tab that is really gone is
        reported, in plain words when a closed or silent browser took it.
        """
        page = self.pages.get(run_id)
        if page is None or page.is_closed() or run_id not in self.runs:
            page = self.find_tab(run_id)
        self.page, self.run = page, self.runs[run_id]

    def find_tab(self, run_id: str):
        if not self.alive():
            self.ensure()  # a new session adopts every application's open tab
        page = self.pages.get(run_id)
        if (page is None or page.is_closed() or run_id not in self.runs) and self.context:
            self.pages.pop(run_id, None)
            self.adopt_pages(only=run_id)
            page = self.pages.get(run_id)
        if page is None or page.is_closed() or run_id not in self.runs:
            self.pages.pop(run_id, None)
            if run_id in self.lost:
                raise ValueError(self.lost.pop(run_id))
            # Never act on another application's tab: a missing tab is reopened, not reused.
            raise ValueError("This application's tab is not open; reopen it before continuing")
        return page

    def select_combobox(self, locator, field, value, profile) -> bool:
        """Select a unique visible exact option; text input alone is not selection."""
        if field.get("selected") and option_matches(str(field["selected"]), value):
            # The form already shows this choice (its default, or a parsed resume put it
            # there): it is recorded, never reopened or toggled.
            return True
        if field.get("widget"):
            return self.select_custom(locator, field, value)
        if questions.school_question(field):
            return self.select_school(locator, field, value, profile)
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
        options = self.form.get_by_role("option")
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
                        if city and self.form.evaluate(SUGGESTION_JS, city):
                            found = True
                            break
                    if found:
                        self.click(
                            self.form.locator('[data-rove-suggestion="1"]').first,
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
            # A place picker commits the suggestion into the input, or shows it as the
            # chosen value beside an emptied input: either text is the proof.
            self.page.wait_for_timeout(300)
            committed = normalized(locator.input_value() + " " + locator.evaluate(CHOSEN_JS))
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
                self.form.wait_for_function(
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
        selected = self.form.get_by_role("option", name=expected, exact=True)
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

    def school_suggestions(self, locator, typed: str, school: str, places: list) -> list[str]:
        """Type into a school typeahead and read what it suggests, waiting (up to about
        five seconds) for the search to list the school itself."""
        locator.fill(typed)
        options = self.form.get_by_role("option")
        texts: list[str] = []
        for _ in range(10):
            with contextlib.suppress(PlaywrightError):
                options.first.wait_for(state="visible", timeout=500)
            # Each suggestion with its lines kept: the name first, then whatever the
            # picker shows under it (a country, a website).
            texts = [
                str(t).strip()
                for t in options.evaluate_all(
                    "els => els.slice(0, 300).map(e => e.innerText || e.textContent || '')"
                )
            ]
            found = (
                questions.school_option(texts, school, places)
                if school
                else questions.other_option(texts)
            )
            if found is not None:
                break
        return texts

    def select_school(self, locator, field: dict, value, profile: dict) -> bool:
        """A school typeahead: type the school's name and choose the suggestion that is
        the school (its exact name, or its name with a campus or place after it), never
        the first suggestion for its own sake. If the list has no such school, the form's
        own "Other"; if it has none, the field waits for the owner with the reason."""
        identity = profile.get("identity") or {}
        places = [identity.get("city"), identity.get("state_region")]
        school = str(value)
        with contextlib.suppress(PlaywrightError):
            if normalized(school) and normalized(school) == normalized(locator.input_value()):
                return True  # an earlier fill of this page already chose it
        locator.click()
        texts = self.school_suggestions(locator, school, school, places)
        index = questions.school_option(texts, school, places)
        if index is None:
            other = questions.other_option(texts)
            if other is None:
                texts = self.school_suggestions(locator, "Other", "", places)
                other = questions.other_option(texts)
            if other is None:
                self.picker_diagnostic(field, school, texts, locator)
                with contextlib.suppress(PlaywrightError):
                    locator.fill("")
                    locator.press("Escape")
                self.picker_reason = (
                    f"Your school ({school}) is not in this form's list of schools, and the "
                    "list has no Other choice. Pick it in the recruiting browser."
                )
                return False
            index, self.picked_label = other, questions.option_name(texts[other])
        expected = questions.option_name(texts[index])
        self.form.get_by_role("option").nth(index).click()
        evidence = locator.evaluate(
            "e=>[e.value||'', e.closest('.select__container')?.querySelector("
            "'.select__single-value')?.innerText||'', e.parentElement?.innerText||'',"
            " e.parentElement?.parentElement?.innerText||''].join(' | ')"
        )
        run: dict = self.run or {}
        write_private(
            state_root() / f"applications/{run.get('id')}/dropdown-{field['key']}.json",
            {"expected": expected, "typed": school, "selected_evidence": evidence},
        )
        if normalized(expected) not in normalized(evidence):
            self.picker_reason = "The school list did not keep the choice. Pick it in the browser."
            return False
        with contextlib.suppress(PlaywrightError):
            locator.press("Escape")
        return True

    def select_choice(self, field: dict, label: str) -> bool:
        """Select one observed option of a radio group or button group and verify it."""
        if field["kind"] == "radio_group":
            index = next(i for i, o in enumerate(field["options"]) if o["label"] == label)
            # The reference is the radio, or the role wrapper that stands in for a hidden one.
            member = self.form.locator(f'[data-rove-field="{int(field["member_refs"][index])}"]')
            try:
                member.check(timeout=3000)
            except PlaywrightError as error:
                if overlays.blocked_click(error):
                    raise  # something sits on top of the form: cleared, then tried again
                if not member.evaluate(form_reading.CHECKED_JS):
                    # Styled radios hide the input; its associated label is the visible target.
                    target = member.get_attribute("id")
                    label_for = self.form.locator(f'label[for="{target}"]') if target else None
                    with contextlib.suppress(PlaywrightError):
                        if label_for is not None and label_for.count():
                            label_for.first.click(timeout=3000)
                        else:
                            member.click(timeout=3000, force=True)
            return bool(member.evaluate(form_reading.CHECKED_JS))
        container = self.form.locator(f'[data-rove-choice="{int(field["ref"])}"]')
        button = container.get_by_role("button", name=label, exact=True)
        if button.count() != 1:
            return False
        self.click(button)
        try:
            self.form.wait_for_function(
                CHOICE_PRESSED_JS,
                arg={"ref": field["ref"], "label": label},
                timeout=3000,
            )
        except PlaywrightError:
            return False
        return True

    def select_custom(self, locator, field: dict, value) -> bool:
        """Choose in a dropdown that is not a plain search input.

        The reference is the widget's visible face (a toggle button over a hidden select,
        the box around a covered search input, or an ARIA dropdown with no input), so the
        click that opens it lands where a person's would. Exactly one option may say the
        value, and the choice counts only when the widget then shows it.
        """
        ref = int(field["ref"])
        try:
            self.click(locator, timeout=4000)
        except PlaywrightError as error:
            if overlays.blocked_click(error):
                raise  # something sits on top of the form: cleared, then tried again
            return False
        texts, matches = [], []
        for attempt in range(10):
            texts = self.form.evaluate(form_reading.OPTIONS_JS, ref)
            matches = [i for i, text in enumerate(texts) if option_matches(text, value)]
            if matches:
                break
            if attempt == 2 and field["widget"] == "shell":
                # A long list filters as you type: type the value into its search input
                # with keys only, since another click on the box could close the list.
                with contextlib.suppress(PlaywrightError):
                    entry = locator.locator("input:not([type=hidden])").first
                    entry.evaluate("e => { e.focus(); e.select(); }", timeout=2000)
                    self.page.keyboard.type(str(value), delay=20)
            self.page.wait_for_timeout(250)
        evidence = state_root() / f"applications/{self.run['id']}/dropdown-{field['key']}.json"
        if len(matches) != 1:
            # Private evidence for the next fix: what the dropdown listed.
            write_private(
                evidence,
                {"expected": str(value), "options_seen": texts[:25], "verified": False},
            )
            with contextlib.suppress(PlaywrightError):
                self.page.keyboard.press("Escape")
            return False
        expected, shown = texts[matches[0]], None
        with contextlib.suppress(PlaywrightError):
            self.click(self.form.locator(f'[data-rove-option="{matches[0]}"]'), timeout=4000)
            for _ in range(12):
                shown = self.form.evaluate(form_reading.PICKED_JS, ref)
                if normalized(str(shown or "")) == normalized(expected):
                    break
                self.page.wait_for_timeout(250)
        verified = normalized(str(shown or "")) == normalized(expected)
        write_private(
            evidence,
            {"expected": expected, "selected_evidence": {"value": shown}, "verified": verified},
        )
        return verified

    def upload(self, locator, path):
        """Attach a frozen file, and note what the page starts doing because of it.

        An upload can close a dialog, remove another upload control, or start a résumé
        parse that fills fields in. The requests it starts are tracked so the fill can wait
        for them, and the form is observed again before anything else is typed.
        """
        pending: set = set()

        def started(request):
            if request.method != "GET" or request.resource_type in {"xhr", "fetch"}:
                pending.add(request)

        def ended(request):
            pending.discard(request)

        page = self.page
        page.on("request", started)
        page.on("requestfinished", ended)
        page.on("requestfailed", ended)
        self.upload_watch = (page, pending, started, ended)
        locator.set_input_files(str(path))
        self.form_changed = self.resume_attached = True

    def keep_upload(self, attached: dict):
        """Mark the page that just took the resume, so a later pass on the same page (one
        not loaded again since) knows the attachment is its own."""
        token = secrets.token_hex(8)
        with contextlib.suppress(PlaywrightError):
            self.form.evaluate(UPLOAD_MARK_JS, token)
            self.run["upload"] = {**attached, "token": token}

    def upload_still_on(self, observation: dict) -> dict | None:
        """The resume this run attached, when the page that took it is still the one open
        and offers no place to attach a resume now. A page loaded again has lost the mark
        and shows its upload control, so the file is attached again there."""
        kept = self.run.get("upload") or {}
        if not kept.get("token") or kept.get("sha256") != self.run.get("resume_sha256"):
            return None
        if any(
            f["kind"] == "file" and RESUME_UPLOAD.search(f"{f['label']} {f['name']} {f['id']}")
            for f in observation.get("fields", [])
        ):
            return None
        try:
            same = self.form.evaluate(UPLOAD_MARKED_JS, kept["token"]) is True
        except PlaywrightError:
            return None
        return {k: kept[k] for k in ("label", "source", "sha256")} if same else None

    def settle_form(self, quiet_ms: int = 600, timeout_ms: int = 12000):
        """Bounded wait until the form stops changing under the fill.

        Settled means no request the upload started is still out and the controls, their
        values and the dialogs in front of them read the same for `quiet_ms`.
        """
        watch = getattr(self, "upload_watch", None) or (self.page, set(), None, None)
        page, pending, started, ended = watch
        form = self.form if page is self.page else page.main_frame
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            state, since = None, time.monotonic()
            while time.monotonic() < deadline:
                page.wait_for_timeout(150)
                now = form.evaluate(form_reading.FORM_STATE_JS)
                if now != state or pending:
                    state, since = now, time.monotonic()
                elif (time.monotonic() - since) * 1000 >= quiet_ms:
                    break
        except PlaywrightError:
            pass
        finally:
            if started is not None:
                page.remove_listener("request", started)
                page.remove_listener("requestfinished", ended)
                page.remove_listener("requestfailed", ended)
            self.upload_watch = None

    def present(self, field: dict) -> bool:
        """The field's element is still on the page under the reference it was observed with."""
        if field["kind"] == "choice":
            selector = f'[data-rove-choice="{int(field["ref"])}"]'
        else:
            ref = field["ref"] if field["ref"] is not None else field["member_refs"][0]
            selector = f'[data-rove-field="{int(ref)}"]'
        return self.form.locator(selector).count() > 0

    def fields_to_fill(self, before: dict):
        """One page's fields in form order, kept true while the page changes under the fill.

        A field whose element left the page after an earlier action is skipped, never
        waited on. After that, or after an upload, the form is given time to settle and is
        observed again; the pass goes on with the fields as they now are, and `before`
        learns the new ones so they are not reported as having appeared unasked.
        """
        self.form_changed = self.resume_attached = False
        self.clear_overlays()  # nothing in front of the form before a batch is typed
        handled: dict = {}

        def remaining(fields):
            # Fields are known by key; two that share one are told apart by their order.
            met: dict = {}
            for item in fields:
                met[item["key"]] = met.get(item["key"], 0) + 1
                if met[item["key"]] > handled.get(item["key"], 0):
                    yield item

        queue = list(before["fields"])
        refreshes = 0
        while queue:
            field = queue.pop(0)
            handled[field["key"]] = handled.get(field["key"], 0) + 1
            if field["disabled"] or field["readonly"] or self.present(field):
                yield field
            else:
                self.form_changed = True
            if not self.form_changed or refreshes >= 6:
                continue
            self.form_changed = False
            refreshes += 1
            self.settle_form()
            fresh = self.observe()
            if fresh["url"] != before["url"]:
                raise PermissionError("Page changed before fill")
            known = {f["key"] for f in fresh["fields"]}
            # What was required when the pass began (the site's own rejection included) stays so.
            required = {f["key"] for f in before["fields"] if f["required"]}
            for item in fresh["fields"]:
                item["required"] = item["required"] or item["key"] in required
            before["fields"] = fresh["fields"] + [
                f for f in before["fields"] if f["key"] not in known
            ]
            queue = list(remaining(fresh["fields"]))

    # --- pop-ups, page dialogs and stray tabs ---------------------------------------

    def run_id_of(self, page) -> str | None:
        return next((r for r, p in self.pages.items() if p is page), None) or (
            self.run["id"] if self.run and page is self.page else None
        )

    def note(self, line: str, detail: str):
        """One quiet thread line for the owner and one system-log line, never a card."""
        run_id = self.run["id"] if self.run else None
        if run_id:
            workflow.record(run_id, "overlay", {"line": line, "detail": detail})
            with contextlib.suppress(Exception):  # a log line never breaks the work
                workflow.flush_events(run_id)

    def watch_dialogs(self, page):
        """Answer the page's own dialogs by a fixed policy, once per page.

        An alert is acknowledged. A confirm is accepted only while this application is
        being sent (the site asking "Submit now?" under Rove's own click); any other
        confirm and every prompt is dismissed. A leave-page warning is accepted only while
        Rove itself is driving the page. Each dialog leaves one quiet line, its text
        clipped and scrubbed.
        """
        if getattr(page, "_rove_dialogs", False):
            return
        page._rove_dialogs = True

        def answer(dialog):
            kind, text = dialog.type, overlays.scrubbed(dialog.message)
            run_id = self.run_id_of(page)
            if kind == "alert":
                accept = True
            elif kind == "confirm":
                try:
                    accept = bool(run_id) and workflow.get(run_id)["status"] == "SUBMITTING"
                except Exception:  # noqa: BLE001 -- an unknown run is never being sent
                    accept = False
            elif kind == "beforeunload":
                accept = page is self.page and getattr(self, "operating", 0) > 0
            else:
                accept = False
            try:
                dialog.accept() if accept else dialog.dismiss()
            except PlaywrightError:
                return  # already answered by the owner in the window
            if run_id:
                self.note(
                    f"{'Accepted' if accept else 'Dismissed'} the page's {kind} dialog: “{text}”",
                    f"dialog · {kind} · {'accepted' if accept else 'dismissed'} · {text}",
                )

        page.on("dialog", answer)

    def close_stray_tabs(self):
        """Tabs the page opened on its own (ads, chat, sign-in windows) are closed.

        A tab that belongs to an application, the tab being driven, and anything the owner
        opened by hand (no opener) stay. `follow` has already adopted the apply tab it was
        waiting for by the time the page settles.
        """
        if not self.context:
            return
        for page in list(self.context.pages):
            try:
                if page.is_closed() or page is self.page or page in self.pages.values():
                    continue
                if page.opener() is None:
                    continue
                where = overlays.scrubbed(page.url, 120)
                page.close()
                self.note("Closed a tab the page opened on its own", f"stray tab closed · {where}")
            except PlaywrightError:
                continue

    def find_overlays(self, after_failure: bool = False) -> list[dict]:
        """The pop-ups in the way of the page right now, the one with controls first.

        The tab's own document is searched, and the form's frame when the form lives in
        one; a pop-up found in the frame remembers it, so it is closed there.
        """
        main = self.page.main_frame
        if len(self.page.frames) > 1:
            self.mark_form_frame()
        found = []
        for frame in [main] if self.form is main else [main, self.form]:
            try:
                seen = frame.evaluate(overlays.FIND_JS)
            except PlaywrightError:
                continue
            for overlay in overlays.in_the_way(seen, after_failure):
                if frame is not main:
                    overlay["_frame"] = frame
                found.append(overlay)
        return found

    def mark_form_frame(self):
        """Before pop-ups are judged: the frame holding the form stands for it in the tab's
        own document (see `mark_frame`). Before any frame is chosen, a frame with two
        controls or more, and more than the tab's own document has, is marked beside it."""
        main = self.page.main_frame
        if self.form is not main:
            self.mark_frame(self.form)
            return
        with contextlib.suppress(PlaywrightError):
            own = main.evaluate(COUNT_JS)
            for frame in self.page.frames:
                if frame is main or frame.is_detached() or not form_frames.candidate(frame.url):
                    continue
                with contextlib.suppress(PlaywrightError):
                    count = frame.evaluate(COUNT_JS)
                    if count >= 2 and count > own:
                        self.mark_frame(frame, alone=False)

    def overlay_frame(self, overlay: dict):
        return overlay.get("_frame") or self.page.main_frame

    def overlay_gone(self, overlay: dict, timeout_ms: int = 2500) -> bool:
        try:
            self.overlay_frame(overlay).wait_for_function(
                overlays.GONE_JS, arg=overlay["ref"], timeout=timeout_ms
            )
            return True
        except PlaywrightError:
            return False

    def press_overlay_button(self, overlay: dict, button: dict) -> bool:
        self.require_public_page()
        locator = self.overlay_frame(overlay).locator(
            f'[data-rove-overlay-button="{int(button["ref"])}"]'
        )
        try:
            self.click(locator, timeout=4000)
        except PlaywrightError:
            return False
        return self.overlay_gone(overlay)

    def dismiss_overlay(self, overlay: dict, button: dict | None) -> str | None:
        """Close one pop-up: its control, then Escape, then its backdrop; how, or None."""
        if button is not None and self.press_overlay_button(overlay, button):
            return f"button “{button['label']}”"
        self.require_public_page()
        with contextlib.suppress(PlaywrightError):
            self.page.keyboard.press("Escape")
        if self.overlay_gone(overlay, 1200):
            return "Escape"
        point = None
        with contextlib.suppress(PlaywrightError):
            frame = self.overlay_frame(overlay)
            point = frame.evaluate(overlays.BACKDROP_POINT_JS, overlay["ref"])
            if point and frame is not self.page.main_frame:
                # The frame's own coordinates, moved to where the frame sits in the tab.
                box = frame.frame_element().bounding_box()
                point = [point[0] + box["x"], point[1] + box["y"]] if box else None
        if point:
            self.require_public_page()
            with contextlib.suppress(PlaywrightError):
                self.page.mouse.click(point[0], point[1])
            if self.overlay_gone(overlay, 1200):
                return "its backdrop"
        return None

    def ask_about_overlay(self, overlay: dict) -> dict | None:
        """Qwen picks one of the pop-up's buttons; the choice passes the same never-list.

        The screenshot taken before asking stays in the application directory. None when
        the model is away, refuses, or names something code may not press.
        """
        from . import reasoning

        directory = state_root() / f"applications/{self.run['id']}"
        count = int(self.run.get("popups_asked", 0)) + 1
        self.run["popups_asked"] = count
        with contextlib.suppress(PlaywrightError, OSError):
            shot = directory / f"popup-{count}.png"
            self.page.screenshot(path=str(shot))
            shot.chmod(0o600)
        context = overlays.qwen_context(overlay, self.page.title())
        try:
            generated = reasoning.generate(directory, context, f"popup-{count}", attempts=1)
            parsed = reasoning.load_json(reasoning.completed_response(generated))
        except Exception as error:  # noqa: BLE001 -- away, refused or malformed: all mean "do not guess"
            self.note(
                "Asked Qwen about a pop-up and got no usable answer",
                f"popup · qwen · {type(error).__name__}: {str(error)[:160]}",
            )
            return None
        return overlays.qwen_choice(parsed, overlay)

    def hold_for_overlay(self, overlay: dict):
        """Stop with a screenshot and plain words: the owner closes it and replies go."""
        directory = state_root() / f"applications/{self.run['id']}"
        with contextlib.suppress(PlaywrightError, OSError):
            self.page.screenshot(path=str(directory / "failure.png"))
            (directory / "failure.png").chmod(0o600)
        self.note(
            f"A pop-up is in the way: “{overlays.describe(overlay)}”",
            f"popup held · {overlays.scrubbed(overlay.get('text'), 160)} · buttons "
            + " / ".join(b["label"] for b in overlay.get("buttons", [])[:8]),
        )
        raise overlays.OverlayInTheWay(overlays.HOLD_WORDS)

    def wait_out_loading(self, seconds: float = 15.0):
        """Bounded wait while the page shows its own loading screen over the window: it
        has no words and no buttons, and it leaves by itself."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                seen = self.page.main_frame.evaluate(overlays.FIND_JS)
            except PlaywrightError:
                return
            if not any(o.get("busy") and o.get("covers") for o in seen):
                return
            self.page.wait_for_timeout(300)

    def clear_overlays(self, after_failure: bool = False) -> int:
        """Get pop-ups out of the way of the form; how many were closed.

        Code decides first: the application itself is kept; a step that goes on without
        signing up for anything is taken; anything else is closed with its dismissive
        control, Escape, or its backdrop. Qwen is asked only when the buttons are ones
        code cannot place. A pop-up still in the way afterwards stops the run for the
        owner, unless the page is a site's front door, where nothing is being filled.
        """
        if self.page is None or self.page.is_closed() or not self.run:
            return 0
        self.close_stray_tabs()
        self.wait_out_loading()
        with contextlib.suppress(PlaywrightError):
            if self.page.main_frame.evaluate(CAPTCHA_SHOWING_JS):
                return 0  # a picture check is the owner's: never closed, never clicked
        closed = 0
        # A site's front door is only visited for the sake of the visit: nothing is filled
        # there, so a pop-up that stays is left alone instead of stopping the run.
        front_door = urlsplit(self.page.url).path in ("", "/")
        for _ in range(3):
            found = self.find_overlays(after_failure)
            if not found:
                return closed
            overlay = found[0]
            if int(self.run.get("popups_closed", 0)) >= overlays.MAX_CLOSED_PER_RUN:
                self.hold_for_overlay(overlay)
            choice = overlays.choose(overlay)
            button = choice["button"] if choice else None
            asked = False
            if button is None and overlay.get("buttons") and not front_door:
                button, asked = self.ask_about_overlay(overlay), True
            how = self.dismiss_overlay(overlay, button)
            if how is None:
                if front_door:
                    return closed
                self.hold_for_overlay(overlay)
            closed += 1
            self.run["popups_closed"] = int(self.run.get("popups_closed", 0)) + 1
            self.note(
                f"Closed a pop-up: “{overlays.describe(overlay)}”",
                f"popup closed · {overlays.scrubbed(overlay.get('text'), 120)} · via {how}"
                + (" · chosen by qwen" if asked else ""),
            )
        return closed

    def fill_cleared(self, run_id: str, before: dict, approved: dict, answers: dict):
        """One fill pass; when something on top stops a click or a fill, the pop-up is
        closed, the page observed again and the pass run once more."""
        try:
            return self._fill_page(run_id, before, approved, answers)
        except PlaywrightError as error:
            if not re.search(
                r"intercepts pointer events|not visible|Timeout \d+ms exceeded", str(error)
            ):
                raise
            if not self.clear_overlays(after_failure=True):
                raise
            fresh = self.observe()
            before.clear()
            before.update(fresh)
            return self._fill_page(run_id, before, approved, answers)

    def _auth_control(self, observation: dict, intent: str):
        control = next(
            (c for c in observation.get("auth_controls", []) if c["intent"] == intent), None
        )
        if control is None:
            raise ValueError(f"No {intent} control is visible on this page")
        return self.form.locator(f'[data-rove-auth="{int(control["ref"])}"]'), control

    def register(self, run_id: str) -> dict:
        """Create the employer account the owner approved: email, generated password, nothing else."""
        from . import credentials

        self.check(run_id)
        with self.guarded(self.page):
            before = self.observe()
            if before.get("auth_page") != "register":
                raise ValueError("This page is not an account-creation form")
            if not approved_ats(before["url"]) and (
                urlsplit(before["url"]).hostname != urlsplit(self.run["target_url"]).hostname
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
                    or field.get("hidden_trap")
                    or field["kind"] in {"file", "hidden", *GROUP_KINDS}
                ):
                    continue
                locator = self.form.locator(f'[data-rove-field="{int(field["ref"])}"]')
                label = normalized(field["label"] + " " + field["name"])
                if field["kind"] == "password":
                    self.fill_secret(locator, password)
                    filled.append("password" if "confirm" not in label else "password confirmation")
                elif field["kind"] == "email" or (field["kind"] == "text" and "email" in label):
                    locator.fill(email)
                    filled.append(field["label"] or "email")
                elif terms_box(field):
                    locator.check()
                    filled.append("accepted: " + (field["label"] or "terms")[:80])
                elif field["kind"] in {"text", "tel"} and not field.get("label_missing"):
                    value, _source = resolve_known(field["label"], profile)
                    if value:
                        self.type_value(locator, value)
                        filled.append(field["label"])
                pace(0.2, 0.6)
            locator, control = self._auth_control(before, "register")
            # Stored before the click: whatever the site does next, the password is not lost.
            credentials.store(host, email, password, run_id)
            self.click(locator)
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=30000)
            except PlaywrightError:
                pass
            self.require_public_page()
            self.settle()
            after = self.observe()
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
                if (
                    field["disabled"]
                    or field.get("hidden_trap")
                    or field["kind"] in {"file", "hidden", *GROUP_KINDS}
                ):
                    continue
                locator = self.form.locator(f'[data-rove-field="{int(field["ref"])}"]')
                label = normalized(field["label"] + " " + field["name"])
                if field["kind"] == "password":
                    self.fill_secret(locator, account["password"])
                elif field["kind"] == "email" or re.search(r"email|user ?name", label):
                    locator.fill(account["username"])
            locator, control = self._auth_control(before, "login")
            self.click(locator)
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=30000)
            except PlaywrightError:
                pass
            self.require_public_page()
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
                    field["kind"] in ("text", "email", "tel", "url", "textarea", *dates.DATE_KINDS)
                    and entry.get("control") != "combobox"
                    and not (dates.same_date if dates.is_date_box(field) else same_value)(
                        entry["value"], field["value"]
                    )
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
            # Twins (two questions in the same words) cannot be told apart by what is
            # remembered by their words, and a later twin takes only its own answer.
            # Which education block each school question is in, for the page as it is now;
            # a repeated education question is the next school's, not a twin.
            questions.number_education_blocks(before["fields"])
            if field.get("occurrence") and not questions.education_field(field):
                return None, None
            return questions.resolve(
                field,
                approved["profile"],
                recall=None
                if field.get("twins") or field.get("occurrence")
                else workflow.recall_answer,
                employer=employer,
                picker=picker,
            )

        grouped = {f["name"] for f in before["fields"] if f["kind"] == "radio_group"}
        for field in self.fields_to_fill(before):
            if field["disabled"] or field["readonly"] or field.get("hidden_trap"):
                continue
            if self.form.url != before["url"]:
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
            locator = self.form.locator(f'[data-rove-field="{int(field["ref"])}"]')
            if field["kind"] == "file":
                # Resume uploads are a separate, explicit preparation action, using a
                # frozen file only. Other requested files always remain unresolved.
                if not RESUME_UPLOAD.search(f"{field['label']} {field['name']} {field['id']}"):
                    if field["required"]:
                        pending.append(
                            {
                                "label": field["label"],
                                "key": field["key"],
                                "reason": "Unapproved required file requested",
                            }
                        )
                    continue
                if self.resume_attached and not field["required"]:
                    continue  # a second way to attach the resume this page already has
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
                accept = upload_control.evaluate("e=>e.getAttribute('accept')||''")
                if not accepts_pdf(accept):
                    # The site takes other file types only: nothing is attached, the owner decides.
                    pending.append(
                        {
                            "label": field["label"],
                            "key": field["key"],
                            "required": field["required"],
                            "reason": f"This upload takes only {accepted_words(accept)} files, "
                            "and the approved resume is a PDF",
                        }
                    )
                    continue
                if upload_control.evaluate("e=>e.files.length===1&&e.files[0].name==='resume.pdf'"):
                    self.resume_attached = True  # a pass run again: the file is already on
                else:
                    self.upload(locator, resume)
                if (
                    upload_control.evaluate(
                        "e=>e.files.length===1 && e.files[0].name==='resume.pdf'"
                    )
                    is not True
                ):
                    raise ValueError("Resume attachment verification failed")
                attached = {
                    "label": field["label"],
                    "source": "frozen Erga job resume"
                    if self.run.get("resume_is_tailored")
                    else "frozen approved base resume",
                    "sha256": self.run["resume_sha256"],
                }
                filled.append(attached)
                self.keep_upload(attached)
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
                    options = self.form.get_by_role("option")
                    options.first.wait_for(state="visible", timeout=3000)
                    choices = options.all_text_contents()[:300]
                except PlaywrightError:
                    pass
                locator.press("Escape")
            if value is None and choices and unread_picker:
                # The options are known now: resolve again against them.
                value, source = resolve({**field, "options": [{"label": c} for c in choices]})
            if field["role"] == "combobox" and value is not None:
                self.picked_label = self.picker_reason = None  # set by a school picker
                if self.select_combobox(locator, field, value, approved["profile"]):
                    filled.append(
                        {
                            "label": field["label"],
                            "value": self.picked_label or value,
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
                        "reason": self.picker_reason or "No unique matching dropdown option",
                    }
                )
                continue
            if field["tag"] == "select" and value is None and dates.part_of(field["options"]):
                # A month, day or year dropdown answers part of a date question: the
                # approved date for that question picks its option.
                asked = dates.question_label(field["label"])
                if asked:
                    value, source = resolve(
                        {**field, "label": asked, "options": [], "kind": "text"}
                    )
            if field["tag"] == "select" and value is not None:
                options = [x for x in field["options"] if option_matches(x["label"], value)]
                by_date = None if options else dates.option_for(field["options"], value)
                if by_date:
                    options = [by_date]
                if len(options) == 1:
                    locator.select_option(value=options[0]["value"])
                    if locator.input_value() != options[0]["value"]:
                        raise ValueError("Selection verification failed")
                    filled.append(
                        {
                            "label": field["label"],
                            "value": by_date["label"] if by_date else value,
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
                if locator.is_checked() != desired:
                    self.set_box(locator, desired)
                if locator.is_checked() != desired:
                    raise ValueError("Selection verification failed")
                filled.append(
                    {"label": field["label"], "value": value, "source": source, "key": field["key"]}
                )
                continue
            if value is not None and dates.is_date_box(field):
                written = dates.for_input(value, field)
                if written is None:
                    pending.append(
                        {
                            "label": field["label"],
                            "required": field["required"],
                            "key": field["key"],
                            "options": choices,
                            "reason": "The date could not be written the way this field takes it",
                            **questions.unlabeled(field),
                        }
                    )
                    continue
                value = written
            if (
                field["kind"]
                not in ("text", "email", "tel", "url", "textarea", "number", *dates.DATE_KINDS)
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
            dated = dates.is_date_box(field)
            same = dates.same_date if dated else same_value
            if field["value"] and not same(value, field["value"]) and not owner_answer:
                pending.append(
                    {
                        "label": field["label"],
                        "key": field["key"],
                        "required": field["required"],
                        "reason": "Existing value differs; preserved for review",
                    }
                )
                continue
            if field["value"] and same(value, field["value"]):
                filled.append(
                    {"label": field["label"], "value": value, "source": source, "key": field["key"]}
                )
                continue
            kept = self.write(locator, field, value)
            if not dated and not same_value(value, kept) and kept and value.startswith(kept):
                # The field silently keeps only its first N characters: fit the text to it.
                value = workflow.brief(value, len(kept))
                kept = self.write(locator, field, value)
                source = f"{source} (shortened to {len(kept)} characters)"
            if not same(value, kept):
                raise ValueError(f"Field verification failed: {field['label'][:80]}")
            filled.append(
                {"label": field["label"], "value": value, "source": source, "key": field["key"]}
            )
        return questions.settled_page(before["fields"], filled, pending)

    def _prepare(self, run_id: str) -> dict:
        from .submission import (
            duplicate_words,
            embedded_form,
            fill_hold_words,
            success_phrases,
        )

        before = self.observe()
        target = self.run.get("target_url", workflow.get(run_id)["url"])
        # The host must be a board or one the owner let a form be filled on, and the page
        # must be the job that was opened; a shared ATS hostname is neither. A board's form
        # inside the employer's page counts when the board's job id is the queued job's.
        hold = fill_hold_words(run_id, before["url"])
        if hold or not (
            same_job(target, before["url"])
            or embedded_form(run_id, target, before.get("page_url"), before["url"])
        ):
            return {
                **before,
                "status": "NEEDS_EMPLOYER_LINK",
                "reason": owner_words(hold)
                or "The form must match the verified employer and job destination before "
                "entering candidate data. A shared ATS hostname is insufficient.",
            }
        sent = duplicate_words(run_id, before["url"])
        if sent:
            # The same job went out through another link (its board, or its employer page).
            return {**before, "status": "ALREADY_SENT_ELSEWHERE", "reason": owner_words(sent)}
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
        # `landed` is the last page reached, for what it says about itself (an assessment).
        result = landed = before
        moved = False
        sent_on_step = ""
        captcha = False
        for _step in range(FORM_PAGES):
            if before.get("code_step"):
                # The site mailed a code to prove the address: fetched and typed, then
                # the page after it is filled like any other.
                if not self.enter_mailed_code(run_id):
                    pending.append(
                        {
                            "label": "The code the site mailed you",
                            "key": "mailed-code",
                            "required": True,
                            "reason": gates.NO_CODE_WORDS,
                            "manual": True,
                        }
                    )
                    result = landed = before
                    break
                before = result = landed = self.observe()
                continue
            pages.append(before["url"])
            page_filled, page_pending = self.fill_cleared(run_id, before, approved, answers)
            filled.extend(page_filled)
            pending.extend(page_pending)
            self.run.update(status="NEEDS_REVIEW", filled=filled, pending=pending)
            self.save()
            result = landed = self.observe()
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
            state = self.form.evaluate(form_reading.FORM_STATE_JS)
            self.run["code_asked_at"] = workflow.now()  # a code mailed from here on is this step's
            with self.requests_heard() as heard, self.step_armed():
                self.click(self.form.locator(f'[data-rove-nav="{int(nav[0]["ref"])}"]'))
                self.next_step(state, result["url"])
            before = landed = self.observe()
            workflow.record(run_id, "form_step", {"clicked": nav[0]["label"], "url": before["url"]})
            if self.form.evaluate(CAPTCHA_SHOWING_JS):
                # The step put a picture check on screen: the owner's, never Rove's.
                # Where the page stands is kept, to tell later that it moved on.
                captcha = True
                self.run["captcha_state"] = self.where()
                self.run["captcha_after"] = str(nav[0]["label"])[:60]
                self.save()
                result = landed = before
                break
            if (
                not before["fields"]
                and not before.get("final_controls")
                and success_phrases(before.get("text")) - success_phrases(result.get("text"))
            ):
                # The site took the application on a step that only said to go on. That
                # is never counted as sent by a guess: the owner looks and says.
                sent_on_step = nav[0]["label"]
                break
            if before["url"] in pages and before["fields"] == result["fields"]:
                # The step did not move: what the site was asked and answered is kept
                # for the system log, without a query string or a body.
                write_private(directory / "step-stuck.json", {"requests": heard[-40:]})
                answered = ", ".join(str(h.get("status") or h.get("failed")) for h in heard[-6:])
                workflow.system_line(
                    run_id,
                    f"step did not move · {nav[0]['label']} · {len(heard)} requests"
                    + (f" · {answered}" if answered else ""),
                )
            if urlsplit(before["url"]).hostname != urlsplit(pages[0]).hostname:
                # The step left the site the form was verified on: nothing more is typed.
                moved = True
                break
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
            if not stuck and form_reading.review_step(before):
                # The last step shows the answers back with Edit links and Submit: done.
                pages.append(before["url"])
                result = before
                break
            if not before["fields"] or stuck:
                break
        if not any(item.get("sha256") for item in filled):
            # A pass run again on a page that took the resume earlier and no longer shows
            # a place for one: the same attachment, on record for the package.
            kept = self.upload_still_on(result)
            if kept:
                filled.append(kept)
        self.run["pending"] = pending
        self.save()
        package = {
            "run_id": run_id,
            "url": self.form.url,
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
            step = form_reading.owner_step(landed)
            if captcha:
                words = gates.captcha_words(self.run.get("captcha_after", ""))
                result = {**result, "captcha": True, "reason": words}
            elif sent_on_step:
                result = {
                    **result,
                    "owner_step": True,
                    "headline": "The site may have taken the application",
                    "reason": (
                        f"After I pressed “{sent_on_step}” the site showed what reads as its "
                        "application-received page, before the send step. I did not count it "
                        "as sent. Check the recruiting browser: reply `applied` if it went "
                        "through, or `park it`."
                    ),
                }
            elif step:
                # A video interview or an assessment: named, with its link, for the owner.
                result = {**result, **step, "owner_step": True}
            else:
                result = {
                    **result,
                    "reason": (
                        "A step of the form moved to another site, so nothing more was typed. "
                        "Check the recruiting browser, then resume."
                        if moved
                        else f"The site rejected a value on this step: {error}. Check it in the "
                        "recruiting browser, then resume."
                        if error
                        else "The form's last step with its Submit control was not reached. "
                        "Check the recruiting browser, then resume."
                    ),
                }
        return {**result, **package, "status": status}

    @contextlib.contextmanager
    def step_armed(self):
        """Lift the preparation guard for one observed click on a form's Next control.

        A site may wire its Next as a submit of the step's own form; the guard that stops
        accidental submits during preparation would swallow it and the step would never
        move. Only a control read as one that goes on is clicked under this, never the
        final one; the guard is back the moment the step has been taken.
        """
        with contextlib.suppress(PlaywrightError):
            self.form.evaluate(ARM_JS)
        try:
            yield
        finally:
            with contextlib.suppress(PlaywrightError):
                self.form.evaluate(DISARM_JS)

    @contextlib.contextmanager
    def requests_heard(self):
        """The data requests the page makes while a step is taken: method, kind, path and
        how each ended. For telling a site that refused from a click that did nothing."""
        heard: list[dict] = []
        page = self.page

        def path(url: str) -> str:
            parts = urlsplit(url)
            return f"{parts.hostname}{parts.path}"[-140:]

        def asked(request):
            if request.resource_type in {"xhr", "fetch", "document"}:
                heard.append(
                    {
                        "method": request.method,
                        "kind": request.resource_type,
                        "path": path(request.url),
                    }
                )

        def answered(response):
            for entry in reversed(heard):
                if entry["path"] == path(response.url) and "status" not in entry:
                    entry["status"] = response.status
                    return

        def failed(request):
            for entry in reversed(heard):
                if entry["path"] == path(request.url) and "status" not in entry:
                    entry["failed"] = str(request.failure or "failed")[:80]
                    return

        page.on("request", asked)
        page.on("response", answered)
        page.on("requestfailed", failed)
        try:
            yield heard
        finally:
            for name, handler in (
                ("request", asked),
                ("response", answered),
                ("requestfailed", failed),
            ):
                with contextlib.suppress(Exception):
                    page.remove_listener(name, handler)

    def next_step(self, state: str, url: str, timeout_ms: int = 12000):
        """After a Next click: wait until the step changed (another address, or other
        controls and values), then for a loading indicator to go and for the new step's
        fields or its final control. A step that shows neither (a video interview, an
        assessment) is read after a short bound, not the ten seconds a fresh page gets."""
        deadline = time.monotonic() + timeout_ms / 1000
        loaded = False
        while time.monotonic() < deadline:
            self.page.wait_for_timeout(100)
            try:
                if self.form.url != url:
                    loaded = True
                    break
                if self.form.evaluate(form_reading.FORM_STATE_JS) != state:
                    break
            except PlaywrightError:
                loaded = True  # the document is being replaced: the step changed
                break
        with contextlib.suppress(PlaywrightError):
            if loaded:
                self.form.wait_for_load_state("domcontentloaded", timeout=15000)
        self.dismiss_consent()
        self.clear_overlays()
        with contextlib.suppress(PlaywrightError):
            self.form.wait_for_function(BUSY_JS, timeout=10000)
        self.wait_for_fields(8000 if loaded else 4000, script=STEP_JS)


def socket_path() -> Path:
    return state_root() / "browser.sock"


def announce_app(settings: dict) -> str | None:
    """At daemon start: one warning line when the shared Google Chrome is configured."""
    if settings.get("browser_app") != browser_app.SHARED_SETTING:
        return None
    warning = (
        "the recruiting browser is the shared Google Chrome (browser_app: shared-chrome): "
        "a Dock click or a link from another app can open in the recruiting profile; "
        "run `rove browser install` and drop the setting to use the Rove Browser"
    )
    print("warning: " + warning, file=sys.stderr, flush=True)
    workflow.system_line("browser", "warning · " + warning)
    return warning


def status_report(browser) -> dict:
    app = browser_app.status(workflow.config())
    running = browser.launcher.running_app()
    return {
        "daemon_running": True,
        "browser_connected": browser.connected(),
        "browser_running": running is not None,
        "app": {**app, "running": running is not None and running == app["path"]},
        "open_tabs": sorted(r for r, page in browser.pages.items() if not page.is_closed()),
        "run_id": browser.run["id"] if browser.run else None,
    }


# Actions that need the browser; it is launched by the first of them, not at login.
BROWSER_ACTIONS = {"open", "observe", "follow", "prepare", "register", "login", "reopen", "submit"}


def checked_run_id(request: dict) -> str | None:
    """Application ids are twelve hex characters; anything else never reaches a path."""
    run_id = request.get("run_id")
    if run_id is None:
        return None
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("Malformed application id")
    return run_id


def handle_request(browser, request: dict):
    action = request["action"]
    if action == "status":
        if not request.get("peek"):  # a peek never reaches for the browser
            browser.attach_if_running()
        return status_report(browser)
    if action == "close":
        # Closing a tab touches no path: the id only names an entry to drop.
        browser.attach_if_running()
        return browser.close_run(request["run_id"])
    if action == "challenge":
        # A look at a tab that is already open; a browser that is not running has none.
        browser.attach_if_running()
        return browser.challenge(str(checked_run_id(request)))
    if action == "hand_over":
        # The owner takes a step in a tab that is already open; nothing is typed or read.
        browser.attach_if_running()
        return browser.hand_over(str(checked_run_id(request)))
    if action not in BROWSER_ACTIONS:
        raise PermissionError("Unsupported browser action")
    run_id = checked_run_id(request)
    # The browser starts with the first call that needs it. One the owner quit is noticed
    # here and reached again once; an operation it dies under is run once more, except a
    # send or an account creation, whose click may already have gone out. A tab that did
    # not survive says so plainly.
    browser.ensure()
    operation = lambda: perform(browser, action, run_id, request)
    if action in RETRIED_ACTIONS:
        return browser.recovering(operation)
    return browser.recovering(operation, retry=False, words=lambda: cut_words(action, run_id))


def cut_words(action: str, run_id: str | None) -> str:
    """What to tell the owner about a step the browser died under and Rove did not repeat.

    A send that never got as far as its claim sent nothing; one that did has its outcome
    on record already, and anything unsure says a click may have gone out.
    """
    if action == "submit" and run_id:
        with contextlib.suppress(Exception):
            if workflow.get(run_id)["status"] == "READY_FOR_REVIEW":
                return SEND_CUT
    return STEP_CUT


def perform(browser, action: str, run_id: str | None, request: dict):
    if action == "open":
        # Read in place only when asked: a plain open is called as it always was.
        how = {"in_place": True} if request.get("in_place") else {}
        return browser.open(request["url"], **how)
    if action == "observe":
        if run_id:
            browser.check(run_id)
        return browser.observe()
    if run_id is None:
        raise ValueError("This browser action needs an application id")
    if action == "follow":
        return browser.follow(run_id, request["observation_id"], request["ref"])
    if action == "prepare":
        return browser.prepare(run_id)
    if action == "register":
        return browser.register(run_id)
    if action == "login":
        return browser.login(run_id)
    if action == "reopen":
        return browser.reopen(run_id)
    if action == "submit":
        # Only the worker calls this, with an authenticated owner approval
        # for one exact package; the model has no submit tool.
        from .submission import submit

        return submit(browser, run_id, request["package_hash"], request["owner_message_id"])
    raise PermissionError("Unsupported browser action")


def failure_screenshot(browser, run_id: str | None):
    """Private evidence of what the tab showed when an action failed, inside the state root."""
    if not run_id or not RUN_ID.fullmatch(run_id):
        return
    root = (state_root() / "applications").resolve()
    directory = (root / run_id).resolve()
    if directory.parent != root or not directory.is_dir():
        return
    page = browser.pages.get(run_id)  # this application's own tab, never another one's
    if page is None or page.is_closed():
        return
    with contextlib.suppress(Exception):
        page.screenshot(path=str(directory / "failure.png"))
        (directory / "failure.png").chmod(0o600)


def respond(browser, raw: bytes) -> dict:
    """One request line in, one response out. Errors are serialized without secrets."""
    from . import credentials

    request = None
    try:
        if len(raw) > 32768:
            raise ValueError("Request too large")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise TypeError("Malformed request")
        return {"result": handle_request(browser, request)}
    except Exception as error:  # noqa: BLE001 -- serialize failures at the IPC boundary
        # A failed fill quotes the value it was typing; the text leaves this process for
        # error.json, the system log and cards, so credentials and typed values come out.
        text = credentials.scrub(str(error), getattr(browser, "secrets", ()))
        run_id = request.get("run_id") if isinstance(request, dict) else None
        failure_screenshot(browser, run_id if isinstance(run_id, str) else None)
        return {"error": text[:800], "error_type": type(error).__name__}


def serve():
    os.umask(0o077)
    lock = open(state_root() / "browser-daemon.lock", "a")  # noqa: SIM115 -- lifetime of service
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    socket_path().unlink(missing_ok=True)
    browser = RecruitingBrowser()
    settings = workflow.config()
    announce_app(settings)
    # The browser starts with the first call that needs it, not at login. A recruiting
    # browser of another app than the configured one is closed now, so the switch happens
    # at the restart and the profile is free when the configured app opens it. A copy that
    # fell behind the owner's Chrome is rebuilt while nothing runs.
    configured = browser_app.status(settings)
    if configured["path"]:
        with contextlib.suppress(Exception):
            browser.launcher.retire_other_apps(configured["path"])
    if not configured["shared_chrome"]:
        try:
            rebuilt = browser_app.refresh()
        except Exception as error:  # noqa: BLE001 -- the next launch reports it again
            rebuilt = f"the Rove Browser could not be rebuilt: {error}"[:400]
        if rebuilt:
            print(rebuilt, file=sys.stderr, flush=True)
            workflow.system_line("browser", rebuilt)

    service = Service(browser)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            response = service.answer(self.rfile.readline(32769))
            self.wfile.write((json.dumps(response) + "\n").encode())

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    with Server(str(socket_path()), Handler) as server:
        socket_path().chmod(0o600)
        threading.Thread(target=server.serve_forever, name="rove-socket", daemon=True).start()
        service.run()  # the browser is driven from this thread only


class Service:
    """The browser daemon's requests: one thread drives the browser, the others answer.

    Patchright's sync API belongs to the thread that started it, so every request that
    touches the browser runs on the service's own thread, one at a time, in arrival order.
    Each connection is read on a thread of its own, so a `status` or a `close` asked while a
    long `prepare` runs does not wait behind it: status answers from the report taken after
    the last request, with what is running now; close is done the moment the running
    request ends, before anything queued after it, and answers at once. `peek` is a status
    that never waits and never reaches the browser, for `rove doctor`.
    """

    WAIT = 0.5  # seconds a status or close waits for an idle browser first

    def __init__(self, browser):
        import queue

        self.browser = browser
        self.jobs: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.busy: dict | None = None
        self.report: dict = {}
        self.closing: list[str] = []

    @staticmethod
    def parsed(raw: bytes) -> dict:
        try:
            request = json.loads(raw) if raw and len(raw) <= 32768 else None
        except ValueError:
            return {}
        return request if isinstance(request, dict) else {}

    def answer(self, raw: bytes) -> dict:
        """One request from a connection thread; the browser work happens on `run`."""
        from concurrent.futures import Future

        request = self.parsed(raw)
        action = request.get("action")
        if action == "status" and request.get("peek"):
            return {"result": self.snapshot()}
        job: Future = Future()
        self.jobs.put((raw, job))
        if action in {"status", "close"}:
            try:
                return job.result(timeout=self.WAIT)
            except TimeoutError:
                if job.cancel():  # still waiting behind a long request: answer around it
                    return self.around(action, request)
        return job.result()

    def around(self, action: str, request: dict) -> dict:
        if action == "status":
            return {"result": self.snapshot()}
        try:
            run_id = checked_run_id(request)
        except ValueError as error:
            return {"error": str(error), "error_type": type(error).__name__}
        if run_id is None:
            return {
                "error": "This browser action needs an application id",
                "error_type": "ValueError",
            }
        from concurrent.futures import Future

        with self.lock:
            self.closing.append(run_id)
            after = (self.busy or {}).get("action")
        self.jobs.put((None, Future()))  # the browser thread closes it as soon as it is free
        return {"result": {"closed": run_id, "after": after}}

    def snapshot(self) -> dict:
        """The last report, the running request, and the app as configured now. Nothing
        here touches the browser itself."""
        with self.lock:
            report, busy = dict(self.report), dict(self.busy or {})
        app = browser_app.status(workflow.config())
        try:
            running = self.browser.launcher.running_app()
        except Exception:  # noqa: BLE001 -- unknown is reported as not running
            running = None
        report.update(
            daemon_running=True,
            browser_running=running is not None,
            app={**app, "running": running is not None and running == app["path"]},
        )
        report.setdefault("browser_connected", None)
        report.setdefault("open_tabs", [])
        report.setdefault("run_id", None)
        if busy:
            report["busy"] = {
                "action": busy.get("action"),
                "seconds": round(time.monotonic() - busy.get("since", time.monotonic()), 1),
            }
        return report

    def run(self):
        while True:
            self.step()

    def step(self):
        """Take one request off the queue and serve it on this thread."""
        raw, job = self.jobs.get()
        if not job.set_running_or_notify_cancel():
            return
        response: dict = {"result": None}
        if raw is not None:
            request = self.parsed(raw)
            with self.lock:
                self.busy = {"action": request.get("action"), "since": time.monotonic()}
            try:
                response = respond(self.browser, raw)
            except Exception as error:  # noqa: BLE001 -- the caller gets an answer, always
                response = {
                    "error": "The browser service failed",
                    "error_type": type(error).__name__,
                }
        self.settle()
        job.set_result(response)

    def settle(self):
        """After each request: the closes asked meanwhile, then a fresh report."""
        with self.lock:
            closing, self.closing = self.closing, []
        for run_id in closing:
            with contextlib.suppress(Exception):
                self.browser.close_run(run_id)
        try:
            report = status_report(self.browser)
        except Exception:  # noqa: BLE001 -- a report is for status only
            report = {}
        with self.lock:
            self.report, self.busy = report, None


# How long a caller waits for the daemon's reply, per action, in seconds. Long enough for
# the daemon's own bounds: a wizard of eight pages with pickers, a send and its 45-second
# confirmation wait, a blocked site's 12-30 s pause before its retry. Short for the
# actions that answer at once, so a status check does not hang behind a long fill.
CALL_SECONDS = {
    "open": 240,
    "reopen": 300,
    "follow": 240,
    "observe": 90,
    "register": 240,
    "login": 240,
    "prepare": 900,
    "submit": 300,
    "status": 20,
    "close": 30,
}


def read_reply(client, seconds: float) -> bytes:
    """The daemon's reply: one line of any length, within `seconds` in all."""
    deadline = time.monotonic() + seconds
    chunks = []
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("The browser service did not answer in time")
        client.settimeout(left)
        chunk = client.recv(1 << 16)
        if not chunk:
            break
        end = chunk.find(b"\n")
        if end >= 0:
            chunks.append(chunk[:end])
            break
        chunks.append(chunk)
    return b"".join(chunks)


@timing.call("browser")
def browser_call(action: str, **kwargs) -> dict:
    def connect():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(10)
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
        result = json.loads(read_reply(client, CALL_SECONDS.get(action, 240)))
    if "error" in result:
        raise BrowserError(result["error"], str(result.get("error_type") or ""))
    return result["result"]
