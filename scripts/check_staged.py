"""Fail on obvious secrets/private artifacts in the Git index; never print matches.

This is a lightweight repository guard, not a comprehensive secret scanner.
Review the staged diff and keep GitHub push protection enabled as well.

    python scripts/check_staged.py          # staged files, before a commit
    python scripts/check_staged.py --all    # every tracked file, as CI runs it
"""

import re
import subprocess
import sys
from pathlib import PurePosixPath

PATTERNS = [
    rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})",
    rb"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}",
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    rb"/Users" + rb"/(?!example(?:/|\b)|username(?:/|\b))[^/\s]+/",
]
ARTIFACTS = {".sqlite", ".sqlite3", ".db", ".gguf", ".safetensors", ".key", ".pem"}


def blocked(path: str, data: bytes) -> bool:
    """Whether a file at `path` holding `data` looks private."""
    p = PurePosixPath(path)
    artifact = p.suffix.lower() in ARTIFACTS
    env = p.name == ".env" or (p.name.startswith(".env.") and p.name != ".env.example")
    return artifact or env or any(re.search(pattern, data) for pattern in PATTERNS)


def main():
    every = "--all" in sys.argv[1:]
    listing = ["git", "ls-files", "-z"] if every else ["git", "diff", "--cached", "--name-only"]
    if not every:
        listing += ["-z", "--diff-filter=ACMR"]
    paths = subprocess.check_output(listing)
    found = []
    for raw in paths.split(b"\0"):
        if not raw:
            continue
        path = raw.decode()
        data = subprocess.check_output(["git", "show", ":" + path])
        if blocked(path, data):
            found.append(path)
    if found:
        print("Review potentially private " + ("tracked" if every else "staged") + " files:")
        print("\n".join(found))
        raise SystemExit(1)
    print(
        ("Tracked" if every else "Staged") + " guard passed; also review the diff for private data."
    )


if __name__ == "__main__":
    main()
