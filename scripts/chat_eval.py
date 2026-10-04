"""How well the agent in agent-control understands the owner, measured on the real model.

Runs every message in `tests/chat_eval/phrases.jsonl` through the Hermes agent as the
gateway builds it (the gateway's profile: model, sampling, Rove's tool list, and this
checkout's persona `integrations/hermes/SOUL.md` unless `--soul` names another),
with Rove's MCP tools replaced by stubs that keep their names, descriptions and
arguments but only record the call and return a canned answer. Nothing touches Rove's
state, Discord or the browser. Prints each miss and the pass rate.

    uv run python scripts/chat_eval.py                 # all phrases
    uv run python scripts/chat_eval.py --match tesla   # phrases containing "tesla"

The script starts itself again under the Hermes Python (`--hermes-python`); the stub
tool server runs under this checkout's Python. One phrase takes one to three model
calls, so a full run takes several minutes on the reference Mac, longer while the
worker is using the model. Run by hand; it is not part of the offline test suite.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
PHRASES = REPOSITORY / "tests/chat_eval/phrases.jsonl"
# Tools that change something; a message that only asks or chats must call none of them.
ACTIONS = {
    "apply_to_link",
    "retry_application",
    "park_application",
    "answer_application",
    "pause_feed",
    "resume_feed",
    "start_job_application",
    "propose_onboarding_section",
    "refresh_job_feed",
}
CANNED = {
    "apply_to_link": "Queued. It goes next.",
    "retry_application": "Going again on that one. It goes next.",
    "park_application": "Parked it. Say “try it again” when you want it back.",
    "answer_application": "Saved that answer. Say “try it again” and I'll go on with it.",
    "rove_status": "3 need you. Ask “what's waiting” to see which.\nIn the queue: 12. Parked: 4.\nSent today: 2. The cap is 30 a day.",
    "whats_waiting": "3 need you:\n• Tesla — Embedded Software Intern: 1 question only you can answer\n"
    "• xAI — Software Engineer Intern: 2 questions only you can answer\n• Sierra — Agent Intern: your call on fit",
    "sends_today": "Sent today: 2. The cap is 30 a day.\nAcme — Web Intern, Globex — Data Intern.",
    "pause_feed": "Paused. Jobs from the feed stay in the queue and wait. Links you paste still go.",
    "resume_feed": "The feed is running again. Its jobs take their turn in the queue.",
    "company_history": "• Acme — Sales Intern: skipped by your feed rules (sales role · posted 40 days ago)",
    "what_you_can_ask": "Things you can ask me:\n• Paste a job link — I queue it as yours\n• `status`\n• `what's waiting on me`",
    "read_candidate_section": json.dumps(
        {
            "section": "education",
            "values": {
                "schools": [
                    {
                        "school": "Example University",
                        "major": "Computer Science",
                        "graduation_month": "2027-12",
                        "gpa": 3.8,
                    }
                ]
            },
            "approved": True,
        }
    ),
    "search_job_feed": json.dumps(
        {"jobs": [{"company": "Example Labs", "title": "Backend Intern", "location": "Remote"}]}
    ),
    "review_job_matches": json.dumps(
        {
            "matches": [
                {"company": "Example Labs", "title": "Backend Intern", "reasons": ["software role"]}
            ]
        }
    ),
    "read_job_listing": json.dumps(
        {"company": "Globex", "title": "Data Intern", "location": "Columbus, OH"}
    ),
    "job_feed_status": json.dumps({"open_jobs": 120, "imported": "today"}),
}


# --- the stub tool server (this checkout's Python) -----------------------------------


def serve_stubs():
    sys.path.insert(0, str(REPOSITORY / "src"))
    from rove import server

    for tool in server.mcp._tool_manager.list_tools():

        def stub(_name=tool.name, **arguments):
            return CANNED.get(_name, "{}")

        tool.fn = stub
        tool.is_async = False
        tool.context_kwarg = None
    server.mcp.run()


# --- scoring ---------------------------------------------------------------------------


def link_key(url) -> str:
    text = str(url or "").strip().strip("<>\"'` ")
    text = re.sub(r"^https?://", "", text).split("#")[0].split("?")[0]
    return text.rstrip("/").lower()


def words(value) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def argument_matches(key, wanted, got) -> bool:
    if key == "url":
        return link_key(wanted) == link_key(got)
    if isinstance(wanted, bool):
        return bool(got) == wanted
    a, b = words(wanted), words(got)
    return bool(a) and (a in b or b in a)


def call_matches(wanted: dict, got: dict) -> bool:
    if wanted["tool"] != got["tool"]:
        return False
    for key, value in wanted.get("args", {}).items():
        if not argument_matches(key, value, got["args"].get(key)):
            return False
    if wanted["tool"] == "apply_to_link" and not wanted.get("args", {}).get("first"):
        return not got["args"].get("first")  # first only when he asked for it
    return True


def meets(expect, calls: list[dict], reply: str) -> bool:
    actions = [c for c in calls if c["tool"] in ACTIONS]
    if isinstance(expect, dict) and "any" in expect:
        return any(meets(option, calls, reply) for option in expect["any"])
    if expect in ("plain", "no_action"):
        return not actions
    if expect == "ask":
        return not actions and "?" in reply
    if isinstance(expect, list):
        wanted_actions = [w for w in expect if w["tool"] in ACTIONS]
        if len(actions) != len(wanted_actions):
            return False
        return all(any(call_matches(w, c) for c in calls) for w in expect)
    raise ValueError(f"Unknown expectation {expect!r}")


# --- the agent (the Hermes Python) ----------------------------------------------------


def run_eval(args):
    checkout = Path(args.hermes_checkout).expanduser()
    os.environ.pop("PYTHONPATH", None)
    os.environ["HERMES_AUTOPILOT_16K"] = "1"
    sys.path.insert(0, str(checkout))
    from hermes_constants import get_default_hermes_root

    os.environ["HERMES_HOME"] = os.environ.get("HERMES_HOME") or str(get_default_hermes_root())
    import hermes_bootstrap  # noqa: F401
    import tools.mcp_tool_config as mcp_config
    from hermes_cli.config import load_config

    config = load_config()
    live = (config.get("mcp_servers") or {}).get("rove") or {}

    def stub_servers():
        return {
            "rove": {
                "command": args.rove_python,
                "args": [str(Path(__file__).resolve()), "--serve-stubs"],
                "cwd": str(REPOSITORY),
                "timeout": 60,
                "connect_timeout": 60,
                "tools": live.get("tools") or {},
            }
        }

    mcp_config._load_mcp_config = stub_servers
    # The persona under test: this checkout's, unless --soul names another file.
    from agent import prompt_builder

    soul = Path(args.soul).read_text().strip()
    prompt_builder.load_soul_md = lambda *a, **k: soul
    from run_agent import AIAgent
    from tools.mcp_tool_discovery import discover_mcp_tools

    discover_mcp_tools()
    key = json.loads((Path.home() / ".omlx/settings.json").read_text())["auth"]["api_key"]
    phrases = [json.loads(line) for line in PHRASES.read_text().splitlines() if line.strip()]
    if args.match:
        phrases = [p for p in phrases if args.match.lower() in p["say"].lower()]
    passed, misses, started = 0, [], time.monotonic()
    for number, phrase in enumerate(phrases, 1):
        agent = AIAgent(
            model=config["model"]["default"],
            provider="custom",
            base_url=config["model"]["base_url"],
            api_key=key,
            enabled_toolsets=["mcp-rove"],
            disabled_toolsets=config["agent"].get("disabled_toolsets"),
            platform="discord",
            load_soul_identity=True,
            skip_context_files=True,
            skip_memory=True,
            skip_background_review=True,
            quiet_mode=True,
            max_iterations=4,
        )
        agent._persist_disabled = True
        took = time.monotonic()
        try:
            result = agent.run_conversation(
                phrase["say"], conversation_history=phrase.get("history") or []
            )
        finally:
            agent.close()
        calls = []
        for message in result.get("messages") or []:
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    arguments = {}
                calls.append(
                    {
                        "tool": str(function.get("name", "")).removeprefix("mcp__rove__"),
                        "args": arguments,
                    }
                )
        reply = str(result.get("final_response") or "")
        ok = meets(phrase["expect"], calls, reply)
        passed += ok
        shown = ", ".join(f"{c['tool']}({json.dumps(c['args'])})" for c in calls) or "no tool"
        line = f"[{number:2d}] {'pass' if ok else 'MISS'} {time.monotonic() - took:5.1f}s  {phrase['say'][:70]!r}"
        print(line, flush=True)
        if not ok or args.verbose:
            print(f"       expected: {json.dumps(phrase['expect'])[:160]}")
            print(f"       got: {shown[:200]}")
            print(f"       said: {reply[:160]!r}", flush=True)
        if not ok:
            misses.append(phrase["say"])
    total = len(phrases)
    print(
        f"\n{passed}/{total} passed ({100 * passed / max(total, 1):.0f} %) in {time.monotonic() - started:.0f}s"
    )
    for say in misses:
        print("  miss:", say)
    return 0 if passed / max(total, 1) >= args.bar else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--serve-stubs", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--in-hermes", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--match", default="", help="only phrases containing this text")
    parser.add_argument("--verbose", action="store_true", help="show every call, not only misses")
    parser.add_argument("--bar", type=float, default=0.9, help="pass rate the exit code checks")
    parser.add_argument("--hermes-checkout", default=str(Path.home() / ".hermes/hermes-agent"))
    found = sorted(glob.glob(str(Path.home() / ".hermes/tools/python-*/bin/python3")))
    parser.add_argument("--hermes-python", default=found[-1] if found else "")
    parser.add_argument("--rove-python", default=str(REPOSITORY / ".venv/bin/python"))
    parser.add_argument("--soul", default=str(REPOSITORY / "integrations/hermes/SOUL.md"))
    args = parser.parse_args()
    if args.serve_stubs:
        serve_stubs()
        return 0
    if not args.in_hermes:
        if not args.hermes_python:
            parser.error("The Hermes Python was not found; pass --hermes-python")
        command = [
            args.hermes_python,
            "-I",
            str(Path(__file__).resolve()),
            "--in-hermes",
            *sys.argv[1:],
        ]
        return subprocess.call(command)
    return run_eval(args)


if __name__ == "__main__":
    sys.exit(main())
