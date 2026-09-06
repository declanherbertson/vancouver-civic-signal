#!/usr/bin/env python3
"""Join classifications to normalized records and publish compact web data."""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from derive_motion_stages import derive_motion_stages


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POSITION_VOTES = {"In Favour", "In Opposition"}
PARTICIPATION_VOTES = POSITION_VOTES | {"Abstain"}


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            value,
            handle,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
        handle.write("\n")


def meeting_agenda_url(minutes_url: str | None) -> str | None:
    if not minutes_url or "/documents/" not in minutes_url or not minutes_url.endswith("min.pdf"):
        return None
    base, filename = minutes_url.rsplit("/documents/", 1)
    return f"{base}/{filename[:-7]}ag.htm"


def summarize_vote_scope(
    vote_records: list[dict], motion_category: dict[str, str]
) -> dict:
    """Build overall and category aggregates for one report-card scope."""
    category_counts: dict[str, Counter] = defaultdict(Counter)
    overall = Counter()
    for vote in vote_records:
        overall[vote["vote"]] += 1
        category_counts[motion_category[vote["motion_id"]]][vote["vote"]] += 1

    categories = []
    for category_id, counts in category_counts.items():
        total = sum(counts.values())
        positions = sum(counts[value] for value in POSITION_VOTES)
        participated = sum(counts[value] for value in PARTICIPATION_VOTES)
        categories.append(
            {
                "category_id": category_id,
                "total": total,
                "positions": positions,
                "participated": participated,
                "participation_rate": round(participated / total, 4) if total else 0,
                "vote_counts": dict(counts),
            }
        )
    categories.sort(key=lambda item: (-item["total"], item["category_id"]))

    positions = sum(overall[value] for value in POSITION_VOTES)
    participated = sum(overall[value] for value in PARTICIPATION_VOTES)
    return {
        "recorded_votes": len(vote_records),
        "positions": positions,
        "participated": participated,
        "participation_rate": round(participated / len(vote_records), 4)
        if vote_records
        else 0,
        "vote_counts": dict(overall),
        "categories": categories,
    }


def build_site_data(project_root: Path = PROJECT_ROOT) -> dict:
    processed = project_root / "data" / "processed"
    web_data = project_root / "web" / "data"
    motions = read_json(processed / "motions.json")
    votes = read_json(processed / "votes.json")
    members = read_json(processed / "members.json")
    metadata = read_json(processed / "metadata.json")
    taxonomy = read_json(project_root / "config" / "taxonomy.json")
    party_config = read_json(project_root / "config" / "member-parties.json")
    featured_config = read_json(project_root / "config" / "featured-motions.json")
    classifications_path = project_root / "data" / "classifications" / "motions.json"
    classifications = read_json(classifications_path) if classifications_path.exists() else []
    enrichment_path = project_root / "data" / "enrichment" / "motions.json"
    enrichment_values = read_json(enrichment_path) if enrichment_path.exists() else []

    known_motion_ids = {motion["motion_id"] for motion in motions}
    classification_map = {}
    for item in classifications:
        motion_id = item.get("motion_id")
        if motion_id not in known_motion_ids:
            continue
        classification_map[motion_id] = item

    enrichment_map = {
        item["motion_id"]: item
        for item in enrichment_values
        if item.get("motion_id") in known_motion_ids
    }
    stage_values = derive_motion_stages(motions, enrichment_values)
    stage_map = {item["motion_id"]: item for item in stage_values}
    write_json(project_root / "data" / "derived" / "motion-stages.json", stage_values, pretty=True)

    enriched_motions = []
    for motion in motions:
        classification = classification_map.get(motion["motion_id"])
        enriched = dict(motion)
        minutes = enrichment_map.get(motion["motion_id"])
        enriched.update(stage_map[motion["motion_id"]])
        if minutes:
            enriched.update(
                {
                    "enrichment_status": minutes.get("status", "pending"),
                    "motion_text": minutes.get("motion_text"),
                    "moved_by": minutes.get("moved_by"),
                    "seconded_by": minutes.get("seconded_by"),
                    "minutes_outcome": minutes.get("minutes_outcome"),
                    "minutes_url": minutes.get("minutes_url"),
                    "minutes_pdf_page": minutes.get("minutes_pdf_page"),
                    "minutes_vote_marker": minutes.get("source_vote_number"),
                    "minutes_text_truncated": minutes.get("text_truncated", False),
                    "enrichment_match_quality": minutes.get("match_quality"),
                }
            )
        else:
            enriched["enrichment_status"] = "pending"
        if classification:
            enriched.update(classification)
            enriched["classification_status"] = "classified"
        else:
            enriched.update(
                {
                    "primary_category": "unclassified",
                    "secondary_categories": [],
                    "policy_direction": "unclear",
                    "public_impact": "unclear",
                    "summary": "Classification pending.",
                    "keywords": [],
                    "confidence": None,
                    "needs_review": True,
                    "rationale": "",
                    "classification_status": "pending",
                }
            )
        enriched_motions.append(enriched)

    motion_category = {
        motion["motion_id"]: motion["primary_category"] for motion in enriched_motions
    }
    enriched_motion_map = {
        motion["motion_id"]: motion for motion in enriched_motions
    }
    divided_motion_ids = {
        motion["motion_id"]
        for motion in enriched_motions
        if (motion.get("vote_counts", {}).get("In Favour", 0) > 0)
        and (motion.get("vote_counts", {}).get("In Opposition", 0) > 0)
    }
    votes_by_member: dict[str, list[dict]] = defaultdict(list)
    for vote in votes:
        votes_by_member[vote["member_id"]].append(vote)

    report_cards = []
    party_members = party_config.get("members", {})
    member_ids = {member["member_id"] for member in members}
    if set(party_members) != member_ids:
        missing = sorted(member_ids - set(party_members))
        extra = sorted(set(party_members) - member_ids)
        raise ValueError(f"party metadata mismatch; missing={missing}, extra={extra}")
    for member in members:
        member_votes = votes_by_member[member["member_id"]]
        headline_votes = [
            vote
            for vote in member_votes
            if stage_map[vote["motion_id"]]["headline_eligible"]
        ]
        divided_headline_votes = [
            vote for vote in headline_votes if vote["motion_id"] in divided_motion_ids
        ]
        headline_scope = summarize_vote_scope(headline_votes, motion_category)
        divided_scope = summarize_vote_scope(divided_headline_votes, motion_category)
        all_overall = Counter(vote["vote"] for vote in member_votes)
        excluded_amendment_votes = len(member_votes) - len(headline_votes)
        report_cards.append(
            {
                **member,
                **party_members[member["member_id"]],
                **headline_scope,
                "all_recorded_votes": len(member_votes),
                "excluded_direct_amendment_votes": excluded_amendment_votes,
                "all_vote_counts": dict(all_overall),
                "decision_scopes": {
                    "all": headline_scope,
                    "divided": divided_scope,
                },
            }
        )

    featured_questions = featured_config.get("questions", [])
    featured_ids = [item.get("motion_id") for item in featured_questions]
    selection = featured_config.get("selection", {})
    pool_count = selection.get("pool_count", selection.get("count"))
    quiz_count = selection.get("quiz_count", pool_count)
    starter_ids = selection.get("starter_question_ids", [])
    if (
        len(featured_ids) != pool_count
        or len(featured_ids) != len(set(featured_ids))
        or not isinstance(quiz_count, int)
        or quiz_count < 1
        or quiz_count > pool_count
        or len(starter_ids) != quiz_count
        or len(starter_ids) != len(set(starter_ids))
        or not set(starter_ids).issubset(featured_ids)
    ):
        raise ValueError("featured or starter motion ids are invalid")
    minimum_confidence = selection.get("minimum_confidence", 0.9)
    minimum_each_side = selection.get("minimum_votes_each_side", 2)
    for question in featured_questions:
        motion_id = question.get("motion_id")
        prompt = question.get("prompt", "").strip()
        context = question.get("context", "").strip()
        tradeoff = question.get("tradeoff", "").strip()
        motion = enriched_motion_map.get(motion_id)
        if motion is None:
            raise ValueError(f"unknown featured motion id: {motion_id}")
        if not prompt or not context or not tradeoff:
            raise ValueError(f"featured motion {motion_id} lacks prompt or context")
        if (
            motion.get("public_impact") != selection.get("public_impact")
            or (motion.get("confidence") or 0) < minimum_confidence
            or (
                selection.get("requires_no_review_flag", False)
                and motion.get("needs_review") is not False
            )
            or (
                selection.get("requires_exact_minutes_match", False)
                and (
                    motion.get("enrichment_status") != "matched"
                    or motion.get("enrichment_match_quality") != "exact"
                )
            )
            or (
                selection.get("requires_not_direct_amendment", False)
                and motion.get("direct_amendment")
            )
            or motion["vote_counts"].get("In Favour", 0) < minimum_each_side
            or motion["vote_counts"].get("In Opposition", 0) < minimum_each_side
        ):
            raise ValueError(f"featured motion {motion_id} does not meet selection criteria")

    featured_group_by_id = {
        motion_id: enriched_motion_map[motion_id]["motion_group_id"]
        for motion_id in featured_ids
    }
    starter_groups = [featured_group_by_id[motion_id] for motion_id in starter_ids]
    if len(starter_groups) != len(set(starter_groups)):
        raise ValueError("starter set contains multiple questions from one motion group")

    published_questions = []
    for question in featured_questions:
        motion = enriched_motion_map[question["motion_id"]]
        published_questions.append(
            {
                **question,
                "primary_category": motion["primary_category"],
                "motion_group_id": motion["motion_group_id"],
                "minutes_url": motion.get("minutes_url"),
                "minutes_pdf_page": motion.get("minutes_pdf_page"),
                "meeting_agenda_url": meeting_agenda_url(motion.get("minutes_url")),
            }
        )

    featured_data = {
        "version": featured_config["version"],
        "curated_at": featured_config["curated_at"],
        "selection": selection,
        "questions": published_questions,
    }

    metadata["classification"] = {
        "status": "complete" if len(classification_map) == len(motions) else "in_progress" if classification_map else "pending",
        "taxonomy_version": taxonomy["version"],
        "classified_motions": len(classification_map),
        "total_motions": len(motions),
    }
    metadata["alignment_ballot"] = {
        "version": featured_config["version"],
        "curated_motions": len(featured_questions),
        "quiz_motions": quiz_count,
        "curated_at": featured_config["curated_at"],
    }
    metadata["motion_stages"] = {
        "method": "deterministic_high_precision_v1",
        "direct_amendments": sum(item["direct_amendment"] for item in stage_values),
        "headline_motions": sum(item["headline_eligible"] for item in stage_values),
        "total_motions": len(stage_values),
    }
    metadata["party_affiliations"] = {
        "version": party_config["version"],
        "as_of": party_config["as_of"],
        "members": len(party_members),
    }

    web_taxonomy = dict(taxonomy)
    web_taxonomy["categories"] = [
        {
            "id": "unclassified",
            "label": "Unclassified",
            "description": "Motions that have not yet been processed by the classification workflow.",
            "color": "#7a7f78",
        },
        *taxonomy["categories"],
    ]

    write_json(web_data / "motions.json", enriched_motions)
    write_json(web_data / "votes.json", votes)
    write_json(web_data / "report_cards.json", report_cards)
    write_json(web_data / "featured_motions.json", featured_data, pretty=True)
    write_json(web_data / "metadata.json", metadata, pretty=True)
    write_json(web_data / "taxonomy.json", web_taxonomy, pretty=True)
    return {
        "motions": len(motions),
        "votes": len(votes),
        "members": len(members),
        "featured": len(featured_questions),
        "classified": len(classification_map),
        "enriched": sum(
            1 for item in enrichment_map.values() if item.get("status") == "matched"
        ),
    }


def main() -> int:
    try:
        result = build_site_data(PROJECT_ROOT)
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Built web data for {result['members']} members and {result['motions']:,} motions "
        f"({result['classified']:,} classified)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
