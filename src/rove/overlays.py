"""Pop-ups in front of the application form: found, sorted and closed by code.

A pop-up is a visible dialog, modal, interstitial or drawer that sits above the form or
covers most of the window. Before anything is touched it is sorted into one of three:

- the application itself (it holds the fields being filled, a file input or the final
  control): kept, never closed;
- a step that offers a way to go on with the application without signing up for anything
  ("Apply manually", "Continue without LinkedIn", "No thanks, continue", "Skip"): that
  way is taken;
- anything unrelated (newsletter, talent community, "get our app", survey, chat invite,
  language picker, promo): closed with its dismissive control, else Escape, else a click
  on its backdrop. A control whose words commit to something (submit, apply, sign up,
  subscribe, agree, allow, accept, buy, enable) is never used, and a pop-up's own fields
  are never typed into.

Qwen is asked only when the wording leaves code with buttons it cannot place; its answer
must be one of the offered labels and passes the same never-list. When that fails, or the
model is away, the run stops with a screenshot and plain words for the owner. Cookie
banners are not pop-ups here: `dismiss_consent` handles them by their own wording.
"""

import re

from . import workflow

HOLD_WORDS = "A pop-up is in the way that I did not want to guess on. Close it and reply `go`."
# More closings than this in one application is a pop-up that keeps coming back.
MAX_CLOSED_PER_RUN = 8

# Visible things in front of the page: dialogs by role, by name, or by sitting on top at
# a few points of the window and at the form's first field. Each gets a reference and a
# compact description; its buttons get references of their own.
FIND_JS = r"""() => {
 const vw=innerWidth,vh=innerHeight;
 const squash=s=>(s||'').replace(/\s+/g,' ').trim();
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const share=e=>{const r=e.getBoundingClientRect();const w=Math.max(0,Math.min(r.right,vw)-Math.max(r.left,0));const h=Math.max(0,Math.min(r.bottom,vh)-Math.max(r.top,0));return (w*h)/(vw*vh);};
 const NAMED='[class*="modal" i],[class*="overlay" i],[class*="popup" i],[class*="pop-up" i],[class*="lightbox" i],[class*="interstitial" i],[class*="drawer" i],[id*="modal" i],[id*="overlay" i],[id*="popup" i],[id*="lightbox" i],[id*="interstitial" i]';
 const found=new Set();
 for(const e of document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open]'))if(visible(e))found.add(e);
 for(const e of document.querySelectorAll(NAMED))if(visible(e)&&share(e)>=0.15)found.add(e);
 // Whatever is on top at the window's middle and quarters, and at the form's first field.
 const first=document.querySelector('[data-rove-field]')||document.querySelector('input:not([type=hidden]),select,textarea');
 let formPoint=null;
 if(first&&visible(first)){const r=first.getBoundingClientRect();if(r.width&&r.height&&r.bottom>0&&r.top<vh)formPoint=[Math.min(Math.max(r.left+r.width/2,1),vw-1),Math.min(Math.max(r.top+r.height/2,1),vh-1)];}
 const points=[[vw/2,vh/2],[vw/4,vh/4],[vw*3/4,vh/4],[vw/4,vh*3/4],[vw*3/4,vh*3/4]];
 const positioned=e=>['fixed','absolute','sticky'].includes(getComputedStyle(e).position);
 const onTop=[];
 for(const [x,y] of points.concat(formPoint?[formPoint]:[])){
   const hit=document.elementFromPoint(x,y);onTop.push(hit);
   for(let e=hit;e&&e!==document.body&&e!==document.documentElement;e=e.parentElement){
     if(found.has(e))break;
     if(positioned(e)&&share(e)>=0.5){found.add(e);break;}
   }
 }
 // The outermost of nested candidates stands for all of them.
 const outer=[...found].filter(e=>![...found].some(o=>o!==e&&o.contains(e)));
 document.querySelectorAll('[data-rove-overlay],[data-rove-overlay-button]').forEach(e=>{e.removeAttribute('data-rove-overlay');e.removeAttribute('data-rove-overlay-button');});
 const CLOSE_WORDS=/^(?:[×✕✖⨯x]|close|dismiss)$/i;
 // A page's own loading screen: no words, no buttons, a progress or spinner mark.
 const BUSY='[role=progressbar],[aria-busy=true],[class*="spinner" i],'+
   '[class*="loading" i],[class*="loader" i]';
 let buttonIndex=0;
 return outer.map((e,i)=>{
   e.setAttribute('data-rove-overlay',String(i));
   const controls=[...e.querySelectorAll('button,a[href],[role=button],input[type=button],input[type=submit],[aria-label*="close" i],[class*="close" i]')].filter(visible);
   const buttons=[];
   for(const b of controls){
     if(b.querySelector('button,a[href],[role=button]'))continue;
     const label=squash(b.innerText||b.value||'')||squash(b.getAttribute('aria-label'))||squash(b.getAttribute('title'));
     const aria=squash(b.getAttribute('aria-label')).toLowerCase();
     const cls=b.getAttribute('class')||'';
     const close=/close|dismiss/.test(aria)||/\bclose\b|close[-_]?(?:btn|button|icon)|(?:btn|button|icon|modal|popup|dialog)[-_]?close/i.test(cls)||CLOSE_WORDS.test(label);
     if(!label&&!close)continue;
     b.setAttribute('data-rove-overlay-button',String(buttonIndex));
     buttons.push({ref:String(buttonIndex++),label:label.slice(0,80),close});
   }
   const fields=[...e.querySelectorAll('input:not([type=hidden]),select,textarea')].filter(visible);
   const first_inside=!!(first&&e.contains(first));
   const covers=onTop.some(hit=>hit&&e.contains(hit));
   const coversForm=!!(formPoint&&onTop[onTop.length-1]&&e.contains(onTop[onTop.length-1]))&&!first_inside;
   const heading=e.querySelector('h1,h2,h3,h4,[role=heading]');
   const named=squash(e.getAttribute('aria-label'))||squash((e.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)?.innerText||'').join(' '))||(heading?squash(heading.innerText):'');
   return {ref:String(i),name:named.slice(0,120),text:squash(e.innerText).slice(0,600),buttons,
     fields:fields.length,file:fields.some(f=>f.type==='file'),holds_form:first_inside||!!e.querySelector('[data-rove-field]'),
     share:Math.round(share(e)*100)/100,covers,covers_form:coversForm,form_in_view:!!formPoint,
     dialog:!!e.closest('[role=dialog],[role=alertdialog],[aria-modal=true],dialog'),
     modal:!!(e.closest('[aria-modal=true]')||e.querySelector('[aria-modal=true]')),
     busy:!squash(e.innerText)&&!buttons.length&&!fields.length
       &&(e.matches(BUSY)||!!e.querySelector(BUSY)),
     backdrop:!squash(e.innerText)&&!buttons.length};
 });
}"""

# A point on the overlay that is the overlay itself, not something inside it: its backdrop.
BACKDROP_POINT_JS = r"""(ref) => {
 const e=document.querySelector('[data-rove-overlay="'+ref+'"]');
 if(!e)return null;
 const r=e.getBoundingClientRect();
 for(const [x,y] of [[r.left+12,r.top+12],[r.right-12,r.top+12],[r.left+12,r.bottom-12],[r.right-12,r.bottom-12],[r.left+12,(r.top+r.bottom)/2]]){
   if(x<0||y<0||x>innerWidth||y>innerHeight)continue;
   if(document.elementFromPoint(x,y)===e)return [x,y];
 }
 return null;
}"""

# Is this pop-up gone from the front of the page (removed, hidden, or moved off it)?
GONE_JS = r"""(ref) => {
 const e=document.querySelector('[data-rove-overlay="'+ref+'"]');
 if(!e||!e.isConnected)return true;
 const cs=getComputedStyle(e);
 if(!e.getClientRects().length||cs.visibility==='hidden'||Number(cs.opacity)===0)return true;
 const r=e.getBoundingClientRect();
 return !(r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth);
}"""


class OverlayInTheWay(RuntimeError):
    """A pop-up code could not close and did not want to guess on; the owner closes it."""


# What Playwright says when something sits on top of the element it was asked to use.
BLOCKED_CLICK = re.compile(
    r"intercepts pointer events|not visible|outside of the viewport|element is not visible",
    re.IGNORECASE,
)


def blocked_click(error: Exception) -> bool:
    return bool(BLOCKED_CLICK.search(str(error)))


# Ways of going on with the application that sign up for nothing, best first.
CONTINUE = (
    r"apply manually",
    r"(?:fill|enter|type)(?: it| this)?(?: out| in)? (?:manually|myself|by hand)",
    r"continue (?:manually|without .*|as (?:a )?guest|to (?:the )?(?:application|form))",
    r"(?:apply|proceed|go on) without .*",
    r"no,? thanks?,? (?:continue|proceed|i'?ll .*)",
    r"i(?:'| wi)?ll (?:do|fill|enter) (?:it|this) (?:myself|manually|by hand)",
    r"skip(?: this(?: step)?| for now)?",
    r"not now",
    r"no,? thanks?",
    r"no,? thank you",
    r"maybe later",
)
# Ways of closing something unrelated, best first.
DISMISS = (
    r"no,? thanks?",
    r"no,? thank you",
    r"not now",
    r"maybe later",
    r"later",
    r"dismiss",
    r"close",
    r"continue to (?:the )?(?:site|page|website|job|posting|application)",
    r"got it",
    r"skip(?: this(?: step)?| for now)?",
    r"[×✕✖⨯x]",
    r"ok(?:ay)?",
)
# Words that commit to something: never clicked to get a pop-up out of the way.
NEVER = re.compile(
    r"\b(?:submit|apply(?! manually)|sign ?up|sign ?in|log ?in|register|subscribe|join|agree|"
    r"allow|accept|purchase|buy|enable|notify|notifications?|download|install|get the app|"
    r"create (?:an? )?(?:account|profile)|continue with|start|yes|confirm|save|send|"
    r"upload|import|connect|verify)\b",
    re.IGNORECASE,
)
# A bare yes or no answers a question about the applicant; it never closes a pop-up.
ANSWER_WORDS = re.compile(r"(?:yes|no|true|false|y|n)[.!]?", re.IGNORECASE)


def normalized(text) -> str:
    return " ".join(re.findall(r"[a-z0-9'’,×✕✖⨯]+", str(text or "").lower())).replace("’", "'")


def matches(label: str, patterns) -> int | None:
    """The rank of the first pattern the whole label matches, or None."""
    text = normalized(label)
    for rank, pattern in enumerate(patterns):
        if re.fullmatch(pattern, text):
            return rank
    return None


def is_form(overlay: dict) -> bool:
    """The pop-up is the application: it holds what is being filled or what sends it."""
    if overlay.get("holds_form") or overlay.get("file"):
        return True
    if any(
        re.fullmatch(
            r"submit(?: (?:my |your )?application)?|send application|apply now|apply",
            normalized(b["label"]),
        )
        for b in overlay.get("buttons", [])
    ):
        return True
    return overlay.get("fields", 0) >= 3 and choose(overlay) is None


def blocking(overlay: dict, after_failure: bool = False) -> bool:
    """It sits above the form, says it is modal, or covers most of the window when no
    form is in view.

    With a form field in view, only what covers that field counts; a fixed shell that
    merely surrounds the page is not in the way. After a click or fill was stopped by
    something on top, anything on top at the probed points counts.
    """
    if (
        re.search(r"cookie|consent", overlay.get("text", ""), re.IGNORECASE)
        and overlay.get("share", 0) < 0.5
    ):
        return False  # the cookie banner's own step handles it
    if overlay.get("covers_form") or overlay.get("modal"):
        return True
    if after_failure:
        return bool(overlay.get("covers"))
    return (
        not overlay.get("form_in_view")
        and bool(overlay.get("covers"))
        and overlay.get("share", 0) >= 0.5
    )


def in_the_way(found: list[dict], after_failure: bool = False) -> list[dict]:
    """The pop-ups to deal with, the one with controls before its bare backdrop.

    When anything blocks the page, every visible dialog counts too: a modal's content and
    its backdrop are often two elements, and the content is where the close control is.
    """
    # A page's own loading screen is waited out by the browser, never closed.
    found = [o for o in found if not o.get("busy")]
    blocked = [o for o in found if blocking(o, after_failure)]
    if blocked:
        blocked = [o for o in found if blocking(o, after_failure) or o.get("dialog")]
    return sorted(
        (o for o in blocked if not is_form(o)),
        key=lambda o: (bool(o.get("backdrop")), not o.get("buttons")),
    )


def choose(overlay: dict) -> dict | None:
    """The control code would press, with the way it was chosen; None when code cannot tell."""
    buttons = overlay.get("buttons", [])
    ranked = []
    for button in buttons:
        rank = matches(button["label"], CONTINUE)
        if rank is not None:
            ranked.append((0, rank, button, "continue"))
            continue
        rank = matches(button["label"], DISMISS)
        if rank is not None or button.get("close"):
            if button.get("close") and rank is None:
                rank = len(DISMISS)
            if rank == len(DISMISS) - 1 and overlay.get("fields", 0):
                continue  # a bare OK beside a field could be the field's answer
            ranked.append((1, rank, button, "dismiss"))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]))
    _, _, button, way = ranked[0]
    return {"button": button, "way": way}


def allowed(label: str) -> bool:
    """A label the never-list and the question rule let through."""
    if matches(label, CONTINUE) is not None:
        return True
    text = normalized(label)
    return bool(text) and not NEVER.search(text) and not ANSWER_WORDS.fullmatch(text)


def describe(overlay: dict) -> str:
    """What the owner reads the pop-up as: its name, else its first words."""
    name = (overlay.get("name") or "").strip()
    if not name:
        name = (overlay.get("text") or "").strip()
    return workflow.clip(name, 60) or "a pop-up"


def scrubbed(text, limit: int = 160) -> str:
    """Dialog or pop-up text for a log line: no links, addresses, codes or long numbers."""
    text = re.sub(r"https?://\S+|www\.\S+", "(link)", str(text or ""))
    text = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "(address)", text)
    text = re.sub(r"\b\d{5,}\b", "(number)", text)
    text = re.sub(
        r"\b(password|passcode|token|code|secret|key)\b\s*[:=]?\s*\S+",
        r"\1 (hidden)",
        text,
        flags=re.IGNORECASE,
    )
    return workflow.clip(" ".join(text.split()), limit)


def qwen_context(overlay: dict, title: str) -> dict:
    """What Qwen may read about a pop-up: bounded text with no links, addresses or asks."""
    from .mail import sanitize_for_model

    return {
        "review_type": "overlay",
        "page_title": workflow.clip(" ".join(str(title or "").split()), 120),
        "overlay_text": sanitize_for_model(overlay.get("text", ""), 600),
        "buttons": [b["label"] for b in overlay.get("buttons", [])][:12],
        "fields_inside": int(overlay.get("fields", 0)),
    }


def qwen_choice(parsed, overlay: dict) -> dict | None:
    """Qwen's answer as a button of the pop-up, or None when it is not one code may press."""
    if not isinstance(parsed, dict) or str(parsed.get("action", "")).lower() != "click":
        return None
    wanted = normalized(parsed.get("button"))
    if not wanted:
        return None
    for button in overlay.get("buttons", []):
        if normalized(button["label"]) == wanted and allowed(button["label"]):
            return button
    return None
