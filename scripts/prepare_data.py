#!/usr/bin/env python3
"""Normalize Vancouver council voting records into motion, vote, and member files."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "raw" / "council-voting-records.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "processed"

EXPECTED_HEADERS = {
    "Meeting ID",
    "Meeting Type",
    "Vote Date",
    "Vote Number",
    "Agenda Description",
    "Vote Start Date Time",
    "Council Member",
    "Vote",
    "Decision",
    "Vote Detail Id",
}

VOTE_ORDER = (
    "In Favour",
    "In Opposition",
    "Abstain",
    "Absent",
    "Declared Conflict",
    "No Vote",
    "Ineligible",
)


@dataclass(frozen=True)
class Window:
    start: date | None
    end: date | None

    def includes(self, value: date) -> bool:
        return (self.start is None or value >= self.start) and (
            self.end is None or value <= self.end
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare Vancouver voting records for classification and the report-card UI."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--as-of",
        type=parse_iso_date,
        default=date.today(),
        help="End of the trailing window (default: today).",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=5,
        help="Number of trailing calendar years to include (default: 5).",
    )
    parser.add_argument("--start-date", type=parse_iso_date)
    parser.add_argument("--end-date", type=parse_iso_date)
    parser.add_argument(
        "--all-dates",
        action="store_true",
        help="Keep every date in the supplied file instead of applying a trailing window.",
    )
    parser.add_argument(
        "--meeting-type",
        action="append",
        dest="meeting_types",
        help="Keep one meeting type. Repeat to keep several; omit to keep all types.",
    )
    parser.add_argument(
        "--no-build-web",
        action="store_true",
        help="Skip refreshing web/data after processing.",
    )
    args = parser.parse_args()
    if args.years < 1:
        parser.error("--years must be at least 1")
    if args.all_dates and (args.start_date or args.end_date):
        parser.error("--all-dates cannot be combined with --start-date or --end-date")
    return args


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value}") from exc


def subtract_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        raise ValueError(f"cannot make an id from {value!r}")
    return slug


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_source(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        headers = reader.fieldnames or []
        missing = EXPECTED_HEADERS.difference(headers)
        if missing:
            raise ValueError(f"source CSV is missing headers: {', '.join(sorted(missing))}")
        rows = list(reader)
    if not rows:
        raise ValueError("source CSV has no data rows")
    return rows, headers


def normalize_rows(
    source_rows: Iterable[dict[str, str]],
    window: Window,
    meeting_types: set[str] | None,
) -> tuple[list[dict], list[dict], list[dict]]:
    selected: list[dict[str, str]] = []
    detail_ids: set[str] = set()
    for row_number, row in enumerate(source_rows, start=2):
        try:
            vote_date = date.fromisoformat(row["Vote Date"])
        except ValueError as exc:
            raise ValueError(f"row {row_number}: invalid Vote Date") from exc
        if not window.includes(vote_date):
            continue
        if meeting_types and row["Meeting Type"] not in meeting_types:
            continue
        detail_id = row["Vote Detail Id"].strip()
        if not detail_id:
            raise ValueError(f"row {row_number}: missing Vote Detail Id")
        if detail_id in detail_ids:
            raise ValueError(f"row {row_number}: duplicate Vote Detail Id {detail_id}")
        detail_ids.add(detail_id)
        selected.append(row)

    if not selected:
        raise ValueError("no rows matched the requested date and meeting filters")

    member_names = sorted({row["Council Member"].strip() for row in selected})
    member_ids = {name: slugify(name) for name in member_names}
    if len(set(member_ids.values())) != len(member_ids):
        raise ValueError("council member names produced colliding normalized ids")

    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    votes: list[dict] = []
    member_dates: dict[str, list[str]] = defaultdict(list)
    member_counts: dict[str, Counter] = defaultdict(Counter)

    for row in selected:
        meeting_id = row["Meeting ID"].strip()
        vote_number = row["Vote Number"].strip()
        motion_id = f"{meeting_id}-{vote_number}"
        member_name = row["Council Member"].strip()
        member_id = member_ids[member_name]
        vote_value = row["Vote"].strip()
        vote = {
            "vote_detail_id": row["Vote Detail Id"].strip(),
            "motion_id": motion_id,
            "member_id": member_id,
            "vote": vote_value,
        }
        votes.append(vote)
        grouped[(meeting_id, vote_number)].append(row)
        member_dates[member_id].append(row["Vote Date"])
        member_counts[member_id][vote_value] += 1

    motions: list[dict] = []
    for (meeting_id, vote_number), rows in grouped.items():
        canonical = rows[0]
        fields_to_match = (
            "Meeting Type",
            "Vote Date",
            "Agenda Description",
            "Vote Start Date Time",
            "Decision",
        )
        for field in fields_to_match:
            values = {row[field].strip() for row in rows}
            if len(values) != 1:
                raise ValueError(
                    f"motion {meeting_id}-{vote_number} has inconsistent {field}: {values}"
                )
        counts = Counter(row["Vote"].strip() for row in rows)
        ordered_counts = {value: counts.get(value, 0) for value in VOTE_ORDER}
        ordered_counts.update(
            {key: value for key, value in sorted(counts.items()) if key not in ordered_counts}
        )
        motions.append(
            {
                "motion_id": f"{meeting_id}-{vote_number}",
                "meeting_id": int(meeting_id) if meeting_id.isdigit() else meeting_id,
                "meeting_type": canonical["Meeting Type"].strip(),
                "vote_date": canonical["Vote Date"].strip(),
                "vote_number": vote_number,
                "agenda_description": canonical["Agenda Description"].strip(),
                "vote_start_date_time": canonical["Vote Start Date Time"].strip(),
                "decision": canonical["Decision"].strip(),
                "vote_counts": ordered_counts,
                "member_vote_count": len(rows),
            }
        )

    members: list[dict] = []
    for name in member_names:
        member_id = member_ids[name]
        counts = member_counts[member_id]
        positions = counts.get("In Favour", 0) + counts.get("In Opposition", 0)
        participated = positions + counts.get("Abstain", 0)
        total = sum(counts.values())
        members.append(
            {
                "member_id": member_id,
                "name": name,
                "role": "Mayor" if name.startswith("Mayor ") else "Councillor",
                "first_vote_date": min(member_dates[member_id]),
                "last_vote_date": max(member_dates[member_id]),
                "recorded_votes": total,
                "positions": positions,
                "participated": participated,
                "participation_rate": round(participated / total, 4) if total else 0,
                "vote_counts": {
                    value: counts.get(value, 0) for value in VOTE_ORDER
                },
            }
        )

    motions.sort(key=lambda item: (item["vote_date"], item["vote_start_date_time"], item["motion_id"]), reverse=True)
    votes.sort(key=lambda item: (item["motion_id"], item["member_id"]))
    members.sort(key=lambda item: (-date.fromisoformat(item["last_vote_date"]).toordinal(), item["name"]))
    return motions, votes, members


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


def write_jsonl(path: Path, values: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def write_csv(path: Path, values: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for value in values:
            row = dict(value)
            if "vote_counts" in row:
                row["vote_counts"] = json.dumps(row["vote_counts"], ensure_ascii=False)
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def prepare(args: argparse.Namespace) -> dict:
    input_path = args.input.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    if args.all_dates:
        window = Window(None, None)
    else:
        start = args.start_date or subtract_years(args.as_of, args.years)
        end = args.end_date or args.as_of
        if start > end:
            raise ValueError("start date must not be after end date")
        window = Window(start, end)

    source_rows, source_headers = read_source(input_path)
    meeting_types = set(args.meeting_types) if args.meeting_types else None
    motions, votes, members = normalize_rows(source_rows, window, meeting_types)

    motion_queue = [
        {
            "motion_id": motion["motion_id"],
            "vote_date": motion["vote_date"],
            "meeting_type": motion["meeting_type"],
            "agenda_description": motion["agenda_description"],
            "decision": motion["decision"],
        }
        for motion in sorted(motions, key=lambda item: item["motion_id"])
    ]

    write_json(output_dir / "motions.json", motions)
    write_jsonl(output_dir / "motions.jsonl", motions)
    write_jsonl(output_dir / "motions_to_classify.jsonl", motion_queue)
    write_json(output_dir / "votes.json", votes)
    write_jsonl(output_dir / "votes.jsonl", votes)
    write_json(output_dir / "members.json", members, pretty=True)
    write_csv(
        output_dir / "motions.csv",
        motions,
        [
            "motion_id",
            "meeting_id",
            "meeting_type",
            "vote_date",
            "vote_number",
            "agenda_description",
            "vote_start_date_time",
            "decision",
            "member_vote_count",
            "vote_counts",
        ],
    )
    write_csv(
        output_dir / "votes.csv",
        votes,
        ["vote_detail_id", "motion_id", "member_id", "vote"],
    )
    write_csv(
        output_dir / "members.csv",
        members,
        [
            "member_id",
            "name",
            "role",
            "first_vote_date",
            "last_vote_date",
            "recorded_votes",
            "positions",
            "participated",
            "participation_rate",
            "vote_counts",
        ],
    )

    source_dates = [row["Vote Date"] for row in source_rows]
    metadata = {
        "title": "Vancouver council voting records",
        "source": {
            "publisher": "City of Vancouver",
            "dataset_id": "council-voting-records",
            "dataset_url": "https://opendata.vancouver.ca/explore/dataset/council-voting-records/",
            "local_file": str(input_path.relative_to(PROJECT_ROOT)) if input_path.is_relative_to(PROJECT_ROOT) else str(input_path),
            "sha256": sha256_file(input_path),
            "headers": source_headers,
            "row_count": len(source_rows),
            "date_min": min(source_dates),
            "date_max": max(source_dates),
        },
        "selection": {
            "window_start": window.start.isoformat() if window.start else None,
            "window_end": window.end.isoformat() if window.end else None,
            "actual_date_min": min(motion["vote_date"] for motion in motions),
            "actual_date_max": max(motion["vote_date"] for motion in motions),
            "meeting_types": sorted({motion["meeting_type"] for motion in motions}),
            "vote_rows": len(votes),
            "motions": len(motions),
            "members": len(members),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "classification": {
            "status": "pending",
            "taxonomy_version": "1.0.0",
            "classified_motions": 0,
        },
        "enrichment": {
            "status": "pending",
            "matched_motions": 0,
            "unmatched_motions": 0,
            "pending_motions": len(motions),
            "total_motions": len(motions),
        },
        "notes": [
            "The City states that corresponding meeting minutes are the official voting record.",
            "Abstain is retained as published and is not merged into In Favour in this project.",
            "The source may lag meetings because records are published after meeting minutes.",
        ],
    }
    write_json(output_dir / "metadata.json", metadata, pretty=True)

    classification_path = PROJECT_ROOT / "data" / "classifications" / "motions.json"
    if not classification_path.exists():
        write_json(classification_path, [], pretty=True)

    if not args.no_build_web:
        from build_site_data import build_site_data

        build_site_data(PROJECT_ROOT)
    return metadata


def main() -> int:
    try:
        args = parse_args()
        metadata = prepare(args)
    except (FileNotFoundError, ValueError, csv.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    selection = metadata["selection"]
    print(
        "Prepared "
        f"{selection['vote_rows']:,} vote rows, {selection['motions']:,} motions, "
        f"and {selection['members']} members "
        f"({selection['actual_date_min']} to {selection['actual_date_max']})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
