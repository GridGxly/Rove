import argparse
import json
import subprocess
from pathlib import Path

from .runtime import client


def main():
    parser = argparse.ArgumentParser(description="Local prepare-only Autopilot")
    sub = parser.add_subparsers(dest="command", required=True)
    model = sub.add_parser("model")
    model.add_argument("action", choices=["start", "stop", "restart", "status", "logs"])
    bench = sub.add_parser("benchmark")
    bench.add_argument("prompts", nargs="+", type=Path)
    sub.add_parser("mcp")
    sub.add_parser("smoke")
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
            subprocess.run([str(Path.home() / ".omlx/bin/omlx"), args.action], check=True)
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
