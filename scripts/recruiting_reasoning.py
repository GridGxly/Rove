"""Kept for existing commands and docs: the review now lives in the package.

`rove.hermes_review` is what the worker runs under the Hermes Python, and
`rove.prompts` holds the prompt texts. This file only forwards to them.
"""

from rove.hermes_review import main
from rove.prompts import (  # noqa: F401 -- re-exported for callers that load this file
    ANSWER_PROMPT,
    JOB_FIT_PROMPT,
    OVERLAY_PROMPT,
    PADDING_NOTE,
    system_prompt,
)

if __name__ == "__main__":
    main()
