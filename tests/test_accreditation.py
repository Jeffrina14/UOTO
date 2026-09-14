import unittest

from pipeline.pipelineUtils.accreditation import (
    ABSENCE_NOTE,
    FLAG_CONTAINS_UNRECOGNIZED,
    FLAG_NEEDS_REVIEW,
    FLAG_RECOGNIZED,
    STATUS_ACCREDITED_OTHER_SOURCE,
    STATUS_FOUND_IN_DIRECTORY,
    STATUS_NOT_FOUND,
    STATUS_UNVERIFIED,
    accreditation_blob_name,
    build_document_result,
    build_institution_result,
    build_search_payload,
    build_search_queries,
    build_summary,
    dedupe_results,
    derive_review_flags,
    directory_for_url,
    extract_institutions,
    is_transcript_output_name,
    normalize_blob_path,
    normalize_search_results,
    same_entity_confirmed,
    unverified_result,
    validate_matched_directories,
)

CIS_URL = "https://www.cois.org/school/bishop-strachan"


def cis_hit(url=CIS_URL):
    return {
        "title": "The Bishop Strachan School | CIS Member",
        "url": url,
        "text": "The Bishop Strachan School is an accredited member of CIS.",
    }


def institution(name="The Bishop Strachan School", **overrides):
    record = {"name": name, "location": None, "country": None, "website": None}
    record.update(overrides)
    return record


class BlobNamingTests(unittest.TestCase):
    def test_output_suffix_is_replaced_not_appended(self):
        self.assertEqual(
            accreditation_blob_name("Transcript_A-output.json"),
            "Transcript_A-accreditation.json",
        )

    def test_container_prefix_is_dropped(self):
        self.assertEqual(
            accreditation_blob_name("silver/EBF - 1013796064.de_identified-output.json"),
            "EBF - 1013796064.de_identified-accreditation.json",
        )

    def test_name_without_output_suffix_still_gets_single_suffix(self):
        result = accreditation_blob_name("Transcript_B.json")
        self.assertEqual(result, "Transcript_B-accreditation.json")
        self.assertEqual(result.count("-accreditation"), 1)

    def test_empty_name_is_rejected(self):
        with self.assertRaises(ValueError):
            accreditation_blob_name("silver/")

    def test_only_transcript_outputs_trigger_stage_two(self):
        self.assertTrue(is_transcript_output_name("silver/Transcript_A-output.json"))
        self.assertFalse(is_transcript_output_name("gold/Transcript_A-accreditation.json"))
        self.assertFalse(is_transcript_output_name("silver/accreditation_summary.json"))

    def test_normalize_blob_path_strips_container_prefix_once(self):
        self.assertEqual(normalize_blob_path("silver", "silver/a-output.json"), "a-output.json")
        self.assertEqual(normalize_blob_path("silver", "a-output.json"), "a-output.json")


class ExtractInstitutionsTests(unittest.TestCase):
    def test_dedupes_case_insensitively_and_preserves_order(self):
        transcript = {"courses": [
            {"institution": "Riverside College"},
            {"institution": "riverside  college"},
            {"institution": "Bishop Strachan"},
            {"institution": None},
            "not-a-dict",
        ]}
        institutions, truncated = extract_institutions(transcript)
        self.assertEqual(
            [item["name"] for item in institutions],
            ["Riverside College", "Bishop Strachan"],
        )
        self.assertFalse(truncated)

    def test_limit_marks_truncation(self):
        transcript = {"courses": [{"institution": f"School {i}"} for i in range(5)]}
        institutions, truncated = extract_institutions(transcript, limit=2)
        self.assertEqual(len(institutions), 2)
        self.assertTrue(truncated)

    def test_rejects_non_object_transcript(self):
        with self.assertRaises(ValueError):
            extract_institutions(["courses"])


class PrivacyTests(unittest.TestCase):
    def test_search_payload_carries_only_query_and_cap(self):
        payload = build_search_payload('"Riverside College" accreditation', 8)
        self.assertEqual(set(payload), {"query", "max_results"})

    def test_queries_never_contain_student_data(self):
        transcript = {
            "student_name": "Jane Alice Doe",
            "courses": [
                {"institution": "Riverside College", "course_name": "Organic Chemistry",
                 "grade": "A-", "student_name": "Jane Alice Doe"}
            ],
        }
        institutions, _ = extract_institutions(transcript)
        queries = build_search_queries(institutions[0]["name"])
        blob = " ".join(queries).lower()
        for leak in ("jane", "doe", "organic chemistry", "a-"):
            self.assertNotIn(leak, blob)

    def test_gold_document_has_no_student_fields(self):
        document = build_document_result("t-output.json", [], "2024-01-01T00:00:00Z")
        self.assertNotIn("student_name", document)
        self.assertNotIn("courses", document)


class SearchNormalizationTests(unittest.TestCase):
    def test_supports_common_response_shapes(self):
        shapes = [
            {"results": [{"title": "A", "url": "https://a.com", "text": "x"}]},
            {"data": [{"name": "A", "link": "https://a.com", "snippet": "x"}]},
            {"hits": [{"title": "A", "url": "https://a.com", "content": "x"}]},
            {"response": {"results": [{"title": "A", "url": "https://a.com", "summary": "x"}]}},
            [{"title": "A", "url": "https://a.com", "text": "x"}],
        ]
        for shape in shapes:
            with self.subTest(shape=shape):
                results = normalize_search_results(shape)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["url"], "https://a.com")

    def test_joins_list_highlights_and_truncates_text(self):
        results = normalize_search_results(
            {"results": [{"title": "A", "url": "https://a.com",
                          "highlights": ["one", "two"]}]}
        )
        self.assertEqual(results[0]["text"], "one two")

        long_text = "word " * 900
        capped = normalize_search_results(
            {"results": [{"url": "https://a.com", "text": long_text}]}, max_text=50
        )
        self.assertLessEqual(len(capped[0]["text"]), 50)

    def test_drops_hits_without_a_usable_http_url(self):
        payload = {"results": [
            {"title": "no url"},
            {"title": "id only", "id": "doc-1"},
            {"title": "ftp", "url": "ftp://a.com"},
            {"title": "relative", "url": "/schools/a"},
            "not-a-dict",
        ]}
        self.assertEqual(normalize_search_results(payload), [])

    def test_unknown_shape_returns_empty_list(self):
        self.assertEqual(normalize_search_results({"unexpected": "shape"}), [])

    def test_dedupe_by_url_and_limit(self):
        hits = [
            {"url": "https://a.com"},
            {"url": "https://A.com"},
            {"url": "https://b.com"},
            {"url": "https://c.com"},
        ]
        merged = dedupe_results(hits, limit=2)
        self.assertEqual([hit["url"] for hit in merged], ["https://a.com", "https://b.com"])


class DirectoryMatchingTests(unittest.TestCase):
    def test_accepts_www_and_subdomains(self):
        self.assertIsNotNone(directory_for_url("https://www.cois.org/x"))
        self.assertIsNotNone(directory_for_url("https://members.cois.org/x"))

    def test_rejects_lookalike_domains(self):
        for url in (
            "https://cois.org.evil.com/x",
            "https://notcois.org/x",
            "https://evil.com/?ref=cois.org",
        ):
            with self.subTest(url=url):
                self.assertIsNone(directory_for_url(url))


class EntityConfirmationTests(unittest.TestCase):
    def test_requires_every_significant_token(self):
        self.assertTrue(same_entity_confirmed(institution(), cis_hit()))
        self.assertFalse(
            same_entity_confirmed(
                institution("Central College"),
                {"title": "Riverside College", "url": "https://a.com", "text": "A college."},
            )
        )

    def test_matching_website_confirms_without_token_overlap(self):
        hit = {"title": "Home", "url": "https://bss.on.ca/about", "text": "Welcome."}
        self.assertTrue(
            same_entity_confirmed(institution(website="https://www.bss.on.ca"), hit)
        )

    def test_website_mismatch_does_not_block_directory_pages(self):
        self.assertTrue(
            same_entity_confirmed(institution(website="https://bss.on.ca"), cis_hit())
        )


class ValidateMatchedDirectoriesTests(unittest.TestCase):
    def test_rejects_urls_the_search_never_returned(self):
        claimed = [{"directory": "CIS", "url": "https://www.cois.org/invented"}]
        self.assertEqual(validate_matched_directories(claimed, [cis_hit()], institution()), [])

    def test_rejects_non_directory_domains(self):
        hit = {"title": "The Bishop Strachan School", "url": "https://blog.example.com/a",
               "text": "The Bishop Strachan School is great."}
        claimed = [{"directory": "CIS", "url": hit["url"]}]
        self.assertEqual(validate_matched_directories(claimed, [hit], institution()), [])

    def test_rejects_matches_for_a_different_entity(self):
        hit = {
            "title": "Riverside College | CIS Member",
            "url": "https://www.cois.org/member-directory?id=4821",
            "text": "Riverside College is a member.",
        }
        claimed = [{"directory": "CIS", "url": hit["url"]}]
        self.assertEqual(validate_matched_directories(claimed, [hit], institution()), [])

    def test_replaces_unquoted_evidence_with_real_snippet(self):
        claimed = [{"directory": "CIS", "url": CIS_URL, "evidence": "totally invented quote"}]
        validated = validate_matched_directories(claimed, [cis_hit()], institution())
        self.assertEqual(len(validated), 1)
        self.assertIn(validated[0]["evidence"], cis_hit()["text"])

    def test_keeps_evidence_quoted_from_the_snippet(self):
        claimed = [{"directory": "CIS", "url": CIS_URL, "evidence": "accredited member of CIS"}]
        validated = validate_matched_directories(claimed, [cis_hit()], institution())
        self.assertEqual(validated[0]["evidence"], "accredited member of CIS")

    def test_deduplicates_repeated_urls(self):
        claimed = [
            {"directory": "CIS", "url": CIS_URL},
            {"directory": "CIS", "url": CIS_URL},
        ]
        self.assertEqual(
            len(validate_matched_directories(claimed, [cis_hit()], institution())), 1
        )


class InstitutionResultTests(unittest.TestCase):
    def test_directory_hit_produces_found_in_directory(self):
        verdict = {
            "accreditation_status": "found_in_directory",
            "matched_directories": [{"directory": "CIS", "url": CIS_URL,
                                     "evidence": "accredited member of CIS"}],
            "confidence": "high",
            "summary": "Listed with CIS.",
        }
        result = build_institution_result(institution(), verdict, [cis_hit()], ["q"])
        self.assertEqual(result["accreditation_status"], STATUS_FOUND_IN_DIRECTORY)
        self.assertTrue(result["same_entity_confirmed"])
        self.assertFalse(result["needs_human_review"])
        self.assertEqual(result["matched_directories"][0]["url"], CIS_URL)

    def test_other_source_requires_a_named_body(self):
        hit = {"title": "The Bishop Strachan School", "url": "https://news.example.com/a",
               "text": "The Bishop Strachan School is accredited by the Ministry."}
        base = {"accreditation_status": "accredited_other_source", "confidence": "high"}

        with_body = build_institution_result(
            institution(), dict(base, accrediting_bodies=["Ministry of Education"]), [hit], ["q"]
        )
        self.assertEqual(with_body["accreditation_status"], STATUS_ACCREDITED_OTHER_SOURCE)
        self.assertEqual(with_body["accrediting_bodies"], ["Ministry of Education"])

        without_body = build_institution_result(
            institution(), dict(base, accrediting_bodies=[]), [hit], ["q"]
        )
        self.assertEqual(without_body["accreditation_status"], STATUS_UNVERIFIED)

    def test_not_found_keeps_absence_note(self):
        hit = {"title": "The Bishop Strachan School", "url": "https://example.com/a",
               "text": "The Bishop Strachan School sports day results."}
        result = build_institution_result(
            institution(),
            {"accreditation_status": "not_found", "notes": "No listing located."},
            [hit], ["q"],
        )
        self.assertEqual(result["accreditation_status"], STATUS_NOT_FOUND)
        self.assertIn(ABSENCE_NOTE, result["notes"])
        self.assertTrue(result["needs_human_review"])

    def test_absence_note_is_not_duplicated(self):
        result = build_institution_result(
            institution(),
            {"accreditation_status": "not_found", "notes": ABSENCE_NOTE},
            [{"title": "The Bishop Strachan School", "url": "https://e.com/a",
              "text": "The Bishop Strachan School page."}],
            ["q"],
        )
        self.assertEqual(result["notes"].count(ABSENCE_NOTE), 1)

    def test_unconfirmed_entity_downgrades_to_unverified(self):
        hit = {"title": "Riverside College", "url": "https://example.com/a",
               "text": "Riverside College is accredited."}
        result = build_institution_result(
            institution(),
            {"accreditation_status": "found_in_directory",
             "matched_directories": [{"directory": "CIS", "url": "https://example.com/a"}],
             "confidence": "high"},
            [hit], ["q"],
        )
        self.assertEqual(result["accreditation_status"], STATUS_UNVERIFIED)
        self.assertEqual(result["confidence"], "low")
        self.assertTrue(result["needs_human_review"])

    def test_no_hits_yields_unverified(self):
        result = build_institution_result(institution(), {}, [], ["q"])
        self.assertEqual(result["accreditation_status"], STATUS_UNVERIFIED)

    def test_confidence_capped_at_medium_without_a_website(self):
        verdict = {
            "accreditation_status": "accredited_other_source",
            "accrediting_bodies": ["Ministry"],
            "confidence": "high",
        }
        hit = {"title": "The Bishop Strachan School", "url": "https://news.example.com/a",
               "text": "The Bishop Strachan School is accredited by the Ministry."}
        self.assertEqual(
            build_institution_result(institution(), verdict, [hit], ["q"])["confidence"],
            "medium",
        )
        confirmed = build_institution_result(
            institution(website="https://news.example.com"), verdict, [hit], ["q"]
        )
        self.assertEqual(confirmed["confidence"], "high")

    def test_exam_board_always_needs_review(self):
        verdict = {
            "accreditation_status": "found_in_directory",
            "matched_directories": [{"directory": "CIS", "url": CIS_URL}],
            "is_exam_board": True,
            "confidence": "high",
        }
        result = build_institution_result(institution(), verdict, [cis_hit()], ["q"])
        self.assertTrue(result["is_exam_board"])
        self.assertTrue(result["needs_human_review"])

    def test_unverified_result_shape_matches_normal_result(self):
        normal = build_institution_result(institution(), {}, [cis_hit()], ["q"])
        fallback = unverified_result(institution(), ["q"], "Search failed.")
        self.assertEqual(set(normal), set(fallback))
        self.assertEqual(fallback["accreditation_status"], STATUS_UNVERIFIED)
        self.assertTrue(fallback["needs_human_review"])


class ReviewFlagTests(unittest.TestCase):
    def test_all_clear_is_recognized(self):
        institutions = [{"accreditation_status": STATUS_FOUND_IN_DIRECTORY,
                         "needs_human_review": False}]
        self.assertEqual(derive_review_flags(institutions), [FLAG_RECOGNIZED])

    def test_not_found_flags_unrecognized(self):
        institutions = [{"accreditation_status": STATUS_NOT_FOUND, "needs_human_review": True}]
        flags = derive_review_flags(institutions)
        self.assertIn(FLAG_CONTAINS_UNRECOGNIZED, flags)
        self.assertIn(FLAG_NEEDS_REVIEW, flags)
        self.assertNotIn(FLAG_RECOGNIZED, flags)

    def test_truncation_forces_review(self):
        institutions = [{"accreditation_status": STATUS_FOUND_IN_DIRECTORY,
                         "needs_human_review": False}]
        self.assertEqual(
            derive_review_flags(institutions, truncated=True), [FLAG_NEEDS_REVIEW]
        )

    def test_empty_document_has_no_flags(self):
        self.assertEqual(derive_review_flags([]), [])


class SummaryTests(unittest.TestCase):
    def test_merges_files_and_keeps_strongest_status(self):
        weak = {
            "source_file": "a-accreditation.json",
            "institutions": [{"institution": "Riverside College",
                              "accreditation_status": STATUS_UNVERIFIED,
                              "confidence": "low", "sources": []}],
        }
        strong = {
            "source_file": "b-accreditation.json",
            "institutions": [{"institution": "riverside college",
                              "accreditation_status": STATUS_FOUND_IN_DIRECTORY,
                              "confidence": "medium",
                              "matched_directories": [{"directory": "CIS", "url": CIS_URL}],
                              "sources": [{"url": CIS_URL}]}],
        }
        summary = build_summary([weak, strong], "2024-01-01T00:00:00Z")
        self.assertEqual(summary["institution_count"], 1)
        record = summary["schools"][0]
        self.assertEqual(record["accreditation_status"], STATUS_FOUND_IN_DIRECTORY)
        self.assertEqual(record["files"], ["a-accreditation.json", "b-accreditation.json"])
        self.assertEqual(record["recognized_by"], ["CIS"])
        self.assertEqual(record["top_source_url"], CIS_URL)

    def test_tally_counts_each_institution_once(self):
        documents = [{
            "source_file": "a-accreditation.json",
            "institutions": [
                {"institution": "A", "accreditation_status": STATUS_FOUND_IN_DIRECTORY},
                {"institution": "B", "accreditation_status": STATUS_NOT_FOUND},
                {"institution": "C", "accreditation_status": STATUS_NOT_FOUND},
            ],
        }]
        summary = build_summary(documents, "2024-01-01T00:00:00Z")
        self.assertEqual(summary["status_tally"],
                         {STATUS_FOUND_IN_DIRECTORY: 1, STATUS_NOT_FOUND: 2})
        self.assertEqual([item["institution"] for item in summary["schools"]],
                         ["A", "B", "C"])

    def test_ignores_malformed_entries(self):
        summary = build_summary(
            ["not-a-dict", {"institutions": ["x", {"institution": "  "}]}],
            "2024-01-01T00:00:00Z",
        )
        self.assertEqual(summary["institution_count"], 0)
        self.assertEqual(summary["schools"], [])


if __name__ == "__main__":
    unittest.main()
