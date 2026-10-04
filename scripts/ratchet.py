"""Hold existing lint and type findings at a ceiling while they are burned down.

    python scripts/ratchet.py quality   # ruff rules listed in [tool.ruff.lint] ignore
    python scripts/ratchet.py types     # mypy, configured in [tool.mypy]

Findings are counted per rule and file and compared with scripts/baselines/<name>.json.
A count above its ceiling fails: new code may not add a finding the old code already
has. A count below its ceiling also fails until `--update` writes the lower number, so
an improvement cannot be spent later. `--update` only lowers ceilings; raising one takes
`--update --allow-increase` and shows up in review as a larger number in the baseline.

`quality` also requires a reason on every suppressed blind `except` and security finding:
`# noqa: BLE001 -- why this is safe`.
"""

import argparse
import json
import re
import subprocess
import sys
import tokenize
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINES = ROOT / "scripts" / "baselines"
MYPY_LINE = re.compile(
    r"^(?P<path>[^:]+):(?P<line>\d+): error: (?P<message>.*?)\s+\[(?P<code>[a-z-]+)\]$"
)
NOQA = re.compile(r"#\s*noqa:\s*(?P<codes>[A-Z]+[0-9]+(?:\s*,\s*[A-Z]+[0-9]+)*)(?P<rest>.*)$")
NEEDS_REASON = re.compile(r"^(BLE|S)[0-9]+$")


def relative(path: str) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        candidate = candidate.relative_to(ROOT)
    return candidate.as_posix()


def ratcheted_rules() -> list[str]:
    lint = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["ruff"]["lint"]
    ignored, external = set(lint.get("ignore", [])), set(lint.get("external", []))
    if ignored != external:
        raise SystemExit(
            "[tool.ruff.lint] ignore and external must list the same rules; differ on: "
            + ", ".join(sorted(ignored ^ external))
        )
    return sorted(ignored)


def quality_findings() -> list[dict]:
    rules = ratcheted_rules()
    output = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            ".",
            "--no-cache",
            "--exit-zero",
            "--output-format",
            "json",
            "--select",
            ",".join(rules),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        {
            "rule": item["code"],
            "path": relative(item["filename"]),
            "line": item["location"]["row"],
            "message": item["message"],
        }
        for item in json.loads(output)
    ]


def types_findings() -> list[dict]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-error-summary",
            "--no-pretty",
            "--no-color-output",
            "--show-error-codes",
            "--hide-error-context",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    findings, unparsed = [], []
    for line in result.stdout.splitlines():
        if ": note: " in line or not line.strip():
            continue
        match = MYPY_LINE.match(line)
        if match:
            findings.append(
                {
                    "rule": match["code"],
                    "path": relative(match["path"]),
                    "line": int(match["line"]),
                    "message": match["message"],
                }
            )
        else:
            unparsed.append(line)
    # Exit status 1 means "found errors"; anything else (a crash, a config error) or
    # output that is not an error line is a broken check, not a count.
    if result.returncode not in (0, 1) or unparsed or (result.returncode == 1 and not findings):
        print(result.stdout + result.stderr, file=sys.stderr)
        raise SystemExit("mypy did not run cleanly; see its output above")
    return findings


def missing_reasons() -> list[str]:
    """Suppressed blind excepts and security findings that do not say why."""
    files = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    problems = []
    for name in files:
        path = ROOT / name
        if not path.exists():
            continue
        with path.open("rb") as source:
            comments = [
                (token.start[0], token.string)
                for token in tokenize.tokenize(source.readline)
                if token.type == tokenize.COMMENT
            ]
        for number, text in comments:
            match = NOQA.search(text)
            if not match:
                continue
            codes = [code.strip() for code in match["codes"].split(",")]
            reason = re.match(r"\s*(?:--|—)\s*\S", match["rest"])
            if any(NEEDS_REASON.match(code) for code in codes) and not reason:
                problems.append(f"{name}:{number}: `noqa: {match['codes']}` needs `-- reason`")
    return problems


def tally(findings: list[dict]) -> dict[str, dict[str, int]]:
    counts = Counter((f["rule"], f["path"]) for f in findings)
    table: dict[str, dict[str, int]] = {}
    for (rule, path), count in sorted(counts.items()):
        table.setdefault(rule, {})[path] = count
    return table


def compare(current: dict, baseline: dict) -> tuple[list, list]:
    """(rule, path, now, ceiling) above and below the baseline."""
    above, below = [], []
    for rule in sorted(set(current) | set(baseline)):
        now_rule, then_rule = current.get(rule, {}), baseline.get(rule, {})
        for path in sorted(set(now_rule) | set(then_rule)):
            now, ceiling = now_rule.get(path, 0), then_rule.get(path, 0)
            if now > ceiling:
                above.append((rule, path, now, ceiling))
            elif now < ceiling:
                below.append((rule, path, now, ceiling))
    return above, below


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("check", choices=["quality", "types"])
    parser.add_argument("--update", action="store_true", help="write lower ceilings")
    parser.add_argument(
        "--allow-increase", action="store_true", help="with --update, also raise ceilings"
    )
    args = parser.parse_args()

    findings = quality_findings() if args.check == "quality" else types_findings()
    reasons = missing_reasons() if args.check == "quality" else []
    current = tally(findings)
    path = BASELINES / f"{args.check}.json"
    stored = json.loads(path.read_text()) if path.exists() else {"counts": {}}
    above, below = compare(current, stored["counts"])

    for problem in reasons:
        print(problem)
    if args.update:
        if above and not args.allow_increase:
            for rule, name, now, ceiling in above:
                print(f"{name}: {rule} {now} > {ceiling}; fix it, or --allow-increase")
            return 1
        updated = current if args.allow_increase else tally_min(current, stored["counts"])
        stored["counts"] = updated
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n")
        print(f"wrote {relative(str(path))}: {summary(updated)}")
        return 1 if reasons else 0

    for rule, name, now, ceiling in above:
        print(f"{name}: {rule} {now} found, ceiling {ceiling}")
        for finding in findings:
            if finding["rule"] == rule and finding["path"] == name:
                print(f"    {name}:{finding['line']}: {finding['message']}")
    for rule, name, now, ceiling in below:
        print(f"{name}: {rule} {now} found, ceiling {ceiling} (improved)")
    if below:
        print(
            f"Lock the improvement in: python scripts/ratchet.py {args.check} --update, "
            f"then commit {relative(str(path))}."
        )
    if above:
        print("New findings above the recorded ceiling. Fix them; do not raise the ceiling.")
    status = 1 if above or below or reasons else 0
    print(f"{args.check}: {summary(current)}" + (" (ok)" if not status else ""))
    return status


def tally_min(current: dict, baseline: dict) -> dict:
    """Current counts, never above the stored ceiling."""
    lowered: dict[str, dict[str, int]] = {}
    for rule, paths in current.items():
        for name, count in paths.items():
            ceiling = baseline.get(rule, {}).get(name, 0)
            if min(count, ceiling):
                lowered.setdefault(rule, {})[name] = min(count, ceiling)
    return lowered


def summary(counts: dict) -> str:
    totals = {rule: sum(paths.values()) for rule, paths in counts.items()}
    if not totals:
        return "no findings"
    parts = ", ".join(f"{rule} {total}" for rule, total in sorted(totals.items()))
    return f"{sum(totals.values())} findings ({parts})"


if __name__ == "__main__":
    raise SystemExit(main())
