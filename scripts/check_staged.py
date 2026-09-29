"""Fail on obvious secrets/private artifacts in the Git index; never print matches.

This is a lightweight repository guard, not a comprehensive secret scanner.
Review the staged diff and keep GitHub push protection enabled as well.
"""

import re
import subprocess
from pathlib import PurePosixPath


def main():
    paths = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"]
    )
    patterns = [
        rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})",
        rb"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}",
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        rb"/Users" + rb"/(?!example(?:/|\b)|username(?:/|\b))[^/\s]+/",
    ]
    blocked = []
    for raw in paths.split(b"\0"):
        if not raw:
            continue
        path = raw.decode()
        p = PurePosixPath(path)
        data = subprocess.check_output(["git", "show", ":" + path])
        artifact = p.suffix.lower() in {
            ".sqlite",
            ".sqlite3",
            ".db",
            ".gguf",
            ".safetensors",
            ".key",
            ".pem",
        }
        env = p.name == ".env" or (p.name.startswith(".env.") and p.name != ".env.example")
        if artifact or env or any(re.search(pattern, data) for pattern in patterns):
            blocked.append(path)
    if blocked:
        print("Review potentially private staged files:")
        print("\n".join(blocked))
        raise SystemExit(1)
    print("Staged guard passed; also review the diff for private data.")


if __name__ == "__main__":
    main()
