"""Intake by score: one stable identity per posting, three tiers, the daily digest, and
the pacing of unattended sending.

Everything here is synthetic. The feed is a snapshot file written by the test, Discord is
a list of recorded calls, and dates are built from today so the suite does not age.
"""

import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest

from rove import discord_feed, intake, jobs, worker, workflow
from rove.onboarding import approve, digest, draft, propose

NOW = datetime.now().astimezone().date()
SUMMER = f"summer-{NOW.year + 1}"
SUMMER_WORDS = f"summer {NOW.year + 1}"
PREFERENCES = {
    "programs": ["internship"],
    "title_keywords": ["software"],
    "excluded_title_keywords": ["machine learning"],
    "excluded_companies": ["Initech Example"],
    "cycles": [SUMMER],
    "preferred_locations": ["Austin"],
    "priority_companies": ["Northwind Example"],
    "minimum_hourly_usd": 25.0,
}
EDUCATION = {
    "schools": [
        {
            "school": "Example State University",
            "graduation_month": f"{NOW.year + 2}-05",
            "student_year": "Sophomore",
        }
    ]
}
PROFILE = {"preferences": PREFERENCES, "education": EDUCATION}
W = intake.WEIGHTS


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVE_STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    propose("identity", {"legal_first_name": "Alex", "legal_last_name": "Example"}, digest(draft()))
    propose("education", EDUCATION, digest(draft()))
    propose("preferences", PREFERENCES, digest(draft()))
    approve(digest(draft()))
    return tmp_path / "state"


def days_ago(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def job(name: str, title: str = "Software Engineer Intern", **extra) -> dict:
    return {
        "id": f"job_{name}",
        "company": "Example Labs",
        "title": title,
        "location": "Austin, TX",
        "program": "internship",
        "status": "open",
        "cycle": SUMMER,
        "url": f"https://jobs.example.com/{name}",
        "posted_at": days_ago(2),
        **extra,
    }


def score(record: dict, profile: dict = PROFILE) -> dict:
    return intake.score_job(record, profile, today=NOW)


def snapshot(path, *records):
    path.write_text(
        json.dumps({"schema_version": 2, "country": "United States", "jobs": list(records)})
    )
    return path


def channels(monkeypatch, **settings) -> list:
    """An enabled workflow with a shortlist and a system log; returns workflow's own calls."""
    base = {
        "enabled": True,
        "action_channel_id": "action",
        "shortlist_channel_id": "short",
        "system_channel_id": "sys",
        "guild_id": "g",
        **settings,
    }
    monkeypatch.setattr(workflow, "config", lambda: base)
    calls = []
    monkeypatch.setattr(
        workflow,
        "discord",
        lambda method, path, payload=None: (
            calls.append((method, path, payload)) or {"id": f"w{len(calls)}"}
        ),
    )
    return calls


def feed(state, monkeypatch, **config) -> list:
    """Enable the feed with a cursor at the start and a Discord that records each call."""
    (state / "config").mkdir(exist_ok=True)
    (state / "config/feed.json").write_text(
        json.dumps({"enabled": True, "channel_id": "jobs", **config})
    )
    monkeypatch.setattr(discord_feed, "sync_keryx", lambda: {"changed_source": False})
    sent = []

    def fake(method, path, payload=None):
        sent.append((method, path, payload))
        return {"id": f"m{len(sent)}"}

    monkeypatch.setattr(discord_feed, "discord", fake)
    with discord_feed.feed_db() as db:
        db.execute("INSERT OR REPLACE INTO feed_cursor VALUES(1,0)")
    return sent


def posts(sent, channel: str) -> list[dict]:
    return [p for m, path, p in sent if m == "POST" and path == f"/channels/{channel}/messages"]


def queue() -> list[tuple]:
    with workflow.db() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT title,source,status FROM application_queue ORDER BY title"
            )
        ]


def owner(content: str, **extra) -> dict:
    return {"id": "900", "author": {"id": "owner"}, "content": content, **extra}


# --- scoring ---------------------------------------------------------------


def test_a_clear_fit_scores_the_sum_of_its_named_weights_and_says_why():
    fit = score(job("fit"))
    assert fit["score"] == (
        W["role_core"] + W["term_wanted"] + W["place_preferred"] + W["posted_within_3_days"]
    )
    assert fit["tier"] == 1 and not fit["skip"]
    assert fit["reason"] == f"software role · {SUMMER_WORDS} · in Austin"
    assert "posted 2 days ago" in fit["reasons"]
    elsewhere = score(job("fit", location="Denver, CO", posted_at=days_ago(10)))
    assert elsewhere["score"] == (
        W["role_core"] + W["term_wanted"] + W["place_fine"] + W["posted_within_14_days"]
    )
    assert elsewhere["tier"] == 1


def test_a_borderline_job_lands_in_the_digest_tier_with_its_doubt_named():
    it = score(job("it", "IT Intern", location="Denver, CO"))
    assert it["score"] == (
        W["role_adjacent"] + W["term_wanted"] + W["place_fine"] + W["posted_within_3_days"]
    )
    assert it["tier"] == 2 and it["reason"].startswith("IT role")
    # A good role with an old posting and no term is asked about, not queued.
    stale = score(job("old", cycle="unscheduled", location="Denver, CO", posted_at=days_ago(40)))
    assert stale["score"] == (
        W["role_core"] + W["term_unlisted"] + W["place_fine"] + W["posted_over_21_days"]
    )
    assert stale["tier"] == 2
    assert stale["reason"] == "software role · term not listed · posted over three weeks ago"
    other_term = score(job("later", cycle=f"fall-{NOW.year + 1}", location="Denver, CO"))
    assert other_term["tier"] == 2 and "not a term you picked" in other_term["reason"]


@pytest.mark.parametrize(
    ("title", "why"),
    [
        ("Senior Software Engineer", "seniority"),
        ("Software Engineer Intern, MS/PhD", "graduate"),
        ("AI Developer Co-op, Graduate Program", "graduate"),
        ("Machine Learning Engineer Intern", "excluded_role"),
        (f"Summer {NOW.year + 1} AI/ML Software Development Internship", "excluded_role"),
        (f"Software Engineering Intern, Fall {NOW.year - 1}", "term_started"),
    ],
)
def test_hard_rules_skip_a_job_whatever_else_it_scores(title, why):
    result = score(job("junk", title))
    assert (result["skip"], result["score"], result["tier"]) == (why, 0, 3)
    assert result["reason"] and len(result["reasons"]) == 1


@pytest.mark.parametrize(
    "title",
    [
        "Mechanical Engineering Intern",
        "Accounting Intern",
        "Data Science Intern",
        "Tax Technology Intern",
        "Marketing Technology Intern",
        "IT Help Desk Assistant",
    ],
)
def test_unrelated_roles_fall_below_the_digest_bar(title):
    # Even at a company the owner put first, in a preferred city, posted today.
    result = score(job("junk", title, company="Northwind Example", posted_at=days_ago(0)))
    assert result["tier"] == 3 and result["score"] < intake.DIGEST_AT


def test_other_hard_rules_company_track_and_place():
    assert score(job("x", company="Initech Example, Inc."))["skip"] == "excluded_company"
    assert score(job("x", program="new-grad"))["skip"] == "program"
    assert score(job("x", location="Toronto, ON"))["skip"] == "location"
    assert score(job("x", location="Vancouver, British Columbia, Canada"))["skip"] == "location"
    # Towns that share a name with somewhere abroad are not abroad.
    assert not score(job("x", location="Ontario, CA"))["skip"]
    assert not score(job("x", location="London, KY, United States"))["skip"]
    assert not score(job("x", location="Albuquerque, New Mexico"))["skip"]
    # A term still ahead is kept; last fall is over.
    assert not score(job("x", f"Software Engineer Co-op, Winter {NOW.year + 1}"))["skip"]
    assert score(job("x", f"Software Engineer Co-op, Fall {NOW.year - 1}"))["skip"]
    excluding = {**PROFILE, "preferences": {**PREFERENCES, "excluded_locations": ["Denver"]}}
    assert score(job("x", location="Denver, CO"), excluding)["skip"] == "location"
    assert not score(job("x", location="Denver, CO; Austin, TX"), excluding)["skip"]


def test_an_excluded_kind_listed_among_options_is_capped_for_the_owner_to_decide():
    for title in (
        "Software Engineer Intern, Backend/Full Stack/ML",
        "Future Intern, Software/AI/ML/Cyber",
        "Software Engineer Intern (Machine Learning Platform)",
    ):
        result = score(job("ml", title, company="Northwind Example"))
        assert (result["score"], result["tier"]) == (intake.QUEUE_AT - 1, 2), title
        assert "mentions machine learning, which you exclude" in result["reason"]


def test_a_field_word_names_the_team_of_a_software_role_but_the_job_of_a_neighbouring_one():
    team = score(job("x", "Software Engineer Intern, Finance Platform"))
    assert team["tier"] == 1 and team["score"] == score(job("x"))["score"]
    discipline = score(job("x", "Software Defined Radio Hardware Intern"))
    assert discipline["score"] == score(job("x"))["score"] + W["role_off_field"]
    assert discipline["tier"] == 2 and "radio work" in discipline["reason"]
    # Another family may be the better reading of the title: the core role wins.
    assert score(job("x", "Technology Intern - Software Engineering"))["tier"] == 1
    assert score(job("x", "Future IT Leaders Intern"))["reason"].startswith("IT role")
    assert score(job("x", "Make it happen Intern"))["family"] is None


def test_caps_points_for_pay_company_class_and_account_first_boards():
    base = score(job("x", location="Denver, CO"))["score"]
    assert score(job("x", "Software Engineer Intern ($30/hr)", location="Denver, CO"))["score"] == (
        base + W["pay_meets_minimum"]
    )
    low = score(job("x", "Software Engineer Intern ($18 per hour)", location="Denver, CO"))
    assert low["score"] == base + W["pay_below_minimum"] and "below your minimum" in low["reason"]
    first = score(job("x", location="Denver, CO", company="Northwind Example"))
    assert first["score"] == base + W["company_priority"]
    window = {
        "requirement_level": "required",
        "graduation_start": f"{NOW.year + 2}-01",
        "graduation_end": f"{NOW.year + 2}-12",
    }
    fits = score(job("x", location="Denver, CO", academic_eligibility=window))
    assert fits["score"] == base + W["class_fits"]
    earlier = {
        "requirement_level": "required",
        "graduation_start": f"{NOW.year + 1}-01",
        "graduation_end": f"{NOW.year + 1}-12",
    }
    missed = score(job("x", location="Denver, CO", academic_eligibility=earlier))
    assert missed["tier"] == 2 and "different graduation date" in missed["reason"]
    workday = score(job("x", url="https://example.wd5.myworkdayjobs.com/careers/job/R-12345"))
    assert workday["tier"] == 2 and "needs an account before applying" in workday["reason"]
    board = score(job("x", url="https://jobright.ai/jobs/info/synthetic"))
    assert board["tier"] == 2 and "job board" in board["reason"]
    unpaid = {**PROFILE, "preferences": {**PREFERENCES, "unpaid_roles": False}}
    assert score(job("x", "Unpaid Software Intern"), unpaid)["skip"] == "pay"


def test_every_weight_in_the_table_is_used_by_the_scorer_and_scores_stay_in_range():
    source = inspect.getsource(intake.score_job)
    assert all(f'"{name}"' in source for name in W), "a weight is never applied"
    best = score(job("x", company="Northwind Example", title="Software Engineer Intern ($40/hr)"))
    assert best["score"] == 100
    assert score(job("x", "Tax Intern"))["score"] == 0
    # A profile with nothing but a keyword still scores without a crash.
    bare = intake.score_job({"title": "Software Intern"}, {"preferences": {}}, today=NOW)
    assert bare["tier"] == 2 and bare["reason"].startswith("software role")


# --- identity --------------------------------------------------------------


def test_a_metadata_rewrite_or_a_renumbered_posting_is_the_same_job(state, monkeypatch, tmp_path):
    sent = feed(state, monkeypatch)
    path = tmp_path / "keryx.json"
    first = job("one", url="https://jobs.example.com/one?utm_source=feed")
    assert jobs.ingest(snapshot(path, first), "a" * 40)["new"] == 1
    assert discord_feed.tick()["sent"] == 1
    # The feed rewrites dates, notes, link status, and how it spells the company and place.
    rewritten = {
        **first,
        "company": "Example Labs, Inc.",
        "location": "Austin, TX, United States",
        "posted_at": days_ago(1),
        "link_status": "ats-verified",
        "sponsorship": "no-sponsorship",
        "academic_eligibility": {"status": "not-found", "summary": "Not stated"},
    }
    assert jobs.identity_key(rewritten) == jobs.identity_key(first)
    result = jobs.ingest(snapshot(path, rewritten), "b" * 40)
    assert (result["rewritten"], result["changed"], result["new"]) == (1, 0, 0)
    with jobs.database() as db:
        assert db.execute("SELECT COUNT(*) FROM job_events").fetchone()[0] == 1
        assert db.execute("SELECT company FROM jobs").fetchone()[0] == "Example Labs, Inc."
    assert discord_feed.tick()["sent"] == 0
    # The feed drops the id and lists the same posting under a new one.
    renumbered = {**rewritten, "id": "job_two", "url": "https://jobs.example.com/one"}
    result = jobs.ingest(snapshot(path, renumbered), "c" * 40)
    assert (result["new"], result["missing"]) == (1, 1)
    assert discord_feed.tick()["sent"] == 0
    assert len(posts(sent, "jobs")) == 1
    assert queue() == [("Example Labs — Software Engineer Intern", "keryx", "QUEUED")]
    with workflow.db() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT job_id,status FROM intake_decisions")]
    assert rows == [("job_two", "queued")]  # one decision, following the posting's new id
    # A real change (a different role at the same link) is a different job.
    assert jobs.identity_key({**first, "title": "Data Engineer Intern"}) != jobs.identity_key(first)


def test_rows_imported_before_identities_existed_are_not_reannounced(state, monkeypatch, tmp_path):
    sent = feed(state, monkeypatch)
    path = tmp_path / "keryx.json"
    first = job("legacy")
    jobs.ingest(snapshot(path, first), "a" * 40)
    with jobs.database() as db:
        db.execute("DELETE FROM job_material")  # as left by a version without the table
        db.execute("DELETE FROM job_events")
    result = jobs.ingest(snapshot(path, {**first, "link_status": "ats-verified"}), "b" * 40)
    assert (result["rewritten"], result["changed"]) == (1, 0)
    assert discord_feed.tick()["sent"] == 0 and not posts(sent, "jobs")


def test_an_outbox_from_before_scores_gains_the_column_and_still_posts(state, monkeypatch):
    listing = job("old")
    with jobs.database() as db:
        db.executescript("""
            CREATE TABLE feed_outbox(
              key TEXT PRIMARY KEY, job_id TEXT NOT NULL, payload TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', message_id TEXT);
        """)
        db.execute(
            "INSERT INTO feed_outbox(key,job_id,payload) VALUES(?,?,?)",
            ("f" * 64, listing["id"], json.dumps(listing)),
        )
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                jobs.REPOSITORY,
                listing["id"],
                listing["company"],
                listing["title"],
                listing["location"],
                "internship",
                listing["cycle"],
                "open",
                1,
                listing["url"],
                None,
                json.dumps(listing),
                "h",
                "a" * 40,
                "t",
                "t",
            ),
        )
    sent = feed(state, monkeypatch)
    assert discord_feed.tick()["sent"] == 1
    (card,) = [p["embeds"][0] for p in posts(sent, "jobs")]
    assert card["description"] == "**Example Labs** · Austin, TX"
    with discord_feed.feed_db() as db:
        assert tuple(db.execute("SELECT status,score FROM feed_outbox").fetchone()) == ("sent", 0)


# --- tiers -----------------------------------------------------------------

BORDERLINE = [
    job("ml", "Software Engineer Intern, Backend/ML", company="Umbrella Example"),  # capped: 69
    job("it", "IT Intern"),  # 65
    job("qa", "QA Engineer Intern", company="Globex Example", location="Denver, CO"),  # 60
    job(
        "fw",
        "Firmware Engineer Intern",
        company="Hooli Example",
        location="Denver, CO",
        posted_at=days_ago(10),
    ),  # 55
]


def test_tier_one_is_queued_tier_two_waits_for_the_digest_tier_three_is_only_counted(
    state, monkeypatch, tmp_path
):
    from rove import reasoning, runtime

    monkeypatch.setattr(runtime, "client", lambda: pytest.fail("intake never calls the model"))
    monkeypatch.setattr(reasoning, "review_job", lambda *a: pytest.fail("no model at intake"))
    log = channels(monkeypatch)
    sent = feed(state, monkeypatch)
    records = [
        job("swe"),
        *BORDERLINE,
        job("tax", "Tax Technology Intern"),
        job("civil", "Civil Engineering Intern"),
    ]
    jobs.ingest(snapshot(tmp_path / "keryx.json", *records), "a" * 40)
    result = discord_feed.tick()
    assert (result["queued"], result["digest"], result["dropped"], result["sent"]) == (1, 4, 2, 1)
    # Only the clear fit is queued and announced, one glanceable card, no identifiers.
    assert queue() == [("Example Labs — Software Engineer Intern", "keryx", "QUEUED")]
    (card,) = [p["embeds"][0] for p in posts(sent, "jobs")]
    assert card["title"] == "Software Engineer Intern"
    assert card["description"] == (
        f"**Example Labs** · Austin, TX\nsoftware role · {SUMMER_WORDS} · in Austin"
    )
    assert card["footer"]["text"] == "Queued"
    # The borderline jobs are one numbered card in the shortlist, best score first.
    (digest_post,) = posts(sent, "short")
    lines = digest_post["embeds"][0]["description"].split("\n")
    assert [line.split(" · ")[0] for line in lines] == [
        "1. **Umbrella Example**",
        "2. **Example Labs**",
        "3. **Globex Example**",
        "4. **Hooli Example**",
    ]
    assert lines[2] == (
        "3. **Globex Example** · [QA Engineer Intern](https://jobs.example.com/qa) · Denver, CO"
        f" · _testing role · {SUMMER_WORDS} · posted 2 days ago_"
    )
    assert "mentions machine learning, which you exclude" in lines[0]
    assert digest_post["embeds"][0]["fields"][0]["value"] == "```\n3 yes\n5 no\nall yes\nnone\n```"
    with workflow.db() as conn:
        rows = conn.execute("SELECT identity,application_id FROM intake_decisions").fetchall()
        dropped = conn.execute(
            "SELECT COUNT(*) FROM intake_decisions WHERE status='dropped'"
        ).fetchone()[0]
    shown = json.dumps([card, digest_post])
    assert dropped == 2 and len(rows) == 7
    hidden = [value for r in rows for value in (r["identity"], r["application_id"]) if value]
    assert len(hidden) == 8 and not any(value in shown for value in hidden)
    # The junk is counted in the system log and appears nowhere the owner reads.
    (line,) = [p["content"] for m, path, p in log if path == "/channels/sys/messages"]
    assert line == "`intake` · feed · 1 queued · 4 for the digest · 2 dropped below the bar"
    assert "Tax" not in shown and "Civil" not in shown
    # A second run announces nothing again and keeps the one digest.
    again = discord_feed.tick()
    assert (again["sent"], again["queued"], again["digest"]) == (0, 0, 0)
    assert len(posts(sent, "short")) == 1 and len(posts(sent, "jobs")) == 1


def test_the_backlog_cap_keeps_the_best_scores_not_the_latest_arrivals(
    state, monkeypatch, tmp_path
):
    sent = feed(state, monkeypatch, max_pending=2, batch_size=1)
    records = [
        job("best", "Backend Developer Intern", company="Northwind Example"),
        job("good", "Software Engineer Intern"),
        job("weak", "Mobile Developer Intern", location="Denver, CO", posted_at=days_ago(30)),
    ]
    assert [score(r)["tier"] for r in records] == [1, 1, 1]
    jobs.ingest(snapshot(tmp_path / "keryx.json", *records), "a" * 40)
    first = discord_feed.tick()
    assert (first["expired"], first["sent"], first["pending"]) == (1, 1, 1)
    second = discord_feed.tick()
    assert (second["expired"], second["sent"], second["pending"]) == (0, 1, 0)
    # The weakest arrived last and is the one left out; the best is announced first.
    assert [p["embeds"][0]["title"] for p in posts(sent, "jobs")] == [
        "Backend Developer Intern",
        "Software Engineer Intern",
    ]
    with discord_feed.feed_db() as db:
        left_out = db.execute("SELECT payload FROM feed_outbox WHERE status='expired'").fetchone()[
            0
        ]
        state_of = db.execute(
            "SELECT status FROM intake_decisions WHERE job_id='job_weak'"
        ).fetchone()[0]
    assert json.loads(left_out)["id"] == "job_weak" and state_of == "capped"
    # A manual seed brings the job the cap left out back in.
    third = discord_feed.tick(seed=True)
    assert third["sent"] == 1
    assert posts(sent, "jobs")[-1]["embeds"][0]["title"] == "Mobile Developer Intern"


def test_the_same_role_in_ten_places_is_one_card_and_one_application(state, monkeypatch, tmp_path):
    sent = feed(state, monkeypatch)
    cities = ["Austin, TX", "Denver, CO", "Boston, MA", "Tampa, FL", "Reno, NV"]
    records = [
        job(f"site{n}", location=cities[n % 5] if n < 5 else f"{cities[n % 5]} (Site {n})")
        for n in range(10)
    ]
    jobs.ingest(snapshot(tmp_path / "keryx.json", *records), "a" * 40)
    result = discord_feed.tick()
    assert (result["sent"], result["queued"]) == (1, 1)
    (card,) = [p["embeds"][0] for p in posts(sent, "jobs")]
    # The preferred city leads; the rest are counted on the same card.
    assert card["description"].startswith("**Example Labs** · Austin, TX · also in 9 other places")
    assert len(queue()) == 1
    # The same role posted in one more city later is not news either.
    jobs.ingest(
        snapshot(tmp_path / "keryx.json", *records, job("site10", location="Miami, FL")), "b" * 40
    )
    assert discord_feed.tick()["sent"] == 0 and len(queue()) == 1


# --- the digest ------------------------------------------------------------


def open_digest(state, monkeypatch, tmp_path) -> list:
    channels(monkeypatch)
    sent = feed(state, monkeypatch)
    jobs.ingest(snapshot(tmp_path / "keryx.json", *BORDERLINE), "a" * 40)
    assert discord_feed.tick()["digest"] == 4
    assert len(posts(sent, "short")) == 1 and queue() == []
    return sent


def decisions() -> dict:
    with workflow.db() as conn:
        return {
            r["job_id"]: r["status"]
            for r in conn.execute("SELECT job_id,status FROM intake_decisions")
        }


def test_digest_replies_queue_and_skip_by_number_and_edit_the_card_in_place(
    state, monkeypatch, tmp_path
):
    sent = open_digest(state, monkeypatch, tmp_path)
    # Anyone but the owner, a bot, or the owner in another channel: not a digest reply.
    stranger = {"author": {"id": "stranger"}, "content": "all yes"}
    assert intake.digest_reply(stranger, "owner", "short") is False
    assert (
        intake.digest_reply(owner("all yes", author={"id": "owner", "bot": True}), "owner", "short")
        is False
    )
    assert intake.digest_reply(owner("all yes"), "owner", "action") is False
    assert intake.digest_reply(owner("go"), "owner", "short") is False
    assert queue() == [] and set(decisions().values()) == {"offered"}

    assert intake.digest_reply(owner("3 yes"), "owner", "short") is True
    assert queue() == [("Globex Example — QA Engineer Intern", "owner_pick", "QUEUED")]
    method, path, edited = sent[-2]
    assert (method, path) == ("PATCH", "/channels/short/messages/m1")
    lines = edited["embeds"][0]["description"].split("\n")
    assert lines[2] == "~~3. Globex Example · QA Engineer Intern~~ queued"
    assert lines[0].startswith("1. **Umbrella Example**")
    assert posts(sent, "short")[-1]["content"] == "Queued Globex Example."

    with pytest.raises(ValueError, match="no number 9 on today's list; it goes up to 4"):
        intake.digest_reply(owner("9 yes"), "owner", "short")
    assert intake.digest_reply(owner("1 no"), "owner", "short") is True
    assert decisions() == {
        "job_ml": "declined",
        "job_it": "offered",
        "job_qa": "picked",
        "job_fw": "offered",
    }
    # A repeated reply changes nothing; `all yes` answers what is still open and the
    # finished card leaves the channel.
    assert intake.digest_reply(owner("3 yes"), "owner", "short") is True
    assert len(queue()) == 1
    assert intake.digest_reply(owner("all yes"), "owner", "short") is True
    assert [row[1] for row in queue()] == ["owner_pick"] * 3
    assert ("DELETE", "/channels/short/messages/m1", None) in sent
    assert set(decisions().values()) == {"picked", "declined"}
    with workflow.db() as conn:
        assert conn.execute("SELECT delivery FROM intake_digests").fetchone()[0] == "withdrawn"
        scores = [r[0] for r in conn.execute("SELECT score FROM queue_scores ORDER BY score")]
    assert scores == [55, 60, 65]


def test_none_skips_the_whole_list_and_several_numbers_fit_in_one_reply(
    state, monkeypatch, tmp_path
):
    assert intake.read_reply("3 yes") == {3: "yes"}
    assert intake.read_reply("1, 3 yes 2 no") == {1: "yes", 3: "yes", 2: "no"}
    assert intake.read_reply("2-4 y") == {2: "yes", 3: "yes", 4: "yes"}
    assert intake.read_reply("`all yes`") == "yes" and intake.read_reply("None.") == "no"
    for other in ("go", "2: Yes", "yes", "3 yes please", "park it", ""):
        assert intake.read_reply(other) is None, other
    sent = open_digest(state, monkeypatch, tmp_path)
    assert intake.digest_reply(owner("none"), "owner", "short") is True
    assert queue() == [] and set(decisions().values()) == {"declined"}
    assert sent[-2][:2] == ("DELETE", "/channels/short/messages/m1")
    assert posts(sent, "short")[-1]["content"] == "Skipped 4."


def test_yes_to_a_link_that_is_already_an_application_adds_nothing(state, monkeypatch, tmp_path):
    sent = open_digest(state, monkeypatch, tmp_path)
    pasted = workflow.enqueue("https://jobs.example.com/qa?utm_source=friend")["application_id"]
    workflow.set_state(pasted, "APPLIED")
    assert intake.digest_reply(owner("3 yes, 4 no"), "owner", "short") is True
    assert queue() == [("", "owner_link", "APPLIED")]
    assert posts(sent, "short")[-1]["content"] == (
        "Already on your list: Globex Example · skipped 1."
    )


def test_a_better_listing_of_an_offered_role_is_queued_and_its_digest_line_closes(
    state, monkeypatch, tmp_path
):
    channels(monkeypatch)
    sent = feed(state, monkeypatch)
    path = tmp_path / "keryx.json"
    board = job("board", "Backend Developer Intern", url="https://jobright.ai/jobs/info/synthetic")
    jobs.ingest(snapshot(path, board, BORDERLINE[1]), "a" * 40)
    assert discord_feed.tick()["digest"] == 2 and queue() == []
    # The feed later finds the employer's own link for the same role.
    direct = {**board, "url": "https://jobs.example.com/backend", "link_status": "ats-verified"}
    assert jobs.ingest(snapshot(path, direct, BORDERLINE[1]), "b" * 40)["changed"] == 1
    result = discord_feed.tick()
    assert (result["queued"], result["sent"]) == (1, 1)
    assert queue() == [("Example Labs — Backend Developer Intern", "keryx", "QUEUED")]
    edited = [p for m, path_, p in sent if m == "PATCH" and path_ == "/channels/short/messages/m1"]
    lines = edited[-1]["embeds"][0]["description"].split("\n")
    assert lines[0] == "~~1. Example Labs · Backend Developer Intern~~ queued from another listing"
    # Saying yes to the closed line queues nothing twice; the other line still answers.
    assert intake.digest_reply(owner("1 yes, 2 no"), "owner", "short") is True
    assert len(queue()) == 1 and decisions()["job_it"] == "declined"
    assert ("DELETE", "/channels/short/messages/m1", None) in sent


def test_an_expired_digest_is_withdrawn_and_its_open_lines_return_with_new_numbers(
    state, monkeypatch, tmp_path
):
    sent = open_digest(state, monkeypatch, tmp_path)
    assert intake.digest_reply(owner("1 yes"), "owner", "short") is True
    tomorrow = datetime.now().astimezone() + timedelta(days=1)
    intake.run_digest({}, now=tomorrow)
    assert ("DELETE", "/channels/short/messages/m1", None) in sent
    fresh = posts(sent, "short")[-1]["embeds"][0]["description"].split("\n")
    assert [line.split(" · ")[0] for line in fresh] == [
        "1. **Example Labs**",
        "2. **Globex Example**",
        "3. **Hooli Example**",
    ]
    assert decisions()["job_ml"] == "picked"
    # A reply to yesterday's card says so and queues nothing.
    stale = owner("2 yes", message_reference={"message_id": "m1"})
    with pytest.raises(ValueError, match="That list has closed"):
        intake.digest_reply(stale, "owner", "short")
    assert len(queue()) == 1
    # Running again the same day keeps the one live card.
    count = len(sent)
    intake.run_digest({}, now=tomorrow)
    assert len(sent) == count
    # Once nothing is open, a numbered reply is told there is no list.
    assert intake.digest_reply(owner("none"), "owner", "short") is True
    with pytest.raises(ValueError, match="No list is open right now"):
        intake.digest_reply(owner("2 yes"), "owner", "short")
    assert len(queue()) == 1
    # Lines nobody answers lapse after the keep window instead of returning forever.
    with workflow.db() as conn:
        conn.execute(
            "UPDATE intake_decisions SET status='digest',decided_at='2020-01-01T00:00:00+00:00'"
        )
    intake.run_digest({}, now=tomorrow + timedelta(days=1))
    assert set(decisions().values()) == {"lapsed"}


def test_the_worker_reads_digest_replies_before_anything_else_in_the_shortlist(
    state, monkeypatch, tmp_path
):
    sent = open_digest(state, monkeypatch, tmp_path)
    monkeypatch.setattr(worker, "private_env", lambda: {"DISCORD_OWNER_USER_ID": "owner"})
    with workflow.db() as conn:
        for channel in ("action", "short", "sys"):
            conn.execute("INSERT INTO workflow_checkpoints VALUES(?,?)", (channel, "100"))
    said = []

    def discord(method, path, payload=None):
        if method == "GET" and path.startswith("/channels/short/"):
            return [
                {"id": "101", "author": {"id": "stranger"}, "content": "all yes"},
                {"id": "102", "author": {"id": "owner"}, "content": "2 yes, 4 no"},
                {"id": "103", "author": {"id": "owner"}, "content": "8 yes"},
            ]
        if method == "POST":
            said.append(payload["content"])
        return []

    monkeypatch.setattr(worker, "discord", discord)
    worker.poll_commands()
    assert queue() == [("Example Labs — IT Intern", "owner_pick", "QUEUED")]
    assert decisions()["job_fw"] == "declined" and decisions()["job_ml"] == "offered"
    # The reply that cannot apply gets one plain line; nobody is told how replies work.
    assert said == ["There is no number 8 on today's list; it goes up to 4."]
    assert posts(sent, "short")[-1]["content"] == "Queued Example Labs · skipped 1."


def test_a_digest_pick_is_held_on_fit_only_for_a_conflict_code_verified(state, monkeypatch):
    from rove import reasoning, submission

    assert intake.fit_may_hold("owner_link", {"decision": "not_fit"}) is False
    assert intake.fit_may_hold("keryx", {"decision": "needs_review"}) is True
    form = {
        "url": "https://jobs.example.com/form",
        "observation_id": "obs-1",
        "fields": [
            {
                "label": "First name",
                "name": "first",
                "kind": "text",
                "options": [],
                "required": True,
            }
        ],
    }
    prepared = {
        "pending": [],
        "package_hash": "a" * 64,
        "filled": [],
        "final_controls": [{"ref": "0", "label": "Submit application"}],
    }
    monkeypatch.setattr(
        worker, "browser_call", lambda action, **kw: prepared if action == "prepare" else form
    )
    monkeypatch.setattr(
        worker, "prepare_resume", lambda *a: {"ready": True, "resume_sha256": "b" * 64}
    )
    monkeypatch.setattr(submission, "enabled_adapter", lambda url: object())
    review = {
        "decision": "needs_review",
        "rationale": "unsure",
        "unverified": [],
        "requirements": [
            {
                "kind": "sponsorship",
                "requirement": "No visa sponsorship",
                "evidence": "",
                "status": "conflict",
                "checked_by": "qwen",
                "note": "",
            }
        ],
    }
    monkeypatch.setattr(reasoning, "review_job", lambda *a: review)
    channels(monkeypatch)
    picked = workflow.enqueue(
        "https://jobs.example.com/picked", source=intake.OWNER_PICK, title="Example — IT Intern"
    )["application_id"]
    assert worker.process(picked)["status"] == "READY_FOR_REVIEW"
    review["decision"] = "not_fit"
    review["requirements"][0]["checked_by"] = "code"
    hard = workflow.enqueue(
        "https://jobs.example.com/hard", source=intake.OWNER_PICK, title="Example — QA Intern"
    )["application_id"]
    assert worker.process(hard)["status"] == "NEEDS_USER"
    assert workflow.latest_hold(hard)["headline"] == "Your call on fit"


# --- the queue: rescoring, order and pacing --------------------------------


def test_queued_feed_jobs_are_rescored_and_junk_is_parked_with_the_reason(
    state, monkeypatch, tmp_path
):
    listed = job("tax", "Tax Technology Intern")
    good = job("good")
    elsewhere = job("twin", location="Denver, CO")  # the same role, a less preferred place
    jobs.ingest(snapshot(tmp_path / "keryx.json", listed, good, elsewhere), "a" * 40)
    junk = workflow.enqueue(
        listed["url"], source="keryx", title="Example Labs — Tax Technology Intern"
    )["application_id"]
    twin = workflow.enqueue(
        elsewhere["url"], source="keryx", title="Example Labs — Software Engineer Intern"
    )["application_id"]
    kept = workflow.enqueue(
        good["url"], source="keryx", title="Example Labs — Software Engineer Intern"
    )["application_id"]
    # No listing to score: only a hard rule parks it, never a low score.
    unlisted = workflow.enqueue(
        "https://jobs.example.com/unlisted", source="keryx", title="Example — Intern"
    )["application_id"]
    picked = workflow.enqueue(
        "https://jobs.example.com/ml",
        source=intake.OWNER_PICK,
        title="Example — Machine Learning Engineer Intern",
    )["application_id"]
    assert worker.prune_excluded() == 2
    assert [workflow.get(a)["status"] for a in (junk, twin, kept, unlisted, picked)] == [
        "DEFERRED",
        "DEFERRED",
        "QUEUED",
        "QUEUED",
        "QUEUED",
    ]
    assert workflow.get(junk)["error"] == (
        "excluded by your rules: general technology role · tax work, outside what you asked for"
        f" · {SUMMER_WORDS}"
    )
    assert workflow.get(twin)["error"] == "the same role is already queued for another place"
    with workflow.db() as conn:
        scores = {r[0]: r[1] for r in conn.execute("SELECT application_id,score FROM queue_scores")}
    assert scores[kept] == score(good)["score"] and unlisted in scores and junk not in scores
    assert worker.prune_excluded() == 0
    # A parked job the owner tells to go again is theirs to decide: it is not parked twice.
    worker.apply_command({"kind": "resume", "application_id": junk}, "m-go")
    assert worker.prune_excluded() == 0 and workflow.get(junk)["status"] == "QUEUED"


def clock(monkeypatch, moment: datetime):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(worker, "datetime", Clock)


def attempt(application_id: str, when: datetime, status: str = "APPLIED"):
    with workflow.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO live_submission_attempts"
            "(application_id,package_hash,owner_message_id,status,created_at) VALUES(?,?,?,?,?)",
            (application_id, "a" * 64, f"auto-submit:{application_id}", status, when.isoformat()),
        )


def queued(path: str, points: int | None = None, source: str = "keryx") -> str:
    application_id = workflow.enqueue(
        f"https://{path}", source=source, title="Example — Software Intern"
    )["application_id"]
    if points is not None:
        with intake.db() as conn:
            intake.record_queue_score(conn, application_id, points, "", "test")
    return application_id


def take() -> str | None:
    """The next application, then out of the way so the following one can be read."""
    application_id = worker.next_queued(1)
    if application_id:
        workflow.set_state(application_id, "DEFERRED")
    return application_id


def test_queue_order_is_resumed_then_pasted_then_picked_then_score_then_newest(state):
    unscored = queued("jobs.example.com/unscored")
    low = queued("jobs.example.com/low", 40)
    older = queued("jobs.example.com/older", 88)
    newer = queued("jobs.example.com/newer", 81)  # same band of ten as 88: the newer goes first
    top = queued("jobs.example.com/top", 95)
    picked = queued("jobs.example.com/picked", 50, source=intake.OWNER_PICK)
    pasted = queued("jobs.example.com/pasted", source="owner_link")
    resumed = queued("jobs.example.com/resumed", 10)
    workflow.set_state(resumed, "NEEDS_USER")
    worker.apply_command({"kind": "resume", "application_id": resumed}, "m-resume")
    order = [take() for _ in range(9)]
    assert order == [resumed, pasted, picked, top, newer, older, low, unscored, None]


def test_the_platform_gap_sends_the_best_job_on_another_platform_instead_of_idling(
    state, monkeypatch
):
    noon = datetime(NOW.year, NOW.month, NOW.day, 12, 0, tzinfo=UTC)
    clock(monkeypatch, noon)
    settings = {"enabled": True, "auto_submit": True}
    monkeypatch.setattr(workflow, "config", lambda: settings)
    assert intake.platform_of("https://boards.greenhouse.io/example/jobs/4000001") == "greenhouse"
    assert intake.platform_of("https://job-boards.greenhouse.io/example/jobs/1") == "greenhouse"
    assert intake.platform_of("https://jobs.lever.co/example/abc-def") == "lever"
    assert intake.platform_of("https://jobs.ashbyhq.com/example/abc-def") == "ashby"
    assert intake.platform_of("https://example.wd5.myworkdayjobs.com/careers/job/R1") == "workday"
    assert intake.platform_of("https://www.example.com/careers/12345") == "example.com"
    assert intake.platform_of("not a link") == ""

    sent = queued("boards.greenhouse.io/example/jobs/4000001", 90)
    workflow.set_state(sent, "APPLIED")
    best = queued("boards.greenhouse.io/example/jobs/4000002", 95)
    lever = queued("jobs.lever.co/example/role-one", 80)
    own_site = queued("careers.example.com/jobs/70001", 72)
    attempt(sent, noon - timedelta(seconds=30))
    # Greenhouse took a submission 30 seconds ago: the best job elsewhere goes next.
    assert worker.next_queued(1) == lever
    with workflow.db() as conn:
        assert conn.execute("SELECT platform FROM live_submission_attempts").fetchone()[0] == (
            "greenhouse"
        )
    attempt(lever, noon - timedelta(seconds=10))
    assert worker.next_queued(1) == own_site
    workflow.set_state(own_site, "DEFERRED")
    assert worker.next_queued(1) is None  # every platform with work is resting
    # The gap is per platform for everything sent unattended, a pasted link included.
    pasted = queued("boards.greenhouse.io/example/jobs/4000003", source="owner_link")
    assert worker.next_queued(1) is None
    clock(monkeypatch, noon + timedelta(seconds=61))
    assert worker.next_queued(1) == pasted  # 91 seconds on: Greenhouse is free again
    workflow.set_state(pasted, "DEFERRED")
    assert worker.next_queued(1) == best
    settings["min_seconds_between_submissions_per_platform"] = 600
    assert worker.next_queued(1) is None
    settings["min_seconds_between_submissions_per_platform"] = 0
    clock(monkeypatch, noon)
    assert worker.next_queued(1) == best


def test_the_daily_cap_defaults_to_thirty_and_null_settings_do_not_crash(state, monkeypatch):
    noon = datetime(NOW.year, NOW.month, NOW.day, 12, 0, tzinfo=UTC)
    clock(monkeypatch, noon)
    settings = {
        "enabled": True,
        "auto_submit": True,
        "max_submissions_per_day": None,
        "min_minutes_between_submissions": None,
        "min_seconds_between_submissions_per_platform": None,
        "max_waiting_applications": None,
    }
    monkeypatch.setattr(workflow, "config", lambda: settings)
    assert intake.number(settings, "max_submissions_per_day", 30) == 30
    assert intake.number({"x": "many"}, "x", 7) == 7 and intake.number({"x": "4"}, "x", 7) == 4
    feed_job = queued("jobs.example.com/feed", 80)
    for index in range(29):
        attempt(f"sent{index:08d}", noon - timedelta(minutes=5 + index))
    # 29 sent today, the last five minutes ago: no global gap by default, so the next goes.
    assert worker.next_queued(1) == feed_job
    attempt("sent00000029", noon - timedelta(minutes=4))
    assert worker.next_queued(1) is None  # thirty attempts on record today
    picked = queued("jobs.example.com/picked", source=intake.OWNER_PICK)
    assert worker.next_queued(1) is None  # a digest pick waits for tomorrow as well
    pasted = queued("jobs.example.com/pasted", source="owner_link")
    assert worker.next_queued(1) == pasted
    workflow.set_state(pasted, "DEFERRED")
    # Yesterday's attempts do not count against today.
    with workflow.db() as conn:
        conn.execute(
            "UPDATE live_submission_attempts SET created_at=? WHERE application_id='sent00000029'",
            ((noon - timedelta(days=1)).isoformat(),),
        )
    assert worker.next_queued(1) == picked
    settings["max_submissions_per_day"] = 3
    assert worker.next_queued(1) is None
    # An owner who still wants the old global gap can set it.
    settings.update(max_submissions_per_day=40, min_minutes_between_submissions=8)
    assert worker.next_queued(1) is None
    settings["min_minutes_between_submissions"] = 5
    assert worker.next_queued(1) == picked


def test_holds_never_stall_an_unattended_queue_and_still_do_with_auto_submit_off(
    state, monkeypatch
):
    from test_unattended_failure_modes import enabled_worker

    holds = [queued(f"jobs.example.com/hold{n}") for n in range(3)]
    for application_id in holds:
        workflow.set_state(application_id, "NEEDS_USER")
    waiting = queued("jobs.example.com/next", 80)
    assert worker.next_queued(2) is None  # auto_submit off: two holds are the limit
    picked = queued("jobs.example.com/picked", source=intake.OWNER_PICK)
    assert worker.next_queued(2) == picked  # the owner's own pick is not held back
    workflow.set_state(picked, "DEFERRED")

    enabled_worker(monkeypatch, auto_submit=True, max_waiting_applications=None)
    processed = []
    monkeypatch.setattr(
        worker,
        "process",
        lambda app: (
            processed.append(app)
            or {"application_id": app, "status": "NEEDS_USER", "submitted": False}
        ),
    )
    assert worker.tick()["application_id"] == waiting
    assert processed == [waiting]


def test_the_platform_column_is_added_once_and_old_attempts_are_kept(state):
    old = queued("boards.greenhouse.io/example/jobs/4000009", 70)
    workflow.set_state(old, "APPLIED")
    stamp = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    with workflow.db() as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(live_submission_attempts)")]
        assert "platform" not in columns
        conn.execute(
            "INSERT INTO live_submission_attempts VALUES(?,?,?,?,?)",
            (old, "a" * 64, "m-old", "APPLIED", stamp),
        )
        conn.execute(
            "INSERT INTO live_submission_attempts VALUES(?,?,?,?,?)",
            ("gone00000001", "a" * 64, "m-gone", "UNKNOWN_SUBMISSION", stamp),
        )
    for _ in range(2):
        assert worker.next_queued(1) is None
    with workflow.db() as conn:
        rows = [
            tuple(r)
            for r in conn.execute(
                "SELECT application_id,owner_message_id,status,created_at,platform "
                "FROM live_submission_attempts ORDER BY owner_message_id"
            )
        ]
    assert rows == [
        ("gone00000001", "m-gone", "UNKNOWN_SUBMISSION", stamp, ""),
        (old, "m-old", "APPLIED", stamp, "greenhouse"),
    ]
    # A submission on record names the page it was sent from when that is known.
    directory = state / "applications" / old
    directory.mkdir(parents=True)
    (directory / "workflow-result.json").write_text(
        json.dumps({"page": {"url": "https://jobs.lever.co/example/role-one/apply"}})
    )
    assert intake.attempt_platform(old, workflow.get(old)["url"]) == "lever"
