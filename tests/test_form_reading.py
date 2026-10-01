"""Real headless browser: every field carries the question it answers.

A radio or checkbox group is one question with its options, an input's name is never a
label, and a question whose text cannot be found is marked unreadable instead of guessed.
The pages are synthetic fixtures served offline with `set_content`.
"""

import json
from pathlib import Path

import pytest
from patchright.sync_api import sync_playwright
from test_live_submission import ASHBY_FORM, FORM

from rove import form_reading, worker, workflow
from rove.live_browser import RecruitingBrowser
from rove.onboarding import approve, digest, draft, propose, read_approved
from rove.runtime import state_root

FIXTURES = Path(__file__).parent / "fixtures"
LEVER = (FIXTURES / "lever_cards.html").read_text()
GROUPS = (FIXTURES / "question_groups.html").read_text()
RUN = "abcdef012345"
AUTHORIZED = "Are you lawfully authorized to work in the United States?"
SPONSORSHIP = (
    "Will you need sponsorship at any point in the future to maintain lawful employment "
    "in the United States?"
)
LANGUAGES = "Which programming languages have you used? (Check all that apply)"
ONE_ROLE = (
    "Your application is reviewed for one role at a time. If several roles interest you, "
    "apply to your first choice."
)
NOTES = (
    "May we use a note-taking assistant during interviews? Your choice does not affect "
    "your candidacy."
)
MARKETING = "Northwind Labs has my consent to contact me about future job opportunities."
ASHBY_SPONSORSHIP = (
    "Will you now or in the future, require sponsorship for employment visa status "
    "(e.g., H1B visa status)?"
)


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A temporary state root and vault, with Discord off and no pacing."""
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    monkeypatch.setattr(workflow, "config", lambda: {"enabled": False, "human_pacing": False})
    return tmp_path / "state"


@pytest.fixture
def reader(state):
    """A recruiting browser on an offline page; `read(html)` loads a page and observes it."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        runtime = RecruitingBrowser(headless=True)
        runtime.page = browser.new_page()
        runtime.run = {"id": RUN, "profile_hash": "x"}

        def read(html):
            runtime.page.set_content(html if isinstance(html, str) else html.decode())
            return runtime.observe()

        runtime.read = read
        yield runtime
        browser.close()


@pytest.fixture
def approved(tmp_path, state):
    """A synthetic approved profile, frozen the way an application freezes it."""
    pdf = tmp_path / "approved.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic approved resume")
    propose(
        "identity",
        {"legal_first_name": "Alex", "legal_last_name": "Example", "email": "alex@example.invalid"},
        digest(draft()),
    )
    propose("evidence", {"resume_path": str(pdf)}, digest(draft()))
    propose(
        "eligibility",
        {"us_work_authorized": True, "sponsorship_now": False, "sponsorship_future": False},
        digest(draft()),
    )
    approve(digest(draft()))
    return read_approved()


def questions(seen: dict) -> dict:
    """The fields a person would call questions: everything except a group's own options."""
    return {f["label"]: f for f in seen["fields"] if not f.get("in_group")}


def option_labels(field: dict) -> list[str]:
    return [o["label"] for o in field["options"]]


def test_lever_card_radio_is_one_question_with_its_options(reader):
    seen = reader.read(LEVER)
    asked = questions(seen)
    authorized = asked[AUTHORIZED]
    assert authorized["kind"] == "radio_group" and authorized["required"]
    assert option_labels(authorized) == ["Yes", "No"]
    assert option_labels(asked[SPONSORSHIP]) == ["Yes", "No"]
    assert option_labels(asked["What degree are you currently pursuing?"]) == [
        "Associate degree",
        "Bachelor degree",
        "Masters or PhD",
    ]
    assert option_labels(asked[NOTES]) == ["Yes, I consent", "No, I do not consent"]
    # The options stay findable for the click, marked as part of their question.
    members = [f for f in seen["fields"] if f["ref"] in authorized["member_refs"]]
    assert [m["label"] for m in members] == ["Yes", "No"]
    assert all(m["in_group"] and m["group"] == AUTHORIZED for m in members)
    assert "Yes" not in asked and "No" not in asked


def test_lever_card_checkboxes_are_one_question_and_a_lone_consent_box_keeps_its_label(reader):
    seen = reader.read(LEVER)
    asked = questions(seen)
    languages = asked[LANGUAGES]
    assert languages["kind"] == "checkbox_group" and languages["required"]
    assert option_labels(languages) == ["Python", "Java", "JavaScript", "Other"]
    assert languages["value"] == "" and len(languages["member_refs"]) == 4
    # One box under a card question is still that question, with the box as its option.
    one_role = asked[ONE_ROLE]
    assert one_role["kind"] == "checkbox_group" and option_labels(one_role) == ["I understand"]
    # A consent box with no question above it is its own question.
    marketing = asked[MARKETING]
    assert marketing["kind"] == "checkbox" and not marketing.get("in_group")
    assert not marketing["required"]
    assert not {"Python", "Java", "JavaScript", "Other", "I understand"} & set(asked)


def test_lever_card_text_textarea_and_dropdown_carry_the_card_question(reader):
    asked = questions(reader.read(LEVER))
    street = asked["Street address"]
    assert street["kind"] == "text" and street["required"]
    assert street["name"].endswith("[field0]")
    heard = asked["How did you hear about Northwind Labs?"]
    assert heard["tag"] == "select" and heard["required"]
    assert option_labels(heard)[1:] == [
        "Careers page",
        "Campus event",
        "Friend or employee",
        "Other",
    ]
    optional = asked["If yes, what type of sponsorship will you require?"]
    assert optional["kind"] == "textarea" and not optional["required"]
    start = asked["What date can you start (month and year)?"]
    assert start["kind"] == "textarea" and start["required"]
    assert asked["Why do you want to work at Northwind Labs?"]["kind"] == "textarea"
    # The placeholder is an instruction, never the question.
    assert "Type your response" not in asked
    # Lever's own rows keep reading as before; a dropdown's options are not its label.
    assert {"Full name✱", "Email✱", "Current location", "LinkedIn URL"} <= set(asked)
    assert asked["Gender"]["tag"] == "select" and asked["Veteran status"]["tag"] == "select"
    assert asked["Additional information"]["kind"] == "textarea"


def test_lever_card_question_falls_back_to_the_card_template(reader):
    reader.page.set_content(LEVER)
    # The visible question text is gone; the card's own description of its fields remains.
    reader.page.evaluate(
        "() => document.querySelectorAll('.custom-question .application-label')"
        ".forEach(e => e.remove())"
    )
    asked = questions(reader.observe())
    assert asked["Street address"]["kind"] == "text" and asked["Street address"]["required"]
    assert asked["How did you hear about Northwind Labs?"]["tag"] == "select"
    assert asked[AUTHORIZED]["kind"] == "radio_group" and asked[AUTHORIZED]["required"]
    assert asked[LANGUAGES]["kind"] == "checkbox_group"
    assert option_labels(asked[LANGUAGES]) == ["Python", "Java", "JavaScript", "Other"]
    optional = asked["If yes, what type of sponsorship will you require?"]
    assert optional["kind"] == "textarea" and not optional["required"]
    assert not any(f.get("label_missing") for f in asked.values())


def test_no_lever_question_is_an_option_or_an_input_name(reader):
    seen = reader.read(LEVER)
    options = {o["label"] for f in seen["fields"] for o in f["options"]}
    for field in questions(seen).values():
        assert field["label"] and not field.get("label_missing"), field["name"]
        assert not form_reading.looks_like_key(field["label"])
        assert field["label"] not in {field["name"], field["id"]}
        if field["kind"] != "checkbox":
            assert field["label"] not in options
    # Groups sit where their first option is, so questions are numbered in form order.
    order = [f["label"] for f in seen["fields"] if not f.get("in_group")]
    assert order.index("Street address") < order.index(AUTHORIZED) < order.index(SPONSORSHIP)
    assert order.index(SPONSORSHIP) < order.index(LANGUAGES) < order.index("Gender")


def test_fieldset_legend_names_its_group_and_not_the_fields_beside_it(reader):
    asked = questions(reader.read(GROUPS))
    teams = asked["Which teams interest you? *"]
    assert teams["kind"] == "checkbox_group" and teams["required"]
    assert option_labels(teams) == ["Platform", "Perception", "Motion planning, controls"]
    assert teams["name"] == "question_31[]"
    relocate = asked["Are you willing to relocate?"]
    assert relocate["kind"] == "radio_group" and option_labels(relocate) == ["Yes", "No"]
    # Options with a write-in after them are still the legend's question.
    found = asked["How did you find this role?"]
    assert found["kind"] == "radio_group" and option_labels(found) == ["Career fair", "Other"]
    assert asked["Other source"]["kind"] == "text"
    # A fieldset around different controls is a section: each control keeps its own label.
    assert asked["Country*"]["tag"] == "select" and asked["Phone*"]["kind"] == "tel"
    assert "Phone" not in asked


def test_aria_names_and_container_headings_name_their_groups(reader):
    asked = questions(reader.read(GROUPS))
    shift = asked["Which shift do you prefer?"]
    assert shift["kind"] == "radio_group" and option_labels(shift) == ["Day", "Night"]
    days = asked["Which days can you work?"]
    assert days["kind"] == "checkbox_group"
    assert option_labels(days) == ["Monday", "Tuesday", "Wednesday"]
    interned = asked["Have you interned before?"]
    assert interned["kind"] == "radio_group" and interned["required"]
    assert option_labels(interned) == ["Yes", "No"]
    tools = asked["Which tools have you used?"]
    assert tools["kind"] == "checkbox_group" and option_labels(tools) == ["CAD", "PLC programming"]
    privacy = asked["I have read the privacy notice"]
    assert privacy["kind"] == "checkbox" and not privacy.get("in_group")
    # The question's label sits outside the fieldset and points at no control.
    outside = questions(
        reader.read(
            "<title>Apply</title><form><div class='entry'>"
            "<label class='title required' for='sched'>Which schedule suits you?</label><fieldset>"
            "<div><span><input type='checkbox' id='sched_0' name='sched'></span>"
            "<label for='sched_0'>Weekdays</label></div>"
            "<div><span><input type='checkbox' id='sched_1' name='sched'></span>"
            "<label for='sched_1'>Weekends</label></div></fieldset></div>"
            "<div class='entry'><label for='n'>Name</label><input id='n' name='name'></div></form>"
        )
    )
    schedule = outside["Which schedule suits you?"]
    assert schedule["kind"] == "checkbox_group" and schedule["required"]
    assert option_labels(schedule) == ["Weekdays", "Weekends"]
    assert outside["Name"]["kind"] == "text"


def test_a_question_without_text_is_marked_unreadable_and_never_named_by_its_input(reader):
    seen = reader.read(GROUPS)
    by_name = {f["name"]: f for f in seen["fields"] if not f.get("in_group")}
    bare = by_name["question_12345"]
    assert bare["label_missing"] and bare["label"] == ""
    # Text that merely sits before the control is a hint, not a label.
    assert by_name["q7"]["label_missing"] and by_name["q7"]["label"] == "Grade point average"
    assert by_name["q8"]["label_missing"] and by_name["q8"]["label"] == "Expected graduation"
    # An ARIA name that is itself a field key is no name at all.
    assert by_name["custom_9"]["label_missing"] and by_name["custom_9"]["label"] == ""
    mystery = by_name["mystery"]
    assert mystery["kind"] == "radio_group" and mystery["label_missing"]
    assert mystery["label"] == "" and option_labels(mystery) == ["Alpha", "Beta"]
    for field in seen["fields"]:
        assert not form_reading.looks_like_key(field["label"]), field
    readable = [f for f in seen["fields"] if not f.get("label_missing")]
    assert all("label_missing" not in f for f in readable) and len(readable) > 20


def test_greenhouse_and_ashby_fixtures_read_as_before(reader):
    plain = questions(reader.read(FORM))
    assert {label: f["kind"] for label, f in plain.items()} == {
        "First name": "text",
        "Email": "text",
        "Resume": "file",
    }
    assert plain["First name"]["required"] and not plain["Resume"]["required"]
    ashby = questions(reader.read(ASHBY_FORM))
    assert list(ashby) == [
        "Name",
        "Email",
        "Resume",
        "Location",
        "Describe a project you built",
        "What is your expected graduation year?",
        "Are you legally authorized to work in the United States?",
        ASHBY_SPONSORSHIP,
        "Are you comfortable working out of our NYC Office 5 days/week?",
    ]
    year = ashby["What is your expected graduation year?"]
    assert year["kind"] == "radio_group" and year["required"]
    assert option_labels(year) == ["December 2026", "Spring 2027", "December 2027", "Other"]
    authorized = ashby["Are you legally authorized to work in the United States?"]
    assert authorized["kind"] == "choice" and authorized["required"]
    assert option_labels(authorized) == ["Yes", "No"]
    assert not any(f.get("label_missing") for f in ashby.values())


def test_nearest_label_is_the_one_written_for_that_control(reader):
    # Labels that point nowhere: each input takes the label just before it, not the first.
    seen = reader.read(
        "<title>Apply</title><form><label>First name</label><input name='a'>"
        "<label>Favorite tool</label><input name='b'></form>"
    )
    assert [f["label"] for f in seen["fields"]] == ["First name", "Favorite tool"]
    # A label that wraps another control belongs to that control only.
    seen = reader.read(
        "<title>Apply</title><ul><li><label><input type='checkbox' name='c'> Subscribe</label></li>"
        "<li><input name='d'></li></ul>"
    )
    unlabeled = next(f for f in seen["fields"] if f["name"] == "d")
    assert unlabeled["label_missing"] and unlabeled["label"] != "Subscribe"


def test_question_lines_say_plainly_when_a_question_could_not_be_read():
    pairs = workflow.numbered(
        [
            {"label": "Need sponsorship?", "key": "aaaaaaaaaaaa", "options": ["Yes", "No"]},
            {"label": "Grade point average", "key": "bbbbbbbbbbbb", "label_missing": True},
            {"label": "", "key": "cccccccccccc", "label_missing": True, "options": ["Alpha"]},
            {"label": "cards[11111111-2222][field3]", "name": "cards[x][field3]", "key": "d" * 12},
            {"name": "question_12345", "key": "eeeeeeeeeeee"},
            form_reading.unreadable_question(
                {"label": "Email", "key": "ffffffffffff", "required": True, "options": []}
            ),
        ]
    )
    lines = workflow.question_lines(pairs).split("\n")
    see = "Look at the screenshot in the thread to see what it asks."
    unread = "A question on the form that Rove could not read"
    assert lines[0] == "1. Need sponsorship?  (Yes / No)"
    assert lines[1] == f"2. {unread} (near “Grade point average”). {see}"
    assert lines[2] == f"3. {unread}. {see}  (Alpha)"
    assert lines[3] == f"4. {unread}. {see}"
    assert lines[4] == f"5. {unread}. {see}"
    assert lines[5] == f"6. {unread} (near “Email”). {see}"
    text = "\n".join(lines)
    for leak in ("cards[", "field3", "question_12345", "aaaaaaaaaaaa", "ffffffffffff"):
        assert leak not in text


def test_key_shaped_text_is_recognised_and_real_questions_are_not():
    for key in (
        "cards[6d127747-2d17-402b-87d6-b1f4045ad776][field3]",
        "question_12345",
        "input-17",
        "q7",
        "first_name",
        "0f3c7a1e-5b2d-4c8e-9a6f-1d2e3f4a5b6c",
    ):
        assert form_reading.looks_like_key(key), key
    for text in ("Yes", "Email", "GPA", "LinkedIn URL", "Street address", "Are you over 18?"):
        assert not form_reading.looks_like_key(text), text


def test_match_options_takes_whole_options_and_refuses_a_part_it_cannot_place():
    languages = ["Python", "Java", "JavaScript", "Other"]
    assert form_reading.match_options("Java, JavaScript", languages) == ["Java", "JavaScript"]
    assert form_reading.match_options("javascript and python", languages) == [
        "Python",
        "JavaScript",
    ]
    assert form_reading.match_options("Python, Rust", languages) is None
    assert form_reading.match_options("", languages) is None
    consent = ["Yes, I consent", "No, I do not consent"]
    assert form_reading.match_options("Yes, I consent", consent) == ["Yes, I consent"]
    assert form_reading.match_options("yes", consent) is None


def test_filling_a_lever_form_asks_real_questions_and_ticks_the_owners_choices(reader, approved):
    seen = reader.read(LEVER)
    reader.run["profile_hash"] = approved["profile_hash"]
    filled, pending = reader._fill_page(RUN, seen, approved, {})
    assert {f["label"] for f in filled} >= {"Full name✱", "Email✱", "Gender", "Veteran status"}
    asked = {q["label"]: q for q in pending}
    assert set(asked) == {
        "Phone ✱",
        "Current location",
        "Current company",
        "LinkedIn URL",
        "GitHub URL",
        "Street address",
        "How did you hear about Northwind Labs?",
        AUTHORIZED,
        SPONSORSHIP,
        "If yes, what type of sponsorship will you require?",
        "What degree are you currently pursuing?",
        LANGUAGES,
        "What date can you start (month and year)?",
        ONE_ROLE,
        "Why do you want to work at Northwind Labs?",
        NOTES,
        "Additional information",
        MARKETING,
    }
    assert asked[AUTHORIZED]["options"] == ["Yes", "No"]
    assert asked[LANGUAGES]["options"] == ["Python", "Java", "JavaScript", "Other"]
    assert asked[ONE_ROLE]["options"] == ["I understand"] and asked[ONE_ROLE]["required"]
    assert not any(form_reading.unreadable(q) for q in pending)
    # The card the owner reads: numbered questions in form order, no option or key as a question.
    listed = workflow.numbered(worker.question_list(pending, [], {}, set()))
    card = workflow.question_lines(listed, limit=len(listed)).split("\n")
    assert len(card) == 18 and "cards[" not in "\n".join(card)
    assert f"8. {AUTHORIZED}  (Yes / No)" in card
    assert f"12. {LANGUAGES}  (Python / Java / JavaScript / Other)" in card
    assert not any(line.split(". ", 1)[1].startswith(("Yes", "No ")) for line in card)
    answers = {
        asked[LANGUAGES]["key"]: {
            "value": "Python, JavaScript",
            "source": "owner Discord message 1",
        },
        asked[ONE_ROLE]["key"]: {"value": "yes", "source": "owner Discord message 2"},
    }
    filled, pending = reader._fill_page(RUN, reader.observe(), approved, answers)
    chosen = {f["label"]: f for f in filled}
    assert chosen[LANGUAGES]["value"] == "Python, JavaScript"
    assert chosen[LANGUAGES]["control"] == "checkbox_group"
    assert chosen[ONE_ROLE]["value"] == "I understand"
    assert LANGUAGES not in {q["label"] for q in pending}
    after = questions(reader.observe())
    assert after[LANGUAGES]["value"] == "Python, JavaScript"
    assert after[ONE_ROLE]["value"] == "I understand"
    boxes = reader.page.locator('input[name$="[field6]"]')
    assert [boxes.nth(i).is_checked() for i in range(4)] == [True, False, True, False]
    assert not reader.page.locator('input[name="consent[marketing]"][type=checkbox]').is_checked()


def test_an_unreadable_question_goes_to_the_owner_and_is_never_filled_from_a_guess(
    reader, approved
):
    seen = reader.read(
        "<title>Apply</title><form><div><span>Email</span><input name='q1' required>"
        "<span>Notes</span><input name='q2'></div>"
        "<div><label><input type='radio' name='pick' value='a' required> Alpha</label>"
        "<label><input type='radio' name='pick' value='b' required> Beta</label></div></form>"
    )
    reader.run["profile_hash"] = approved["profile_hash"]
    filled, pending = reader._fill_page(RUN, seen, approved, {})
    assert filled == []
    assert reader.page.locator("input[name=q1]").input_value() == ""
    assert [q["label"] for q in pending] == [
        "A question on the form that Rove could not read (near “Email”)",
        "A question on the form that Rove could not read (near “Notes”)",
        "A question on the form that Rove could not read",
    ]
    assert all(q["label_missing"] for q in pending)
    assert [q["required"] for q in pending] == [True, False, True]
    assert pending[2]["options"] == ["Alpha", "Beta"]
    # The numbered card keeps the flag, so the line points the owner at the screenshot.
    listed = worker.question_list(pending, [], {}, set())
    assert all(q["label_missing"] for q in listed)
    card = workflow.question_lines(workflow.numbered(listed))
    assert card.count("Look at the screenshot in the thread") == 3
    assert "q1" not in card and "pick" not in card
    # A model draft, even one used under the owner's draft policy, never answers it.
    drafted = {
        pending[0]["key"]: {
            "value": "guess@example.invalid",
            "source": "owner Discord message auto-draft:0123456789ab",
        }
    }
    _, again = reader._fill_page(RUN, reader.observe(), approved, drafted)
    assert reader.page.locator("input[name=q1]").input_value() == ""
    assert [q["key"] for q in again] == [q["key"] for q in pending]
    # What the owner then says is used for this form.
    answers = {
        pending[0]["key"]: {"value": "alex@example.invalid", "source": "owner Discord message 3"},
        pending[2]["key"]: {"value": "Beta", "source": "owner Discord message 4"},
    }
    filled, pending = reader._fill_page(RUN, reader.observe(), approved, answers)
    assert reader.page.locator("input[name=q1]").input_value() == "alex@example.invalid"
    assert reader.page.locator("input[name=pick][value=b]").is_checked()
    assert [q["label"] for q in pending] == [
        "A question on the form that Rove could not read (near “Notes”)"
    ]


def test_qwen_is_not_asked_to_draft_an_answer_to_an_unreadable_question(
    state, approved, monkeypatch
):
    from rove import reasoning

    async def evidence(_query):
        return {"results": []}

    monkeypatch.setattr(reasoning, "career_evidence", evidence)
    monkeypatch.setattr(reasoning, "company_context", lambda *_: "")
    monkeypatch.setattr(reasoning, "generate", lambda *a, **k: pytest.fail("nothing to draft"))
    unread = form_reading.unreadable_question(
        {"label": "Email", "key": "aaaaaaaaaaaa", "required": True, "options": []}
    )
    page = {"profile_hash": approved["profile_hash"], "pending": [unread], "text": ""}
    application_id = workflow.enqueue("https://jobs.example.com/unread")["application_id"]
    (state / "applications" / application_id).mkdir(parents=True, exist_ok=True)
    assert reasoning.review_application(application_id, page) == {"answers": []}
    asked = []

    def fake_generate(directory, context, basename, attempts=2):
        asked.append([q["key"] for q in context["questions"]])
        answer = {
            "key": "bbbbbbbbbbbb",
            "kind": "needs_user",
            "value": "",
            "sources": [],
            "explanation": "Only the owner knows.",
        }
        return {
            "model": "m",
            "result": {
                "completed": True,
                "turn_exit_reason": "text_response(finish_reason=stop)",
                "final_response": json.dumps({"answers": [answer]}),
            },
        }

    monkeypatch.setattr(reasoning, "generate", fake_generate)
    page["pending"].append({"key": "bbbbbbbbbbbb", "label": "Why this team?"})
    result = reasoning.review_application(application_id, page)
    assert asked == [["bbbbbbbbbbbb"]]
    assert [a["key"] for a in result["answers"]] == ["bbbbbbbbbbbb"]


def test_an_answer_to_an_unreadable_question_is_not_remembered_for_other_forms(state, approved):
    application_id = workflow.enqueue("https://jobs.example.com/1")["application_id"]
    fields = [
        {"label": "Notes", "name": "q2", "kind": "text", "options": [], "required": True} | extra
        for extra in ({"label_missing": True}, {"name": "notes"})
    ]
    for field in fields:
        field["key"] = workflow.field_key(field)
    directory = state_root() / "applications" / application_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "observation.json").write_text(json.dumps({"fields": fields}))
    command = {"application_id": application_id, "kind": "answer", "value": "Evenings only"}
    worker.apply_command({**command, "field_key": fields[0]["key"]}, "msg-1")
    assert workflow.approved_answers(application_id)[fields[0]["key"]]["value"] == "Evenings only"
    assert workflow.recall_answer("Notes") is None
    worker.apply_command({**command, "field_key": fields[1]["key"]}, "msg-2")
    assert workflow.recall_answer("Notes") == "Evenings only"
