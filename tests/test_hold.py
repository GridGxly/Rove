"""`rove hold`: while the hold file exists no service works and nothing leaves the Mac."""

import sys

import pytest

from rove import cli, discord_feed, live_browser, mail, runtime, services, worker


@pytest.fixture
def held():
    runtime.write_private(runtime.hold_path(), {"since": "2026-10-20T17:43:37+00:00"})


def test_no_hold_by_default():
    assert not runtime.on_hold()


def test_discord_refuses_reads_and_posts(held):
    with pytest.raises(runtime.OnHold):
        discord_feed.discord("POST", "/channels/1/messages", {"content": "hello"})
    with pytest.raises(runtime.OnHold):
        discord_feed.discord("GET", "/channels/1/messages")


def test_model_and_zoho_refuse(held):
    with pytest.raises(runtime.OnHold):
        runtime.client()
    with pytest.raises(runtime.OnHold), mail.Zoho({}) as zoho:
        zoho.connect()


def test_ticks_do_nothing_before_reading_any_config(held):
    # No feed.json, mail.json or workflow.json exists in this state root.
    assert worker.tick() == {"held": True}
    assert discord_feed.tick() == {"held": True}
    assert mail.tick() == {"held": True}


def test_browser_service_exits_without_taking_its_lock(held):
    live_browser.serve()
    assert not (runtime.state_root() / "browser-daemon.lock").exists()


def test_start_commands_are_refused(held, monkeypatch):
    for argv in (["start"], ["install-services"], ["gateway", "restart"], ["model", "start"]):
        monkeypatch.setattr(sys, "argv", ["rove", *argv])
        with pytest.raises(SystemExit) as stopped:
            cli.main()
        assert stopped.value.code == 2


class Launchd:
    def __init__(self):
        self.calls = []
        self.loaded = set()

    def run(self, args, **_kwargs):
        self.calls.append((args[1:], runtime.on_hold()))
        verb, target = args[1], args[-1]
        label = target.rsplit("/", 1)[-1].removesuffix(".plist")
        code = 0
        if verb == "bootout":
            self.loaded.discard(label)
        elif verb == "bootstrap":
            self.loaded.add(label)
        elif verb == "print":
            code = 0 if label in self.loaded else 113
        return services.subprocess.CompletedProcess(args, code)


def test_hold_on_writes_the_file_before_stopping_every_agent(monkeypatch):
    launchd = Launchd()
    launchd.loaded = set(services.labels())
    monkeypatch.setattr(services.subprocess, "run", launchd.run)
    result = services.hold("on")
    assert result["held"] and result["since"]
    assert set(result["services"].values()) == {"stopped"}
    stops = [call for call in launchd.calls if call[0][0] in {"disable", "bootout"}]
    assert len(stops) == 2 * len(services.labels())
    assert all(was_held for _, was_held in stops)
    assert "ai.hermes.gateway" in services.labels()


def test_hold_off_removes_the_file_before_loading_agents(monkeypatch, held):
    launchd = Launchd()
    monkeypatch.setattr(services.subprocess, "run", launchd.run)
    agents = services.Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    for label in services.labels():
        (agents / (label + ".plist")).write_bytes(b"")
    result = services.hold("off")
    assert not result["held"]
    assert set(result["services"].values()) == {"running"}
    loads = [call for call in launchd.calls if call[0][0] in {"enable", "bootstrap"}]
    assert len(loads) == 2 * len(services.labels())
    assert not any(was_held for _, was_held in loads)
