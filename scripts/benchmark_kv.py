"""Compare VLM MTP with 8-bit TurboQuant KV, restoring settings even on failure.

Run with the repo environment and an idle model service. Pause incoming gateway
work first. Pass a private synthetic message-array JSON as the positional path.
"""

import argparse
import json

import httpx

from erga_autopilot.benchmark import request
from erga_autopilot.runtime import MODEL, api_key, state_root, write_private


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    args = parser.parse_args()
    with open(args.prompt) as file:
        messages = json.load(file)
    keys = ("vlm_mtp_enabled", "turboquant_kv_enabled", "turboquant_kv_bits")
    results = []
    with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=120) as admin:
        admin.post("/admin/api/login", json={"api_key": api_key()}).raise_for_status()
        activity = admin.get("/admin/api/activity").json()["active_models"]
        if activity["total_active_requests"] or activity["total_waiting_requests"]:
            raise RuntimeError("Wait until the model is idle before changing cache settings")
        models = admin.get("/admin/api/models").json()["models"]
        previous = next(m["settings"] for m in models if m["id"] == MODEL)
        restore = {key: previous[key] for key in keys}
        endpoint = f"/admin/api/models/{MODEL}/settings"
        try:
            for label, settings in [
                ("mtp-current", None),
                (
                    "kv8-first",
                    {
                        "vlm_mtp_enabled": False,
                        "turboquant_kv_enabled": True,
                        "turboquant_kv_bits": 8,
                    },
                ),
                ("kv8-repeat", None),
            ]:
                if settings:
                    admin.put(endpoint, json=settings).raise_for_status()
                result = request(messages)
                result["variant"] = label
                results.append(result)
                write_private(state_root() / "benchmarks/kv-comparison.json", {"results": results})
                print(
                    json.dumps(
                        {"variant": label, "ttft": result["ttft_seconds"], "usage": result["usage"]}
                    ),
                    flush=True,
                )
        finally:
            admin.put(endpoint, json=restore).raise_for_status()


if __name__ == "__main__":
    main()
