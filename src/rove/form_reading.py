"""Reading a form: which question each control answers, and which controls are one question.

The browser observation injects `READING_JS` to find the question text for every control
and to recognise radio and checkbox groups; `group_choices` then turns each group into one
field with its options. A control whose question cannot be found is marked
`label_missing`, with the closest text as a hint, and is never answered from a guess.

The reader also walks open shadow roots (web-component forms), reads a rich-text editor
as a long-text field, reads a grid of choices as one question per row, and leaves out the
controls of a window that does not block the page, such as a chat box.

This module imports nothing from the workflow or the browser, so both can use it.
"""

import re

# Injected into the observation script after `visible` is defined. The script uses
# `shown`, `requiredBy`, `readLabel`, `readOption`, `readContainer`, `groupFor`,
# `deepAll`, `sectionOf` and `FINAL`.
READING_JS = r"""
 const NATIVE='input:not([type=hidden]),select,textarea';
 // A rich-text editor (Quill, ProseMirror, Draft, Slate): the editable box itself.
 const RICH='[contenteditable]:not([contenteditable=false]):is([role=textbox],.ql-editor,.ProseMirror,.public-DraftEditor-content,[data-slate-editor])';
 // A control is a form element, an ARIA dropdown that has no form element inside it, or
 // a rich-text editor.
 const CONTROLS=NATIVE+',[role=combobox]:not(input,select,textarea):not(:has('+NATIVE+')),'+RICH;
 // What sends an application, word for word.
 const FINAL=/^(submit|submit application|submit my application|submit your application|submit now|send application|complete application|finish application)$/i;
 // Open shadow roots: a web-component form keeps its controls inside them. Without any,
 // every query below is the plain document query it always was.
 const rootsIn=r=>{const out=[];for(const e of r.querySelectorAll('*'))if(e.shadowRoot)out.push(e.shadowRoot,...rootsIn(e.shadowRoot));return out;};
 const ROOTS=rootsIn(document);
 const SHADOW=ROOTS.length>0;
 // Every match under `root` and inside the open shadow roots below it, in reading order:
 // a component's own controls come where the component stands.
 const deepAll=(sel,root=document)=>{if(!SHADOW)return [...root.querySelectorAll(sel)];
   const out=[];const walk=r=>{for(const e of r.querySelectorAll('*')){if(e.matches(sel))out.push(e);if(e.shadowRoot)walk(e.shadowRoot);}};walk(root);return out;};
 const deepOne=sel=>document.querySelector(sel)||(SHADOW?deepAll(sel)[0]||null:null);
 // An id names an element of the same tree: the document, or the shadow root around it.
 const byId=(n,id)=>{const r=n.getRootNode();return (r!==document&&r.getElementById&&r.getElementById(id))||document.getElementById(id);};
 const hostOf=e=>{const r=e.getRootNode();return r!==document&&r.host?r.host:null;};
 const SKIP='script,style,noscript,select,option,textarea,button,[aria-hidden=true],[role=alert],[role=tooltip]';
 const HEADING='legend,label,h1,h2,h3,h4,h5,h6,[role=heading],[class*="label" i],[class*="question" i],[class*="title" i],[class*="legend" i],[class*="heading" i],[class*="prompt" i]';
 const GENERIC_HINT=/^(?:please\s+)?(?:type|enter|write|add|start typing|select|choose|search|pick)?\s*(?:your|an?|the|one)?\s*(?:answer|response|text|value|option)?\s*(?:here)?\s*[.…:]*$/i;
 // What an upload button says about itself, not what the form asks for.
 const CAPTION=/^(?:or\s+)?(?:choose|select|attach|upload|browse|add|drop|drag)\b.{0,40}$|^no files? (?:selected|chosen)\.?$|^file[-_ ]?input$|^(?:file|files|attachment)$/i;
 const TRAP_NAME=/honey|hpot|(^|[-_ ])hp([-_ ]|$)|trap|bot[-_ ]?(check|field)|nickname|leave[-_ ]?(this[-_ ]?|the[-_ ]?)?(field[-_ ]?|box[-_ ]?)?(blank|empty)|do[-_ ]?not[-_ ]?fill/i;
 const squash=s=>(s||'').replace(/\s+/g,' ').trim();
 const before=(a,b)=>!!(a.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING);
 const textual=c=>c.tagName==='TEXTAREA'||(c.tagName==='INPUT'&&!['file','radio','checkbox','submit','button','image','reset','range','color','password'].includes(c.type));
 // The visible thing a person clicks when the real control is hidden or covered: the role
 // wrapper of a styled radio or checkbox, the toggle button over a hidden select, or the
 // box around a search input whose shown value sits on top of the input.
 const wrapperOf=c=>(c.type==='radio'||c.type==='checkbox')?c.closest('[role=radio],[role=checkbox],[role=switch]'):null;
 const toggleOf=c=>{if(c.tagName!=='SELECT'||visible(c))return null;let p=c.parentElement;
   for(let d=0;p&&d<3;d++,p=p.parentElement){
     if([...p.querySelectorAll(NATIVE)].some(x=>x!==c&&visible(x)))return null;
     const toggle=[...p.querySelectorAll('button[aria-haspopup],[role=button][aria-haspopup]')].find(visible);
     if(toggle)return toggle;
   }
   return null;};
 const shellOf=c=>{if(!textual(c)||c.tagName!=='INPUT'||c.getAttribute('role')==='combobox'||c.hasAttribute('aria-haspopup'))return null;
   const s=c.parentElement&&c.parentElement.closest('[aria-haspopup][aria-expanded]');
   return s&&!s.matches('label,form,body')&&[...s.querySelectorAll(NATIVE)].filter(visible).every(x=>x===c)?s:null;};
 const faceOf=c=>c.matches(NATIVE)?(wrapperOf(c)||toggleOf(c)||shellOf(c)):null;
 // A trap for bots: a text field no person can reach. One parked off the page is always a
 // trap; one that is transparent, sizeless or inside hidden markup is a trap when its
 // name or label says so (a dropdown's own search input can be transparent too).
 const trap=c=>{if(!textual(c))return false;
   const r=c.getBoundingClientRect();
   if(r.right+window.scrollX<=0||r.bottom+window.scrollY<=0)return true;
   const named=TRAP_NAME.test([c.name,c.id,c.getAttribute('autocomplete')||'',...[...(c.labels||[])].map(l=>l.textContent)].join(' '));
   if(!named)return false;
   if(r.width<2||r.height<2||(c.parentElement&&c.parentElement.closest('[aria-hidden=true]')))return true;
   for(let p=c,d=0;p&&d<4;d++,p=p.parentElement){if(Number(getComputedStyle(p).opacity)===0)return true;}
   return false;};
 // A control in a window that does not block the page (a chat box, a help widget) is not
 // part of the application: a dialog that is not modal, with fewer than three controls,
 // no upload, and no button that sends or continues an application.
 const GOES_ON=/^(apply|apply now|next|continue)$/i;
 const asideMemo=new WeakMap();
 const aside=c=>{const d=c.closest('[role=dialog],[role=alertdialog],dialog');
   if(!d||d.getAttribute('aria-modal')==='true'||(d.tagName==='DIALOG'&&d.matches(':modal')))return false;
   if(!asideMemo.has(d)){const inside=[...d.querySelectorAll(CONTROLS)];
     asideMemo.set(d,inside.length<3&&!inside.some(x=>x.type==='file')&&![...d.querySelectorAll('button,input[type=submit],[role=button]')].some(b=>{const t=(b.innerText||b.value||'').trim();return FINAL.test(t)||GOES_ON.test(t);}));}
   return asideMemo.get(d);};
 const shownMemo=new WeakMap();
 const shown=c=>{if(!shownMemo.has(c)){const face=faceOf(c);shownMemo.set(c,(c.type==='file'||visible(c)||(!!face&&visible(face)))&&!trap(c)&&!aside(c)&&!(c.matches(RICH)&&!!c.parentElement?.closest(RICH)));}return shownMemo.get(c);};
 // The components that hold a shown control: in the tree around a component, the
 // component stands for the controls inside it.
 let HOSTS=null;
 const hostsOf=()=>{if(!HOSTS){HOSTS=new Set();for(const c of deepAll(CONTROLS))if(shown(c))for(let h=hostOf(c);h;h=hostOf(h))HOSTS.add(h);}return HOSTS;};
 const controlsIn=p=>{const light=[...p.querySelectorAll(CONTROLS)].filter(shown);if(!SHADOW)return light;
   const held=[...p.querySelectorAll('*')].filter(x=>hostsOf().has(x));
   return held.length?[...light,...held].sort((a,b)=>a===b?0:before(a,b)?-1:1):light;};
 // An input name or id is never a question: cards[..][field0], question_12345, input-17.
 const machine=t=>{t=squash(t);return !!t&&!/\s/.test(t)&&(/[\[\]{}=_]/.test(t)||/^(?:[a-z]+[-.:]|input|question|field|control|widget|q)\d+$/i.test(t)||/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(t)||/^[0-9a-f]{12,}$/i.test(t));};
 // A class that marks its element required, hashed or not: `required`, `is-required`,
 // `_required_f7cvd_91`.
 const requiredClass=c=>/(^|[\s_-])required($|[\s_-])/i.test(String(c||''));
 // Human text inside `root`, in document order: before `stop`, after `after`, outside
 // `without`, and without option labels, other controls' labels, hidden text, required
 // markers or any piece `drop` matches.
 const pieces=(root,o={})=>{
   const out=[];let required=false;
   const walker=document.createTreeWalker(root,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT,{acceptNode:n=>n.nodeType===1?(n.matches(SKIP)||n===o.without?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_SKIP):NodeFilter.FILTER_ACCEPT});
   for(let n=walker.nextNode();n;n=walker.nextNode()){
     if(o.stop&&!before(n,o.stop))break;
     if(o.after&&!before(o.after,n))continue;
     const el=n.parentElement;const t=squash(n.nodeValue);
     if(!t||!el||el.closest(SKIP)||!visible(el))continue;
     const l=el.closest('label');if(l&&l.control&&l!==o.own)continue;
     if(/^[*✱]+$/.test(t)||/^\(\s*required\s*\)$/i.test(t)){required=true;continue;}
     if(requiredClass(el.getAttribute('class')))required=true;
     if(o.drop&&o.drop.test(t))continue;
     out.push(t);
   }
   let text=out.join(' ');
   if(/[*✱]\s*$/.test(text)){required=true;text=text.replace(/\s*[*✱]+\s*$/,'');}
   return {text:text.slice(0,600),required};
 };
 // A label belongs to a control when it points at it, or points nowhere and wraps no other control.
 const owns=(l,e)=>{const target=l.htmlFor?byId(l,l.htmlFor):null;if(target)return target===e||e.contains(target);
   const inner=l.querySelector(CONTROLS);return !inner||inner===e||e.contains(inner);};
 // The closest unclaimed <label>: the one written for this control's part of the container.
 // `strict` takes only a label with no other control between it and this one; without it,
 // the last label before the control is accepted even across another control.
 const nearestEl=(e,mine,strict)=>{mine=mine||[e];let p=e.parentElement;
   for(let d=0;p&&d<4;d++,p=p.parentElement){
     const free=[...p.querySelectorAll('label')].filter(l=>!l.contains(e)&&!e.contains(l)&&owns(l,e));
     if(!free.length)continue;
     const prior=free.filter(l=>before(l,e));
     const previous=controlsIn(p).filter(c=>!mine.includes(c)&&!e.contains(c)&&before(c,e)).pop();
     const segment=prior.filter(l=>!previous||before(previous,l));
     // A label after the control is its own only when no other control comes between them.
     const next=controlsIn(p).find(c=>!mine.includes(c)&&!e.contains(c)&&before(e,c));
     const close=segment[0]||(prior.length?null:free.find(l=>!next||before(l,next)))||null;
     if(close||strict)return close;
     return prior[prior.length-1]||null;
   }
   return null;};
 const nearestText=(e,mine,strict)=>{const l=nearestEl(e,mine,strict);const t=l?l.innerText.trim():'';return machine(t)?'':t;};
 const labelEls=e=>{const ls=[...(e.labels||[])];if(ls.length)return ls;const ids=(e.getAttribute('aria-labelledby')||'').split(' ').map(id=>byId(e,id)).filter(Boolean);if(ids.length)return ids;const l=nearestEl(e);return l?[l]:[];};
 // Required by its label (a class, a trailing asterisk or "(required)") or by the class of
 // the group that holds only this question.
 const requiredBy=e=>{if(labelEls(e).some(l=>requiredClass(l.getAttribute('class'))||/(\*|\(\s*required\s*\))\s*$/i.test((l.innerText||'').trim())))return true;
   const box=e.parentElement&&e.parentElement.closest('.form-required,.is-required,.field-required');
   return !!box&&controlsIn(box).every(c=>c===e);};
 // The smallest container that holds only this question: its heading, or all its text
 // before the control. A container that holds another question is not this question's.
 const leading=(first,mine,drop)=>{let p=first.parentElement;
   for(let d=0;p&&d<6&&!p.matches('form,body,html,main');d++,p=p.parentElement){
     if(controlsIn(p).some(c=>!mine.includes(c)&&!first.contains(c)))return null;
     const all=pieces(p,{stop:first,drop});
     if(!all.text)continue;
     const head=[...p.querySelectorAll(HEADING)].find(h=>before(h,first)&&!h.contains(first)&&!h.querySelector(CONTROLS)&&!(h.closest('label')&&h.closest('label').control)&&visible(h)&&pieces(h,{drop}).text);
     const text=head?pieces(head,{drop}).text:all.text;
     return machine(text)?null:{text,required:all.required};
   }
   return null;};
 // Lever keeps each card's questions as JSON beside the card; fieldN is the Nth of them.
 const template=e=>{const m=/^(cards\[[^\]]+\])\[field(\d+)\]$/.exec(e.name||'');if(!m)return null;
   const holder=[...e.getRootNode().querySelectorAll('input[type=hidden]')].find(h=>h.name===m[1]+'[baseTemplate]');
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
 // A label wrapped around a dropdown would read as the question plus every option, or
 // plus the value the dropdown shows: take its heading, or its text without the widget.
 const wrapped=(l,without)=>{const head=[...l.querySelectorAll(HEADING)].find(h=>!h.querySelector(CONTROLS)&&!(without&&(without.contains(h)||h.contains(without)))&&visible(h)&&pieces(h,{own:l}).text);
   const all=pieces(l,{own:l,without});return {text:head?pieces(head,{own:l}).text:all.text,required:all.required};};
 // Accessible names take precedence over a shared enclosing label. In a compound
 // phone field, the picker is "Country code" and only the number input is "Phone".
 const ownText=e=>{let required=false;const shell=e.matches(NATIVE)?shellOf(e):null;
   const by=(e.getAttribute('aria-labelledby')||'').split(' ').map(id=>byId(e,id)?.textContent||'').join(' ').trim();
   if(by&&!machine(by))return {text:by};
   const aria=e.getAttribute('aria-label');
   if(aria&&!machine(aria))return {text:aria};
   const own=[...(e.labels||[])].map(l=>{const inside=shell&&l.contains(shell)?shell:null;if(!inside&&!l.querySelector('select'))return l.innerText;const read=wrapped(l,inside);required=required||read.required;return read.text;}).join(' ').trim();
   if(own&&!machine(own))return {text:own,required};
   return null;
 };
 // The value a custom dropdown shows as chosen; a prompt such as "Select..." or "--" is none.
 const PROMPT=/^\W*(select|choose|pick|please)\b/i;
 const chosenText=t=>{t=squash(t);return t&&/[\p{L}\p{N}]/u.test(t)&&!PROMPT.test(t)?t:null;};
 const picked=(e,face)=>{
   if(face&&face.matches('button,[role=button]'))return face.querySelector('[class*="placeholder" i]')?null:chosenText(face.innerText);
   if(face){const v=[...face.querySelectorAll('[class*="single-value" i],[class*="singleValue" i]')].find(visible);return v?chosenText(v.innerText):null;}
   return chosenText([...e.querySelectorAll('*')].filter(n=>!n.children.length&&visible(n)&&!n.closest('[role=listbox],[role=option],[role=menu]')).map(n=>n.textContent).join(' '));};
 // The options an ARIA dropdown already holds in the page, shown or not.
 const listed=e=>{const ids=((e.getAttribute('aria-owns')||'')+' '+(e.getAttribute('aria-controls')||'')).split(' ').filter(Boolean);
   const roots=[e,...ids.map(id=>byId(e,id)).filter(Boolean)];
   return [...new Set(roots.flatMap(r=>[...r.querySelectorAll('[role=option]')]))].map(o=>squash(o.textContent)).filter(Boolean).map(t=>({label:t,value:t})).slice(0,300);};
 // One option of a group: its own text, never the group's question.
 const readOption=e=>{const own=ownText(e);const text=own?own.text:trailing(e);if(text)return {text};
   const value=e.getAttribute('value')||'';return {text:value&&value!=='on'&&!machine(value)?value:''};};
 const readLabelOwn=e=>{
   const file=e.type==='file';
   if(file){
     // An upload is named by what its ARIA name points at, minus the button's own caption.
     const by=(e.getAttribute('aria-labelledby')||'').split(' ').map(id=>squash(byId(e,id)?.innerText)).filter(t=>t&&!CAPTION.test(t)).join(' ');
     if(by&&!machine(by))return {text:by};
   }
   const own=ownText(e);
   // With no label element, an upload's ARIA name is often only "file-input" or "Choose file".
   if(own&&!(file&&!(e.labels||[]).length&&CAPTION.test(squash(own.text))))return own;
   if(e.type==='checkbox'||e.type==='radio'){const after=trailing(e);if(after)return {text:after};}
   const near=nearestText(e,[e],true);
   if(near)return {text:near};
   const lead=leading(e,[e],file?CAPTION:null);
   if(lead)return lead;
   const card=template(e);
   if(card)return card;
   // A control inside a web component: what names the component, or the text around it.
   const outer=hostName(e);
   if(outer)return outer;
   // A label that another control stands between is a weaker claim than the question's own text.
   const far=nearestText(e);
   if(far)return {text:far};
   const hint=e.getAttribute('placeholder')||'';
   if(hint.trim()&&!machine(hint)&&!GENERIC_HINT.test(hint.trim()))return {text:hint};
   const title=squash(e.getAttribute('title'));
   if(title&&!machine(title))return {text:title};
   if(file){
     // Nothing names the upload: the button beside it says what it takes.
     const button=e.parentElement&&[...e.parentElement.querySelectorAll('button,[role=button],label')].map(b=>squash(b.textContent)).find(t=>t&&!machine(t));
     if(button)return {text:button.slice(0,120)};
   }
   return {text:nearby(e,[e]),missing:true};
 };
 const hostName=e=>{for(let h=hostOf(e);h;h=hostOf(h)){
     const named=squash(h.getAttribute('aria-label')||h.getAttribute('label')||h.getAttribute('label-hint'));
     if(named&&!machine(named))return {text:named};
     const own=[...(h.labels||[])].map(l=>squash(l.innerText)).join(' ');
     if(own&&!machine(own))return {text:own};
     const near=nearestText(h,[h],true);if(near)return {text:near};
     const lead=leading(h,[h]);if(lead)return lead;}
   return null;};
 // A month, day or year box is one part of a date question: it carries the question, so
 // "Month" reads as "Expected graduation date — Month".
 const DATE_PART=/^(?:month|day|year|mm|dd|yy|yyyy)$/i;
 const partName=c=>{const own=ownText(c);return squash(own?own.text:(c.getAttribute&&c.getAttribute('placeholder'))||'');};
 const partOf=(e,read)=>{if(read.missing||!DATE_PART.test(squash(read.text)))return read;
   for(let p=e.parentElement,d=0;p&&d<4&&!p.matches('form,body,html');d++,p=p.parentElement){
     const mine=controlsIn(p);if(mine.length<2)continue;
     if(!mine.every(c=>c.matches(CONTROLS)&&DATE_PART.test(partName(c))))return read;
     const near=nearestText(mine[0],mine,true);
     const q=(p.matches('fieldset,[role=group]')?boxTitle(p,mine[0],mine,false):null)||leading(mine[0],mine)||(near?{text:near}:null);
     return q&&q.text?{text:q.text+' — '+squash(read.text),required:!!read.required||!!q.required}:read;}
   return read;};
 const readLabel=e=>partOf(e,readLabelOwn(e));
 // A button group or other widget read as one question.
 const readContainer=c=>{const mine=controlsIn(c);
   const near=nearestText(c,mine,true);
   if(near)return {text:near};
   const lead=leading(c,mine);
   if(lead)return lead;
   const far=nearestText(c,mine);
   if(far)return {text:far};
   return {text:nearby(c,mine),missing:true};
 };
 // A fieldset or ARIA group names its options with its legend, its ARIA name, or (when it
 // holds nothing but the options) a label of its own that points at no control.
 const boxTitle=(box,first,members,loose)=>{
   const title=box.querySelector('legend')||(loose?[...box.querySelectorAll('label')].find(l=>owns(l,first)&&l.control!==first&&!members.includes(l.control)):null);
   const named=title?title.innerText.trim():(box.getAttribute('aria-label')||(box.getAttribute('aria-labelledby')||'').split(' ').map(id=>byId(box,id)?.innerText||'').join(' ')).trim();
   if(!named||machine(named))return null;
   return {text:named,required:box.getAttribute('aria-required')==='true'||(!!title&&(requiredClass(title.getAttribute('class'))||!!title.querySelector('[class*="required" i]')||/\*\s*$/.test(named)))};};
 // The question a radio or checkbox group answers; never one option's text.
 const groupLabel=members=>{const first=members[0];
   const box=first.closest('fieldset,[role=radiogroup],[role=group]');
   const inside=box?controlsIn(box):[];
   const whole=!!box&&inside.every(c=>members.includes(c));
   if(whole){const named=boxTitle(box,first,members,true);if(named)return named;}
   const lead=leading(first,members);
   if(lead)return {...lead,required:lead.required||(whole&&box.getAttribute('aria-required')==='true')};
   // Options with a write-in after them ("Other: ___") are still the fieldset's question.
   if(box&&inside.filter(c=>c.type==='radio'||c.type==='checkbox').every(c=>members.includes(c))&&!inside.some(c=>!members.includes(c)&&before(c,first))){const named=boxTitle(box,first,members,false);if(named)return named;}
   const card=template(first);
   if(card)return card;
   return {text:nearby(first,members),missing:true};
 };
 const choiceOnly=(box,type)=>{const inside=controlsIn(box);return inside.length&&inside.every(c=>c.type===type)?inside:null;};
 // A box that names its options: a legend, an ARIA name, or a label of its own that
 // points at no control (a board's question title).
 const titledBox=box=>!!(box.querySelector('legend')||box.getAttribute('aria-label')||box.getAttribute('aria-labelledby')||[...box.querySelectorAll('label')].some(l=>!l.control&&squash(l.innerText)));
 const membersOf=(e,all)=>{
   const box=e.closest(e.type==='radio'?'fieldset,[role=radiogroup],[role=group]':'fieldset,[role=group]');
   if(e.name){let same=all.filter(x=>x.type===e.type&&x.name===e.name&&x.form===e.form&&x.getRootNode()===e.getRootNode());
     // Checkboxes that share a name but sit in separate titled boxes are separate
     // questions (a board that names every lone box "Yes").
     if(e.type==='checkbox'&&box&&titledBox(box)&&same.some(x=>x.closest('fieldset,[role=group]')!==box))same=same.filter(x=>x.closest('fieldset,[role=group]')===box);
     if(same.length>=2)return same;}
   const boxed=box&&choiceOnly(box,e.type);
   if(boxed&&(boxed.length>=2||(e.type==='checkbox'&&titledBox(box))))return boxed;
   if(e.type==='checkbox'){
     // A question row whose only control is this checkbox, with the question in its own label block (Lever cards).
     const row=e.closest('.application-question');const carded=row&&choiceOnly(row,'checkbox');
     if(carded&&[...row.querySelectorAll('.application-label')].some(l=>!l.closest('label')&&pieces(l).text))return carded;
   }
   return null;};
 // A grid of choices (a table with a header row over two or more rows of options, each
 // option in its own cell): every row is one question, named by the grid's title and the
 // row's own heading, and every option by its column's heading.
 const GRID='table,[role=table],[role=grid]';
 const cells=r=>[...r.children].filter(c=>c.matches('td,th,[role=cell],[role=gridcell],[role=columnheader],[role=rowheader]'));
 const cellOf=e=>e.closest('td,th,[role=cell],[role=gridcell]');
 const gridOf=e=>{const row=e.closest('tr,[role=row]');const table=row&&row.closest(GRID);if(!table)return null;
   const rows=[...table.querySelectorAll('tr,[role=row]')].filter(r=>r.closest(GRID)===table);
   const kind=r=>[...r.querySelectorAll('input[type='+e.type+']')].filter(shown);
   const full=rows.filter(r=>kind(r).length>=2);
   if(full.length<2||!full.includes(row))return null;
   const members=kind(row);
   if(new Set(members.map(cellOf)).size!==members.length||members.some(m=>!cellOf(m)))return null;
   const head=rows.find(r=>before(r,full[0])&&!r.querySelector(CONTROLS)&&cells(r).length>=2);
   return head?{table,row,head,members}:null;};
 const gridTitle=g=>{const caption=g.table.querySelector('caption');let t=caption?squash(caption.innerText):'';
   if(!t)t=squash(g.table.getAttribute('aria-label')||(g.table.getAttribute('aria-labelledby')||'').split(' ').map(id=>byId(g.table,id)?.innerText||'').join(' '));
   if(!t){const box=g.table.closest('fieldset');const l=box&&box.querySelector('legend');if(l&&controlsIn(box).every(c=>g.table.contains(c)))t=squash(l.innerText);}
   if(!t){const lead=leading(g.table,controlsIn(g.table));t=lead?lead.text:'';}
   if(!t){const corner=cells(g.head)[0];t=corner?squash(corner.innerText):'';}
   return machine(t)?'':t;};
 const rowTitle=g=>{const c=cells(g.row).find(c=>!c.querySelector(CONTROLS)&&squash(c.innerText));return c?squash(c.innerText):'';};
 const columnOf=(g,e)=>{const i=cells(g.row).indexOf(cellOf(e));const h=i>=0?cells(g.head)[i]:null;return h?squash(h.innerText):'';};
 const gridLabel=g=>{let t=gridTitle(g);const required=/[*✱]\s*$/.test(t);t=t.replace(/\s*[*✱]+\s*$/,'');const r=rowTitle(g);
   if(!t&&!r)return {text:nearby(g.members[0],g.members),missing:true};
   return {text:t&&r?t+' — '+r:(t||r),required};};
 const groupFor=(()=>{const found=new Map();const list=[];
   return (e,all)=>{
     if(e.type!=='radio'&&e.type!=='checkbox')return null;
     if(found.has(e))return found.get(e);
     const grid=gridOf(e);
     const members=grid?grid.members:membersOf(e,all);
     if(!members){found.set(e,null);return null;}
     const read=grid?gridLabel(grid):groupLabel(members);
     const group={key:String(list.length),label:read.text,required:!!read.required,missing:!!read.missing,...(grid?{column:m=>columnOf(grid,m)}:{})};
     list.push(group);members.forEach(m=>found.set(m,group));
     return group;};})();
 // The heading of the part of the form a control sits in: the last heading or legend before it.
 const SECTION='h1,h2,h3,h4,h5,h6,[role=heading],legend';
 let HEADS=null;
 const sectionOf=e=>{if(!HEADS)HEADS=deepAll(SECTION).filter(h=>visible(h)&&squash(h.innerText));
   let last=null;for(const h of HEADS){if(!h.contains(e)&&before(h,e))last=h;}
   return last?squash(last.innerText).slice(0,120):'';};
"""

VISIBLE_JS = (
    " const visible=e=>!!e.getClientRects().length && getComputedStyle(e).visibility!=='hidden'"
    " && e.getAttribute('aria-hidden')!=='true';\n"
)

# The first element a selector names in the document, else inside an open shadow root.
DEEP_ONE_JS = r""" const deepOne=sel=>{const walk=r=>{const hit=r.querySelector(sel);if(hit)return hit;
   for(const e of r.querySelectorAll('*'))if(e.shadowRoot){const h=walk(e.shadowRoot);if(h)return h;}return null;};return walk(document);};
"""

# Given a field reference: the options an opened custom dropdown offers, each marked with
# `data-rove-option`. A list with ARIA roles is read by them; a list without is read as the
# text rows of the element the dropdown says it owns.
OPTIONS_JS = (
    "(ref) => {\n"
    + VISIBLE_JS
    + DEEP_ONE_JS
    + r""" const squash=s=>(s||'').replace(/\s+/g,' ').trim();
 document.querySelectorAll('[data-rove-option]').forEach(e=>e.removeAttribute('data-rove-option'));
 const face=deepOne('[data-rove-field="'+ref+'"]');
 if(!face)return [];
 const root=face.getRootNode();
 const ids=[face,...face.querySelectorAll('[aria-owns],[aria-controls]')].flatMap(o=>['aria-owns','aria-controls','data-menu-id'].flatMap(a=>(o.getAttribute(a)||'').split(' '))).filter(Boolean);
 const owned=ids.map(id=>(root!==document&&root.getElementById(id))||document.getElementById(id)).filter(Boolean);
 const roles='[role=option],[role=menuitem],[role=menuitemradio]';
 let found=[...new Set([face,...owned].flatMap(r=>[...r.querySelectorAll(roles)]))].filter(visible);
 if(!found.length)found=[...document.querySelectorAll(roles)].filter(visible);
 if(!found.length&&root!==document)found=[...root.querySelectorAll(roles)].filter(visible);
 if(!found.length)found=owned.flatMap(r=>[...r.querySelectorAll('*')]).filter(n=>visible(n)&&squash(n.textContent)&&![...n.children].some(c=>squash(c.textContent)));
 return found.slice(0,300).map((o,i)=>{o.setAttribute('data-rove-option',String(i));return squash(o.textContent);});
}"""
)

# Given a field reference: what a custom dropdown shows as chosen (null for a prompt).
PICKED_JS = (
    "(ref) => {\n"
    + VISIBLE_JS
    + READING_JS
    + r""" const face=deepOne('[data-rove-field="'+ref+'"]');
 if(!face)return null;
 const e=face.matches(CONTROLS)?face:deepAll(NATIVE).find(c=>faceOf(c)===face);
 return e?picked(e,e===face?null:face):null;
}"""
)

# On a radio or checkbox, or the role wrapper that stands in for a hidden one.
CHECKED_JS = (
    "e => e.getAttribute('aria-checked')==='true' || !!e.checked"
    " || [...e.querySelectorAll('input[type=radio],input[type=checkbox]')].some(c=>c.checked)"
)

# What a form holds right now: its controls, their values and the dialogs in front of it.
# Two equal readings a moment apart mean nothing is still filling the form in.
FORM_STATE_JS = r"""() => {
 const all=[];const walk=r=>{for(const e of r.querySelectorAll('*')){if(e.matches('input,select,textarea'))all.push(e);if(e.shadowRoot)walk(e.shadowRoot);}};
 walk(document);
 return JSON.stringify(all.map(e=>[e.type,e.name||e.id,e.type==='file'?e.files.length:e.value,!!e.checked]))
  +'|'+[...document.querySelectorAll('[role=dialog],dialog')].filter(d=>d.getClientRects().length).length;
}"""

UNREADABLE = "A question on the form that Rove could not read"

MACHINE_KEY = re.compile(
    r"[^\s]*[\[\]{}=_][^\s]*|(?:[a-z]+[-.:]|input|question|field|control|widget|q)\d+"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[^\s]*|[0-9a-f]{12,}",
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
    if "_section" in members[0]:
        group["_section"] = members[0]["_section"]
    return group


def distinct_keys(fields: list[dict], key) -> None:
    """Give every question its key, and twins (two "City" fields, "Email" twice) their own.

    `key(field)` is `workflow.field_key`. The first of several questions with the same
    label, name, id, kind and options keeps the plain key, so an answer stored for it
    before still lands on it, and a form without twins keys exactly as it always did.
    Each later twin also carries its section heading and its place among the twins, so
    one answer never lands on two fields; the first is marked `twins`. A group's own
    options are not questions and keep the plain key.
    """
    first: dict[str, dict] = {}
    seen: dict[str, int] = {}
    for field in fields:
        section = field.pop("_section", None)
        field["key"] = key(field)
        if field.get("in_group"):
            continue
        count = seen.get(field["key"], 0)
        seen[field["key"]] = count + 1
        if not count:
            first[field["key"]] = field
            continue
        first[field["key"]]["twins"] = True
        field["section"] = section or ""
        field["occurrence"] = count
        field["key"] = key(field)


def review_step(reading: dict) -> bool:
    """The last step of a wizard that shows the answers back: nothing left to fill (its
    Edit links are not fields) and exactly one control that sends the application."""
    open_fields = [
        f for f in reading.get("fields") or [] if not f["disabled"] and not f["readonly"]
    ]
    return not open_fields and len(reading.get("final_controls") or []) == 1


# The video-interview and assessment sites, as the owner knows them.
STEP_SITES = {
    "hirevue": "HireVue",
    "sparkhire": "Spark Hire",
    "modernhire": "Modern Hire",
    "willo": "Willo",
    "myinterview": "myInterview",
    "vidcruiter": "VidCruiter",
    "interviewstream": "interviewstream",
    "talview": "Talview",
    "codility": "Codility",
    "hackerrank": "HackerRank",
    "codesignal": "CodeSignal",
    "testgorilla": "TestGorilla",
    "karat": "Karat",
    "pymetrics": "pymetrics",
    "harver": "Harver",
    "vervoe": "Vervoe",
    "shl": "SHL",
    "mettl": "Mercer Mettl",
    "criteriacorp": "Criteria",
    "coderbyte": "Coderbyte",
    "qualified": "Qualified",
    "devskiller": "DevSkiller",
    "wonderlic": "Wonderlic",
    "hackerearth": "HackerEarth",
    "imocha": "iMocha",
    "testdome": "TestDome",
}


def owner_step(reading: dict) -> dict | None:
    """A video interview or an online assessment in the application: the owner's to take.

    The headline and the reason are plain words for the owner, with the link to open.
    """
    marker = (reading.get("ats_markers") or {}).get("assessment")
    if not marker:
        return None
    host = str(marker.get("host") or "").lower()
    name = next((n for key, n in STEP_SITES.items() if key in host.split(".")), "")
    on = f" on {name}" if name else ""
    link = marker.get("link") or reading.get("url") or ""
    what = (
        ("Video interview step", f"a recorded video interview{on}, which Rove does not record")
        if marker.get("kind") == "video"
        else ("Assessment step", f"an online assessment{on}, which Rove does not take")
    )
    return {
        "headline": what[0],
        "reason": f"The application goes on to {what[1]}. Nothing was sent. Open it here: "
        f"{link} and finish the application there, then reply `applied`, or `park it`.",
    }


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
