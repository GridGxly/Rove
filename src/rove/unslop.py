"""Writing cleanup for Qwen drafts, following the Unslop contract and Humanizer's tells.

Unslop (https://github.com/theclaymethod/unslop, MIT) finds formulaic AI writing and
repairs only the defective spans while preserving facts and voice. When a local clone
is configured, its scanners run on every draft; otherwise a small built-in list of the
same hard tells is used. Humanizer (https://github.com/blader/humanizer, MIT) catalogs
the shapes AI prose keeps even as its vocabulary changes; a digest of it runs on every
draft either way. Findings never change a draft silently: Qwen is asked for one bounded
repair pass and the result is re-scanned and shown with the original.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

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

# Digest of Humanizer's pattern catalog (after Wikipedia's "Signs of AI writing"). A hard
# entry justifies a repair on one hit; a soft one counts only beside another finding.
HUMANIZER_HARD = [
    # staging instead of stating: contrasts, deep-sounding sayings, run-ups, straw men
    "not merely",
    "the real question is",
    "at its core",
    "what really matters",
    "the heart of the matter",
    "let's dive in",
    "let's explore",
    "let's break this down",
    "here's what you need to know",
    "without further ado",
    "let's be honest",
    "real talk",
    "the thing is",
    "don't get me wrong",
    "this is not to say",
    "some might say",
    "one might be tempted",
    "read that again",
    # inflation and borrowed authority
    "tapestry",
    "pivotal",
    "meticulous",
    "meticulously",
    "intricacies",
    "interplay",
    "garnered",
    "bolster",
    "bolstered",
    "vibrant",
    "enduring",
    "indelible",
    "underscores the",
    "underscores its",
    "underscore the",
    "plays a key role",
    "setting the stage",
    "evolving landscape",
    "lasting legacy",
    "reflects a broader",
    "the future looks bright",
    "exciting times ahead",
    "step in the right direction",
    "groundbreaking",
    "renowned",
    "diverse array",
    "breathtaking",
    "nestled",
    "exemplifies",
    "profound",
    "experts argue",
    "experts agree",
    "studies show",
    "industry reports",
    "serves as a",
    "stands as a",
    "boasts",
    # stacked qualifiers
    "could potentially",
    "might arguably",
    # leftovers from chat and drafting
    "i hope this helps",
    "great question",
    "you're absolutely right",
    "let me know if",
    "would you like me",
    "certainly!",
    "of course!",
    "as an ai",
    "my last training",
    "based on available information",
    "it is believed that",
    "in this response",
    "in this essay",
]

HUMANIZER_SOFT = [
    "to be clear",
    "i'm not saying",
    "you might think",
    "to be fair",
    "it's also possible",
    "fundamentally",
    "in reality",
    "crucial",
    "highlight",
    "highlighting",
    "enhance",
    "valuable",
    "align with",
    "aligns with",
    "additionally",
    "actually",
    "quietly",
    "intricate",
    "underscoring",
    "emphasizing",
    "ensuring",
    "symbolizing",
    "cultivating",
    "encompassing",
    "commitment to",
    "associated with",
    "linked to",
    "tied to",
    "connected to",
    "functions as",
    "represents a",
]

HUMANIZER_TELLS = (
    "no 'not X but Y' contrasts, no run-up before the point, no closing line that restates "
    "the answer, no hedge stacks, no list of three for rhythm, no dash as a connector, no "
    "inflated words (pivotal, meticulous, vibrant, tapestry, testament, serves as), plain "
    "is/has verbs, straight quotes"
)

# Two sentences for the drafting prompt; the lists above are the scanner's side of them.
HUMANIZER_RULES = (
    "Humanizer rules: say each point once, in plain words, as the person who did the work: "
    + HUMANIZER_TELLS
    + ", and sentences of different lengths. A reader should not be able to tell the text "
    "from something the owner typed."
)

CLEANUP_PROMPT = (
    "You are Qwen, the local recruiting agent in Hermes, acting as a careful editor under "
    "the Unslop contract. You receive a short application answer and a list of flagged "
    "spans. Repair only the flagged spans with the smallest edit that keeps the meaning; "
    "copy every other sentence exactly. Preserve every fact, number, name, date, "
    "technology and claim. Add nothing: no new claims, praise, personality or conclusion. "
    "Do not shorten the answer to fragments. A repair states the flagged point plainly "
    "instead of swapping one tell for another: " + HUMANIZER_TELLS + ". Return ONLY the "
    "corrected text, no quotes, no markdown, no explanation."
)


def scanner_dir() -> Path | None:
    # Imported lazily: the Hermes-managed Python that runs the prompts lacks PyYAML,
    # which the workflow module's import chain needs.
    from . import workflow

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


def _normalized(text: str) -> str:
    """Lowercase with straight apostrophes; the length is unchanged so columns line up."""
    return text.lower().replace("’", "'")


def _phrase_hits(text: str, phrases: list[str], severity: str, category: str) -> list[dict]:
    lowered = _normalized(text)
    hits = []
    for phrase in phrases:
        for match in re.finditer(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", lowered):
            hits.append(
                {
                    "phrase": phrase,
                    "severity": severity,
                    "category": category,
                    "column": match.start(),
                }
            )
    return hits


def builtin_scan(text: str) -> list[dict]:
    return _phrase_hits(text, BUILTIN_HARD, "hard", "builtin")


SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# An em dash anywhere, or an en dash or double hyphen used as a spaced connector; a date
# range such as 2019–2021 is not a connector.
CONNECTOR_DASH = re.compile(r"—|\s–\s|\s--\s")
TRIAD = re.compile(
    r"\b[\w'-]+(?: [\w'-]+)?, [\w'-]+(?: [\w'-]+)?,? (?:and|or) [\w'-]+", re.IGNORECASE
)


def _shape_hits(text: str) -> list[dict]:
    """Humanizer's shape tells: the habits that survive a change of vocabulary."""
    hits = []

    def add(phrase: str, severity: str, column: int):
        hits.append(
            {
                "phrase": phrase,
                "severity": severity,
                "category": "humanizer-shape",
                "column": column,
            }
        )

    dashes = list(CONNECTOR_DASH.finditer(text))
    if dashes:
        # Unslop treats two connector dashes in one paragraph as a hard violation.
        add(dashes[0].group().strip(), "hard" if len(dashes) >= 2 else "soft", dashes[0].start())
    sentences = [s.strip() for s in SENTENCE_END.split(text.strip()) if s.strip()]
    for sentence in sentences:
        if sentence.endswith("?"):
            add(sentence[:80], "soft", text.find(sentence))
    openers = [re.sub(r"[^a-z]", "", s.split(maxsplit=1)[0].lower()) for s in sentences]
    for i in range(len(openers) - 2):
        # Three sentences in a row opening on the same word; a first-person "I" is the
        # natural opener of an application answer and is not a tell.
        word = openers[i]
        if word and word != "i" and openers[i + 1] == word and openers[i + 2] == word:
            add(word, "soft", text.find(sentences[i]))
            break
    triads = list(TRIAD.finditer(text))
    if len(triads) >= 2:
        add(triads[0].group(), "soft", triads[0].start())
    if "“" in text or "”" in text:
        add("curly quotes", "soft", max(text.find("“"), text.find("”")))
    return hits


def humanizer_scan(text: str) -> list[dict]:
    return (
        _phrase_hits(text, HUMANIZER_HARD, "hard", "humanizer")
        + _phrase_hits(text, HUMANIZER_SOFT, "soft", "humanizer")
        + _shape_hits(text)
    )


def _merge(primary: list[dict], extra: list[dict]) -> list[dict]:
    """Primary findings win; an extra hit on a phrase already reported is dropped."""
    known = {str(v.get("phrase", "")).lower() for v in primary}
    merged = primary + [hit for hit in extra if hit["phrase"].lower() not in known]
    return sorted(merged, key=lambda v: v.get("column", 0))


def scan(text: str) -> dict:
    """Deterministic detection; the findings are candidates, never edits."""
    scripts = scanner_dir()
    shape = humanizer_scan(text)
    if scripts is None:
        return {
            "source": "builtin",
            "violations": _merge(builtin_scan(text), shape),
            "structure": [],
        }
    phrases = _run_scanner(scripts, "banned_phrase_scan.py", text)
    structure = _run_scanner(scripts, "structure_scan.py", text)
    violations = [
        {
            k: v
            for k, v in item.items()
            if k in {"phrase", "category", "severity", "suggestion", "column"}
        }
        for item in phrases.get("violations", [])
    ]
    return {
        "source": "unslop",
        "violations": _merge(violations, shape),
        "structure": list(structure.get("flags", [])),
    }


def needs_cleanup(report: dict) -> bool:
    """Only phrase-level tells authorize a repair; cadence scores alone never do."""
    hard = [v for v in report["violations"] if v.get("severity") == "hard"]
    return bool(hard) or len(report["violations"]) >= 2


def _flag_names(report: dict) -> list[str]:
    names = []
    for flag in report.get("structure", []):
        names.append(str(flag.get("metric", flag)) if isinstance(flag, dict) else str(flag))
    return names


def summary(before: dict, after: dict | None) -> str:
    phrases = [v["phrase"] for v in before["violations"]][:6]
    notes = _flag_names(before)[:3]
    if after is None:
        text = f"clean ({before['source']} scan)"
        if phrases or notes:
            text += " · advisory: " + ", ".join(phrases + notes)
        return text
    remaining = [v["phrase"] for v in after["violations"]][:4]
    text = (
        f"{len(before['violations'])} tell{'s' if len(before['violations']) != 1 else ''} "
        f"repaired ({before['source']} scan): " + ", ".join(phrases)
    )
    if remaining:
        text += " · still flagged: " + ", ".join(remaining)
    return text


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
    """Every number and capitalized token survives, no new number appears, no padding."""
    tokens = set(re.findall(r"\b(?:\d[\d,.%]*|[A-Z][A-Za-z0-9+#./-]{1,})\b", original))
    numbers_after = set(re.findall(r"\d[\d,.%]*", cleaned))
    numbers_before = set(re.findall(r"\d[\d,.%]*", original))
    return (
        all(token in cleaned for token in tokens)
        and numbers_after <= numbers_before
        and len(cleaned) <= int(len(original) * 1.15) + 40
    )
