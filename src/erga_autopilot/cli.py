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
    parser = argparse.ArgumentParser(description="Local prepare-only Autopilot")
    sub = parser.add_subparsers(dest="command", required=True)
    model = sub.add_parser("model")
    model.add_argument("action", choices=["start", "stop", "restart", "status", "logs"])
    bench = sub.add_parser("benchmark")
    bench.add_argument("prompts", nargs="+", type=Path)
    sub.add_parser("mcp")
    sub.add_parser("smoke")
    for command in ["start", "stop", "status"]:
        sub.add_parser(command)
    gw = sub.add_parser("gateway")
    gw.add_argument("action", choices=["start", "stop", "restart", "status"])
    args = parser.parse_args()
    if args.command == "model":
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

        print(json.dumps(smoke(), indent=2))


if __name__ == "__main__":
    main()
