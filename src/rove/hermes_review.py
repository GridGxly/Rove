"""One bounded review inside the installed Hermes harness.

Run as a file under the Hermes Python (`python hermes_review.py --hermes-checkout ...`),
with the package's parent directory on `PYTHONPATH`. It is the worker's default path to
the model: one turn in Hermes with private input and output files and no tools. The only
output is an unapproved proposal validated by the calling process.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from rove.prompts import system_prompt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-checkout", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ["HERMES_AUTOPILOT_16K"] = "1"
    sys.path.insert(0, str(args.hermes_checkout))
    import hermes_bootstrap  # noqa: F401 -- load the pinned Hermes dependency environment
    from hermes_cli.config import load_config
    from run_agent import AIAgent

    from rove.runtime import api_key, write_private

    config = load_config()
    context = json.loads(args.input.read_text())
    system = system_prompt(context.get("review_type"))
    agent = AIAgent(
        model=config["model"]["default"],
        provider="custom",
        base_url=config["model"]["base_url"],
        api_key=api_key(),
        enabled_toolsets=[],
        disabled_toolsets=config["agent"]["disabled_toolsets"],
        ephemeral_system_prompt=system,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        # No tools exist, so a second iteration only serves Hermes' own continuation of a
        # response that hit the output ceiling. The caller still rejects incomplete turns.
        max_iterations=2,
        max_tokens=2048,
        stream_delta_callback=lambda _: None,
        request_overrides={
            "temperature": 0,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        },
    )
    # One bounded, non-streamed request: a dropped stream must fail clearly instead of
    # being continued from a partial fragment.
    agent._disable_streaming = True
    try:
        if agent.tools:
            raise PermissionError("Reasoning mode must not expose tools")
        result = agent.run_conversation(args.input.read_text())
        write_private(
            args.output,
            {
                "model": config["model"]["default"],
                "harness": "Hermes",
                "tool_count": len(agent.tools),
                "streaming": False,
                "result": result,
            },
        )
    finally:
        agent.close()


if __name__ == "__main__":
    main()
