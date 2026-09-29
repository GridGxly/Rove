"""Apply the opt-in 16K compatibility floor to the pinned Hermes source install.

This changes a capacity heuristic, not tool authorization. Keep oMLX's actual
16K cap and the narrow MCP allowlist. Updates may require reviewing this patch.
"""

import argparse
import hashlib
from pathlib import Path

ORIGINAL = "MINIMUM_CONTEXT_LENGTH = 64_000"
REPLACEMENT = """# Autopilot compatibility: explicit opt-in for a certified narrow local harness.
import os as _autopilot_os
MINIMUM_CONTEXT_LENGTH = (
    16_384 if _autopilot_os.environ.get("HERMES_AUTOPILOT_16K") == "1" else 64_000
)"""


def patched(source: str) -> str:
    if REPLACEMENT in source:
        return source
    if source.count(ORIGINAL) != 1:
        raise ValueError("Hermes source changed; review the compatibility patch before applying")
    return source.replace(ORIGINAL, REPLACEMENT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args()
    target = args.checkout / "agent/model_metadata.py"
    source = target.read_text()
    replacement = patched(source)
    compile(replacement, str(target), "exec")
    if replacement != source:
        backup = target.with_suffix(".py.autopilot-backup")
        if not backup.exists():
            backup.write_text(source)
        target.write_text(replacement)
    print("Compatibility patch SHA256:", hashlib.sha256(replacement.encode()).hexdigest())
    print("Enable only for this local setup with HERMES_AUTOPILOT_16K=1.")


if __name__ == "__main__":
    main()
