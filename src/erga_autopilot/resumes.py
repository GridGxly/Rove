"""Trusted bridge to Erga's supported intake; no arbitrary model-selected paths."""

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .onboarding import read_approved
from .runtime import state_root, write_private


async def erga_call(name: str, arguments: dict) -> dict:
    if name not in {
        "intake_job_url",
        "validate_tailored_resume",
        "confirm_application_submission",
        "update_application_status",
    }:
        raise PermissionError("Unsupported Erga workflow operation")
    parameters = StdioServerParameters(
        command=str(Path.home() / ".local/bin/erga-mcp"),
        env=dict(
            os.environ,
            ERGA_MCP_CONFIG=str(state_root() / "erga/config.toml"),
            ERGA_MCP_TOOL_PROFILE="default",
        ),
    )
    async with (
        stdio_client(parameters) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        result = (await session.call_tool(name, arguments)).model_dump(by_alias=True)
    if result.get("isError"):
        # Full upstream details are private evidence, never raw public logs.
        raise RuntimeError("Erga could not complete this operation; inspect its private result")
    return result.get("structuredContent", {})


def prepare_resume(application_id: str, url: str) -> dict:
    directory = state_root() / "applications" / application_id
    saved = directory / "erga-result.json"
    if saved.exists():
        result = json.loads(saved.read_text())
    else:
        result = asyncio.run(
            erga_call("intake_job_url", {"job_url": url, "application_slug": application_id})
        )
        write_private(saved, result)
    data = result.get("result", result)
    if not isinstance(data, dict):
        raise TypeError("Unexpected Erga response")
    # Locate only a returned PDF within the configured private Erga output root.
    import tomllib

    configuration = tomllib.loads((state_root() / "erga/config.toml").read_text())
    output_root = Path(configuration["resume"]["output_root"]).expanduser().resolve()
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
            "warning": "Erga tailoring did not pass layout validation; using the approved base PDF for review.",
            "job_url": url,
            "application_id": data.get("application_id"),
            "tailoring_review_required": True,
        }
        write_private(directory / "resume-manifest.json", manifest)
        return manifest
    source = paths[0]
    tex = source.with_suffix(".tex")
    if not tex.is_file():
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
    manifest = {
        "ready": True,
        "tailored": bool(data.get("tailoring_meaningful_change")),
        "resume_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "source": "Erga job intake",
        "job_url": url,
        "validation": check,
        "application_id": data.get("application_id"),
        "tailoring_review_required": True,
    }
    write_private(directory / "resume-manifest.json", manifest)
    return manifest
