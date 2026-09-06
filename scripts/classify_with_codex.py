#!/usr/bin/env python3
"""Incrementally classify normalized motions with non-interactive Codex.

The script is inert unless --execute is supplied. Existing valid classifications are
kept, making interrupted runs safe to resume.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "processed" / "motions_to_classify.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "classifications" / "motions.json"
DEFAULT_TAXONOMY = PROJECT_ROOT / "config" / "taxonomy.json"
DEFAULT_SCHEMA = PROJECT_ROOT / "config" / "classification-batch.schema.json"
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "classify-motions.md"
DEFAULT_LOG = PROJECT_ROOT / "data" / "classifications" / "run_log.jsonl"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify prepared motions with Codex in resumable batches."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument(
        "--limit", type=int, help="Classify at most this many still-pending motions."
    )
    parser.add_argument(
        "--motion-id", action="append", dest="motion_ids", help="Only classify this motion id; repeatable."
    )
    parser.add_argument("--model", help="Optional Codex model override; default uses CLI configuration.")
    parser.add_argument(
        "--force", action="store_true", help="Reclassify selected motions even when output exists."
    )
    parser.add_argument(
        "--reclassify-existing",
        action="store_true",
        help="Reclassify only motions already present in the output (useful after enrichment).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually invoke codex exec. Without this flag the script only prints a plan.",
    )
    args = parser.parse_args()
    if args.force and args.reclassify_existing:
        parser.error("--force and --reclassify-existing cannot be combined")
    if args.batch_size < 1 or args.batch_size > 100:
        parser.error("--batch-size must be between 1 and 100")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict]:
    values = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
    return values


def write_json_atomic(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def append_log(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


def make_prompt(template: str, taxonomy: dict, motions: list[dict]) -> str:
    if "{{TAXONOMY_JSON}}" not in template or "{{MOTIONS_JSON}}" not in template:
        raise ValueError("prompt template is missing a required placeholder")
    return template.replace(
        "{{TAXONOMY_JSON}}", json.dumps(taxonomy, ensure_ascii=False, indent=2)
    ).replace("{{MOTIONS_JSON}}", json.dumps(motions, ensure_ascii=False, indent=2))


def validate_batch(result, batch: list[dict], taxonomy: dict) -> list[dict]:
    if not isinstance(result, dict) or set(result) != {"classifications"}:
        raise ValueError("Codex result must contain only a classifications array")
    values = result["classifications"]
    if not isinstance(values, list):
        raise ValueError("classifications must be an array")

    expected_ids = {motion["motion_id"] for motion in batch}
    actual_ids = {
        item.get("motion_id") for item in values if isinstance(item, dict)
    }
    if actual_ids != expected_ids or len(values) != len(batch):
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids, key=str)
        raise ValueError(f"classification id mismatch; missing={missing}, extra={extra}")

    categories = {item["id"] for item in taxonomy["categories"]}
    directions = {item["id"] for item in taxonomy["policy_directions"]}
    impacts = set(taxonomy["public_impact_levels"])
    required = {
        "motion_id",
        "primary_category",
        "secondary_categories",
        "policy_direction",
        "public_impact",
        "summary",
        "keywords",
        "confidence",
        "needs_review",
        "rationale",
    }
    for item in values:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError(f"invalid fields for classification {item!r}")
        primary = item["primary_category"]
        secondary = item["secondary_categories"]
        if primary not in categories:
            raise ValueError(f"unknown primary category: {primary}")
        if (
            not isinstance(secondary, list)
            or len(secondary) > 2
            or len(secondary) != len(set(secondary))
            or any(value not in categories for value in secondary)
            or primary in secondary
        ):
            raise ValueError(f"invalid secondary categories for {item['motion_id']}")
        if item["policy_direction"] not in directions:
            raise ValueError(f"unknown policy direction for {item['motion_id']}")
        if item["public_impact"] not in impacts:
            raise ValueError(f"unknown public impact for {item['motion_id']}")
        confidence = item["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError(f"invalid confidence for {item['motion_id']}")
        if not isinstance(item["needs_review"], bool):
            raise ValueError(f"invalid needs_review for {item['motion_id']}")
        if not isinstance(item["summary"], str) or not item["summary"].strip():
            raise ValueError(f"empty summary for {item['motion_id']}")
        if not isinstance(item["rationale"], str) or not item["rationale"].strip():
            raise ValueError(f"empty rationale for {item['motion_id']}")
        keywords = item["keywords"]
        if (
            not isinstance(keywords, list)
            or not 1 <= len(keywords) <= 5
            or len(keywords) != len(set(keywords))
            or any(not isinstance(word, str) or not word.strip() for word in keywords)
        ):
            raise ValueError(f"invalid keywords for {item['motion_id']}")
    return values


def invoke_codex(
    executable: str,
    prompt: str,
    schema_path: Path,
    model: str | None,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="vancouver-votes-codex-") as temp_dir:
        result_path = Path(temp_dir) / "result.json"
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
        ]
        if model:
            command.extend(["--model", model])
        command.append("-")
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"codex exec failed ({completed.returncode}): {detail}")
        if not result_path.exists():
            raise RuntimeError("codex exec did not write the requested output file")
        try:
            return read_json(result_path)
        except json.JSONDecodeError as exc:
            raise RuntimeError("codex output was not valid JSON") from exc


def chunked(values: list[dict], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def main() -> int:
    args = parse_args()
    try:
        motions = read_jsonl(args.input.resolve())
        taxonomy = read_json(args.taxonomy.resolve())
        template = args.prompt.resolve().read_text(encoding="utf-8")
        existing_values = read_json(args.output.resolve()) if args.output.exists() else []
        existing = {item["motion_id"]: item for item in existing_values}
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    known_ids = {motion["motion_id"] for motion in motions}
    unknown_existing = set(existing) - known_ids
    if unknown_existing:
        print(
            f"error: output contains unknown motion ids: {sorted(unknown_existing)[:5]}",
            file=sys.stderr,
        )
        return 1

    selected_ids = set(args.motion_ids or [])
    missing_selected = selected_ids - known_ids
    if missing_selected:
        print(f"error: unknown --motion-id values: {sorted(missing_selected)}", file=sys.stderr)
        return 1

    pending = []
    for motion in motions:
        motion_id = motion["motion_id"]
        if selected_ids and motion_id not in selected_ids:
            continue
        if args.reclassify_existing:
            if motion_id in existing:
                pending.append(motion)
        elif args.force or motion_id not in existing:
            pending.append(motion)
    if args.limit is not None:
        pending = pending[: args.limit]

    print(
        f"Classification plan: {len(pending):,} pending motions in "
        f"{(len(pending) + args.batch_size - 1) // args.batch_size:,} batch(es); "
        f"{len(existing):,} already classified."
    )
    if not pending:
        return 0
    if not args.execute:
        sample = make_prompt(template, taxonomy, pending[: min(args.batch_size, len(pending))])
        print(f"Dry run only. First prompt would contain {len(sample):,} characters.")
        print("Re-run with --execute to invoke Codex.")
        return 0

    executable = shutil.which("codex")
    if not executable:
        print("error: codex executable was not found on PATH", file=sys.stderr)
        return 1

    output_path = args.output.resolve()
    log_path = args.log.resolve()
    completed_count = 0
    exit_code = 0
    try:
        for batch_number, batch in enumerate(chunked(pending, args.batch_size), start=1):
            print(
                f"Running batch {batch_number} "
                f"({len(batch)} motions; {completed_count:,}/{len(pending):,} completed)...",
                flush=True,
            )
            started_at = datetime.now(timezone.utc)
            prompt = make_prompt(template, taxonomy, batch)
            result = invoke_codex(executable, prompt, args.schema.resolve(), args.model)
            validated = validate_batch(result, batch, taxonomy)
            for item in validated:
                existing[item["motion_id"]] = item
            ordered = [existing[motion["motion_id"]] for motion in motions if motion["motion_id"] in existing]
            write_json_atomic(output_path, ordered)
            completed_count += len(batch)
            append_log(
                log_path,
                {
                    "started_at": started_at.isoformat(),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "taxonomy_version": taxonomy["version"],
                    "model_override": args.model,
                    "motion_ids": [motion["motion_id"] for motion in batch],
                    "status": "completed",
                },
            )
    except (RuntimeError, ValueError, KeyboardInterrupt) as exc:
        print(f"error: {exc}", file=sys.stderr)
        exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        try:
            from build_site_data import build_site_data

            build_site_data(PROJECT_ROOT)
        except Exception as exc:  # Keep classifications even if presentation sync fails.
            print(f"warning: could not refresh web data: {exc}", file=sys.stderr)

    if exit_code == 0:
        print(f"Classified {completed_count:,} motions; output now has {len(existing):,} records.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
