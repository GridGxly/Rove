"""Oracle profile cards: inspect imported education, without trusting resume parsing.

Only a unique exact school match supplies facts. Unmatched or duplicate cards stay for
the owner; no card is deleted and no new education history is inferred.
"""

from . import education, questions, workflow
from .runtime import state_root, write_private

READ_JS = r"""(() => {
 const containers=[];
 document.querySelectorAll('[data-rove-profile-card],[data-rove-card-container]').forEach(e=>{
   e.removeAttribute('data-rove-profile-card');e.removeAttribute('data-rove-card-container');});
 return [...document.querySelectorAll('[data-profile-item-tile=true]')].filter(visible).map((e,i)=>{
   const root=e.closest('.profile-item-container');
   const block=e.closest('.apply-flow-block');
   const heading=squash(block?.querySelector('.apply-flow-block__title')?.innerText||'');
   const school=squash(e.querySelector('[class*=summary-subtitle]')?.innerText||'');
   const title=squash(e.querySelector('[class*=summary-title]')?.innerText||'');
   const kind=/^education(?:\s+(?:required|\*))?$/i.test(heading)?'education':'other';
   let container=null;
   if(root){if(!containers.includes(root))containers.push(root);container=String(containers.indexOf(root));
     root.setAttribute('data-rove-card-container',container);}
   e.setAttribute('data-rove-profile-card',String(i));
   return {ref:String(i),container,kind,school,title,
     invalid:e.getAttribute('data-profile-item-invalid')==='true'};
 });
})()"""


def school_index(card: dict, profile: dict) -> int | None:
    name = " ".join(str(card.get("school") or "").casefold().split())
    matches = [
        i
        for i, school in enumerate(education.schools(profile))
        if name and name == " ".join(str(school.get("school") or "").casefold().split())
    ]
    return matches[0] if len(matches) == 1 else None


def hold(card: dict, reason: str) -> dict:
    return {
        "label": f"Education card: {card.get('school') or 'unnamed school'}",
        "key": f"education-card-{card['ref']}",
        "required": True,
        "control_issue": True,
        "reason": reason,
    }


def editor_fields(editor, fields: list[dict]) -> list[dict]:
    refs = set(
        editor.locator("[data-rove-field]").evaluate_all(
            "es=>es.map(e=>e.getAttribute('data-rove-field'))"
        )
    )
    return [
        f
        for f in fields
        if f.get("ref") in refs
        or set(f.get("member_refs") or []).issubset(refs)
        and f.get("member_refs")
    ]


def context(run: dict, frame, data: dict):
    """Keep an owner reply bound to the school editor Rove itself opened."""
    opened = run.get("education_editor")
    if not opened:
        return
    matches = [
        c
        for c in data.get("profile_cards", [])
        if c["kind"] == "education" and c["school"] == opened["school"]
    ]
    if len(matches) != 1 or matches[0].get("container") is None:
        return
    container = int(matches[0]["container"])
    editor = frame.locator(f'[data-rove-card-container="{container}"] .profile-item-content')
    if editor.count() != 1 or not editor.is_visible():
        return
    fields = editor_fields(editor, data["fields"])
    if school_matches(fields, opened["school"]):
        for field in fields:
            field["education_profile_index"] = opened["profile_index"]


def scoped_read(runtime, editor, index: int) -> dict:
    seen = runtime.observe()
    seen["fields"] = editor_fields(editor, seen["fields"])
    for field in seen["fields"]:
        field["education_profile_index"] = index
        field["key"] = workflow.field_key(field)
    return seen


def school_matches(fields: list[dict], expected: str) -> bool:
    """A school shown inside the editor must agree with the card before any write."""
    name = " ".join(expected.casefold().split())
    for field in fields:
        if questions.classify(field["label"]).canonical_id != "school":
            continue
        actual = field.get("selected") or field.get("value")
        if actual and " ".join(str(actual).casefold().split()) != name:
            return False
    return True


def open_editor(runtime, card: dict, index: int):
    if card.get("container") is None:
        return None
    root = runtime.form.locator(f'[data-rove-card-container="{int(card["container"])}"]')
    editor = root.locator(".profile-item-content")
    identity = {"school": card["school"], "profile_index": index}
    if editor.count() and editor.is_visible():
        return editor if runtime.run.get("education_editor") == identity else None
    tile = runtime.form.locator(f'[data-rove-profile-card="{int(card["ref"])}"]')
    button = (
        tile
        if tile.evaluate("e=>e.matches('button')")
        else tile.locator(".apply-flow-profile-item-tile__edit-item-icon")
    )
    if button.count() != 1:
        return None
    runtime.click(button)
    editor.wait_for(state="visible", timeout=5000)
    runtime.note(f"Opened education card: {card['school']}", "education card · inspect")
    return editor


def edit(runtime, card: dict, index: int, approved: dict, answers: dict) -> tuple[list, list]:
    editor = open_editor(runtime, card, index)
    if editor is None:
        return [], [hold(card, "This education editor cannot be safely opened or resumed")]
    before = scoped_read(runtime, editor, index)
    if not before["fields"]:
        return [], [hold(card, "The education editor's fields could not be read")]
    if not school_matches(before["fields"], card["school"]):
        return [], [hold(card, "The editor shows a different school; nothing was filled or saved")]
    runtime.run["education_editor"] = {"school": card["school"], "profile_index": index}
    runtime.save()
    filled, pending = runtime._fill_page(runtime.run["id"], before, approved, answers)
    after = scoped_read(runtime, editor, index)
    runtime._verify_batch(after, filled)
    if pending:
        return filled, [{**q, "control_issue": True} for q in pending]
    container = f'[data-rove-card-container="{int(card["container"])}"]'
    save = runtime.form.locator(f"{container} .profile-item-footer button.save-btn")
    if save.count() != 1:
        return filled, [hold(card, "The education editor has no unique Save control")]
    with runtime.step_armed(), runtime.requests_heard() as heard:
        runtime.click(save)
        editor.wait_for(state="hidden", timeout=5000)
    runtime.run.pop("education_editor", None)
    runtime.save()
    current = runtime.observe().get("profile_cards", [])
    matches = [c for c in current if c["school"] == card["school"]]
    if len(matches) != 1 or matches[0]["invalid"]:
        return filled, [hold(card, "The site did not keep a valid saved education card")]
    write_private(
        state_root() / f"applications/{runtime.run['id']}/education-{index}.json",
        {
            "school": card["school"],
            "profile_index": index,
            "fields": filled,
            "requests": heard,
            "saved": True,
        },
    )
    runtime.note(f"Verified saved education card: {card['school']}", "education card · saved")
    return filled, []


def prepare(runtime, before: dict, approved: dict, answers: dict) -> tuple[list, list]:
    cards = [c for c in before.get("profile_cards", []) if c["kind"] == "education"]
    filled, pending = (
        [],
        [
            {
                **hold(c, "An imported profile card still has errors"),
                "label": "Imported profile card",
            }
            for c in before.get("profile_cards", [])
            if c["kind"] != "education" and c["invalid"]
        ],
    )
    indexes = [school_index(c, approved["profile"]) for c in cards]
    for card, index in zip(cards, indexes, strict=True):
        if index is None or indexes.count(index) > 1:
            pending.append(
                hold(card, "The imported school is not a unique approved education record")
            )
            continue
        added, unresolved = edit(runtime, card, index, approved, answers)
        filled.extend(added)
        pending.extend(unresolved)
        if unresolved:
            break  # leave the editor and its exact unresolved fields visible
    return filled, pending


def state(cards: list[dict]) -> list[dict]:
    """Stable saved summaries, excluding temporary browser references."""
    return [{k: c.get(k) for k in ("kind", "school", "title", "invalid")} for c in cards]
