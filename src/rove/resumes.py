"""Trusted bridge to Erga's supported intake; no arbitrary model-selected paths."""

import asyncio
import fcntl
import functools
import hashlib
import json
import shutil
import threading
import tomllib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import erga_session
from .onboarding import read_approved
from .reasoning import posting_text_for
from .runtime import state_root, write_private

# Erga rejects supplied job text above its 2 MiB page-snapshot limit.
ERGA_JOB_TEXT_MAX_BYTES = 2 * 1024 * 1024
WORKFLOW_TOOLS = {
    "intake_job_url",
    "validate_tailored_resume",
    "confirm_application_submission",
    "update_application_status",
}
# This many identical intake failures in a row pause intake for PAUSE_MINUTES; the
# approved base PDF is used meanwhile. Both are workflow settings.
FAILURE_LIMIT = 3
PAUSE_MINUTES = 60
# The thread line reads "Resume ready · your approved base PDF · <reason>".
INTAKE_FAILED = "Erga's tailoring failed; the system log has the details"
LAYOUT_REJECTED = "Erga's tailored draft failed its layout check"


async def erga_call(name: str, arguments: dict) -> dict:
    if name not in WORKFLOW_TOOLS:
        raise PermissionError("Unsupported Erga workflow operation")
    # Raises erga_session.ErgaError (a RuntimeError) when Erga could not do it.
    return await erga_session.call(name, arguments)


def base_resume_manifest(directory: Path, url: str, warning: str, erga_id=None) -> dict:
    source = Path(read_approved()["profile"]["evidence"]["resume_path"]).resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        return {"ready": False, "reason": "No validated generated or approved base PDF"}
    target = directory / "resume.pdf"
    shutil.copyfile(source, target)
    target.chmod(0o600)
    manifest = {
        "ready": True,
        "tailored": False,
        "source": "approved factual base resume",
        "resume_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "warning": warning,
        "job_url": url,
        "application_id": erga_id,
        "tailoring_review_required": True,
    }
    write_private(directory / "resume-manifest.json", manifest)
    return manifest


def capture_posting(directory: Path, text: str):
    """Keep the posting the browser read, for Erga, before the job-fit review stores its own."""
    if text.strip():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(directory / "erga-job-text.json", {"job_text": text})


def intake_arguments(application_id: str, url: str, directory: Path) -> dict:
    """Erga's intake request, carrying the posting the browser captured when there is one.

    Careers sites that refuse Erga's own fetch still served the page to the recruiting
    browser; the text the job-fit review reads lets Erga tailor from that instead.
    """
    arguments = {"job_url": url, "application_slug": application_id}
    text = ""
    captured = directory / "erga-job-text.json"
    if captured.is_file():
        try:
            text = str(json.loads(captured.read_text()).get("job_text") or "")
        except (ValueError, OSError):
            text = ""
    text = text or posting_text_for(directory, {})
    if text.strip():
        bounded = text.encode("utf-8")[:ERGA_JOB_TEXT_MAX_BYTES]
        arguments["job_text"] = bounded.decode("utf-8", errors="ignore")
    return arguments


def _number(key: str, default: int) -> int:
    from . import workflow

    value = workflow.config().get(key)
    if value is None or isinstance(value, bool):
        return default
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return default


def health_path() -> Path:
    return state_root() / "erga-intake.json"


def intake_health() -> dict:
    """Consecutive identical intake failures, any pause, and the last fallback reason."""
    try:
        return json.loads(health_path().read_text())
    except (OSError, ValueError):
        return {}


def paused_until(health: dict, now: datetime) -> datetime | None:
    try:
        until = datetime.fromisoformat(health["paused_until"])
    except (KeyError, TypeError, ValueError):
        return None
    return until if until > now else None


def note_failure(signature: str) -> dict:
    """Count a failed intake; the configured run of identical failures pauses intake."""
    now = datetime.now(UTC)
    health = intake_health()
    same = health.get("signature") == signature
    count = int(health.get("count") or 0) + 1 if same else 1
    limit = _number("erga_failure_limit", FAILURE_LIMIT)
    minutes = _number("erga_pause_minutes", PAUSE_MINUTES)
    health.update(signature=signature, count=count, last_failure=now.isoformat())
    paused = bool(limit) and bool(minutes) and count >= limit
    if paused:
        health["paused_until"] = (now + timedelta(minutes=minutes)).isoformat()
    write_private(health_path(), health)
    return {"count": count, "paused": paused, "minutes": minutes}


def forget(*keys: str):
    health = intake_health()
    if any(key in health for key in keys):
        write_private(health_path(), {k: v for k, v in health.items() if k not in keys})


def note_answered():
    """Erga answered the intake: a run of identical failures, and any pause, ends."""
    forget("signature", "count", "paused_until", "last_failure")


def note_tailored():
    """A tailored resume was used: the next fallback, whatever its reason, is news again."""
    forget("last_fallback")


def first_fallback(reason: str) -> bool:
    """True when this fallback reason differs from the last one the owner was told."""
    health = intake_health()
    new = health.get("last_fallback") != reason
    if new:
        write_private(health_path(), {**health, "last_fallback": reason})
    return new


def fallback(directory: Path, url: str, warning: str, reason: str, note: str = "", erga_id=None):
    """The approved base PDF, saying why once per reason; technical detail for the log."""
    manifest = base_resume_manifest(directory, url, warning, erga_id)
    if manifest.get("ready"):
        manifest.update(
            fallback_reason=reason,
            warning_is_new=first_fallback(reason),
            system_note=note,
            announced=False,
        )
        write_private(directory / "resume-manifest.json", manifest)
    return manifest


@contextmanager
def preparation_lock(directory: Path):
    """One resume preparation per application, even across worker processes."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(directory / ".resume.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def prepare_resume(application_id: str, url: str) -> dict:
    directory = state_root() / "applications" / application_id
    with preparation_lock(directory):
        existing = directory / "resume-manifest.json"
        if existing.exists():
            # Another worker finished this application's resume while this one waited.
            return json.loads(existing.read_text())
        return _prepare(application_id, url, directory)


def _prepare(application_id: str, url: str, directory: Path) -> dict:
    saved = directory / "erga-result.json"
    if saved.exists():
        result = json.loads(saved.read_text())
    else:
        health = intake_health()
        until = paused_until(health, datetime.now(UTC))
        if until:
            # Erga failed the same way several times in a row: no call until the pause ends.
            write_private(
                directory / "erga-error.json",
                {"skipped": "intake paused", "until": until.isoformat(), "job_url": url},
            )
            return fallback(directory, url, INTAKE_FAILED, f"intake: {health.get('signature')}")
        try:
            result = asyncio.run(
                erga_call("intake_job_url", intake_arguments(application_id, url, directory))
            )
        except RuntimeError as error:
            # Erga could not build a role-specific proposal at all. That is a tailoring
            # failure, not a reason to stop: keep the approved factual base PDF, say so
            # once, and preserve the failure for review.
            signature = getattr(error, "signature", f"intake_job_url · {error}"[:240])
            run = note_failure(signature)
            write_private(
                directory / "erga-error.json",
                {"error": str(error), "cause": signature, "job_url": url},
            )
            note = f"erga intake failed · {signature} · {run['count']} in a row"
            if run["paused"]:
                note += f" · intake paused for {run['minutes']} min, base PDF meanwhile"
            return fallback(directory, url, INTAKE_FAILED, f"intake: {signature}", note)
        note_answered()
        write_private(saved, result)
    data = result.get("result", result)
    if not isinstance(data, dict):
        raise TypeError("Unexpected Erga response")
    # Locate only a returned PDF within the configured private Erga output root.
    configuration = tomllib.loads((state_root() / "erga/config.toml").read_text())
    output_root = Path(configuration["resume"]["output_root"]).expanduser()
    if not output_root.is_absolute():
        # Erga resolves a relative output root from its config file's folder.
        output_root = state_root() / "erga" / output_root
    output_root = output_root.resolve()
    candidates = []

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"proposal_pdf", "pdf", "resume_pdf"} and isinstance(item, str):
                    candidates.append(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(data)
    paths = []
    for candidate in candidates:
        path = Path(candidate).expanduser().resolve()
        if path.is_relative_to(output_root) and path.suffix.lower() == ".pdf" and path.is_file():
            paths.append(path)
    if not paths:
        # Failed tailoring never silently becomes a valid generated resume. Preserve
        # the approved factual base and expose the failure in the review package.
        validation = data.get("validation") or {}
        fill = validation.get("page_fill_ratio")
        manifest = fallback(
            directory,
            url,
            LAYOUT_REJECTED,
            "layout",
            f"erga draft rejected · {validation.get('skipped') or 'no PDF returned'}"[:300],
            data.get("application_id"),
        )
        if manifest.get("ready") and data.get("proposal_tex"):
            manifest.update(rejected_proposal_tex=str(data["proposal_tex"]), page_fill_ratio=fill)
            write_private(directory / "resume-manifest.json", manifest)
        return manifest
    source = paths[0]
    # Erga names the PDF after the candidate; its source is the package's proposal.tex.
    tex = Path(str(data.get("proposal_tex") or source.with_suffix(".tex"))).expanduser().resolve()
    if tex.suffix != ".tex" or tex.parent != source.parent or not tex.is_file():
        return {
            "ready": False,
            "reason": "Generated PDF has no matching source for independent validation",
        }
    validation = asyncio.run(erga_call("validate_tailored_resume", {"proposal_tex": str(tex)}))
    check = validation.get("result", validation)
    if check.get("returncode") != 0 or check.get("skipped") or not check.get("pdf"):
        return {"ready": False, "reason": "Erga render validation failed"}
    target = directory / "resume.pdf"
    shutil.copyfile(source, target)
    target.chmod(0o600)
    note_tailored()
    manifest = {
        "ready": True,
        "tailored": bool(data.get("tailoring_meaningful_change")),
        "resume_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "source": "Erga job intake",
        "job_url": url,
        "validation": check,
        "application_id": data.get("application_id"),
        "tailoring_review_required": True,
        "announced": False,
    }
    write_private(directory / "resume-manifest.json", manifest)
    return manifest


class Preparation:
    """Erga's intake for one application on a background thread, joined before upload."""

    def __init__(self, prepare, application_id: str, url: str):
        self.value: dict | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(
            target=self._run, args=(prepare, application_id, url), name=f"resume-{application_id}"
        )

    def _run(self, prepare, application_id: str, url: str):
        try:
            self.value = prepare(application_id, url)
        except BaseException as error:  # noqa: BLE001 -- re-raised in the worker at join
            self.error = error

    def result(self) -> dict:
        self.thread.join()
        if self.error is not None:
            raise self.error
        assert self.value is not None
        return self.value


_running: list[Preparation] = []


def start_preparation(prepare, application_id: str, url: str, text: str) -> Preparation | None:
    """Begin Erga's intake beside the job-fit review, unless the resume is already settled.

    `prepare` is the worker's own prepare_resume, passed in so a stand-in replaces it too.
    """
    directory = state_root() / "applications" / application_id
    if (directory / "resume-manifest.json").exists():
        return None
    capture_posting(directory, text)
    preparation = Preparation(prepare, application_id, url)
    _running.append(preparation)
    preparation.thread.start()
    return preparation


def one_erga_pass(function):
    """One Erga process serves the whole pass, and no preparation outlives it.

    A pass that ends early (a hold on fit, an error) still waits for a running intake,
    so its files are never left half-written and nothing writes after the pass is over.
    """

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        erga_session.begin_pass()
        try:
            return function(*args, **kwargs)
        finally:
            while _running:
                _running.pop().thread.join()
            erga_session.end_pass()

    return wrapper
