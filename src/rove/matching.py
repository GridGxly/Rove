"""Explainable discovery ranking; never asserts eligibility or creates applications."""

import json
import re
from collections import Counter
from urllib.parse import urlsplit

from . import education
from .jobs import database
from .onboarding import digest, draft, read_approved

# Approved exclusions are phrases; postings abbreviate them. An "AI/ML" or "ML" title is
# the machine-learning specialist role the exclusion names.
EXCLUSION_SYNONYMS = {
    "machine learning": ("ai ml", "ml", "ml ai"),
    "artificial intelligence": ("ai ml", "ml ai"),
}


def contains(text: str, phrase: str) -> bool:
    normalize = lambda value: " ".join(re.findall(r"[a-z0-9]+", value.lower()))
    haystack = f" {normalize(text)} "
    wanted = [normalize(phrase), *EXCLUSION_SYNONYMS.get(normalize(phrase), ())]
    return any(f" {needle} " in haystack for needle in wanted)


def excluded_by_rules(title: str, company: str = "", prefs: dict | None = None) -> str:
    """The approved exclusion a job trips, or an empty string. Rules can change after intake.

    An excluded keyword counts when it names the role itself; a title that only lists it
    among other options is a question for the owner, not an exclusion.
    """
    from .intake import score_job

    prefs = prefs or read_approved()["profile"]["preferences"]
    result = score_job({"title": title, "company": company}, {"preferences": prefs})
    return result["reason"] if result["skip"] in {"excluded_role", "excluded_company"} else ""


def review_matches(limit: int = 10, *, preview_draft: bool = False) -> dict:
    if not 1 <= limit <= 25:
        raise ValueError("Use a limit of 1–25")
    if preview_draft:
        profile = draft()
        profile_hash = digest(profile)
    else:
        approved = read_approved()
        profile, profile_hash = approved["profile"], approved["profile_hash"]
    prefs = profile["preferences"]
    if not prefs["programs"] or not prefs["title_keywords"]:
        raise ValueError("Choose job programs and title keywords in onboarding first")
    from .intake import score_job

    db = database()
    ranked, excluded = [], Counter()
    try:
        for row in db.execute("SELECT metadata,revision FROM jobs WHERE active=1"):
            job = json.loads(row["metadata"])
            # The same deterministic score the feed uses; anything below the digest bar
            # is left out and counted by the reason it fell.
            result = score_job(job, profile)
            if result["tier"] == 3:
                excluded[
                    result["skip"] or ("low_score" if result["family"] else "no_title_match")
                ] += 1
                continue
            keywords = [word for word in prefs["title_keywords"] if contains(job["title"], word)]
            holds = ["Verify the current employer posting and all requirements before preparing."]
            if not prefs["any_cycle"] and prefs["cycles"] and job["cycle"] not in prefs["cycles"]:
                holds.append("Recruiting term is outside the selected terms or not stated.")
            if (
                "co op" in " ".join(re.findall(r"[a-z]+", job["title"].lower()))
                and profile["availability"]["co_op_semester_off"] is not True
            ):
                holds.append("Co-op schedule or school leave needs confirmation.")
            if not profile["availability"]["hours_per_week"]:
                holds.append("Candidate weekly availability is not confirmed.")
            academic = job["academic_eligibility"]
            if academic.get("requirement_level") == "required":
                graduation = education.graduation(profile)  # the school applications state
                start, end = academic.get("graduation_start"), academic.get("graduation_end")
                if graduation and ((start and graduation < start) or (end and graduation > end)):
                    holds.append(
                        "Source graduation requirement appears to conflict with the profile."
                    )
            if academic.get("status") in ("unavailable", "not-found", None):
                holds.append("Academic requirements are unavailable or unstated in the feed.")
            url = job["url"]
            fallback = bool(url and urlsplit(url).hostname in ("jobright.ai", "www.jobright.ai"))
            if not url or fallback:
                holds.append("Direct employer application URL still needs resolution.")
            ranked.append(
                {
                    "id": job["id"],
                    "company": job["company"],
                    "title": job["title"],
                    "location": job["location"],
                    "cycle": job["cycle"],
                    "url": url,
                    "posted_at": job["posted_at"],
                    "program": job["program"],
                    "score": result["score"],
                    "tier": result["tier"],
                    "reason": result["reason"],
                    "matched_keywords": keywords,
                    "review_needed": holds,
                    "academic_summary": str(academic.get("summary", "Not available"))[:600],
                    "source_revision": row["revision"],
                }
            )
    finally:
        db.close()
    ranked.sort(key=lambda j: (j["score"], j["posted_at"] or "", j["id"]), reverse=True)
    return {
        "mode": "draft_preferences_preview" if preview_draft else "approved_preferences",
        "profile_hash": profile_hash,
        "total_matches": len(ranked),
        "excluded": dict(excluded),
        "authority": "Discovery suggestions only. Not verified eligibility or application approval.",
        "submission_enabled": False,
        "jobs": ranked[:limit],
    }
