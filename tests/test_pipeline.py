from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import classify_with_codex  # noqa: E402
import derive_motion_stages  # noqa: E402
import enrich_motions  # noqa: E402
import prepare_data  # noqa: E402
import recover_missing_minutes  # noqa: E402


class PrepareDataTests(unittest.TestCase):
    def test_trailing_window_and_motion_grouping(self):
        rows = [
            "Meeting ID;Meeting Type;Vote Date;Vote Number;Agenda Description;Vote Start Date Time;Council Member;Vote;Decision;Vote Detail Id",
            "1;Council;2021-01-19;10;Old motion;2021-01-19T10:00:00-08:00;Councillor A One;In Favour;Carried;1",
            "2;Council;2022-01-20;11;Current motion;2022-01-20T10:00:00-08:00;Councillor A One;In Favour;Carried;2",
            "2;Council;2022-01-20;11;Current motion;2022-01-20T10:00:00-08:00;Councillor B Two;In Opposition;Carried;3",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.csv"
            source.write_text("\ufeff" + "\r\n".join(rows) + "\r\n", encoding="utf-8")
            output = root / "processed"
            args = SimpleNamespace(
                input=source,
                output_dir=output,
                as_of=date(2026, 9, 5),
                years=5,
                start_date=None,
                end_date=None,
                all_dates=False,
                meeting_types=None,
                no_build_web=True,
            )
            metadata = prepare_data.prepare(args)
            motions = json.loads((output / "motions.json").read_text())
            votes = json.loads((output / "votes.json").read_text())
            self.assertEqual(metadata["selection"]["vote_rows"], 2)
            self.assertEqual(len(motions), 1)
            self.assertEqual(len(votes), 2)
            self.assertEqual(motions[0]["motion_id"], "2-11")
            self.assertEqual(motions[0]["vote_counts"]["In Favour"], 1)
            self.assertEqual(motions[0]["vote_counts"]["In Opposition"], 1)

    def test_leap_day_year_subtraction(self):
        self.assertEqual(
            prepare_data.subtract_years(date(2024, 2, 29), 1), date(2023, 2, 28)
        )


class ClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.taxonomy = json.loads(
            (PROJECT_ROOT / "config" / "taxonomy.json").read_text()
        )

    def valid_result(self):
        return {
            "classifications": [
                {
                    "motion_id": "2-11",
                    "primary_category": "housing_development",
                    "secondary_categories": [],
                    "policy_direction": "unclear_or_mixed",
                    "public_impact": "moderate",
                    "summary": "A neutral summary.",
                    "keywords": ["housing"],
                    "confidence": 0.75,
                    "needs_review": True,
                    "rationale": "The title mentions housing.",
                }
            ]
        }

    def test_batch_validation_accepts_exact_ids(self):
        batch = [{"motion_id": "2-11"}]
        result = classify_with_codex.validate_batch(
            self.valid_result(), batch, self.taxonomy
        )
        self.assertEqual(result[0]["motion_id"], "2-11")

    def test_batch_validation_rejects_repeated_primary_category(self):
        batch = [{"motion_id": "2-11"}]
        result = self.valid_result()
        result["classifications"][0]["secondary_categories"] = [
            "housing_development"
        ]
        with self.assertRaisesRegex(ValueError, "secondary categories"):
            classify_with_codex.validate_batch(result, batch, self.taxonomy)

    def test_schema_category_enum_matches_taxonomy(self):
        schema = json.loads(
            (PROJECT_ROOT / "config" / "classification-batch.schema.json").read_text()
        )
        schema_categories = set(
            schema["properties"]["classifications"]["items"]["properties"][
                "primary_category"
            ]["enum"]
        )
        taxonomy_categories = {item["id"] for item in self.taxonomy["categories"]}
        self.assertEqual(schema_categories, taxonomy_categories)

    def test_schema_uses_supported_array_keywords(self):
        schema = json.loads(
            (PROJECT_ROOT / "config" / "classification-batch.schema.json").read_text()
        )

        def visit(value):
            if isinstance(value, dict):
                self.assertNotIn("uniqueItems", value)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(schema)

    def test_batch_validation_rejects_duplicate_keywords(self):
        batch = [{"motion_id": "2-11"}]
        result = self.valid_result()
        result["classifications"][0]["keywords"] = ["housing", "housing"]
        with self.assertRaisesRegex(ValueError, "keywords"):
            classify_with_codex.validate_batch(result, batch, self.taxonomy)


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.pages = [
            """COUNCIL MEETING MINUTES
1. Housing program
MOVED by Councillor Alpha
SECONDED by Councillor Beta
THAT Council approve 100 homes.

AMENDMENT MOVED by Councillor Gamma
SECONDED by Councillor Delta
THAT the number \"100\" be replaced by \"120\".
LOST (Vote No. 00123)
(Councillor Alpha opposed)
The amendment having lost, the motion was put and CARRIED (Vote No. 00124).
FINAL MOTION AS APPROVED
THAT Council approve 100 homes.
""",
            """2. Later item
THAT Council receive the report for information.
ADOPTED ON CONSENT (Vote Number: 125)
""",
        ]

    def test_vote_number_extracts_specific_amendment(self):
        result = enrich_motions.extract_motion_from_pages(self.pages, "123")
        self.assertIsNotNone(result)
        self.assertIn('replaced by "120"', result["motion_text"])
        self.assertNotIn("approve 100 homes", result["motion_text"])
        self.assertNotIn("LOST (", result["motion_text"])
        self.assertEqual(result["moved_by"], "Councillor Gamma")
        self.assertEqual(result["seconded_by"], "Councillor Delta")
        self.assertEqual(result["minutes_outcome"], "Lost")
        self.assertEqual(result["minutes_pdf_page"], 1)

    def test_final_vote_uses_final_approved_text(self):
        result = enrich_motions.extract_motion_from_pages(self.pages, 124)
        self.assertIsNotNone(result)
        self.assertIn("FINAL MOTION AS APPROVED", result["motion_text"])
        self.assertIn("approve 100 homes", result["motion_text"])
        self.assertEqual(result["minutes_outcome"], "Carried")

    def test_page_number_and_vote_number_variant(self):
        result = enrich_motions.extract_motion_from_pages(self.pages, "0125")
        self.assertIsNotNone(result)
        self.assertEqual(result["minutes_pdf_page"], 2)
        self.assertIn("receive the report", result["motion_text"])

    def test_reconvened_meeting_candidates_look_back(self):
        urls = enrich_motions.candidate_minutes_urls(
            "Special Council", "2021-10-07", 2
        )
        self.assertEqual(
            urls[-1],
            "https://council.vancouver.ca/20211005/documents/spec20211005min.pdf",
        )

    def test_title_fallback_is_limited_to_same_date_minutes(self):
        current = "https://council.vancouver.ca/20231017/documents/regu20231017min.pdf"
        earlier = "https://council.vancouver.ca/20231003/documents/regu20231003min.pdf"
        self.assertTrue(enrich_motions.title_fallback_allowed("2023-10-17", current))
        self.assertFalse(enrich_motions.title_fallback_allowed("2023-10-17", earlier))

    def test_unsafe_saved_inference_is_automatically_reprocessed(self):
        args = SimpleNamespace(force=False, retry_unmatched=False)
        motion = {"motion_id": "1-123", "vote_date": "2023-10-17"}
        current = {
            "status": "matched",
            "match_quality": "inferred",
            "minutes_url": "https://council.vancouver.ca/20231003/documents/regu20231003min.pdf",
        }
        self.assertTrue(enrich_motions.should_process_motion(motion, current, args))
        current["minutes_url"] = (
            "https://council.vancouver.ca/20231017/documents/regu20231017min.pdf"
        )
        self.assertFalse(enrich_motions.should_process_motion(motion, current, args))

    def test_manual_source_takes_precedence_over_cached_archive(self):
        source = Path("/tmp/manual-minutes")
        cache = Path("/tmp/cache/20250917/pspc20250917min.pdf")
        candidates = enrich_motions.local_pdf_candidates(source, cache, "18673")
        self.assertEqual(candidates[0], source / cache.name)
        self.assertEqual(candidates[-1], cache)

    def test_local_provenance_does_not_embed_home_path(self):
        cached = PROJECT_ROOT / "data" / "enrichment" / "minutes" / "minutes.pdf"
        self.assertEqual(
            enrich_motions.portable_local_path(cached),
            "data/enrichment/minutes/minutes.pdf",
        )
        self.assertEqual(
            enrich_motions.portable_local_path(Path("/tmp/manual-minutes.pdf")),
            "manual-minutes.pdf",
        )

    def test_queue_uses_minutes_as_structured_evidence(self):
        motion = {
            "motion_id": "1-123",
            "vote_date": "2021-10-07",
            "meeting_type": "Special Council",
            "agenda_description": "Housing amendment",
            "decision": "Lost",
        }
        enrichment = {
            "status": "matched",
            "motion_text": "THAT Council approve 100 homes.",
            "minutes_url": "https://example.test/minutes.pdf",
            "minutes_pdf_page": 4,
        }
        record = enrich_motions.queue_record(motion, enrichment)
        self.assertEqual(record["enrichment_status"], "matched")
        self.assertEqual(record["minutes_excerpt"], enrichment["motion_text"])
        self.assertEqual(record["source"]["minutes_pdf_page"], 4)

    def test_title_fallback_records_minutes_vote_number(self):
        result = enrich_motions.extract_motion_by_title(
            self.pages, "NB1. Housing program"
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["source_vote_number"], "123")
        self.assertIn('replaced by "120"', result["motion_text"])


class MotionStageTests(unittest.TestCase):
    def motion(self, title):
        return {"motion_id": "1-2", "meeting_id": 1, "agenda_description": title}

    def test_explicit_agenda_suffix_is_direct_amendment(self):
        result = derive_motion_stages.derive_motion_stage(
            self.motion("R1. Housing plan - Amendment to B"), None
        )
        self.assertTrue(result["direct_amendment"])
        self.assertFalse(result["headline_eligible"])
        self.assertIn("agenda_stage_label", result["stage_evidence"])

    def test_minutes_amendment_moved_is_direct_amendment(self):
        result = derive_motion_stages.derive_motion_stage(
            self.motion("R1. Housing plan"),
            {
                "status": "matched",
                "match_quality": "exact",
                "motion_text": "AMENDMENT MOVED by Councillor A THAT B be struck.",
            },
        )
        self.assertTrue(result["direct_amendment"])
        self.assertEqual(result["amendment_type"], "amendment")

    def test_policy_text_amendment_is_not_debate_amendment(self):
        result = derive_motion_stages.derive_motion_stage(
            self.motion("CD-1 Text Amendment: 100 Main Street"),
            {"motion_text": "MOVED by Councillor A THAT the application be approved."},
        )
        self.assertFalse(result["direct_amendment"])
        self.assertTrue(result["headline_eligible"])

    def test_final_motion_narrative_does_not_trigger_amendment(self):
        result = derive_motion_stages.derive_motion_stage(
            self.motion("Housing plan"),
            {"motion_text": "The amendment having lost, the motion was put and CARRIED."},
        )
        self.assertFalse(result["direct_amendment"])


class MissingMinutesRecoveryTests(unittest.TestCase):
    def test_selects_highest_impact_missing_meetings(self):
        meetings = [
            recover_missing_minutes.MissingMeeting(
                meeting_id=str(number),
                vote_date=f"2025-01-{number:02d}",
                meeting_type="Council",
                motion_count=impact,
                unmatched_motion_count=impact,
                vote_numbers=(str(100 + number),),
                expected_minutes_url=(
                    f"https://council.vancouver.ca/202501{number:02d}/documents/"
                    f"regu202501{number:02d}min.pdf"
                ),
                reason="minutes_not_available",
            )
            for number, impact in [(1, 3), (2, 20), (3, 8)]
        ]
        selected = recover_missing_minutes.select_meetings(meetings, 2)
        self.assertEqual([item.meeting_id for item in selected], ["2", "3"])

    def test_excludes_known_missing_meeting(self):
        meetings = [
            recover_missing_minutes.MissingMeeting(
                meeting_id=value,
                vote_date="2025-01-01",
                meeting_type="Council",
                motion_count=1,
                unmatched_motion_count=1,
                vote_numbers=("100",),
                expected_minutes_url=(
                    "https://council.vancouver.ca/20250101/documents/regu20250101min.pdf"
                ),
                reason="minutes_not_available",
            )
            for value in ["keep", "skip"]
        ]
        selected = recover_missing_minutes.select_meetings(
            meetings, 20, excluded_meeting_ids=["skip"]
        )
        self.assertEqual([item.meeting_id for item in selected], ["keep"])

    def test_lookback_candidate_requires_expected_vote_number(self):
        meeting = recover_missing_minutes.MissingMeeting(
            meeting_id="1",
            vote_date="2025-01-03",
            meeting_type="Council",
            motion_count=1,
            unmatched_motion_count=1,
            vote_numbers=("100",),
            expected_minutes_url=(
                "https://council.vancouver.ca/20250103/documents/regu20250103min.pdf"
            ),
            reason="minutes_not_available",
        )
        candidates = recover_missing_minutes.candidate_minutes(meeting, 2)
        self.assertEqual(candidates[-1][0], 2)
        self.assertEqual(
            candidates[-1][1].expected_minutes_url,
            "https://council.vancouver.ca/20250101/documents/regu20250101min.pdf",
        )
        self.assertFalse(
            recover_missing_minutes.candidate_is_acceptable(
                {"valid": True, "matched_vote_count": 0}, 2
            )
        )
        self.assertTrue(
            recover_missing_minutes.candidate_is_acceptable(
                {"valid": True, "matched_vote_count": 1}, 2
            )
        )

    def test_rejects_html_response_as_pdf(self):
        meeting = recover_missing_minutes.MissingMeeting(
            meeting_id="1",
            vote_date="2025-01-01",
            meeting_type="Council",
            motion_count=1,
            unmatched_motion_count=1,
            vote_numbers=("100",),
            expected_minutes_url=(
                "https://council.vancouver.ca/20250101/documents/regu20250101min.pdf"
            ),
            reason="minutes_not_available",
        )
        result = recover_missing_minutes.validate_pdf_bytes(b"<html>blocked</html>", meeting)
        self.assertFalse(result["valid"])
        self.assertEqual(result["error"], "response_is_not_pdf")


class BuiltDataIntegrityTests(unittest.TestCase):
    def test_processed_data_joins_and_counts(self):
        processed = PROJECT_ROOT / "data" / "processed"
        motions = json.loads((processed / "motions.json").read_text())
        votes = json.loads((processed / "votes.json").read_text())
        members = json.loads((processed / "members.json").read_text())
        metadata = json.loads((processed / "metadata.json").read_text())
        motion_ids = {item["motion_id"] for item in motions}
        member_ids = {item["member_id"] for item in members}
        self.assertEqual(len(motion_ids), len(motions))
        self.assertTrue(all(vote["motion_id"] in motion_ids for vote in votes))
        self.assertTrue(all(vote["member_id"] in member_ids for vote in votes))
        self.assertEqual(sum(item["recorded_votes"] for item in members), len(votes))
        self.assertEqual(metadata["selection"]["motions"], len(motions))
        self.assertEqual(metadata["selection"]["vote_rows"], len(votes))

    def test_raw_source_checksum(self):
        raw = PROJECT_ROOT / "data" / "raw" / "council-voting-records.csv"
        metadata = json.loads(
            (PROJECT_ROOT / "data" / "processed" / "metadata.json").read_text()
        )
        digest = hashlib.sha256(raw.read_bytes()).hexdigest()
        self.assertEqual(digest, metadata["source"]["sha256"])

    def test_every_report_card_has_party_metadata(self):
        reports = json.loads(
            (PROJECT_ROOT / "web" / "data" / "report_cards.json").read_text()
        )
        self.assertEqual(len(reports), 18)
        for report in reports:
            self.assertTrue(report["party"])
            self.assertTrue(report["party_short"])
            self.assertRegex(report["party_color"], r"^#[0-9a-fA-F]{6}$")
            self.assertTrue(report["party_history"])
            self.assertTrue(report["party_source_url"].startswith("https://"))
            self.assertIsInstance(report["currently_sitting"], bool)
        self.assertEqual(sum(report["currently_sitting"] for report in reports), 11)

    def test_featured_motions_meet_published_selection_criteria(self):
        featured = json.loads(
            (PROJECT_ROOT / "web" / "data" / "featured_motions.json").read_text()
        )
        motions = json.loads(
            (PROJECT_ROOT / "web" / "data" / "motions.json").read_text()
        )
        motion_map = {item["motion_id"]: item for item in motions}
        questions = featured["questions"]
        selection = featured["selection"]
        ids = [item["motion_id"] for item in questions]
        starter_ids = selection["starter_question_ids"]
        self.assertEqual(len(ids), selection["pool_count"])
        self.assertLessEqual(selection["quiz_count"], selection["pool_count"])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(starter_ids), selection["quiz_count"])
        self.assertEqual(len(starter_ids), len(set(starter_ids)))
        self.assertTrue(set(starter_ids).issubset(ids))
        motion_groups = {
            question["motion_id"]: question["motion_group_id"]
            for question in questions
        }
        self.assertEqual(
            len({motion_groups[motion_id] for motion_id in starter_ids}),
            len(starter_ids),
        )
        for question in questions:
            motion = motion_map[question["motion_id"]]
            self.assertTrue(question["prompt"].startswith("Would you"))
            self.assertTrue(question["context"])
            self.assertTrue(question["tradeoff"])
            self.assertEqual(motion["public_impact"], "high")
            self.assertGreaterEqual(motion["confidence"], selection["minimum_confidence"])
            self.assertEqual(motion["enrichment_status"], "matched")
            self.assertEqual(motion["enrichment_match_quality"], "exact")
            self.assertFalse(motion["direct_amendment"])
            self.assertGreaterEqual(
                motion["vote_counts"]["In Favour"],
                selection["minimum_votes_each_side"],
            )
            self.assertGreaterEqual(
                motion["vote_counts"]["In Opposition"],
                selection["minimum_votes_each_side"],
            )
        question_map = {item["motion_id"]: item for item in questions}
        self.assertIn("4.5%", question_map["18690-11155"]["prompt"])
        self.assertIn("six-storey", question_map["18775-11136"]["prompt"])
        self.assertIn("87-unit", question_map["18775-11136"]["prompt"])
        self.assertIn("townhouses", question_map["18182-8155"]["prompt"])
        self.assertIn("18 rental tenancies", question_map["18182-8155"]["prompt"])

    def test_report_cards_exclude_only_labelled_direct_amendments(self):
        reports = json.loads(
            (PROJECT_ROOT / "web" / "data" / "report_cards.json").read_text()
        )
        for report in reports:
            self.assertEqual(
                report["recorded_votes"] + report["excluded_direct_amendment_votes"],
                report["all_recorded_votes"],
            )
            self.assertEqual(sum(report["vote_counts"].values()), report["recorded_votes"])

    def test_divided_report_scope_contains_only_two_sided_headline_votes(self):
        reports = json.loads(
            (PROJECT_ROOT / "web" / "data" / "report_cards.json").read_text()
        )
        motions = json.loads(
            (PROJECT_ROOT / "web" / "data" / "motions.json").read_text()
        )
        votes = json.loads(
            (PROJECT_ROOT / "web" / "data" / "votes.json").read_text()
        )
        divided_ids = {
            motion["motion_id"]
            for motion in motions
            if motion["headline_eligible"]
            and motion["vote_counts"].get("In Favour", 0) > 0
            and motion["vote_counts"].get("In Opposition", 0) > 0
        }
        expected_by_member = Counter(
            vote["member_id"] for vote in votes if vote["motion_id"] in divided_ids
        )
        for report in reports:
            divided = report["decision_scopes"]["divided"]
            self.assertEqual(
                divided["recorded_votes"], expected_by_member[report["member_id"]]
            )
            self.assertLessEqual(divided["recorded_votes"], report["recorded_votes"])
            self.assertEqual(
                sum(divided["vote_counts"].values()), divided["recorded_votes"]
            )


if __name__ == "__main__":
    unittest.main()
