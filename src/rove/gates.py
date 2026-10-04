"""Steps a board puts in front of its form that are not questions.

Two of them, and who takes each:

- A picture check (a CAPTCHA). Only the owner passes it, in the recruiting browser. Rove
  never solves one and never works around one: it says so on one card, leaves the tab as
  it is, and carries on by itself in that same tab once the check is gone and the page
  has moved on.
- A code the site mails to the owner's application address to prove the address is his.
  Rove reads it from his own mailbox, from the site's own sender only, and types it. A
  code sent by text message or made by an authenticator app is never Rove's to enter.

Everything here is a rule or a page script; the browser service and the worker call it.
"""

import re
from urllib.parse import urlsplit

# A page that says a code was sent and asks for it.
CODE_PAGE = re.compile(
    r"\b(?:verification|security|confirmation|one[- ]time|access)\s+(?:code|pin|passcode)\b"
    r"|\benter (?:the|your) (?:\d[- ]digit )?(?:code|pin|passcode)\b"
    r"|\b(?:code|pin|passcode)\b[^.]{0,60}\b(?:sent|emailed|e-mailed|mailed)\b"
    r"|\b(?:sent|emailed|e-mailed|mailed)\b[^.]{0,60}\b(?:code|pin|passcode)\b",
    re.IGNORECASE,
)
# A code that came another way is the owner's own step.
OTHER_CHANNEL = re.compile(
    r"\btext message\b|\bsms\b|\bauthenticator\b|\bauthentication app\b|\bphone (?:number|call)\b"
    r"|\bwhatsapp\b|\bsecurity key\b",
    re.IGNORECASE,
)

# The boxes a code goes into: one box, or one box per digit. Each is marked so the
# browser types into exactly what was read. Returns how many, in page order.
CODE_BOXES_JS = r"""() => {
  const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  document.querySelectorAll('[data-rove-code]').forEach(e => e.removeAttribute('data-rove-code'));
  const named = e => [e.id, e.name, e.getAttribute('aria-label'), e.getAttribute('autocomplete'),
    e.getAttribute('class'), e.getAttribute('placeholder'),
    ...[...(e.labels || [])].map(l => l.innerText)].join(' ').toLowerCase();
  const CODE = new RegExp('one-time-code|pin[-_ ]?code|verification[-_ ]?code|' +
    'security[-_ ]?code|passcode|\\botp\\b|\\bcode\\b');
  const boxes = [...document.querySelectorAll('input')].filter(e =>
    visible(e) && !e.disabled && !e.readOnly &&
    ['text', 'tel', 'number', 'password', '']
      .includes((e.getAttribute('type') || '').toLowerCase()) &&
    CODE.test(named(e)));
  const segmented = boxes.length >= 4 && boxes.length <= 8;
  if (!(boxes.length === 1 || segmented)) return {count: 0, segmented: false};
  boxes.forEach((e, i) => e.setAttribute('data-rove-code', String(i)));
  return {count: boxes.length, segmented};
}"""

# The control that takes the typed code: marked when exactly one reads as it.
CODE_GO_JS = r"""() => {
  const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  document.querySelectorAll('[data-rove-code-go]')
    .forEach(e => e.removeAttribute('data-rove-code-go'));
  const GO = /^(?:verify|verify code|confirm|confirm code|continue|next|submit|submit code)$/i;
  const found = [...document.querySelectorAll('button,input[type=submit],[role=button]')]
    .filter(e => visible(e) && !e.disabled && GO.test((e.innerText || e.value || '').trim()));
  if (found.length !== 1) return '';
  found[0].setAttribute('data-rove-code-go', '1');
  return (found[0].innerText || found[0].value || '').trim();
}"""

# Job boards whose mail comes from a domain other than the one their forms are on.
BOARD_SENDERS = {
    "oraclecloud.com": ("oracle.com",),
    "greenhouse.io": ("greenhouse-mail.io",),
    "myworkdayjobs.com": ("myworkday.com", "workday.com"),
}


def code_step(text: str, boxes: dict) -> bool:
    """Whether a page asks for a code it mailed: it has the boxes for one and says so,
    and it does not say the code came by text message, a call or an app."""
    words = " ".join(str(text or "").split())[:4000]
    return (
        bool(boxes.get("count"))
        and bool(CODE_PAGE.search(words))
        and not OTHER_CHANNEL.search(words)
    )


def registrable(host: str) -> str:
    labels = [label for label in str(host or "").lower().strip(".").split(".") if label]
    return ".".join(labels[-2:])


def code_senders(*urls: str) -> list[str]:
    """The mail domains a site's own code may come from: the domain its form is on, the
    one its board mails from, and the employer's when the posting is on the employer's
    own site. Never a public mailbox provider (the mail module refuses those)."""
    hosts: list[str] = []
    for url in urls:
        domain = registrable(urlsplit(str(url or "")).hostname or "")
        if not domain:
            continue
        for host in (domain, *BOARD_SENDERS.get(domain, ())):
            if host not in hosts:
                hosts.append(host)
    return hosts


CAPTCHA_WORDS = (
    "The site shows a picture check that only a person may pass. Open the recruiting browser "
    "and solve it. If you see no check there, press the page's Next: the check comes up after "
    "that press. I carry on by myself in the same tab; nothing was sent."
)
CAPTCHA_HEADLINE = "CAPTCHA needs you"


def captcha_words(after: str = "") -> str:
    """What the CAPTCHA card tells the owner. `after` is the control whose press brought
    the check up: such a check closes by itself when nobody answers it, so the card says
    which control to press rather than pointing at a check that may be gone. The label is
    the page's own text and is cut down to plain words before it is shown."""
    label = " ".join(re.sub(r"[^A-Za-z0-9 &'-]", " ", str(after or "")).split())[:30].strip()
    if not label:
        return CAPTCHA_WORDS
    return (
        f"This site checks for a person once “{label}” is pressed, with a picture check that "
        f"only you may pass. In the recruiting browser, press “{label}” on this application's "
        "tab and solve the check that comes up. I carry on by myself in the same tab; nothing "
        "was sent."
    )


NO_CODE_WORDS = (
    "The site mailed a code to your application address and I could not find it in your "
    "mailbox. Enter it in the recruiting browser, then reply `go`."
)
