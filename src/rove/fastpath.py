"""Decisions code makes so the local model is not asked twice, or for nothing.

Most of an application's time is the model's. Two things here keep that time for the work
only the model can do: a job-fit review is stored once per job and read back on every
later pass, and a drafting call is made only when a pending question needs writing or a
choice. Neither loosens a check: code re-runs its own comparisons each time a stored
review is read, and a question the model is not asked goes to the owner, never to a guess.
Which questions those are is the shared draft gate's call (questions.py), the same one
the review and the auto-use policy apply to whatever the model returns.
"""

import hashlib
import json

from . import questions, timing, workflow


def posting_hash(text: str) -> str:
    """The posting's content: the same words are the same posting however they wrapped."""
    return hashlib.sha256(" ".join(str(text or "").split()).casefold().encode()).hexdigest()


def review_key(job_text: str, profile_hash: str, prompt_version: str) -> tuple[str, str, str]:
    """What a stored job-fit review depends on. A change to any part is a new review."""
    return posting_hash(job_text), str(profile_hash), str(prompt_version)


def review_db():
    conn = workflow.db()
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS fit_reviews(
        application_id TEXT NOT NULL, posting_hash TEXT NOT NULL, profile_hash TEXT NOT NULL,
        prompt_version TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(application_id, posting_hash, profile_hash, prompt_version));
    """)
    return conn


def stored_review(application_id: str, key: tuple[str, str, str]) -> dict | None:
    """The review this job already has for exactly this posting, profile and prompt."""
    with review_db() as conn:
        row = conn.execute(
            "SELECT result FROM fit_reviews WHERE application_id=? AND posting_hash=? "
            "AND profile_hash=? AND prompt_version=?",
            (application_id, *key),
        ).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["result"])
    except ValueError:
        return None
    # Without the model's own output there is nothing for code to re-evaluate.
    return result if isinstance(result, dict) and result.get("qwen_output") else None


def review_change(application_id: str, key: tuple[str, str, str]) -> str:
    """Why this job needs a model review now, for the timing record: `first`, or which
    parts of the key differ from its latest stored review (`posting`, `profile`, `prompt`)."""
    try:
        with review_db() as conn:
            row = conn.execute(
                "SELECT posting_hash, profile_hash, prompt_version FROM fit_reviews "
                "WHERE application_id=? ORDER BY created_at DESC LIMIT 1",
                (application_id,),
            ).fetchone()
    except Exception:  # noqa: BLE001 -- a reason that cannot be read is not worth a failure
        return "unknown"
    if not row:
        return "first"
    names = ("posting", "profile", "prompt")
    return "_".join(n for n, old, new in zip(names, row, key, strict=True) if old != new) or "same"


def store_review(application_id: str, key: tuple[str, str, str], result: dict):
    with review_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO fit_reviews VALUES(?,?,?,?,?,?)",
            (application_id, *key, json.dumps(result), workflow.now()),
        )


# The form wants a file nobody approved: a draft cannot be uploaded.
UNAPPROVED_FILE = "Unapproved required file requested"
CHOICE_KINDS = {"radio_group", "choice", "select-one", "select-multiple", "checkbox", "radio"}
SHORT_KINDS = {"text", "search", "url", "email", "tel", "number", "date", "month", "week", "time"}


def owner_only(question: dict, kind: str = "") -> bool:
    """Whether a model draft could never be the answer to this pending question.

    The verdict is the shared draft gate's (questions.py): a legal or personal question,
    or one whose text the form did not give, is the owner's whatever a model would say,
    and the review and the auto-use policy refuse such drafts for the same reason. A file
    the form wants that nobody approved is the owner's too: no draft can be uploaded.
    """
    if (
        question.get("manual")
        or question.get("reason") == UNAPPROVED_FILE
        or kind in {"file", "password", "hidden"}
    ):
        return True
    return questions.draft_gate({**question, "kind": kind}) is not None


def question_kind(question: dict, field: dict | None = None) -> str:
    """What a pending item needs: writing, choice, short answer, owner or control repair.

    Known-value control failures and owner-only facts bypass drafting. Unknown controls
    still go to the model; only a proven unusable draft is skipped.
    """
    if question.get("control_issue"):
        return "control"
    kind = str((field or {}).get("kind") or question.get("kind") or "")
    if owner_only(question, kind):
        return "owner"
    if question.get("options") or kind in CHOICE_KINDS:
        return "choice"
    if kind in SHORT_KINDS:
        return "short"
    return "writing"


def drafting_counts(pending: list, fields: list | None = None) -> dict:
    """How many pending questions need writing, a choice, a short answer, or the owner."""
    by_key = {f.get("key"): f for f in fields or [] if isinstance(f, dict) and f.get("key")}
    counts = {"questions": 0, "writing": 0, "choices": 0, "short": 0, "owner_only": 0}
    names = {"writing": "writing", "choice": "choices", "short": "short", "owner": "owner_only"}
    for question in pending:
        counts["questions"] += 1
        kind = question_kind(question, by_key.get(question.get("key")))
        name = "control_issues" if kind == "control" else names[kind]
        counts[name] = counts.get(name, 0) + 1
    return counts


def needs_model(counts: dict) -> bool:
    """Whether a drafting call has anything to write or choose."""
    return counts["writing"] + counts["choices"] + counts["short"] > 0


def fill_counts(application_id: str, page: dict) -> dict:
    """Who supplied each filled field: code (approved facts, policy, remembered answers),
    a model draft, or an answer the owner gave for this application. Measurement only."""
    supplied = {}
    try:
        conn = timing.connect()  # a busy database is not waited for; the count is left out
        try:
            rows = conn.execute(
                "SELECT a.field_key, a.owner_message_id, c.kind FROM application_answers a "
                "LEFT JOIN owner_commands c ON c.message_id=a.owner_message_id "
                "WHERE a.application_id=?",
                (application_id,),
            ).fetchall()
        finally:
            conn.close()
        for field_key, source, command in rows:
            if str(source).startswith("auto-skip:"):
                continue
            drafted = str(source).startswith("auto-draft:") or command == "use"
            supplied[field_key] = "model" if drafted else "owner"
    except Exception:  # noqa: BLE001 -- a count that cannot be read is left out
        return {}
    counts = {"fields_code": 0, "fields_model": 0, "fields_owner": 0}
    for entry in page.get("filled") or []:
        counts["fields_" + supplied.get(entry.get("key"), "code")] += 1
    counts["fields_pending"] = len(page.get("pending") or [])
    return counts
