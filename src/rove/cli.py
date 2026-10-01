import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx

from .runtime import MODEL, client


def gateway(action: str):
    subprocess.run([str(Path.home() / ".local/bin/hermes"), "gateway", action], check=True)


def model_control(action: str):
    subprocess.run([str(Path.home() / ".omlx/bin/omlx"), action], check=True)


def wait_for_api():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with client() as c:
                r = c.get("/models", timeout=1)
                if r.is_success and any(m["id"] == MODEL for m in r.json()["data"]):
                    return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("oMLX did not advertise the configured model within 30 seconds")


def main():
    parser = argparse.ArgumentParser(description="Local prepare-only Rove")
    sub = parser.add_subparsers(dest="command", required=True)
    model = sub.add_parser("model")
    model.add_argument("action", choices=["start", "stop", "restart", "status", "logs"])
    bench = sub.add_parser("benchmark")
    bench.add_argument("prompts", nargs="+", type=Path)
    sub.add_parser("mcp")
    smoke_parser = sub.add_parser("smoke")
    smoke_parser.add_argument("--hold-seconds", type=int, default=0)
    for command in ["start", "stop", "status"]:
        sub.add_parser(command)
    gw = sub.add_parser("gateway")
    gw.add_argument("action", choices=["start", "stop", "restart", "status"])
    jobs = sub.add_parser("jobs")
    jobs.add_argument("action", choices=["sync", "status", "search", "read", "matches"])
    jobs.add_argument("--query", default="")
    jobs.add_argument("--program", default="", choices=["", "internship", "new-grad"])
    jobs.add_argument("--cycle", default="")
    jobs.add_argument("--limit", type=int, default=10)
    jobs.add_argument("--offset", type=int, default=0)
    jobs.add_argument("--id")
    jobs.add_argument("--preview-draft", action="store_true")
    onboard = sub.add_parser("onboarding")
    onboard.add_argument("action", choices=["status", "show", "propose", "approve"])
    onboard.add_argument("--section")
    onboard.add_argument("--file", type=Path)
    onboard.add_argument("--expected-hash")
    memory = sub.add_parser("memory")
    memory.add_argument("action", choices=["index", "search"])
    memory.add_argument("--query", default="")
    sub.add_parser("install-services")
    live = sub.add_parser("browser")
    live.add_argument("action", choices=["serve", "status", "open", "observe", "prepare"])
    live.add_argument("--url")
    live.add_argument("--run-id")
    feed = sub.add_parser("feed")
    feed.add_argument("action", choices=["tick", "seed"])
    workflow = sub.add_parser("workflow")
    workflow.add_argument("action", choices=["tick", "status", "enqueue", "resume", "defer"])
    workflow.add_argument("--url")
    workflow.add_argument("--id")
    # The owner types this in his own terminal, so the link is his unless he says otherwise.
    workflow.add_argument("--source", default="owner_link")
    mail = sub.add_parser("mail")
    mail.add_argument("action", choices=["tick", "status"])
    args = parser.parse_args()
    if args.command == "workflow":
        from . import worker, workflow

        if args.action == "tick":
            result = worker.tick()
        elif args.action == "enqueue":
            if not args.url:
                parser.error("enqueue requires --url")
            if args.source not in workflow.SOURCES:
                parser.error("--source must be one of: " + ", ".join(workflow.SOURCES))
            result = workflow.enqueue(args.url, source=args.source)
        elif args.action in {"resume", "defer"}:
            # Local owner operation, equivalent to the Discord command of the same name.
            if not args.id:
                parser.error(f"{args.action} requires --id")
            from datetime import UTC, datetime

            worker.apply_command(
                {"kind": args.action, "application_id": args.id},
                f"local-owner:{args.action}:{datetime.now(UTC).isoformat()}",
            )
            result = workflow.get(args.id)
        else:
            result = workflow.status()
        print(json.dumps(result, indent=2))
    elif args.command == "install-services":
        from .services import install

        print(json.dumps(install(), indent=2))
    elif args.command == "browser":
        from .live_browser import browser_call, serve

        if args.action == "serve":
            serve()
        else:
            params = {}
            if args.url:
                params["url"] = args.url
            if args.run_id:
                params["run_id"] = args.run_id
            print(json.dumps(browser_call(args.action, **params), indent=2))
    elif args.command == "feed":
        from .discord_feed import tick

        print(json.dumps(tick(seed=args.action == "seed"), indent=2))
    elif args.command == "mail":
        from . import mail as recruiting_mail

        result = recruiting_mail.tick() if args.action == "tick" else recruiting_mail.status()
        print(json.dumps(result, indent=2))
    elif args.command == "model":
        if args.action == "status":
            with client() as c:
                r = c.get("/models")
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2))
        elif args.action == "logs":
            subprocess.run(
                ["tail", "-n", "80", "-f", str(Path.home() / ".omlx/logs/server.log")], check=False
            )
        else:
            model_control(args.action)
    elif args.command == "start":
        model_control("start")
        wait_for_api()
        gateway("start")
        print("Local API and Hermes gateway started. Weights load on the first request.")
    elif args.command == "stop":
        gateway("stop")
        model_control("stop")
    elif args.command == "status":
        gateway("status")
        try:
            with client() as c:
                r = c.get("/models")
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2))
        except httpx.HTTPError as error:
            print(f"Local API is unavailable: {type(error).__name__}")
            raise SystemExit(1) from None
    elif args.command == "gateway":
        gateway(args.action)
    elif args.command == "benchmark":
        from .benchmark import run_suite

        print(run_suite(args.prompts))
    elif args.command == "mcp":
        from .server import run

        run()
    elif args.command == "smoke":
        from .browser import smoke

        print(json.dumps(smoke(hold_seconds=args.hold_seconds), indent=2))
    elif args.command == "jobs":
        from .jobs import job_status, read_job, search_jobs, sync_keryx

        if args.action == "sync":
            result = sync_keryx()
        elif args.action == "status":
            result = job_status()
        elif args.action == "read":
            if not args.id:
                parser.error("jobs read requires --id")
            result = read_job(args.id)
        elif args.action == "matches":
            from .matching import review_matches

            result = review_matches(args.limit, preview_draft=args.preview_draft)
        else:
            result = search_jobs(args.query, args.program, args.cycle, args.limit, args.offset)
        print(json.dumps(result, indent=2))
    elif args.command == "memory":
        from .memory import index_candidate_memory, search_candidate_memory

        result = (
            index_candidate_memory()
            if args.action == "index"
            else search_candidate_memory(args.query)
        )
        print(json.dumps(result, indent=2))
    elif args.command == "onboarding":
        from .onboarding import approve, draft, onboarding_status, propose

        if args.action == "show":
            result = draft()
        elif args.action == "status":
            result = onboarding_status(args.section)
        elif args.action == "propose":
            if not all([args.section, args.file, args.expected_hash]):
                parser.error("propose requires --section, --file, and --expected-hash")
            result = propose(args.section, json.loads(args.file.read_text()), args.expected_hash)
        else:
            if not args.expected_hash:
                parser.error("approve requires --expected-hash from the exact reviewed draft")
            result = approve(args.expected_hash)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
