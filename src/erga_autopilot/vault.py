"""Human-readable application notes in the private Obsidian vault.

One note per application under `Erga Autopilot/Applications/`, rebuilt from SQLite and
the private artifacts whenever the application changes. The note is for reading and
searching in Obsidian; it is never a candidate fact and never feeds the profile.
"""

import json
import os
import re
from pathlib import Path

from . import workflow
from .onboarding import atomic_private, vault_note
from .runtime import state_root


def vault_root() -> Path:
    return vault_note().parent.parent


def safe_name(text: str, limit: int = 80) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:limit].rstrip() or "Application"


def sync_answers() -> Path | None:
    """A readable copy of the answers the owner gave once; SQLite stays the exact store."""
    if not os.environ.get("OBSIDIAN_VAULT_PATH"):
        return None
    path = vault_root() / "Answers.md"
    rows = workflow.remembered_answers()
    lines = [
        "# Remembered answers",
        "",
        "Facts you answered once in Discord. Autopilot fills them on any later form that asks the",
        "same question. Edit or remove a line here and tell Autopilot in `#memory` to change it;",
        "the exact store is the local database.",
        "",
        "| Question | Answer | Remembered |",
        "| --- | --- | --- |",
    ]
    for row in rows:
        label = row["label"].replace("|", "/")
        lines.append(f"| {label} | {row['value'].replace('|', '/')} | {row['created_at'][:10]} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def note_path(item: dict) -> Path:
    return (
        vault_root()
        / "Applications"
        / f"{safe_name(workflow.display_title(item))} · {item['id']}.md"
    )


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def render(item: dict, events: list[dict], package: dict, review: dict, proposals: dict) -> str:
    title = workflow.display_title(item)
    forum = workflow.forum_url(item["id"]) or ""
    lines = [
        "---",
        "type: application",
        f"application_id: {item['id']}",
        f"status: {item['status']}",
        f"source: {item['source']}",
        f"url: {item['url']}",
        f"forum: {forum}",
        f"profile_hash: {item['profile_hash']}",
        f"package_hash: {item.get('package_hash') or ''}",
        f"resume_sha256: {package.get('resume_sha256') or ''}",
        f"updated: {item['updated_at']}",
        "authority: derived from local workflow state; not a candidate fact",
        "---",
        "",
        f"# {title}",
        "",
        f"**Status:** {item['status']}  ",
        f"**Posting:** {item['url']}  ",
    ]
    if forum:
        lines.append(f"**Forum:** {forum}  ")
    if review:
        lines += ["", "## Job fit", "", f"Decision: **{review.get('decision')}**", ""]
        if review.get("rationale"):
            lines += [review["rationale"], ""]
        for req in review.get("requirements", []):
            mark = {"satisfied": "✅", "conflict": "⛔"}.get(req.get("status"), "❓")
            lines.append(
                f"- {mark} {req.get('requirement', '')[:160]}"
                + (" _(checked by code)_" if req.get("checked_by") == "code" else "")
            )
    filled = package.get("filled", [])
    if filled:
        lines += ["", "## Answers on the form", "", "| Field | Value | Source |", "|---|---|---|"]
        for field in filled:
            value = field.get("value") or f"resume PDF `{(field.get('sha256') or '')[:12]}`"
            lines.append(
                f"| {field.get('label', '')} | {str(value).replace('|', '/')[:200]} | {field.get('source', '')} |"
            )
    pending = package.get("pending", [])
    if pending:
        lines += ["", "## Open questions", ""]
        drafts = {a["key"]: a for a in proposals.get("answers", [])}
        for question in pending:
            lines.append(f"- **{question.get('label', '')}** · `{question.get('key', '')}`")
            draft = drafts.get(question.get("key"))
            if draft and draft.get("kind") == "proposal":
                lines.append(f"  - Qwen draft: {draft['value']}")
                if draft.get("unslop"):
                    lines.append(f"  - Unslop: {draft['unslop']}")
                if draft.get("approve_command"):
                    lines.append(f"  - Approve: `{draft['approve_command']}`")
            elif draft:
                lines.append(f"  - Needs you: {draft.get('explanation', '')}")
    lines += ["", "## Timeline", ""]
    for event in events[-40:]:
        data = event.get("data", {})
        detail = ""
        if event["kind"] == "lifecycle":
            detail = f"{data.get('from')} → {data.get('to')} · {data.get('trigger', '')}"
        elif event["kind"] in {"needs_action", "shortlisted"}:
            detail = str(data.get("reason", ""))[:160]
        elif event["kind"] == "submission_confirmed":
            detail = str(data.get("confirmation_url", ""))
        elif event["kind"] == "opened":
            detail = str(data.get("url", ""))
        lines.append(
            f"- {event['created_at'][:19]} · {event['kind'].replace('_', ' ')}"
            + (f" · {detail}" if detail else "")
        )
    return "\n".join(lines) + "\n"


def sync_application(application_id: str) -> Path | None:
    """Rewrite the note for one application; a missing vault is not an error."""
    try:
        item = workflow.get(application_id)
        path = note_path(item)
    except ValueError:
        return None
    with workflow.db() as conn:
        rows = conn.execute(
            "SELECT kind,data,created_at FROM application_events WHERE application_id=? ORDER BY id",
            (application_id,),
        ).fetchall()
    events = [
        {"kind": r["kind"], "data": json.loads(r["data"]), "created_at": r["created_at"]}
        for r in rows
    ]
    directory = state_root() / "applications" / application_id
    text = render(
        item,
        events,
        _load(directory / "package.json"),
        _load(directory / "job-review.json"),
        _load(directory / "answer-proposals.json"),
    )
    atomic_private(path, text)
    return path
