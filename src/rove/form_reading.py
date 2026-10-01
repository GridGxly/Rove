"""Reading a form: which question each control answers, and which controls are one question.

The browser observation injects `READING_JS` to find the question text for every control
and to recognise radio and checkbox groups; `group_choices` then turns each group into one
field with its options. A control whose question cannot be found is marked
`label_missing`, with the closest text as a hint, and is never answered from a guess.

This module imports nothing from the workflow or the browser, so both can use it.
"""

import re

# Injected into the observation script after `visible` is defined. The script uses
# `shown`, `requiredBy`, `readLabel`, `readOption`, `readContainer` and `groupFor`.
READING_JS = r"""
 const CONTROLS='input:not([type=hidden]),select,textarea';
 const SKIP='script,style,noscript,select,option,textarea,button,[aria-hidden=true],[role=alert],[role=tooltip]';
 const HEADING='legend,label,h1,h2,h3,h4,h5,h6,[role=heading],[class*="label" i],[class*="question" i],[class*="title" i],[class*="legend" i],[class*="heading" i],[class*="prompt" i]';
 const GENERIC_HINT=/^(?:please\s+)?(?:type|enter|write|add|start typing|select|choose|search|pick)?\s*(?:your|an?|the|one)?\s*(?:answer|response|text|value|option)?\s*(?:here)?\s*[.…:]*$/i;
 const squash=s=>(s||'').replace(/\s+/g,' ').trim();
 const shown=c=>visible(c)||c.type==='file';
 const controlsIn=p=>[...p.querySelectorAll(CONTROLS)].filter(shown);
 const before=(a,b)=>!!(a.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING);
 // An input name or id is never a question: cards[..][field0], question_12345, input-17.
 const machine=t=>{t=squash(t);return !!t&&!/\s/.test(t)&&(/[\[\]{}=_]/.test(t)||/^[a-z]+[-.:]?\d+$/i.test(t)||/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(t)||/^[0-9a-f]{12,}$/i.test(t));};
 // Human text inside `root`, in document order: before `stop`, after `after`, without
 // option labels, other controls' labels, hidden text or required markers.
 const pieces=(root,o={})=>{
   const out=[];let required=false;
   const walker=document.createTreeWalker(root,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT,{acceptNode:n=>n.nodeType===1?(n.matches(SKIP)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_SKIP):NodeFilter.FILTER_ACCEPT});
   for(let n=walker.nextNode();n;n=walker.nextNode()){
     if(o.stop&&!before(n,o.stop))break;
     if(o.after&&!before(o.after,n))continue;
     const el=n.parentElement;const t=squash(n.nodeValue);
     if(!t||!el||el.closest(SKIP)||!visible(el))continue;
     const l=el.closest('label');if(l&&l.control&&l!==o.own)continue;
     if(/^[*✱]+$/.test(t)){required=true;continue;}
     if(/(^|[\s_-])required($|[\s_-])/i.test(el.getAttribute('class')||''))required=true;
     out.push(t);
   }
   let text=out.join(' ');
   if(/[*✱]\s*$/.test(text)){required=true;text=text.replace(/\s*[*✱]+\s*$/,'');}
   return {text:text.slice(0,600),required};
 };
 // A label belongs to a control when it points at it, or points nowhere and wraps no other control.
 const owns=(l,e)=>{const target=l.htmlFor?document.getElementById(l.htmlFor):null;if(target)return target===e||e.contains(target);
   const inner=l.querySelector(CONTROLS);return !inner||inner===e||e.contains(inner);};
 // The closest unclaimed <label>: the one written for this control's part of the container.
 const nearestEl=(e,mine)=>{mine=mine||[e];let p=e.parentElement;
   for(let d=0;p&&d<4;d++,p=p.parentElement){
     const free=[...p.querySelectorAll('label')].filter(l=>!l.contains(e)&&!e.contains(l)&&owns(l,e));
     if(!free.length)continue;
     const prior=free.filter(l=>before(l,e));
     const previous=controlsIn(p).filter(c=>!mine.includes(c)&&!e.contains(c)&&before(c,e)).pop();
     const segment=prior.filter(l=>!previous||before(previous,l));
     return segment[0]||prior[prior.length-1]||free[0];
   }
   return null;};
 const nearestText=(e,mine)=>{const l=nearestEl(e,mine);const t=l?l.innerText.trim():'';return machine(t)?'':t;};
 const labelEls=e=>{const ls=[...(e.labels||[])];if(ls.length)return ls;const ids=(e.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)).filter(Boolean);if(ids.length)return ids;const l=nearestEl(e);return l?[l]:[];};
 const requiredBy=e=>labelEls(e).some(l=>/\brequired\b/i.test(l.className)||/\*\s*$/.test((l.innerText||'').trim()));
 // The smallest container that holds only this question: its heading, or all its text
 // before the control. A container that holds another question is not this question's.
 const leading=(first,mine)=>{let p=first.parentElement;
   for(let d=0;p&&d<6&&!p.matches('form,body,html,main');d++,p=p.parentElement){
     if(controlsIn(p).some(c=>!mine.includes(c)&&!first.contains(c)))return null;
     const all=pieces(p,{stop:first});
     if(!all.text)continue;
     const head=[...p.querySelectorAll(HEADING)].find(h=>before(h,first)&&!h.contains(first)&&!h.querySelector(CONTROLS)&&!(h.closest('label')&&h.closest('label').control)&&visible(h)&&pieces(h).text);
     const text=head?pieces(head).text:all.text;
     return machine(text)?null:{text,required:all.required};
   }
   return null;};
 // Lever keeps each card's questions as JSON beside the card; fieldN is the Nth of them.
 const template=e=>{const m=/^(cards\[[^\]]+\])\[field(\d+)\]$/.exec(e.name||'');if(!m)return null;
   const holder=[...document.querySelectorAll('input[type=hidden]')].find(h=>h.name===m[1]+'[baseTemplate]');
   try{const f=JSON.parse(holder.value).fields[Number(m[2])];const text=squash(f.text);return text&&!machine(text)?{text,required:!!f.required}:null;}catch(_){return null;}};
 // The best guess when nothing names the control: the text just before it.
 const nearby=(first,mine)=>{let p=first.parentElement;
   for(let d=0;p&&d<5&&!p.matches('body,html');d++,p=p.parentElement){
     const previous=controlsIn(p).filter(c=>!mine.includes(c)&&!first.contains(c)&&before(c,first)).pop();
     const t=pieces(p,{stop:first,after:previous}).text;
     if(t)return machine(t)?'':t.slice(-160);
   }
   return '';};
 const trailing=e=>{const p=e.parentElement;if(!p)return '';const next=controlsIn(p).find(c=>c!==e&&before(e,c));
   const t=pieces(p,{after:e,stop:next}).text.slice(0,300);return machine(t)?'':t;};
 // A label wrapped around a dropdown would read as the question plus every option:
 // take its heading, or its text without the options.
 const wrapped=l=>{const head=[...l.querySelectorAll(HEADING)].find(h=>!h.querySelector(CONTROLS)&&visible(h)&&pieces(h,{own:l}).text);
   const all=pieces(l,{own:l});return {text:head?pieces(head,{own:l}).text:all.text,required:all.required};};
 // What the page itself ties to the control: its labels, then its ARIA name.
 const ownText=e=>{let required=false;
   const own=[...(e.labels||[])].map(l=>{if(!l.querySelector('select'))return l.innerText;const read=wrapped(l);required=required||read.required;return read.text;}).join(' ').trim();
   if(own&&!machine(own))return {text:own,required};
   const aria=e.getAttribute('aria-label');
   if(aria&&!machine(aria))return {text:aria};
   const by=(e.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)?.innerText||'').join(' ').trim();
   return by&&!machine(by)?{text:by}:null;
 };
 // One option of a group: its own text, never the group's question.
 const readOption=e=>{const own=ownText(e);const text=own?own.text:trailing(e);if(text)return {text};
   const value=e.getAttribute('value')||'';return {text:value&&value!=='on'&&!machine(value)?value:''};};
 const readLabel=e=>{
   const own=ownText(e);
   if(own)return own;
   if(e.type==='checkbox'||e.type==='radio'){const after=trailing(e);if(after)return {text:after};}
   const near=nearestText(e);
   if(near)return {text:near};
   const lead=leading(e,[e]);
   if(lead)return lead;
   const card=template(e);
   if(card)return card;
   const hint=e.getAttribute('placeholder')||'';
   if(hint.trim()&&!machine(hint)&&!GENERIC_HINT.test(hint.trim()))return {text:hint};
   const title=squash(e.getAttribute('title'));
   if(title&&!machine(title))return {text:title};
   return {text:nearby(e,[e]),missing:true};
 };
 // A button group or other widget read as one question.
 const readContainer=c=>{const mine=controlsIn(c);
   const near=nearestText(c,mine);
   if(near)return {text:near};
   const lead=leading(c,mine);
   if(lead)return lead;
   return {text:nearby(c,mine),missing:true};
 };
 // A fieldset or ARIA group names its options with its legend, its ARIA name, or (when it
 // holds nothing but the options) a label of its own that points at no control.
 const boxTitle=(box,first,members,loose)=>{
   const title=box.querySelector('legend')||(loose?[...box.querySelectorAll('label')].find(l=>owns(l,first)&&l.control!==first&&!members.includes(l.control)):null);
   const named=title?title.innerText.trim():(box.getAttribute('aria-label')||(box.getAttribute('aria-labelledby')||'').split(' ').map(id=>document.getElementById(id)?.innerText||'').join(' ')).trim();
   if(!named||machine(named))return null;
   return {text:named,required:box.getAttribute('aria-required')==='true'||(!!title&&(/\brequired\b/i.test(title.className)||!!title.querySelector('[class*="required" i]')||/\*\s*$/.test(named)))};};
 // The question a radio or checkbox group answers; never one option's text.
 const groupLabel=members=>{const first=members[0];
   const box=first.closest('fieldset,[role=radiogroup],[role=group]');
   const inside=box?controlsIn(box):[];
   if(box&&inside.every(c=>members.includes(c))){const named=boxTitle(box,first,members,true);if(named)return named;}
   const lead=leading(first,members);
   if(lead)return lead;
   // Options with a write-in after them ("Other: ___") are still the fieldset's question.
   if(box&&inside.filter(c=>c.type==='radio'||c.type==='checkbox').every(c=>members.includes(c))&&!inside.some(c=>!members.includes(c)&&before(c,first))){const named=boxTitle(box,first,members,false);if(named)return named;}
   const card=template(first);
   if(card)return card;
   return {text:nearby(first,members),missing:true};
 };
 const choiceOnly=(box,type)=>{const inside=controlsIn(box);return inside.length&&inside.every(c=>c.type===type)?inside:null;};
 const membersOf=(e,all)=>{
   if(e.name){const same=all.filter(x=>x.type===e.type&&x.name===e.name&&x.form===e.form);if(same.length>=2)return same;}
   const box=e.closest(e.type==='radio'?'fieldset,[role=radiogroup],[role=group]':'fieldset,[role=group]');
   const boxed=box&&choiceOnly(box,e.type);
   if(boxed&&(boxed.length>=2||(e.type==='checkbox'&&(box.querySelector('legend')||box.getAttribute('aria-label')||box.getAttribute('aria-labelledby')))))return boxed;
   if(e.type==='checkbox'){
     // A question row whose only control is this checkbox, with the question in its own label block (Lever cards).
     const row=e.closest('.application-question');const carded=row&&choiceOnly(row,'checkbox');
     if(carded&&[...row.querySelectorAll('.application-label')].some(l=>!l.closest('label')&&pieces(l).text))return carded;
   }
   return null;};
 const groupFor=(()=>{const found=new Map();const list=[];
   return (e,all)=>{
     if(e.type!=='radio'&&e.type!=='checkbox')return null;
     if(found.has(e))return found.get(e);
     const members=membersOf(e,all);
     if(!members){found.set(e,null);return null;}
     const read=groupLabel(members);
     const group={key:String(list.length),label:read.text,required:!!read.required,missing:!!read.missing};
     list.push(group);members.forEach(m=>found.set(m,group));
     return group;};})();
"""

UNREADABLE = "A question on the form that Rove could not read"

MACHINE_KEY = re.compile(
    r"[^\s]*[\[\]{}=_][^\s]*|[a-z]+[-.:]?\d+|[0-9a-f]{8}-[0-9a-f]{4}-[^\s]*|[0-9a-f]{12,}",
    re.IGNORECASE,
)


def squash(text) -> str:
    return " ".join(str(text or "").split())


def looks_like_key(text) -> bool:
    """An input name or id (`cards[..][field0]`, `question_12345`, `input-17`), not a question."""
    return bool(MACHINE_KEY.fullmatch(squash(text)))


def unreadable(question: dict) -> bool:
    """True when the owner would be shown nothing, or a field key, instead of a question."""
    label = squash(question.get("label"))
    return (
        bool(question.get("label_missing"))
        or not label
        or looks_like_key(label)
        or label.startswith(UNREADABLE)
    )


def unreadable_label(nearby="") -> str:
    """The plain name of a question Rove could not read, with the text beside it as a hint."""
    hint = squash(nearby)
    if looks_like_key(hint):
        hint = ""
    if len(hint) > 60:
        hint = hint[:59].rstrip() + "…"
    return UNREADABLE + (f" (near “{hint}”)" if hint else "")


def owner_line(question: dict) -> str:
    """What the owner reads for a question whose text could not be found on the form."""
    label = squash(question.get("label"))
    if not label.startswith(UNREADABLE):
        label = unreadable_label(label if question.get("label_missing") else "")
    return f"{label}. Look at the screenshot in the thread to see what it asks."


def unreadable_question(field: dict) -> dict:
    """The open question for a field whose text could not be read: plain words, no key."""
    return {
        "label": unreadable_label(field.get("label")),
        "key": field["key"],
        "required": field["required"],
        "options": [o["label"] for o in field.get("options") or []],
        "label_missing": True,
        "reason": "The form's text for this question could not be read",
    }


def group_choices(fields: list[dict]) -> list[dict]:
    """One question with its options per radio or checkbox group, in form order.

    The observation marks each grouped option with `_group`; the options stay in the list
    (marked `in_group`) so their controls can still be found, and the group is placed just
    before its first option.
    """
    groups: dict[str, list[dict]] = {}
    details: dict[str, dict] = {}
    for field in fields:
        detail = field.pop("_group", None)
        if detail:
            field["in_group"] = True
            groups.setdefault(detail["key"], []).append(field)
            details[detail["key"]] = detail
    result, placed = [], set()
    for field in fields:
        key = next((k for k, members in groups.items() if members[0] is field), None)
        if key is not None and key not in placed:
            placed.add(key)
            result.append(group_field(groups[key], details[key]))
        result.append(field)
    return result


def group_field(members: list[dict], detail: dict) -> dict:
    multiple = members[0]["kind"] == "checkbox"
    kind = "checkbox_group" if multiple else "radio_group"
    checked = [m["label"] for m in members if m["checked"]]
    group = {
        # A checkbox group points at its first option so code that locates every field
        # still finds a control; a radio group keeps no control of its own.
        "ref": members[0]["ref"] if multiple else None,
        "label": members[0].get("group") or "",
        "group": "",
        "name": members[0]["name"],
        "id": "",
        "kind": kind,
        "tag": "checkboxes" if multiple else "radios",
        "role": kind,
        "selected": None,
        "selection_code": None,
        "required": bool(detail.get("required")) or any(m["required"] for m in members),
        "disabled": all(m["disabled"] for m in members),
        "readonly": False,
        "checked": False,
        "value": ", ".join(checked) if multiple else next(iter(checked), ""),
        "options": [{"label": m["label"], "value": m.get("value") or m["label"]} for m in members],
        "member_refs": [m["ref"] for m in members],
    }
    if detail.get("missing"):
        group["label_missing"] = True
    return group


def words(text) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def match_options(answer, options: list[str]) -> list[str] | None:
    """The options an answer names, in the form's order; None when any part names no option.

    Options are matched whole, longest first, so "Java, JavaScript" picks both and an
    option that itself contains a comma is still one option.
    """
    remaining = f" {words(answer)} "
    chosen = set()
    for index in sorted(range(len(options)), key=lambda i: -len(words(options[i]))):
        needle = words(options[index])
        if needle and f" {needle} " in remaining:
            remaining = remaining.replace(f" {needle} ", "  ", 1)
            chosen.add(index)
    if not chosen or set(remaining.split()) - {"and", "or"}:
        return None
    return [options[i] for i in sorted(chosen)]
