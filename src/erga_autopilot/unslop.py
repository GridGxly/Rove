"""Writing cleanup for Qwen drafts, following the Unslop contract.

Unslop (https://github.com/theclaymethod/unslop, MIT) finds formulaic AI writing and
repairs only the defective spans while preserving facts and voice. When a local clone
is configured, its scanners run on every draft; otherwise a small built-in list of the
same hard tells is used. Findings never change a draft silently: Qwen is asked for one
bounded repair pass and the result is re-scanned and shown with the original.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

from . import workflow

# Digest of the Unslop taboo catalog for the drafting prompt.
RULES = (
    "Write like a careful person, not a press release: no throat-clearing openers "
    "(here's the thing, it's worth noting, in today's world), no emphasis crutches "
    "(this matters because, full stop, make no mistake), no business jargon (leverage, "
    "utilize, robust, seamless, cutting-edge, delve, showcase, testament, spearhead, foster, "
    "harness, streamline, navigate, landscape, game-changer, synergy, scalable, actionable), "
    "no 'not just X but Y', no rhetorical questions, no lists of three for rhythm, no "
    "generic upbeat closers, no em dashes, no 'passionate about'. Plain verbs, concrete "
    "nouns, exact facts, one idea per sentence."
)

BUILTIN_HARD = [
    "here's the thing",
    "it's worth noting",
    "it is worth noting",
    "in today's",
    "this matters because",
    "make no mistake",
    "full stop",
    "let that sink in",
    "game-changer",
    "game changer",
    "navigate the landscape",
    "navigating the complexities",
    "delve",
    "deep dive",
    "synergy",
    "leverage",
    "leveraging",
    "utilize",
    "cutting-edge",
    "seamless",
    "seamlessly",
    "robust",
    "spearhead",
    "testament to",
    "showcase",
    "foster",
    "fostering",
    "harness",
    "streamline",
    "actionable",
    "scalable",
    "passionate about",
    "at the end of the day",
    "in order to",
    "due to the fact that",
    "not just",
    "not only",
]

CLEANUP_PROMPT = (
    "You are Qwen, the local recruiting agent in Hermes, acting as a careful editor under "
    "the Unslop contract. You receive a short application answer and a list of flagged "
    "spans. Repair only the flagged spans with the smallest edit that keeps the meaning; "
    "copy every other sentence exactly. Preserve every fact, number, name, date, "
    "technology and claim. Add nothing: no new claims, praise, personality or conclusion. "
    "Do not shorten the answer to fragments. Return ONLY the corrected text, no quotes, "
    "no markdown, no explanation."
)


def scanner_dir() -> Path | None:
    configured = workflow.config().get("unslop_path")
    if not configured:
        return None
    scripts = Path(configured).expanduser() / "scripts"
    return scripts if (scripts / "banned_phrase_scan.py").is_file() else None


def _run_scanner(scripts: Path, name: str, text: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(scripts / name)],
        input=text,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=str(scripts),
    )
    if result.returncode not in (0, 1) or not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def builtin_scan(text: str) -> list[dict]:
    lowered = text.lower()
    hits = []
    for phrase in BUILTIN_HARD:
        for match in re.finditer(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", lowered):
            hits.append(
                {
                    "phrase": phrase,
                    "severity": "hard",
                    "category": "builtin",
                    "column": match.start(),
                }
            )
    if "—" in text:
        hits.append(
            {
                "phrase": "—",
                "severity": "soft",
                "category": "punctuation",
                "column": text.index("—"),
            }
        )
    return hits


def scan(text: str) -> dict:
    """Deterministic detection; the findings are candidates, never edits."""
    scripts = scanner_dir()
    if scripts is None:
        hits = builtin_scan(text)
        return {"source": "builtin", "violations": hits, "structure": []}
    phrases = _run_scanner(scripts, "banned_phrase_scan.py", text)
    structure = _run_scanner(scripts, "structure_scan.py", text)
    return {
        "source": "unslop",
        "violations": [
            {
                k: v
                for k, v in item.items()
                if k in {"phrase", "category", "severity", "suggestion", "column"}
            }
            for item in phrases.get("violations", [])
        ],
        "structure": list(structure.get("flags", [])),
    }


def needs_cleanup(report: dict) -> bool:
    hard = [v for v in report["violations"] if v.get("severity") == "hard"]
    return bool(hard) or len(report["violations"]) >= 2 or bool(report["structure"])


def summary(before: dict, after: dict | None) -> str:
    def describe(report: dict) -> str:
        phrases = [v["phrase"] for v in report["violations"]][:6]
        parts = []
        if phrases:
            parts.append("phrases: " + ", ".join(phrases))
        if report["structure"]:
            parts.append("structure: " + ", ".join(str(f) for f in report["structure"][:4]))
        return "; ".join(parts) or "none"

    if after is None:
        return f"clean ({before['source']} scan)"
    return (
        f"fixed {len(before['violations'])} → {len(after['violations'])} tells "
        f"({before['source']} scan). Before: {describe(before)}. After: {describe(after)}."
    )


def cleanup_context(text: str, report: dict) -> dict:
    return {
        "review_type": "cleanup",
        "text": text,
        "findings": [
            {
                "span": v["phrase"],
                "category": v.get("category", ""),
                "severity": v.get("severity", ""),
                "suggestion": v.get("suggestion", ""),
            }
            for v in report["violations"][:20]
        ]
        + [{"span": str(flag), "category": "structure"} for flag in report["structure"][:5]],
        "contract": "Repair only the listed spans; preserve every fact; return only the text.",
    }


def preserved_facts(original: str, cleaned: str) -> bool:
    """Every number and capitalized token of the original must survive the repair."""
    tokens = set(re.findall(r"\b(?:\d[\d,.%]*|[A-Z][A-Za-z0-9+#./-]{1,})\b", original))
    return all(token in cleaned for token in tokens)
