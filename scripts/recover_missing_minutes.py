#!/usr/bin/env python3
"""Recover, validate, and reprocess missing Vancouver council minutes.

The City document host rejects ordinary scripted HTTP clients. This command uses
a dedicated real Chrome profile, validates every response as the expected PDF,
and can run the existing enrichment job only for successfully validated meetings.
It is inert unless --execute is supplied.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MISSING_REPORT = PROJECT_ROOT / "data" / "enrichment" / "missing_meetings.csv"
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "data" / "enrichment" / "browser_minutes"
DEFAULT_ENRICHMENT = PROJECT_ROOT / "data" / "enrichment" / "motions.json"
DEFAULT_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
VOTE_PATTERN = re.compile(
    r"\bvote\s+(?:no\.?|number)\s*[:#]?\s*0*(\d+)\b", re.IGNORECASE
)
MEETING_TERMS = {
    "Council": ("council meeting", "city council"),
    "Special Council": ("special council", "special meeting"),
    "Public Hearing": ("public hearing",),
    "Policy & Strategic Priorities": ("policy and strategic priorities",),
    "City Finance & Services": ("city finance and services",),
    "Auditor General Committee": ("auditor general committee",),
}


@dataclass(frozen=True)
class MissingMeeting:
    meeting_id: str
    vote_date: str
    meeting_type: str
    motion_count: int
    unmatched_motion_count: int
    vote_numbers: tuple[str, ...]
    expected_minutes_url: str
    reason: str

    @property
    def filename(self) -> str:
        return self.expected_minutes_url.rsplit("/", 1)[-1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and validate the highest-impact missing minutes with Chrome."
    )
    parser.add_argument("--missing-report", type=Path, default=DEFAULT_MISSING_REPORT)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument(
        "--profile-dir",
        type=Path,
        help="Optional dedicated Chrome profile to retain Cloudflare session state.",
    )
    parser.add_argument("--enrichment", type=Path, default=DEFAULT_ENRICHMENT)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--meeting-id", action="append", dest="meeting_ids", help="Select a meeting ID; repeatable."
    )
    parser.add_argument(
        "--exclude-meeting-id",
        action="append",
        dest="excluded_meeting_ids",
        help="Skip a meeting ID; repeatable.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=31,
        help="Try earlier date-based minutes URLs after the expected URL returns no valid PDF.",
    )
    parser.add_argument("--chrome-executable", type=Path, default=DEFAULT_CHROME)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument(
        "--challenge-timeout",
        type=float,
        default=60.0,
        help="How long a visible browser may wait for Cloudflare to finish.",
    )
    parser.add_argument("--delay", type=float, default=0.75)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument(
        "--run-enrichment",
        action="store_true",
        help="Run enrich_motions.py offline for each validated meeting.",
    )
    parser.add_argument(
        "--execute", action="store_true", help="Download files. Without this flag, print a plan."
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.lookback_days < 0 or args.lookback_days > 90:
        parser.error("--lookback-days must be between 0 and 90")
    if args.timeout <= 0 or args.challenge_timeout < 0 or args.delay < 0:
        parser.error("timeouts must be positive and delay cannot be negative")
    if args.run_enrichment and not args.execute:
        parser.error("--run-enrichment requires --execute")
    return args


def normalize_vote_number(value: str) -> str:
    value = str(value).strip()
    return str(int(value)) if value.isdigit() else value.casefold()


def load_missing_meetings(path: Path) -> list[MissingMeeting]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {
        "meeting_id",
        "vote_date",
        "meeting_type",
        "motion_count",
        "unmatched_motion_count",
        "vote_numbers",
        "expected_minutes_url",
        "reason",
    }
    if not rows:
        return []
    missing_fields = required - set(rows[0])
    if missing_fields:
        raise ValueError(f"missing report lacks columns: {sorted(missing_fields)}")
    meetings = []
    for row in rows:
        meetings.append(
            MissingMeeting(
                meeting_id=row["meeting_id"].strip(),
                vote_date=row["vote_date"].strip(),
                meeting_type=row["meeting_type"].strip(),
                motion_count=int(row["motion_count"]),
                unmatched_motion_count=int(row["unmatched_motion_count"]),
                vote_numbers=tuple(
                    normalize_vote_number(item)
                    for item in row["vote_numbers"].split(",")
                    if item.strip()
                ),
                expected_minutes_url=row["expected_minutes_url"].strip(),
                reason=row["reason"].strip(),
            )
        )
    return meetings


def select_meetings(
    meetings: list[MissingMeeting],
    limit: int,
    meeting_ids: list[str] | None = None,
    excluded_meeting_ids: list[str] | None = None,
) -> list[MissingMeeting]:
    requested = set(meeting_ids or [])
    excluded = set(excluded_meeting_ids or [])
    known = {item.meeting_id for item in meetings}
    unknown = requested - known
    if unknown:
        raise ValueError(f"unknown --meeting-id values: {sorted(unknown)}")
    selected = [
        item
        for item in meetings
        if item.meeting_id not in excluded
        and (not requested or item.meeting_id in requested)
    ]
    selected.sort(
        key=lambda item: (-item.unmatched_motion_count, item.vote_date, item.meeting_id)
    )
    return selected[:limit]


def candidate_minutes(meeting: MissingMeeting, lookback_days: int) -> list[tuple[int, MissingMeeting]]:
    filename_match = re.fullmatch(r"([a-z]+)\d{8}min\.pdf", meeting.filename, re.IGNORECASE)
    if not filename_match:
        raise ValueError(f"cannot derive minutes filename pattern from {meeting.filename}")
    prefix = filename_match.group(1)
    parsed = date.fromisoformat(meeting.vote_date)
    values = []
    for offset in range(lookback_days + 1):
        candidate_date = parsed - timedelta(days=offset)
        stamp = candidate_date.strftime("%Y%m%d")
        url = f"https://council.vancouver.ca/{stamp}/documents/{prefix}{stamp}min.pdf"
        values.append((offset, replace(meeting, expected_minutes_url=url)))
    return values


def candidate_is_acceptable(validation: dict, lookback_offset: int) -> bool:
    if not validation.get("valid"):
        return False
    return lookback_offset == 0 or validation.get("matched_vote_count", 0) > 0


def expected_date_tokens(value: str) -> tuple[str, str, str]:
    parsed = date.fromisoformat(value)
    return parsed.strftime("%B").casefold(), str(parsed.day), str(parsed.year)


def validate_pdf_bytes(data: bytes, meeting: MissingMeeting) -> dict:
    result = {
        "valid": False,
        "bytes": len(data),
        "page_count": 0,
        "text_characters": 0,
        "date_verified": False,
        "meeting_type_verified": False,
        "expected_vote_count": len(meeting.vote_numbers),
        "matched_vote_count": 0,
        "matched_vote_numbers": [],
        "missing_vote_numbers": list(meeting.vote_numbers),
        "warnings": [],
        "error": None,
    }
    if not data.lstrip().startswith(b"%PDF"):
        result["error"] = "response_is_not_pdf"
        return result
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        result["error"] = f"pdf_parse_error:{type(exc).__name__}"
        return result
    text = "\n".join(pages)
    normalized_text = re.sub(r"\s+", " ", text).casefold()
    first_pages = re.sub(r"\s+", " ", "\n".join(pages[:3])).casefold()
    month, day, year = expected_date_tokens(meeting.vote_date)
    result["page_count"] = len(pages)
    result["text_characters"] = len(text.strip())
    result["date_verified"] = (
        year in first_pages and month in first_pages and re.search(rf"\b{re.escape(day)}\b", first_pages)
        is not None
    )
    terms = MEETING_TERMS.get(meeting.meeting_type, (meeting.meeting_type.casefold(),))
    result["meeting_type_verified"] = any(term in first_pages for term in terms)
    found_votes = {normalize_vote_number(match.group(1)) for match in VOTE_PATTERN.finditer(text)}
    expected = set(meeting.vote_numbers)
    matched = sorted(expected & found_votes, key=lambda value: int(value) if value.isdigit() else value)
    missing = sorted(expected - found_votes, key=lambda value: int(value) if value.isdigit() else value)
    result["matched_vote_count"] = len(matched)
    result["matched_vote_numbers"] = matched
    result["missing_vote_numbers"] = missing
    if not result["date_verified"]:
        result["warnings"].append("meeting_date_not_verified_in_first_three_pages")
    if not result["meeting_type_verified"]:
        result["warnings"].append("meeting_type_not_verified_in_first_three_pages")
    if not matched:
        result["warnings"].append("none_of_the_expected_vote_numbers_were_found")
    if result["text_characters"] < 100:
        result["error"] = "pdf_has_too_little_extractable_text"
        return result
    if not result["date_verified"] and not matched:
        result["error"] = "pdf_identity_not_verified"
        return result
    if not result["meeting_type_verified"] and not matched:
        result["error"] = "pdf_meeting_type_not_verified"
        return result
    result["valid"] = True
    return result


def validate_pdf_file(path: Path, meeting: MissingMeeting) -> dict:
    try:
        return validate_pdf_bytes(path.read_bytes(), meeting)
    except OSError as exc:
        return {
            "valid": False,
            "bytes": 0,
            "page_count": 0,
            "text_characters": 0,
            "date_verified": False,
            "meeting_type_verified": False,
            "expected_vote_count": len(meeting.vote_numbers),
            "matched_vote_count": 0,
            "matched_vote_numbers": [],
            "missing_vote_numbers": list(meeting.vote_numbers),
            "warnings": [],
            "error": f"file_read_error:{type(exc).__name__}",
        }


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def write_json_atomic(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def configure_browser_profile(profile_dir: Path, download_dir: Path) -> None:
    """Tell Chrome to download PDFs instead of opening its internal viewer."""
    preferences_path = profile_dir / "Default" / "Preferences"
    preferences_path.parent.mkdir(parents=True, exist_ok=True)
    preferences = {}
    if preferences_path.exists():
        try:
            preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            preferences = {}
    preferences.setdefault("download", {}).update(
        {
            "default_directory": str(download_dir),
            "prompt_for_download": False,
        }
    )
    preferences.setdefault("plugins", {})["always_open_pdf_externally"] = True
    write_json_atomic(preferences_path, preferences)


def load_statuses(path: Path, motion_ids_by_meeting: dict[str, set[str]]) -> dict[str, dict[str, int]]:
    if not path.exists():
        return {}
    values = json.loads(path.read_text(encoding="utf-8"))
    motion_to_meeting = {
        motion_id: meeting_id
        for meeting_id, motion_ids in motion_ids_by_meeting.items()
        for motion_id in motion_ids
    }
    statuses: dict[str, dict[str, int]] = {}
    for item in values:
        meeting_id = motion_to_meeting.get(item.get("motion_id"))
        if meeting_id:
            group = statuses.setdefault(meeting_id, {})
            status = item.get("status", "unknown")
            group[status] = group.get(status, 0) + 1
    return statuses


def motion_ids_for_meetings(meetings: list[MissingMeeting]) -> dict[str, set[str]]:
    motions_path = PROJECT_ROOT / "data" / "processed" / "motions.json"
    motions = json.loads(motions_path.read_text(encoding="utf-8"))
    selected = {item.meeting_id for item in meetings}
    result = {meeting_id: set() for meeting_id in selected}
    for motion in motions:
        meeting_id = str(motion["meeting_id"])
        if meeting_id in selected:
            result[meeting_id].add(motion["motion_id"])
    return result


def fetch_pdf(context, meeting: MissingMeeting, args: argparse.Namespace) -> tuple[bytes | None, str]:
    page = None
    downloads = []
    try:
        page = context.new_page()
        page.on("download", lambda download: downloads.append(download))
        try:
            response = page.goto(
                meeting.expected_minutes_url,
                wait_until="commit",
                timeout=int(args.timeout * 1000),
            )
        except Exception as exc:
            if "Download is starting" not in str(exc):
                return None, f"browser_navigation_error:{type(exc).__name__}"
            response = None

        if not downloads and response is not None:
            status = response.status
            content_type = response.headers.get("content-type", "").casefold()
            if status not in {403, 429, 503} and "application/pdf" not in content_type:
                return None, f"http_{status}_not_pdf"

        # The download event can arrive just after goto reports that navigation
        # was converted to a download. Known non-PDF responses return above and
        # avoid this wait during date lookback scans.
        event_deadline = time.monotonic() + 2
        while not downloads and time.monotonic() < event_deadline:
            try:
                page.wait_for_timeout(100)
            except Exception:
                pass

        challenge_deadline = time.monotonic() + args.challenge_timeout
        while not downloads and time.monotonic() < challenge_deadline:
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass
        if not downloads:
            return None, "browser_download_timeout"
        download = downloads[0]
        path = download.path()
        if path is None:
            return None, "browser_download_has_no_file"
        return Path(path).read_bytes(), "browser_download"
    except Exception as exc:
        return None, f"browser_download_error:{type(exc).__name__}"
    finally:
        if page is not None and not page.is_closed():
            page.close()


def run_enrichment(meetings: list[MissingMeeting], source_dir: Path) -> int:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "enrich_motions.py"),
        "--source-dir",
        str(source_dir),
        "--retry-unmatched",
        "--offline",
        "--execute",
    ]
    for meeting in meetings:
        command.extend(["--meeting-id", meeting.meeting_id])
    completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    return completed.returncode


def main() -> int:
    args = parse_args()
    try:
        meetings = load_missing_meetings(args.missing_report.expanduser().resolve())
        selected = select_meetings(
            meetings,
            args.limit,
            args.meeting_ids,
            args.excluded_meeting_ids,
        )
    except (FileNotFoundError, OSError, ValueError, csv.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not selected:
        print("No missing meetings remain.")
        return 0

    total_impact = sum(item.unmatched_motion_count for item in selected)
    print(
        f"Recovery plan: {len(selected)} meeting(s), representing "
        f"{total_impact:,} currently unmatched motion(s)."
    )
    for item in selected:
        print(
            f"  {item.unmatched_motion_count:>3}  {item.vote_date}  "
            f"{item.meeting_type}  meeting {item.meeting_id}"
        )
    if not args.execute:
        print("Dry run only. Re-run with --execute to open Chrome and retrieve minutes.")
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "error: Playwright is required; run `.venv/bin/python -m pip install -r requirements.txt`",
            file=sys.stderr,
        )
        return 1

    chrome = args.chrome_executable.expanduser().resolve()
    if not chrome.exists():
        print(f"error: Chrome executable not found: {chrome}", file=sys.stderr)
        return 1

    source_dir = args.source_dir.expanduser().resolve()
    source_dir.mkdir(parents=True, exist_ok=True)
    temporary_profile = None
    if args.profile_dir:
        profile_dir = args.profile_dir.expanduser().resolve()
    else:
        temporary_profile = tempfile.TemporaryDirectory(prefix="vancouver-minutes-chrome-")
        profile_dir = Path(temporary_profile.name)
    configure_browser_profile(profile_dir, source_dir)
    motion_ids = motion_ids_for_meetings(selected)
    before = load_statuses(args.enrichment.expanduser().resolve(), motion_ids)
    results = []
    validated = []
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=str(chrome),
            headless=args.headless,
            accept_downloads=True,
            args=["--disable-features=Translate"],
        )
        # Chrome closes a tab when a direct PDF navigation becomes a download.
        # Keep a blank tab alive so that closing a download tab does not also end
        # the persistent browser context between meetings.
        keeper_page = context.new_page()
        keeper_page.goto("about:blank")
        for index, meeting in enumerate(selected, start=1):
            print(
                f"[{index}/{len(selected)}] {meeting.vote_date} "
                f"{meeting.meeting_type} ({meeting.unmatched_motion_count} unmatched)...",
                flush=True,
            )
            validation = None
            retrieval = None
            destination = source_dir / meeting.filename
            selected_minutes_url = None
            candidate_attempts = []
            for lookback_offset, candidate in candidate_minutes(meeting, args.lookback_days):
                candidate_destination = source_dir / candidate.filename
                validation = None
                retrieval = None
                if candidate_destination.exists() and not args.force_download:
                    validation = validate_pdf_file(candidate_destination, meeting)
                    retrieval = "existing_file"
                if validation is None or not candidate_is_acceptable(validation, lookback_offset):
                    data, retrieval = fetch_pdf(context, candidate, args)
                    if data is None:
                        validation = {
                            "valid": False,
                            "bytes": 0,
                            "page_count": 0,
                            "text_characters": 0,
                            "date_verified": False,
                            "meeting_type_verified": False,
                            "expected_vote_count": len(meeting.vote_numbers),
                            "matched_vote_count": 0,
                            "matched_vote_numbers": [],
                            "missing_vote_numbers": list(meeting.vote_numbers),
                            "warnings": [],
                            "error": retrieval,
                        }
                    else:
                        validation = validate_pdf_bytes(data, meeting)
                        if lookback_offset and validation.get("valid") and not validation.get(
                            "matched_vote_count"
                        ):
                            validation["valid"] = False
                            validation["error"] = "lookback_pdf_has_no_expected_vote_numbers"
                        if candidate_is_acceptable(validation, lookback_offset):
                            write_bytes_atomic(candidate_destination, data)
                candidate_attempts.append(
                    {
                        "lookback_days": lookback_offset,
                        "url": candidate.expected_minutes_url,
                        "retrieval": retrieval,
                        "valid": candidate_is_acceptable(validation, lookback_offset),
                        "error": validation.get("error"),
                        "matched_vote_count": validation.get("matched_vote_count", 0),
                    }
                )
                if candidate_is_acceptable(validation, lookback_offset):
                    destination = candidate_destination
                    selected_minutes_url = candidate.expected_minutes_url
                    break
            record = {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                **asdict(meeting),
                "vote_numbers": list(meeting.vote_numbers),
                "destination": str(destination),
                "selected_minutes_url": selected_minutes_url,
                "retrieval": retrieval,
                "validation": validation,
                "candidate_attempts": candidate_attempts,
            }
            results.append(record)
            write_json_atomic(source_dir / "validation_results.json", results)
            if candidate_is_acceptable(validation, candidate_attempts[-1]["lookback_days"]):
                validated.append(meeting)
                print(
                    f"  valid PDF: {validation['page_count']} pages, "
                    f"{validation['matched_vote_count']}/{validation['expected_vote_count']} "
                    "expected vote markers found"
                    f" (source lookback {candidate_attempts[-1]['lookback_days']} days)",
                    flush=True,
                )
            else:
                print(f"  not recovered: {validation['error']}", flush=True)
            time.sleep(args.delay)
        context.close()
    if temporary_profile is not None:
        temporary_profile.cleanup()

    print(
        f"Validated {len(validated)}/{len(selected)} minutes PDF(s) in {source_dir}."
    )
    if args.run_enrichment and validated:
        print(f"Running targeted enrichment for {len(validated)} validated meeting(s)...")
        return_code = run_enrichment(validated, source_dir)
        if return_code:
            print(f"error: enrichment exited with status {return_code}", file=sys.stderr)
            return return_code
        after = load_statuses(args.enrichment.expanduser().resolve(), motion_ids)
        before_matched = sum(group.get("matched", 0) for group in before.values())
        after_matched = sum(group.get("matched", 0) for group in after.values())
        print(
            f"Targeted meetings now contain {after_matched:,} matched motion(s), "
            f"a gain of {after_matched - before_matched:,}."
        )
    elif args.run_enrichment:
        print("No validated PDFs were available, so enrichment was not run.")
    return 0 if validated else 2


if __name__ == "__main__":
    raise SystemExit(main())
