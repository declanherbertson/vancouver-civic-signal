#!/usr/bin/env python3
"""Conservatively identify direct amendments in Vancouver vote records.

The rules intentionally optimize for precision. A record is excluded from headline
report-card totals only when the agenda label or exact minutes excerpt explicitly
identifies the vote as an amendment made during debate. Policy items that amend a
by-law (for example, a "CD-1 Text Amendment") remain eligible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MOTIONS = PROJECT_ROOT / "data" / "processed" / "motions.json"
DEFAULT_ENRICHMENT = PROJECT_ROOT / "data" / "enrichment" / "motions.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "derived" / "motion-stages.json"

STAGE_SUFFIX_PATTERN = re.compile(
    r"(?i)(?:\s[-\u2013\u2014]\s*)"
    r"(?:"
    r"amendment(?:\b.*)?|"
    r"amended\s+amendment(?:\b.*)?|"
    r"(?:first|second|third|fourth|\d+(?:st|nd|rd|th))\s+amendment(?:\b.*)?|"
    r"amended\s+language(?:\b.*)?"
    r")\s*$"
)
STAGE_PREFIX_PATTERN = re.compile(r"(?i)^\s*amendment\s*[-\u2013\u2014:]\s*")
MINUTES_AMENDMENT_PATTERN = re.compile(
    r"(?i)\b(AMENDMENT(?:\s+TO\s+(?:THE\s+)?(?:AMENDMENT|REFERRAL|MOTION))?"
    r"|AMENDMENT\s+TO\s+STRIKE\b.{0,80}?|AMENDED\s+AMENDMENT)"
    r".{0,120}?\bMOVED\s+by\b"
)
FINAL_PATTERN = re.compile(
    r"(?i)(?:FINAL\s+MOTION\s+AS\s+(?:APPROVED|AMENDED)|MOTION\s+AS\s+AMENDED)"
)


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def title_says_direct_amendment(title: str) -> bool:
    value = compact(title)
    return bool(STAGE_PREFIX_PATTERN.search(value) or STAGE_SUFFIX_PATTERN.search(value))


def minutes_say_direct_amendment(text: str | None) -> tuple[bool, str | None]:
    # Vote-specific extraction normally puts the operative block first. Restricting
    # the search window avoids treating later narrative about an earlier amendment
    # as the stage of the current vote.
    opening = compact(text)[:1000]
    match = MINUTES_AMENDMENT_PATTERN.search(opening)
    if not match:
        return False, None
    first_motion = re.search(r"(?i)\bMOVED\s+by\b", opening)
    if first_motion and match.start() > first_motion.start():
        return False, None
    label = compact(match.group(1)).lower()
    if "to the amendment" in label or "amended amendment" in label:
        return True, "amendment_to_amendment"
    if "to the referral" in label:
        return True, "amendment_to_referral"
    return True, "amendment"


def normalize_group_title(title: str) -> str:
    value = compact(title)
    value = STAGE_PREFIX_PATTERN.sub("", value)
    value = STAGE_SUFFIX_PATTERN.sub("", value)
    value = re.sub(
        r"(?i)\s[-\u2013\u2014]\s*(?:final\s+)?motion\s+as\s+(?:approved|amended)\s*$",
        "",
        value,
    )
    return value.strip(" -\u2013\u2014")


def derive_motion_stage(motion: dict, enrichment: dict | None) -> dict:
    title = motion.get("agenda_description", "")
    text = (enrichment or {}).get("motion_text")
    title_evidence = title_says_direct_amendment(title)
    exact_minutes = bool(
        enrichment
        and enrichment.get("status") == "matched"
        and enrichment.get("match_quality") == "exact"
    )
    minutes_evidence, subtype = minutes_say_direct_amendment(text) if exact_minutes else (False, None)
    evidence = []
    if title_evidence:
        evidence.append("agenda_stage_label")
    if minutes_evidence:
        evidence.append("minutes_amendment_moved")

    direct_amendment = bool(evidence)
    if direct_amendment:
        stage = "direct_amendment"
        confidence = 0.99 if len(evidence) == 2 else (0.97 if minutes_evidence else 0.94)
    elif FINAL_PATTERN.search(compact(title)) or FINAL_PATTERN.search(compact(text)[:1200]):
        stage = "main_or_final_motion"
        confidence = 0.95
        evidence.append("final_motion_label")
    else:
        # This is deliberately not a claim that the record is certainly a main
        # motion. It means only that no direct-amendment evidence was found.
        stage = "not_identified_as_direct_amendment"
        confidence = None

    base_title = normalize_group_title(title)
    digest = hashlib.sha1(base_title.casefold().encode("utf-8")).hexdigest()[:12]
    return {
        "motion_id": motion["motion_id"],
        "motion_stage": stage,
        "direct_amendment": direct_amendment,
        "amendment_type": subtype if direct_amendment else None,
        "headline_eligible": not direct_amendment,
        "stage_confidence": confidence,
        "stage_evidence": evidence,
        "motion_group_id": f"{motion['meeting_id']}-{digest}",
        "motion_group_title": base_title,
    }


def derive_motion_stages(motions: list[dict], enrichment_values: list[dict]) -> list[dict]:
    enrichment = {item["motion_id"]: item for item in enrichment_values}
    return [derive_motion_stage(motion, enrichment.get(motion["motion_id"])) for motion in motions]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--motions", type=Path, default=DEFAULT_MOTIONS)
    parser.add_argument("--enrichment", type=Path, default=DEFAULT_ENRICHMENT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stages = derive_motion_stages(read_json(args.motions), read_json(args.enrichment))
    write_json(args.output, stages)
    excluded = sum(item["direct_amendment"] for item in stages)
    print(f"Labelled {len(stages):,} motions; {excluded:,} direct amendments excluded from headline totals.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
