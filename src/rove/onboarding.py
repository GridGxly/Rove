"""Resumable draft interview and locally approved, immutable candidate snapshots.

The model may propose a section. Approval is a separate local CLI operation and is
never registered as an MCP tool. Unknown answers remain null, not guessed defaults.
"""

import hashlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .jobs import database
from .runtime import state_root, write_private_bytes

Text = Annotated[str, Field(min_length=1, max_length=3000)]
Month = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
Decision = Literal["ask", "decline_when_optional"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Identity(StrictModel):
    legal_first_name: Text | None = None
    legal_middle_name: Text | None = None
    legal_last_name: Text | None = None
    preferred_name: Text | None = None
    email: Text | None = None
    phone: Text | None = None
    city: Text | None = None
    state_region: Text | None = None
    country: Text | None = None
    postal_code: Text | None = None
    linkedin: Text | None = None
    github: Text | None = None
    portfolio: Text | None = None


class EducationEntry(StrictModel):
    school: Text
    degree: Text | None = None
    major: Text | None = None
    minor: Text | None = None
    start_month: Month | None = None
    graduation_month: Month | None = None
    currently_enrolled: bool | None = None
    student_year: Text | None = None
    gpa: float | None = Field(default=None, ge=0, le=100)
    gpa_scale: float | None = Field(default=None, gt=0, le=100)
    disclose_gpa: bool | None = None
    honors: list[Text] = Field(default_factory=list, max_length=30)
    relevant_courses: list[Text] = Field(default_factory=list, max_length=50)
    # The entry applications state (education.py). Optional, and left out of the stored
    # profile when unset, so a profile approved before it existed keeps its hash.
    apply_as: bool | None = None

    @model_validator(mode="after")
    def check_dates_and_gpa(self):
        if self.gpa is not None and (self.gpa_scale is None or self.gpa > self.gpa_scale):
            raise ValueError("GPA needs a matching scale and cannot exceed it")
        if self.start_month and self.graduation_month and self.start_month > self.graduation_month:
            raise ValueError("Graduation cannot precede enrollment")
        return self

    @model_serializer(mode="wrap")
    def leave_out_unset_apply_as(self, handler):
        data = handler(self)
        if isinstance(data, dict) and data.get("apply_as") is None:
            data.pop("apply_as", None)
        return data


class Education(StrictModel):
    schools: list[EducationEntry] = Field(default_factory=list, max_length=10)
    returns_to_school_after_internship: bool | None = None
    internship_credit_required: bool | None = None

    @model_validator(mode="after")
    def one_school_to_apply_as(self):
        if sum(1 for school in self.schools if school.apply_as is True) > 1:
            raise ValueError("Only one school can be the one applications state")
        return self


class Eligibility(StrictModel):
    us_work_authorized: bool | None = None
    sponsorship_now: bool | None = None
    sponsorship_future: bool | None = None
    us_citizen: bool | None = None
    other_authorized_countries: list[Text] = Field(default_factory=list, max_length=20)
    restrictions: Text | None = None
    at_least_18: bool | None = None
    export_control_questions: Literal["ask_each_time"] = "ask_each_time"


class Availability(StrictModel):
    earliest_start: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None
    latest_end: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None
    hours_per_week: int | None = Field(default=None, ge=1, le=80)
    term_notes: Text | None = None
    co_op_semester_off: bool | None = None

    @model_validator(mode="after")
    def check_dates(self):
        for value in (self.earliest_start, self.latest_end):
            if value:
                date.fromisoformat(value)
        if self.earliest_start and self.latest_end and self.earliest_start > self.latest_end:
            raise ValueError("Availability ends before it starts")
        return self


class Preferences(StrictModel):
    roles_ranked: list[Text] = Field(default_factory=list, max_length=30)
    title_keywords: list[Text] = Field(default_factory=list, max_length=40)
    excluded_title_keywords: list[Text] = Field(default_factory=list, max_length=40)
    programs: list[Literal["internship", "new-grad"]] = Field(default_factory=list, max_length=2)
    cycles: list[Text] = Field(default_factory=list, max_length=15)
    any_cycle: bool | None = None
    preferred_locations: list[Text] = Field(default_factory=list, max_length=30)
    excluded_locations: list[Text] = Field(default_factory=list, max_length=30)
    work_styles: list[Literal["remote", "hybrid", "onsite"]] = Field(
        default_factory=list, max_length=3
    )
    relocate: bool | None = None
    relocation_support_required: bool | None = None
    minimum_hourly_usd: float | None = Field(default=None, ge=0)
    minimum_salary_usd: float | None = Field(default=None, ge=0)
    unpaid_roles: bool | None = None
    excluded_companies: list[Text] = Field(default_factory=list, max_length=100)
    excluded_industries: list[Text] = Field(default_factory=list, max_length=30)
    priority_companies: list[Text] = Field(default_factory=list, max_length=50)
    other_requirements: list[Text] = Field(default_factory=list, max_length=30)


class Evidence(StrictModel):
    resume_path: Text | None = None
    resume_current_as_of: Month | None = None
    approved_public_links: list[Text] = Field(default_factory=list, max_length=50)
    skills: list[Text] = Field(default_factory=list, max_length=100)
    project_notes: list[Text] = Field(default_factory=list, max_length=30)
    experience_notes: list[Text] = Field(default_factory=list, max_length=30)
    existing_applications_notes: Text | None = None
    referral_policy: Text | None = None


class Stories(StrictModel):
    introduction: Text | None = None
    motivation: Text | None = None
    proudest_project: Text | None = None
    teamwork: Text | None = None
    leadership: Text | None = None
    challenge_and_learning: Text | None = None
    career_direction: Text | None = None
    writing_voice: Text | None = None
    avoid_in_writing: list[Text] = Field(default_factory=list, max_length=30)


class ApplicationPolicy(StrictModel):
    mode: Literal["review_before_submit"] = "review_before_submit"
    written_answers: Literal["review_each"] = "review_each"
    unknown_facts: Literal["ask"] = "ask"
    demographics: Decision = "ask"
    disability: Decision = "ask"
    veteran_status: Decision = "ask"
    salary_questions: Literal["ask", "use_approved_range"] = "ask"
    address_questions: Literal["ask"] = "ask"
    account_creation: Literal["ask"] = "ask"
    marketing_opt_in: bool | None = None
    talent_network_opt_in: bool | None = None
    daily_review_limit: int | None = Field(default=None, ge=1, le=100)
    notifications: Text | None = None
    quiet_hours: Text | None = None
    timezone: Text | None = None


class Candidate(StrictModel):
    schema_version: Literal[1] = 1
    identity: Identity = Field(default_factory=Identity)
    education: Education = Field(default_factory=Education)
    eligibility: Eligibility = Field(default_factory=Eligibility)
    availability: Availability = Field(default_factory=Availability)
    preferences: Preferences = Field(default_factory=Preferences)
    evidence: Evidence = Field(default_factory=Evidence)
    stories: Stories = Field(default_factory=Stories)
    application_policy: ApplicationPolicy = Field(default_factory=ApplicationPolicy)


SECTIONS = [key for key in Candidate.model_fields if key != "schema_version"]


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def atomic_private(path: Path, text: str):
    write_private_bytes(path, text.encode())


def onboarding_db():
    db = database()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS onboarding (
          id INTEGER PRIMARY KEY CHECK(id=1), draft_hash TEXT NOT NULL,
          revision INTEGER NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS profile_versions (
          hash TEXT PRIMARY KEY, snapshot_path TEXT NOT NULL, approved_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS active_profile (
          id INTEGER PRIMARY KEY CHECK(id=1), hash TEXT NOT NULL REFERENCES profile_versions(hash));
        CREATE TABLE IF NOT EXISTS onboarding_events (
          id INTEGER PRIMARY KEY, event TEXT NOT NULL, section TEXT,
          draft_hash TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS onboarding_issues (
          key TEXT PRIMARY KEY, detail TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0);
    """)
    return db


def vault_note() -> Path:
    configured = os.environ.get("OBSIDIAN_VAULT_PATH")
    if not configured:
        config = state_root() / "config/recruiting.json"
        if config.exists():
            configured = json.loads(config.read_text()).get("obsidian_vault_path")
    if not configured:
        raise ValueError("Configure OBSIDIAN_VAULT_PATH before approving a profile")
    vault = Path(configured).expanduser().resolve()
    if not vault.is_dir():
        raise ValueError("Configured Obsidian vault is not a directory")
    return vault / "Rove/Profile/Candidate.md"


def draft() -> dict:
    path = state_root() / "onboarding/draft.json"
    if not path.exists():
        return Candidate().model_dump()
    return Candidate.model_validate_json(path.read_text()).model_dump()


def propose(section: str, values: dict, expected_hash: str) -> dict:
    """Replace one known draft section with optimistic concurrency; never approve it."""
    if section not in SECTIONS:
        raise ValueError("Unknown onboarding section")
    if len(json.dumps(values)) > 50000:
        raise ValueError("Section is too large")
    db = onboarding_db()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            current = draft()
            if digest(current) != expected_hash:
                raise ValueError("Draft changed; read it again before proposing edits")
            current[section] = values
            current = Candidate.model_validate(current).model_dump()
            new_hash = digest(current)
            now = datetime.now(UTC).isoformat()
            atomic_private(
                state_root() / "onboarding/draft.json", json.dumps(current, indent=2) + "\n"
            )
            db.execute(
                "INSERT INTO onboarding VALUES(1,?,1,?) ON CONFLICT(id) "
                "DO UPDATE SET draft_hash=excluded.draft_hash,revision=revision+1,"
                "updated_at=excluded.updated_at",
                (new_hash, now),
            )
            db.execute(
                "INSERT INTO onboarding_events(event,section,draft_hash,created_at) "
                "VALUES ('proposal',?,?,?)",
                (section, new_hash, now),
            )
    finally:
        db.close()
    return {"saved": True, "approved": False, "section": section, "draft_hash": new_hash}


def gaps(profile: dict) -> list[str]:
    missing = []
    for section, keys in {
        "identity": ["legal_first_name", "legal_last_name", "email", "phone", "city", "country"],
        "education": ["schools"],
        "eligibility": ["us_work_authorized", "sponsorship_now", "sponsorship_future"],
        "availability": ["earliest_start", "hours_per_week"],
        "preferences": ["roles_ranked", "title_keywords", "programs", "work_styles"],
        "evidence": ["resume_path"],
    }.items():
        for key in keys:
            if profile[section][key] is None or profile[section][key] in ("", []):
                missing.append(f"{section}.{key}")
    for i, school in enumerate(profile["education"]["schools"]):
        for key in ("degree", "major", "graduation_month", "currently_enrolled"):
            if school[key] is None:
                missing.append(f"education.schools.{i}.{key}")
    if not profile["preferences"]["cycles"] and profile["preferences"]["any_cycle"] is not True:
        missing.append("preferences.cycles_or_any_cycle")
    return missing


def onboarding_status(section: str | None = None) -> dict:
    if section and section not in SECTIONS:
        raise ValueError("Unknown onboarding section")
    current = draft()
    db = onboarding_db()
    try:
        active = db.execute("SELECT hash FROM active_profile WHERE id=1").fetchone()
        issues = [
            dict(row)
            for row in db.execute("SELECT key,detail FROM onboarding_issues WHERE resolved=0")
        ]
    finally:
        db.close()
    result = {
        "draft_hash": digest(current),
        "approved_hash": active[0] if active else None,
        "sections": SECTIONS,
        "missing": gaps(current),
        "draft_is_approved": bool(active and active[0] == digest(current)),
        "submission_enabled": False,
        "unresolved_conflicts": issues,
    }
    if section:
        result["section"] = section
        result["values"] = current[section]
        result["schema"] = Candidate.model_fields[section].annotation.model_json_schema()
    return result


def read_approved(_db=None) -> dict:
    db = _db or onboarding_db()
    try:
        row = db.execute(
            "SELECT p.* FROM profile_versions p JOIN active_profile a ON a.hash=p.hash WHERE a.id=1"
        ).fetchone()
    finally:
        if _db is None:
            db.close()
    if not row:
        raise ValueError("No candidate profile has been approved")
    snapshot = json.loads(Path(row["snapshot_path"]).read_text())
    profile = Candidate.model_validate(snapshot["profile"]).model_dump()
    if digest(profile) != row["hash"]:
        raise ValueError("Approved snapshot changed; review required")
    note = vault_note().read_text()
    if not note.startswith("---\n"):
        raise ValueError("Candidate note is missing validated frontmatter")
    front = yaml.safe_load(note.split("---\n", 2)[1])
    canonical = Candidate.model_validate(front["profile"]).model_dump()
    if digest(canonical) != row["hash"] or front.get("profile_hash") != row["hash"]:
        raise ValueError("Obsidian profile changed after approval; review it before use")
    return {"profile_hash": row["hash"], "profile": profile, "missing": gaps(profile)}


def approve(expected_hash: str) -> dict:
    """Local owner operation, deliberately absent from the model's MCP surface."""
    db = onboarding_db()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            current = draft()
            if digest(current) != expected_hash:
                raise ValueError("The reviewed draft changed; approval rejected")
            if current == Candidate().model_dump():
                raise ValueError("An empty profile cannot be approved")
            if db.execute("SELECT 1 FROM onboarding_issues WHERE resolved=0 LIMIT 1").fetchone():
                raise ValueError("Resolve the recorded onboarding conflicts before approval")
            active = db.execute("SELECT hash FROM active_profile WHERE id=1").fetchone()
            note = vault_note()
            if active:
                read_approved(db)  # Refuse to overwrite unreviewed canonical edits.
            elif note.exists():
                raise ValueError("Existing candidate note needs explicit import/review first")
            now = datetime.now(UTC).isoformat()
            snapshot = state_root() / f"profiles/snapshots/{expected_hash}.json"
            contents = {"profile": current, "profile_hash": expected_hash, "approved_at": now}
            if snapshot.exists():
                if digest(json.loads(snapshot.read_text())["profile"]) != expected_hash:
                    raise ValueError("Existing immutable snapshot failed its hash check")
            else:
                atomic_private(snapshot, json.dumps(contents, indent=2) + "\n")
            front = yaml.safe_dump(contents, sort_keys=False, allow_unicode=True)
            atomic_private(
                note,
                "---\n" + front + "---\n\n# Candidate profile\n\n"
                "The structured fields above contain reviewed answers. Null means unknown.\n"
                "Changes require validation and a new approval before application use.\n",
            )
            db.execute(
                "INSERT OR IGNORE INTO profile_versions VALUES (?,?,?)",
                (expected_hash, str(snapshot), now),
            )
            db.execute("INSERT OR REPLACE INTO active_profile VALUES (1,?)", (expected_hash,))
            db.execute(
                "INSERT INTO onboarding_events(event,draft_hash,created_at) "
                "VALUES ('owner_approval',?,?)",
                (expected_hash, now),
            )
    finally:
        db.close()
    return {"approved_hash": expected_hash, "missing": gaps(current), "submission_enabled": False}
