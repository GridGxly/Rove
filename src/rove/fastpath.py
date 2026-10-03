"""Decisions code makes so the local model is not asked twice, or for nothing.

Most of an application's time is the model's. Two things here keep that time for the work
only the model can do: a job-fit review is stored once per job and read back on every
later pass, and a drafting call is made only when a pending question needs writing or a
choice. Neither loosens a check: code re-runs its own comparisons each time a stored
review is read, and a question the model is not asked goes to the owner, never to a guess.
"""

import hashlib
import json
import re

from . import timing, workflow


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


# Questions a model draft is never the answer to, and that the approved profile has no
# field for: clearance, criminal history, self-identification, signed statements. Only the
# owner can answer them. Kept narrow on purpose. Work authorization, sponsorship,
# citizenship and age are left out because the profile can hold them: until code resolves
# every wording of those, skipping the model would ask the owner for a fact he already
# approved. A label this misses is sent to the model as before.
OWNER_ONLY = re.compile(
    r"security clearance|criminal|convict|felon|misdemeanou?r|background check"
    r"|\bgender\b|\brace\b|ethnic|hispanic|latin[oax]\b|veteran|disabilit|sexual orientation"
    r"|self.?identif|date of birth"
    r"|\bi (?:hereby )?(?:certify|acknowledge|attest|agree|consent)\b|signature",
    re.IGNORECASE,
)
# The form wants a file nobody approved: a draft cannot be uploaded.
UNAPPROVED_FILE = "Unapproved required file requested"
CHOICE_KINDS = {"radio_group", "choice", "select-one", "select-multiple", "checkbox", "radio"}
SHORT_KINDS = {"text", "search", "url", "email", "tel", "number", "date", "month", "week", "time"}


def owner_only(question: dict) -> bool:
    """Whether a model draft may never answer this question.

    The one place the drafting gate asks what kind of question this is. When the shared
    question classifier lands, this body becomes its call (`questions.draft_gate(question)`)
    and OWNER_ONLY above goes away.
    """
    return bool(OWNER_ONLY.search(str(question.get("label") or "")))


def question_kind(question: dict, field: dict | None = None) -> str:
    """What a pending question needs: `writing`, `choice`, `short` or `owner`.

    `owner` is the only kind the model is not asked. Anything else, an unknown control
    included, still goes to the model, so the call is skipped only when code is sure a
    draft could not be used.
    """
    kind = str((field or {}).get("kind") or "")
    if (
        question.get("reason") == UNAPPROVED_FILE
        or kind in {"file", "password", "hidden"}
        # A question whose text could not be read is never drafted; the call leaves it out.
        or question.get("label_missing")
        or owner_only(question)
    ):
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
        counts[names[question_kind(question, by_key.get(question.get("key")))]] += 1
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
