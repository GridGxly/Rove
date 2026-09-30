"""Explainable discovery ranking; never asserts eligibility or creates applications."""

import json
import re
from collections import Counter
from urllib.parse import urlsplit

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
    """The approved exclusion a job trips, or an empty string. Rules can change after intake."""
    prefs = prefs or read_approved()["profile"]["preferences"]
    for word in prefs.get("excluded_title_keywords", []):
        if contains(title, word):
            return f"title matches your excluded keyword '{word}'"
    for name in prefs.get("excluded_companies", []):
        if company and contains(company, name):
            return f"company matches your exclusion '{name}'"
    return ""


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
    db = database()
    ranked, excluded = [], Counter()
    try:
        for row in db.execute("SELECT metadata,revision FROM jobs WHERE active=1"):
            job = json.loads(row["metadata"])
            if job["program"] not in prefs["programs"]:
                excluded["program"] += 1
                continue
            if any(contains(job["title"], word) for word in prefs["excluded_title_keywords"]):
                excluded["excluded_role"] += 1
                continue
            keywords = [word for word in prefs["title_keywords"] if contains(job["title"], word)]
            if not keywords:
                excluded["no_title_match"] += 1
                continue
            if any(contains(job["company"], c) for c in prefs["excluded_companies"]):
                excluded["excluded_company"] += 1
                continue
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
                graduation = next(
                    (
                        s["graduation_month"]
                        for s in profile["education"]["schools"]
                        if s["graduation_month"]
                    ),
                    None,
                )
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
            score = 10 + (4 if contains(job["title"], "software") else 0)
            score += 3 if url and not fallback else 0
            score += 2 if job["link_status"] == "ats-verified" else 0
            if any(contains(job["company"], c) for c in prefs["priority_companies"]):
                score += 5
            ranked.append(
                {
                    "id": job["id"],
                    "company": job["company"],
                    "title": job["title"],
                    "location": job["location"],
                    "cycle": job["cycle"],
                    "url": url,
                    "posted_at": job["posted_at"],
                    "score": score,
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
