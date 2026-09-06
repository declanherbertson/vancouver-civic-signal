#!/usr/bin/env python3
"""Enrich normalized Vancouver motions with exact text from meeting minutes.

The job is inert unless --execute is supplied. It caches minutes PDFs and extracted
text, writes one enrichment record per attempted motion after every meeting, and can
resume safely after interruption.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "processed" / "motions.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "enrichment" / "motions.json"
DEFAULT_METADATA = PROJECT_ROOT / "data" / "enrichment" / "metadata.json"
DEFAULT_QUEUE = PROJECT_ROOT / "data" / "processed" / "motions_to_classify.jsonl"
DEFAULT_CACHE = PROJECT_ROOT / "data" / "enrichment" / "minutes"
DEFAULT_WAYBACK_INDEX = PROJECT_ROOT / "data" / "enrichment" / "wayback_index.json"
DEFAULT_MISSING = PROJECT_ROOT / "data" / "enrichment" / "missing_meetings.csv"

MEETING_PREFIXES = {
    "Council": "regu",
    "Special Council": "spec",
    "Public Hearing": "phea",
    "Policy & Strategic Priorities": "pspc",
    "City Finance & Services": "cfsc",
    "Auditor General Committee": "agc",
}
VOTE_PATTERN = re.compile(
    r"\bvote\s+(?:no\.?|number)\s*[:#]?\s*0*(\d+)\b", re.IGNORECASE
)
MOVED_PATTERN = re.compile(r"(?im)^.*?MOVED\s+by\s+([^\n]+)")
SECONDED_PATTERN = re.compile(r"(?im)^\s*SECONDED\s+by\s+([^\n]+)")
MOTION_START_PATTERN = re.compile(
    r"(?im)^(?:[A-Z][A-Z -]{0,60}\s+)?MOVED\s+by\b"
)
FINAL_MOTION_PATTERN = re.compile(r"(?im)^\s*FINAL\s+MOTION\s+AS\s+APPROVED\s*$")
AGENDA_PREFIX_PATTERN = re.compile(
    r"^\s*(?:(?:[A-Z]{1,12}|REPORT)\s*\d+[A-Z]?|\d+[A-Z]?)\s*[.:]\s*",
    re.IGNORECASE,
)
OUTCOME_PATTERNS = (
    "CARRIED UNANIMOUSLY",
    "ADOPTED ON CONSENT",
    "CARRIED",
    "LOST",
    "APPROVED",
    "REFERRED",
    "WITHDRAWN",
)
USER_AGENT = "VancouverVoteTracker/1.0 (public civic-data research)"
MAX_DOWNLOAD_BYTES = 75 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich prepared motions with vote-specific text from official minutes."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--metadata", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--queue-output", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--wayback-index", type=Path, default=DEFAULT_WAYBACK_INDEX)
    parser.add_argument("--missing-output", type=Path, default=DEFAULT_MISSING)
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="Optional folder of manually downloaded minutes PDFs to check before the network.",
    )
    parser.add_argument(
        "--meeting-id", action="append", dest="meeting_ids", help="Only enrich this meeting id; repeatable."
    )
    parser.add_argument(
        "--limit-meetings", type=int, help="Process at most this many still-pending meetings."
    )
    parser.add_argument("--lookback-days", type=int, default=31)
    parser.add_argument("--max-motion-chars", type=int, default=6000)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--force", action="store_true", help="Replace selected enrichment records.")
    parser.add_argument(
        "--retry-unmatched", action="store_true", help="Retry records previously saved as unmatched or error."
    )
    parser.add_argument("--offline", action="store_true", help="Use cached or local PDFs only.")
    parser.add_argument("--no-direct", action="store_true", help="Do not request the official document host.")
    parser.add_argument("--no-wayback", action="store_true", help="Do not use Internet Archive fallback copies.")
    parser.add_argument(
        "--refresh-wayback-index", action="store_true", help="Refresh the cached archive index before running."
    )
    parser.add_argument(
        "--execute", action="store_true", help="Actually download and parse minutes. Without this flag, print a plan."
    )
    args = parser.parse_args()
    if args.limit_meetings is not None and args.limit_meetings < 1:
        parser.error("--limit-meetings must be at least 1")
    if args.lookback_days < 0 or args.lookback_days > 90:
        parser.error("--lookback-days must be between 0 and 90")
    if args.max_motion_chars < 500:
        parser.error("--max-motion-chars must be at least 500")
    if args.timeout <= 0 or args.delay < 0:
        parser.error("--timeout must be positive and --delay cannot be negative")
    return args


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_atomic(path: Path, value, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            value,
            handle,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
        handle.write("\n")
    temporary.replace(path)


def write_jsonl_atomic(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
    temporary.replace(path)


def normalize_vote_number(value) -> str:
    text = str(value).strip()
    return str(int(text)) if text.isdigit() else text.casefold()


def official_minutes_url(meeting_type: str, anchor_date: date) -> str:
    prefix = MEETING_PREFIXES[meeting_type]
    stamp = anchor_date.strftime("%Y%m%d")
    return f"https://council.vancouver.ca/{stamp}/documents/{prefix}{stamp}min.pdf"


def canonical_url(value: str) -> str:
    parsed = urlparse(value)
    return f"{parsed.netloc}{parsed.path}".casefold().rstrip("/")


def minutes_url_date(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"/(\d{8})/documents/", urlparse(value).path)
    if not match:
        return None
    stamp = match.group(1)
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"


def title_fallback_allowed(vote_date: str, minutes_url: str) -> bool:
    """Avoid attaching a similarly titled item from an earlier meeting.

    Lookback candidates are required for meetings that reconvene on later dates,
    but only an exact vote-number match is strong enough evidence across dates.
    """
    return minutes_url_date(minutes_url) == vote_date


def candidate_minutes_urls(meeting_type: str, vote_date: str, lookback_days: int) -> list[str]:
    parsed = date.fromisoformat(vote_date)
    return [
        official_minutes_url(meeting_type, parsed - timedelta(days=offset))
        for offset in range(lookback_days + 1)
    ]


def fetch_bytes(url: str, timeout: float, retries: int = 2) -> tuple[bytes | None, str | None, int | None]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/pdf,application/json;q=0.9,*/*;q=0.5",
    }
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=timeout) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                    return None, "document_too_large", response.status
                data = response.read(MAX_DOWNLOAD_BYTES + 1)
                if len(data) > MAX_DOWNLOAD_BYTES:
                    return None, "document_too_large", response.status
                return data, None, response.status
        except HTTPError as exc:
            if exc.code in {403, 404}:
                return None, f"http_{exc.code}", exc.code
            if exc.code < 500 or attempt == retries:
                return None, f"http_{exc.code}", exc.code
        except (URLError, TimeoutError, OSError) as exc:
            if attempt == retries:
                return None, f"network_error:{type(exc).__name__}", None
        time.sleep(min(2 ** attempt, 4))
    return None, "network_error", None


def parse_wayback_index(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        return []
    if isinstance(raw[0], list):
        headers = raw[0]
        return [dict(zip(headers, row)) for row in raw[1:]]
    return [item for item in raw if isinstance(item, dict)]


def load_wayback_index(
    path: Path, offline: bool, refresh: bool, timeout: float
) -> tuple[dict[str, dict], str | None]:
    if path.exists() and not refresh:
        payload = read_json(path)
        entries = payload.get("entries", payload) if isinstance(payload, dict) else payload
        return {canonical_url(item["original"]): item for item in entries}, None
    if offline:
        return {}, "archive_index_not_cached"
    pairs = [
        ("url", "council.vancouver.ca/*"),
        ("output", "json"),
        ("filter", "statuscode:200"),
        ("filter", "mimetype:application/pdf"),
        ("filter", r"urlkey:.*min\.pdf"),
        ("collapse", "urlkey"),
        ("from", "2021"),
        ("to", str(datetime.now(timezone.utc).year)),
        ("fl", "timestamp,original,digest,length"),
    ]
    url = "https://web.archive.org/cdx/search/cdx?" + urlencode(pairs)
    data, error, _ = fetch_bytes(url, max(timeout, 120), retries=2)
    if error or data is None:
        return {}, error or "archive_index_download_failed"
    try:
        entries = parse_wayback_index(json.loads(data))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, f"archive_index_invalid:{type(exc).__name__}"
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source_url": url,
        "entries": entries,
    }
    write_json_atomic(path, payload)
    return {canonical_url(item["original"]): item for item in entries}, None


def archive_url(capture: dict) -> str:
    return f"https://web.archive.org/web/{capture['timestamp']}id_/{capture['original']}"


def clean_page_text(value: str) -> str:
    value = value.replace("\u00ad", "").replace("\x00", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\t ]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def extract_pdf_pages(path: Path) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "pypdf is required; create a virtual environment and run "
            "`python -m pip install -r requirements.txt`"
        ) from exc
    try:
        reader = PdfReader(path)
        pages = [clean_page_text(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        raise RuntimeError(f"could not extract {path.name}: {exc}") from exc
    if sum(len(page) for page in pages) < 100:
        raise RuntimeError(f"{path.name} contains too little extractable text")
    return pages


def require_pdf_dependency() -> None:
    try:
        import pypdf  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "pypdf is required; create a virtual environment and run "
            "`python -m pip install -r requirements.txt`"
        ) from exc


def pages_to_text(pages: list[str]) -> tuple[str, list[int]]:
    parts = []
    starts = []
    length = 0
    for page in pages:
        starts.append(length)
        parts.append(page)
        length += len(page) + 3
    return "\n\f\n".join(parts), starts


def trim_motion_segment(value: str, max_chars: int, from_end: bool = False) -> tuple[str, bool]:
    value = value.replace("\f", "\n")
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if len(value) <= max_chars:
        return value, False
    clipped = value[-max_chars:] if from_end else value[:max_chars]
    newline = clipped.find("\n") if from_end else clipped.rfind("\n")
    if newline >= 0:
        clipped = clipped[newline + 1 :] if from_end else clipped[:newline]
    return clipped.strip(), True


def clean_between_votes(value: str) -> str:
    value = re.sub(r"^\s*\)\.?\s*", "", value)
    while value.lstrip().startswith("("):
        stripped = value.lstrip()
        closing = stripped.find(")")
        if closing < 0 or closing > 500:
            break
        value = stripped[closing + 1 :]
    value = re.sub(
        r"(?s)\n(?:CARRIED|LOST|ADOPTED|APPROVED|REFERRED|WITHDRAWN)[A-Z0-9\s,&/.'-]{0,240}\(\s*$",
        "",
        value,
    )
    return value.strip()


def find_vote_markers(text: str) -> list[tuple[str, re.Match]]:
    return [(normalize_vote_number(match.group(1)), match) for match in VOTE_PATTERN.finditer(text)]


def outcome_before_marker(text: str, marker_start: int) -> str | None:
    context = text[max(0, marker_start - 300) : marker_start].upper()
    matches = [(context.rfind(value), value.title()) for value in OUTCOME_PATTERNS]
    matches = [item for item in matches if item[0] >= 0]
    return max(matches)[1] if matches else None


def last_named_match(pattern: re.Pattern, value: str) -> str | None:
    matches = [match.group(1).strip(" .") for match in pattern.finditer(value)]
    return matches[-1] if matches else None


def extract_motion_from_pages(
    pages: list[str], vote_number, max_chars: int = 6000
) -> dict | None:
    text, page_starts = pages_to_text(pages)
    markers = find_vote_markers(text)
    normalized = normalize_vote_number(vote_number)
    positions = [index for index, (value, _) in enumerate(markers) if value == normalized]
    if not positions:
        return None
    marker_index = positions[0]
    marker = markers[marker_index][1]
    previous_end = markers[marker_index - 1][1].end() if marker_index else 0
    next_start = markers[marker_index + 1][1].start() if marker_index + 1 < len(markers) else len(text)

    before = clean_between_votes(text[previous_end : marker.start()])
    motion_starts = list(MOTION_START_PATTERN.finditer(before))
    if motion_starts:
        before = before[motion_starts[-1].start() :]
    before, before_truncated = trim_motion_segment(before, max_chars, from_end=True)

    after = text[marker.end() : next_start]
    final_match = FINAL_MOTION_PATTERN.search(after)
    final_text = ""
    final_truncated = False
    if final_match and final_match.start() <= 1200:
        final_text, final_truncated = trim_motion_segment(
            after[final_match.start() :], max_chars, from_end=False
        )

    contains_motion_language = bool(
        re.search(r"\b(THAT|WHEREAS|BE IT RESOLVED)\b", before, re.IGNORECASE)
    )
    if final_text and (not contains_motion_language or len(before) < 500):
        motion_text = final_text
        text_truncated = final_truncated
    elif final_text:
        remaining = max(500, max_chars - len(before) - 2)
        final_text, extra_truncated = trim_motion_segment(final_text, remaining)
        motion_text = f"{before}\n\n{final_text}".strip()
        text_truncated = before_truncated or final_truncated or extra_truncated
    else:
        motion_text = before
        text_truncated = before_truncated

    page_number = bisect.bisect_right(page_starts, marker.start())
    name_context = f"{before}\n{final_text}"
    return {
        "motion_text": motion_text,
        "moved_by": last_named_match(MOVED_PATTERN, name_context),
        "seconded_by": last_named_match(SECONDED_PATTERN, name_context),
        "minutes_outcome": outcome_before_marker(text, marker.start()),
        "minutes_pdf_page": page_number,
        "text_truncated": text_truncated,
        "vote_marker": marker.group(0).replace("\n", " "),
        "duplicate_vote_markers": len(positions) > 1,
    }


def extract_motion_by_title(
    pages: list[str], agenda_description: str, max_chars: int = 6000
) -> dict | None:
    """Conservatively match an exact agenda title to the nearest following vote.

    This fallback exists for documented source discrepancies where the voting export
    and official minutes use different vote numbers. It intentionally does not use
    fuzzy title similarity.
    """
    text, _ = pages_to_text(pages)
    title = AGENDA_PREFIX_PATTERN.sub("", agenda_description).strip()
    if len(title) < 8:
        return None
    words = title.split()
    title_pattern = re.compile(
        r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)",
        re.IGNORECASE,
    )
    markers = find_vote_markers(text)
    candidates = []
    for title_match in title_pattern.finditer(text):
        for marker_value, marker in markers:
            distance = marker.start() - title_match.end()
            if 0 <= distance <= 15000:
                candidates.append((distance, marker_value))
                break
    if not candidates:
        return None
    _, marker_value = min(candidates)
    extracted = extract_motion_from_pages(pages, marker_value, max_chars)
    if extracted:
        extracted["source_vote_number"] = marker_value
    return extracted


def load_or_extract_pages(pdf_path: Path) -> list[str]:
    text_path = pdf_path.with_suffix(".pages.json")
    if text_path.exists():
        value = read_json(text_path)
        if isinstance(value, list) and value:
            return value
    pages = extract_pdf_pages(pdf_path)
    write_json_atomic(text_path, pages, pretty=False)
    return pages


def cache_path_for_url(cache_dir: Path, url: str) -> Path:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    stamp = parts[-3] if len(parts) >= 3 and parts[-3].isdigit() else "unknown-date"
    filename = parts[-1] if parts else hashlib.sha256(url.encode()).hexdigest() + ".pdf"
    return cache_dir / stamp / filename


def local_pdf_candidates(source_dir: Path | None, cache_path: Path, meeting_id: str) -> list[Path]:
    candidates = []
    if source_dir:
        candidates.extend([source_dir / cache_path.name, source_dir / f"{meeting_id}.pdf"])
    candidates.append(cache_path)
    return [path for index, path in enumerate(candidates) if path not in candidates[:index]]


def portable_local_path(path: Path) -> str:
    """Keep local provenance useful without embedding a contributor's home path."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return resolved.name


def save_pdf(path: Path, data: bytes) -> None:
    if not data.lstrip().startswith(b"%PDF"):
        raise ValueError("download did not contain a PDF")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def group_motions(motions: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for motion in motions:
        meeting_id = str(motion["meeting_id"])
        grouped[meeting_id].append(motion)
    meetings = []
    for meeting_id, values in grouped.items():
        dates = {item["vote_date"] for item in values}
        types = {item["meeting_type"] for item in values}
        if len(dates) != 1 or len(types) != 1:
            raise ValueError(f"meeting {meeting_id} has inconsistent dates or types")
        meeting_type = next(iter(types))
        if meeting_type not in MEETING_PREFIXES:
            raise ValueError(f"unsupported meeting type: {meeting_type}")
        meetings.append(
            {
                "meeting_id": meeting_id,
                "vote_date": next(iter(dates)),
                "meeting_type": meeting_type,
                "motions": sorted(values, key=lambda item: int(item["vote_number"])),
            }
        )
    meetings.sort(key=lambda item: (item["vote_date"], item["meeting_type"], item["meeting_id"]))
    return meetings


def should_process_motion(
    motion: dict, current: dict | None, args: argparse.Namespace
) -> bool:
    if args.force or current is None:
        return True
    if args.retry_unmatched and current.get("status") != "matched":
        return True
    return (
        current.get("status") == "matched"
        and current.get("match_quality") == "inferred"
        and not title_fallback_allowed(motion["vote_date"], current.get("minutes_url", ""))
    )


def pending_meetings(meetings: list[dict], existing: dict[str, dict], args: argparse.Namespace) -> list[dict]:
    selected = set(args.meeting_ids or [])
    known = {item["meeting_id"] for item in meetings}
    missing = selected - known
    if missing:
        raise ValueError(f"unknown --meeting-id values: {sorted(missing)}")
    result = []
    for meeting in meetings:
        if selected and meeting["meeting_id"] not in selected:
            continue
        should_process = False
        for motion in meeting["motions"]:
            current = existing.get(motion["motion_id"])
            if should_process_motion(motion, current, args):
                should_process = True
        if should_process:
            result.append(meeting)
    if args.limit_meetings is not None:
        result = result[: args.limit_meetings]
    return result


def queue_record(motion: dict, enrichment: dict | None) -> dict:
    enrichment = enrichment or {}
    return {
        "motion_id": motion["motion_id"],
        "vote_date": motion["vote_date"],
        "meeting_type": motion["meeting_type"],
        "agenda_description": motion["agenda_description"],
        "decision": motion["decision"],
        "enrichment_status": enrichment.get("status", "pending"),
        "minutes_excerpt": enrichment.get("motion_text"),
        "minutes_outcome": enrichment.get("minutes_outcome"),
        "moved_by": enrichment.get("moved_by"),
        "seconded_by": enrichment.get("seconded_by"),
        "text_truncated": enrichment.get("text_truncated", False),
        "enrichment_match_quality": enrichment.get("match_quality"),
        "minutes_vote_marker": enrichment.get("source_vote_number"),
        "source": {
            "minutes_url": enrichment.get("minutes_url"),
            "minutes_pdf_page": enrichment.get("minutes_pdf_page"),
        },
    }


def build_metadata(motions: list[dict], enrichments: dict[str, dict], meetings: list[dict]) -> dict:
    counts = Counter(item.get("status", "unknown") for item in enrichments.values())
    processed_meetings = 0
    matched_meetings = 0
    for meeting in meetings:
        values = [enrichments.get(item["motion_id"]) for item in meeting["motions"]]
        if all(values):
            processed_meetings += 1
        if values and all(item and item.get("status") == "matched" for item in values):
            matched_meetings += 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "publisher": "City of Vancouver",
            "minutes_url_pattern": "https://council.vancouver.ca/YYYYMMDD/documents/PREFIXYYYYMMDDmin.pdf",
            "match_key": "Vote No. in official meeting minutes to vote_number in the voting dataset",
            "archive_fallback": "Internet Archive Wayback Machine",
        },
        "total_motions": len(motions),
        "recorded_motions": len(enrichments),
        "pending_motions": len(motions) - len(enrichments),
        "status_counts": dict(sorted(counts.items())),
        "total_meetings": len(meetings),
        "processed_meetings": processed_meetings,
        "fully_matched_meetings": matched_meetings,
    }


def write_missing_meetings(path: Path, meetings: list[dict], enrichments: dict[str, dict], lookback_days: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    fields = [
        "meeting_id",
        "vote_date",
        "meeting_type",
        "motion_count",
        "unmatched_motion_count",
        "vote_numbers",
        "expected_minutes_url",
        "reason",
    ]
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for meeting in meetings:
            unmatched = [
                motion for motion in meeting["motions"]
                if enrichments.get(motion["motion_id"], {}).get("status")
                in {"unmatched", "error"}
            ]
            if not unmatched:
                continue
            reasons = sorted(
                {
                    enrichments.get(item["motion_id"], {}).get("reason", "not_processed")
                    for item in unmatched
                }
            )
            writer.writerow(
                {
                    "meeting_id": meeting["meeting_id"],
                    "vote_date": meeting["vote_date"],
                    "meeting_type": meeting["meeting_type"],
                    "motion_count": len(meeting["motions"]),
                    "unmatched_motion_count": len(unmatched),
                    "vote_numbers": ",".join(str(item["vote_number"]) for item in unmatched),
                    "expected_minutes_url": candidate_minutes_urls(
                        meeting["meeting_type"], meeting["vote_date"], lookback_days
                    )[0],
                    "reason": ",".join(reasons),
                }
            )
    temporary.replace(path)


def persist_outputs(
    motions: list[dict],
    meetings: list[dict],
    enrichments: dict[str, dict],
    args: argparse.Namespace,
) -> dict:
    ordered = [enrichments[item["motion_id"]] for item in motions if item["motion_id"] in enrichments]
    metadata = build_metadata(motions, enrichments, meetings)
    queue = [queue_record(item, enrichments.get(item["motion_id"])) for item in sorted(motions, key=lambda x: x["motion_id"])]
    write_json_atomic(args.output.resolve(), ordered)
    write_json_atomic(args.metadata.resolve(), metadata)
    write_jsonl_atomic(args.queue_output.resolve(), queue)
    write_missing_meetings(args.missing_output.resolve(), meetings, enrichments, args.lookback_days)

    processed_metadata_path = PROJECT_ROOT / "data" / "processed" / "metadata.json"
    if processed_metadata_path.exists():
        processed_metadata = read_json(processed_metadata_path)
        unmatched_count = metadata["status_counts"].get("unmatched", 0)
        enrichment_status = "in_progress"
        if metadata["pending_motions"] == 0:
            enrichment_status = "complete_with_gaps" if unmatched_count else "complete"
        processed_metadata["enrichment"] = {
            "status": enrichment_status,
            "matched_motions": metadata["status_counts"].get("matched", 0),
            "unmatched_motions": metadata["status_counts"].get("unmatched", 0),
            "pending_motions": metadata["pending_motions"],
            "total_motions": metadata["total_motions"],
            "generated_at": metadata["generated_at"],
        }
        write_json_atomic(processed_metadata_path, processed_metadata)
    return metadata


def materialize_candidate(
    url: str,
    meeting_id: str,
    args: argparse.Namespace,
    archive_index: dict[str, dict],
    official_state: dict,
) -> tuple[Path | None, str | None, str | None]:
    cache_path = cache_path_for_url(args.cache_dir.resolve(), url)
    for local_path in local_pdf_candidates(
        args.source_dir.expanduser().resolve() if args.source_dir else None,
        cache_path,
        meeting_id,
    ):
        if local_path.exists():
            return local_path, "local_cache", portable_local_path(local_path)

    if args.offline:
        return None, None, "not_cached"

    direct_error = None
    if not args.no_direct and not official_state.get("blocked"):
        data, direct_error, status = fetch_bytes(url, args.timeout)
        time.sleep(args.delay)
        if data:
            try:
                save_pdf(cache_path, data)
                return cache_path, "official", url
            except ValueError:
                direct_error = "official_response_not_pdf"
        if status == 403:
            official_state["blocked"] = True

    if not args.no_wayback:
        capture = archive_index.get(canonical_url(url))
        if capture:
            retrieved_url = archive_url(capture)
            data, archive_error, _ = fetch_bytes(retrieved_url, args.timeout)
            time.sleep(args.delay)
            if data:
                try:
                    save_pdf(cache_path, data)
                    return cache_path, "internet_archive", retrieved_url
                except ValueError:
                    archive_error = "archive_response_not_pdf"
            return None, None, archive_error or direct_error or "archive_download_failed"
    return None, None, direct_error or "minutes_not_available"


def enrich_meeting(
    meeting: dict,
    existing: dict[str, dict],
    args: argparse.Namespace,
    archive_index: dict[str, dict],
    official_state: dict,
) -> tuple[int, int, str | None]:
    selected = []
    for motion in meeting["motions"]:
        current = existing.get(motion["motion_id"])
        if should_process_motion(motion, current, args):
            selected.append(motion)
    if not selected:
        return 0, 0, None

    candidates = candidate_minutes_urls(
        meeting["meeting_type"], meeting["vote_date"], args.lookback_days
    )
    last_reason = "minutes_not_available"
    for url in candidates:
        pdf_path, retrieval_method, retrieved_from = materialize_candidate(
            url, meeting["meeting_id"], args, archive_index, official_state
        )
        if pdf_path is None:
            if retrieved_from:
                last_reason = retrieved_from
            continue
        try:
            pages = load_or_extract_pages(pdf_path)
        except RuntimeError as exc:
            last_reason = f"extraction_error:{exc}"
            continue
        matched = {}
        for motion in selected:
            extracted = extract_motion_from_pages(
                pages, motion["vote_number"], args.max_motion_chars
            )
            if extracted:
                extracted["_match_method"] = "vote_number"
                extracted["_match_quality"] = "exact"
                extracted["source_vote_number"] = normalize_vote_number(
                    motion["vote_number"]
                )
            elif title_fallback_allowed(meeting["vote_date"], url):
                extracted = extract_motion_by_title(
                    pages, motion["agenda_description"], args.max_motion_chars
                )
                if extracted:
                    extracted["_match_method"] = "agenda_title_then_nearest_vote"
                    extracted["_match_quality"] = "inferred"
            matched[motion["motion_id"]] = extracted
        if not any(matched.values()):
            last_reason = "vote_numbers_not_found_in_candidate"
            continue
        fetched_at = datetime.now(timezone.utc).isoformat()
        matched_count = 0
        for motion in selected:
            extracted = matched[motion["motion_id"]]
            if extracted:
                match_method = extracted.pop("_match_method")
                match_quality = extracted.pop("_match_quality")
                existing[motion["motion_id"]] = {
                    "motion_id": motion["motion_id"],
                    "status": "matched",
                    "match_method": match_method,
                    "match_quality": match_quality,
                    "minutes_url": url,
                    "retrieval_method": retrieval_method,
                    "retrieved_from": retrieved_from,
                    "fetched_at": fetched_at,
                    **extracted,
                }
                matched_count += 1
        if matched_count:
            for motion in selected:
                if matched[motion["motion_id"]] is None:
                    existing[motion["motion_id"]] = {
                        "motion_id": motion["motion_id"],
                        "status": "unmatched",
                        "reason": "vote_number_not_found_in_matched_minutes",
                        "minutes_url": url,
                        "retrieval_method": retrieval_method,
                        "retrieved_from": retrieved_from,
                        "fetched_at": fetched_at,
                    }
            return matched_count, len(selected) - matched_count, url

    attempted_at = datetime.now(timezone.utc).isoformat()
    for motion in selected:
        existing[motion["motion_id"]] = {
            "motion_id": motion["motion_id"],
            "status": "unmatched",
            "reason": last_reason,
            "minutes_url": candidates[0],
            "retrieval_method": None,
            "retrieved_from": None,
            "fetched_at": attempted_at,
        }
    return 0, len(selected), None


def main() -> int:
    args = parse_args()
    try:
        motions = read_json(args.input.expanduser().resolve())
        meetings = group_motions(motions)
        existing_values = read_json(args.output.expanduser().resolve()) if args.output.exists() else []
        existing = {item["motion_id"]: item for item in existing_values}
        unknown_existing = set(existing) - {item["motion_id"] for item in motions}
        if unknown_existing:
            raise ValueError(
                f"output contains unknown motion ids: {sorted(unknown_existing)[:5]}"
            )
        pending = pending_meetings(meetings, existing, args)
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Enrichment plan: {len(pending):,} pending meeting(s), "
        f"{sum(len(item['motions']) for item in pending):,} motion(s); "
        f"{len(existing):,} enrichment record(s) already saved."
    )
    if not pending:
        persist_outputs(motions, meetings, existing, args)
        return 0
    if not args.execute:
        print("Dry run only. Re-run with --execute to download and parse minutes.")
        return 0

    try:
        require_pdf_dependency()
        if not args.offline and not args.no_wayback:
            archive_index, archive_error = load_wayback_index(
                args.wayback_index.expanduser().resolve(),
                args.offline,
                args.refresh_wayback_index,
                args.timeout,
            )
            if archive_error:
                print(f"warning: archive index unavailable: {archive_error}", file=sys.stderr)
        else:
            archive_index = {}

        official_state = {"blocked": False}
        total_matched = 0
        total_unmatched = 0
        for number, meeting in enumerate(pending, start=1):
            print(
                f"Meeting {number}/{len(pending)}: {meeting['vote_date']} "
                f"{meeting['meeting_type']} ({len(meeting['motions'])} motions)...",
                flush=True,
            )
            matched, unmatched, matched_url = enrich_meeting(
                meeting, existing, args, archive_index, official_state
            )
            total_matched += matched
            total_unmatched += unmatched
            source = f" via {matched_url}" if matched_url else ""
            print(f"  matched {matched}, unmatched {unmatched}{source}", flush=True)
            persist_outputs(motions, meetings, existing, args)
    except (RuntimeError, ValueError, KeyboardInterrupt) as exc:
        print(f"error: {exc}", file=sys.stderr)
        try:
            persist_outputs(motions, meetings, existing, args)
        except Exception as persist_exc:
            print(f"warning: could not save final state: {persist_exc}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1

    try:
        from build_site_data import build_site_data

        build_site_data(PROJECT_ROOT)
    except Exception as exc:
        print(f"warning: could not refresh web data: {exc}", file=sys.stderr)

    metadata = build_metadata(motions, existing, meetings)
    print(
        f"Enriched {total_matched:,} motion(s); {total_unmatched:,} unmatched in this run. "
        f"Output now contains {len(existing):,} records."
    )
    if metadata["status_counts"].get("unmatched", 0):
        print(f"Review missing sources in {args.missing_output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
