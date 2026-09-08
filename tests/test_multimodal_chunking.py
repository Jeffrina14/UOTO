import importlib.util
import json
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


class _Blueprint:
    def function_name(self, _name):
        return lambda function: function

    def activity_trigger(self, input_name=None):
        return lambda function: function


class _Configuration:
    def get_value(self, key, default=None):
        return default


def _load_activity_module():
    durable_functions = types.ModuleType("azure.durable_functions")
    durable_functions.Blueprint = _Blueprint

    prompts = types.ModuleType("pipelineUtils.prompts")
    prompts.load_prompts = lambda: {
        "system_prompt": "system",
        "user_prompt": "user",
    }

    blob_functions = types.ModuleType("pipelineUtils.blob_functions")
    blob_functions.get_blob_content = lambda **kwargs: b""

    azure_openai = types.ModuleType("pipelineUtils.azure_openai")
    class RequestTooLargeError(Exception):
        pass

    azure_openai.RequestTooLargeError = RequestTooLargeError
    azure_openai.run_prompt = lambda *args, **kwargs: "{}"

    configuration = types.ModuleType("configuration")
    configuration.Configuration = _Configuration

    module_name = "test_call_foundry_multimodal_module"
    repository_root = Path(__file__).parents[1]
    pipeline_path = repository_root / "pipeline"
    module_path = pipeline_path / "activities" / "callFoundryMultiModal.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)

    with patch.object(sys, "path", [str(pipeline_path)] + sys.path):
        with patch.dict(
            sys.modules,
            {
                "azure.durable_functions": durable_functions,
                "pipelineUtils.prompts": prompts,
                "pipelineUtils.blob_functions": blob_functions,
                "pipelineUtils.azure_openai": azure_openai,
                "configuration": configuration,
            },
        ):
            spec.loader.exec_module(module)
    return module


class MultimodalChunkingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.activity = _load_activity_module()

    def test_verification_is_disabled_by_default(self):
        self.assertFalse(self.activity._verification_enabled())

    def test_document_classification_supported_formats(self):
        cases = [
            ("Ontario bilingual transcript", {
                "document_type": "transcript",
                "jurisdiction": "Ontario, Canada",
                "source_languages": ["English", "French"],
                "contains_results": True,
                "has_course_table": True,
                "page_roles": ["academic-result-page", "translation"],
            }),
            ("US transcript with missing levels", {
                "document_type": "transcript",
                "jurisdiction": "United States",
                "source_languages": ["English"],
                "contains_results": True,
                "has_course_table": True,
                "page_roles": ["academic-result-page"],
            }),
            ("UK multi-page transcript", {
                "document_type": "transcript",
                "jurisdiction": "United Kingdom",
                "source_languages": ["English"],
                "contains_results": True,
                "has_course_table": True,
                "page_roles": ["academic-result-page", "academic-result-page"],
            }),
            ("UK current-course letter", {
                "document_type": "letter",
                "jurisdiction": "United Kingdom",
                "source_languages": ["English"],
                "contains_results": False,
                "has_course_table": False,
                "page_roles": ["letter"],
            }),
            ("Ukraine two-institution transcript", {
                "document_type": "transcript",
                "jurisdiction": "Ukraine",
                "source_languages": ["Ukrainian", "English"],
                "contains_results": True,
                "has_course_table": True,
                "page_roles": ["academic-result-page", "translation"],
            }),
            ("French report without course codes", {
                "document_type": "report",
                "jurisdiction": None,
                "source_languages": ["French"],
                "contains_results": True,
                "has_course_table": True,
                "page_roles": ["academic-result-page"],
            }),
        ]
        for name, expected in cases:
            with self.subTest(name=name):
                expected["is_translation"] = False
                expected["institution_context"] = []
                expected["issue_date"] = None
                expected["official_translation_detected"] = False
                actual = self.activity._parse_document_classification(
                    json.dumps(expected),
                    len(expected["page_roles"]),
                )
                self.assertEqual(actual, expected)

    def test_classification_calls_model_with_images_and_no_logging(self):
        calls = []

        def model_runner(instance_id, system_prompt, user_prompt, base64_images, **kwargs):
            calls.append((base64_images, kwargs))
            return '{"document_type":"unknown","page_roles":["unknown"]}'

        result = self.activity._classify_document_pages(
            ["page-1"], "instance-1", model_runner=model_runner
        )
        self.assertEqual(result["page_roles"], ["unknown"])
        self.assertEqual(calls, [(["page-1"], {"log_interaction": False})])

    def test_classification_context_is_passed_to_extraction_prompt(self):
        prompts = []

        def model_runner(instance_id, system_prompt, user_prompt, base64_images, **kwargs):
            prompts.append(user_prompt)
            return '{"courses":[]}'

        self.activity._extract_chunked_transcript(
            ["page-1"],
            "instance-1",
            "system",
            "user",
            2,
            document_context=self.activity._format_extraction_context({
                "document_type": "letter",
                "jurisdiction": "United Kingdom",
                "source_languages": ["English"],
                "contains_results": False,
                "has_course_table": False,
                "is_translation": False,
                "page_roles": ["letter"],
            }),
            model_runner=model_runner,
            verify=False,
        )

        self.assertIn('"document_type": "letter"', prompts[0])
        self.assertIn("course mentions outside result tables", prompts[0])

    def test_single_institution_context_fills_missing_course_institution(self):
        result = self.activity._normalize_transcript_result(
            {"courses": [{"course_name": "English", "institution": None}]},
            {
                "document_type": "transcript",
                "jurisdiction": "United States",
                "institution_context": ["University of Chicago"],
            },
        )
        self.assertEqual(
            result["courses"][0]["institution"], "University of Chicago"
        )

    def test_multiple_institution_context_does_not_overwrite_rows(self):
        course = {"course_name": "English", "institution": None}
        result = self.activity._normalize_transcript_result(
            {"courses": [course]},
            {"document_type": "transcript", "institution_context": ["A", "B"]},
        )
        self.assertIsNone(result["courses"][0]["institution"])

    def test_source_format_values_remain_unchanged_for_non_ontario(self):
        course = {
            "course_code": " MOD 101 ",
            "course_level": None,
            "grade": "14,5",
            "year_or_session": "2025/2026",
            "notes": "Crédit : 1,00; Mention: A+",
        }
        result = self.activity._normalize_transcript_result(
            {"courses": [course]},
            {"document_type": "report", "jurisdiction": "France"},
        )
        self.assertEqual(result["courses"][0], course)

    def test_ontario_ocr_code_and_compulsory_marker_are_normalized(self):
        result = self.activity._normalize_transcript_result(
            {"courses": [{
                "course_code": "MPMID",
                "course_name": "Principles of Mathematics",
                "course_level": None,
                "notes": "Credit: 1.00; X",
            }]},
            {"document_type": "transcript", "jurisdiction": "Ontario, Canada"},
        )
        course = result["courses"][0]
        self.assertEqual(course["course_code"], "MPM1D")
        self.assertEqual(course["course_level"], "1")
        self.assertEqual(course["notes"], "Credit: 1.00; Compulsory")

    def test_valid_ontario_code_is_preserved(self):
        course = {"course_code": "ENG1D", "course_name": "English", "notes": "Yes"}
        result = self.activity._normalize_transcript_result(
            {"courses": [course]},
            {"document_type": "transcript", "jurisdiction": "Ontario"},
        )
        self.assertEqual(result["courses"][0]["course_code"], "ENG1D")
        self.assertEqual(result["courses"][0]["notes"], "Compulsory")

    def test_non_ontario_profiles_preserve_local_values(self):
        cases = [
            ("United States", {"course_code": "HISP3U", "course_level": None, "grade": "A-"}),
            ("United Kingdom", {"course_code": "HISP3U", "course_level": "Level 5", "grade": "Distinction"}),
            ("Ukraine", {"course_code": "КП-101", "course_level": None, "grade": "добре"}),
            (None, {"course_code": None, "course_level": None, "grade": "14/20"}),
        ]
        for jurisdiction, course in cases:
            with self.subTest(jurisdiction=jurisdiction):
                result = self.activity._normalize_transcript_result(
                    {"courses": [course]},
                    {"document_type": "transcript", "jurisdiction": jurisdiction},
                )
                self.assertEqual(result["courses"][0], course)

    def test_letter_has_no_course_normalization(self):
        result = self.activity._normalize_transcript_result(
            {"courses": [], "document_type": "letter", "is_academic_record": False},
            {"document_type": "letter", "jurisdiction": "United Kingdom"},
        )
        self.assertEqual(result["courses"], [])

    def test_repeated_attempts_with_different_grades_are_preserved(self):
        courses = [
            {"course_code": "ENG1D", "course_name": "English", "grade": "80", "notes": None},
            {"course_code": "ENG1D", "course_name": "English", "grade": "85", "notes": None},
        ]
        result = self.activity._normalize_transcript_result(
            {"courses": courses},
            {"document_type": "transcript", "jurisdiction": "Ontario"},
        )
        self.assertEqual(result["courses"], courses)

    def test_ambiguous_ontario_code_remains_unchanged(self):
        course = {"course_code": "HISP3U", "course_name": "Spanish", "course_level": None, "notes": None}
        result = self.activity._normalize_transcript_result(
            {"courses": [course]},
            {"document_type": "transcript", "jurisdiction": "Ontario"},
        )
        self.assertEqual(result["courses"][0], course)

    def test_exact_duplicate_rows_are_reduced_to_one(self):
        course = {"institution": "School", "year_or_session": "2020-06",
                  "course_level": "12", "course_name": "English",
                  "course_code": "ENG4U", "grade": "95", "notes": "Final"}
        result = self.activity._merge_transcript_results([
            {"courses": [course]}, {"courses": [dict(course)]}
        ])
        self.assertEqual(result["courses"], [course])

    def test_same_course_code_with_different_sessions_is_preserved(self):
        first = {"course_code": "ENG4U", "year_or_session": "2020-06"}
        second = {"course_code": "ENG4U", "year_or_session": "2021-01"}
        result = self.activity._merge_transcript_results([
            {"courses": [first]}, {"courses": [second]}
        ])
        self.assertEqual(result["courses"], [first, second])

    def test_same_course_code_with_different_grades_is_preserved(self):
        first = {"course_code": "ENG4U", "grade": "90"}
        second = {"course_code": "ENG4U", "grade": "95"}
        result = self.activity._merge_transcript_results([
            {"courses": [first]}, {"courses": [second]}
        ])
        self.assertEqual(result["courses"], [first, second])

    def test_same_course_with_different_notes_is_preserved(self):
        first = {"course_code": "ENG4U", "notes": "Credit: 1.00"}
        second = {"course_code": "ENG4U", "notes": "Credit: 1.00; Compulsory"}
        result = self.activity._merge_transcript_results([
            {"courses": [first]}, {"courses": [second]}
        ])
        self.assertEqual(result["courses"], [first, second])

    def test_missing_fields_do_not_remove_unrelated_rows(self):
        first = {"course_code": "ENG4U", "grade": None}
        second = {"course_code": "ENG4U", "grade": "95"}
        result = self.activity._merge_transcript_results([
            {"courses": [first]}, {"courses": [second]}
        ])
        self.assertEqual(result["courses"], [first, second])

    def test_duplicate_normalization_preserves_original_order(self):
        first = {"course_code": "ENG4U", "course_name": " English  "}
        duplicate = {"course_code": "ENG4U", "course_name": "English"}
        other = {"course_code": "MHF4U", "course_name": "Advanced Functions"}
        result = self.activity._merge_transcript_results([
            {"courses": [first, other]}, {"courses": [duplicate]}
        ])
        self.assertEqual(result["courses"], [first, other])

    def test_pdf_pages_use_two_times_rendering_without_alpha(self):
        calls = []

        class Pixmap:
            def tobytes(self, image_format):
                self.image_format = image_format
                return b"png-bytes"

        class Page:
            def get_pixmap(self, **kwargs):
                calls.append(kwargs)
                return Pixmap()

        @contextmanager
        def opened_document(**kwargs):
            yield [Page(), Page()]

        with patch.object(self.activity.fitz, "Matrix") as matrix_factory:
            matrix = object()
            matrix_factory.return_value = matrix
            with patch.object(self.activity.fitz, "open", opened_document):
                images = self.activity.convert_to_base64_images(
                    {"name": "transcript.pdf"},
                    b"pdf-bytes",
                )

        matrix_factory.assert_called_once_with(2, 2)
        self.assertEqual(calls, [
            {"matrix": matrix, "alpha": False},
            {"matrix": matrix, "alpha": False},
        ])
        self.assertEqual(len(images), 2)

    def test_layout_polygon_converts_pdf_inches_to_two_times_pixels(self):
        rect = self.activity._polygon_to_render_rect(
            [1, 2, 3, 2, 3, 4, 1, 4]
        )
        self.assertEqual((rect.x0, rect.y0, rect.x1, rect.y1), (144, 288, 432, 576))

    def test_layout_row_crops_are_ordered_by_page_and_row(self):
        class Region:
            def __init__(self, page_number, polygon):
                self.page_number = page_number
                self.polygon = polygon

        class Cell:
            def __init__(self, row_index, region):
                self.row_index = row_index
                self.bounding_regions = [region]

        class Table:
            cells = [
                Cell(1, Region(2, [0, 1, 1, 1, 1, 2, 0, 2])),
                Cell(0, Region(1, [0, 0, 1, 0, 1, 1, 0, 1])),
            ]

        class LayoutResult:
            tables = [Table()]
        rows = self.activity._layout_rows(LayoutResult())
        self.assertEqual([row[:2] for row in rows], [(1, 0), (2, 1)])
        self.assertEqual(len(rows[0][2]), 1)
        self.assertEqual(len(rows[1][2]), 1)

    def test_layout_failure_returns_full_page_fallback(self):
        class FailingClient:
            def __init__(self, **kwargs):
                raise RuntimeError("layout unavailable")

        document_intelligence = types.ModuleType("azure.ai.documentintelligence")
        document_intelligence.DocumentIntelligenceClient = FailingClient
        models = types.ModuleType("azure.ai.documentintelligence.models")
        models.AnalyzeDocumentRequest = object
        azure_ai = types.ModuleType("azure.ai")
        azure_ai.__path__ = []

        with patch.dict(sys.modules, {
            "azure.ai": azure_ai,
            "azure.ai.documentintelligence": document_intelligence,
            "azure.ai.documentintelligence.models": models,
        }):
            self.assertEqual(self.activity._layout_row_crops(b"pdf-bytes"), [])

    def test_three_crop_groups_make_three_model_calls(self):
        calls = []

        def model_runner(instance_id, system_prompt, user_prompt, base64_images, **kwargs):
            calls.append(base64_images)
            return '{"courses":[]}'

        self.activity._extract_chunked_transcript(
            ["crop-1", "crop-2", "crop-3"],
            "instance-1",
            "system",
            "user",
            1,
            model_runner=model_runner,
            verify=False,
        )
        self.assertEqual(calls, [["crop-1"], ["crop-2"], ["crop-3"]])

    def test_one_page_without_verification_makes_one_first_pass_call(self):
        calls = []

        def model_runner(instance_id, system_prompt, user_prompt, base64_images, **kwargs):
            calls.append(user_prompt)
            return (
                '{"document_type":"transcript","courses":[{'
                '"institution":"School","course_name":"English",'
                '"course_code":"ENG1D","grade":"80",'
                '"notes":"Credit: 1.00; Compulsory"}]}'
            )

        result = self.activity._extract_chunked_transcript(
            ["page-1"],
            "instance-1",
            "system",
            "user",
            2,
            model_runner=model_runner,
            verify=False,
        )

        self.assertEqual(len(calls), 1)
        self.assertNotIn("Verify only", calls[0])
        self.assertEqual(result["document_type"], "transcript")
        self.assertEqual(result["courses"], [{
            "institution": "School",
            "year_or_session": None,
            "course_level": None,
            "course_name": "English",
            "course_code": "ENG1D",
            "grade": "80",
            "notes": "Credit: 1.00; Compulsory",
        }])
        self.assertFalse(any(
            "row_index" in course for course in result["courses"]
        ))

    def test_valid_response_makes_one_unlogged_model_call(self):
        calls = []

        def model_runner(instance_id, system_prompt, user_prompt, base64_images, **kwargs):
            calls.append((base64_images, kwargs))
            return '{"courses":[]}'

        result = self.activity._extract_chunked_transcript(
            ["page-1"], "instance-1", "system", "user", 2,
            model_runner=model_runner, verify=False,
        )

        self.assertEqual(result["courses"], [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ["page-1"])
        self.assertEqual(calls[0][1], {"log_interaction": False})

    def test_truncated_response_retries_same_chunk_once(self):
        calls = []
        responses = ['{"courses":[', '{"courses":[]}']

        def model_runner(instance_id, system_prompt, user_prompt, base64_images, **kwargs):
            calls.append((user_prompt, base64_images, kwargs))
            return responses.pop(0)

        result = self.activity._extract_chunked_transcript(
            ["page-1", "page-2"], "instance-1", "system", "user", 2,
            model_runner=model_runner, verify=False,
        )

        self.assertEqual(result["courses"], [])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(calls[0][2], {"log_interaction": False})
        self.assertEqual(calls[1][2], {"log_interaction": False})
        self.assertNotEqual(calls[0][0], calls[1][0])

    def test_two_image_413_splits_into_two_ordered_one_image_calls(self):
        calls = []
        attempts = {"two": True}

        def model_runner(instance_id, system_prompt, user_prompt, base64_images, **kwargs):
            calls.append((list(base64_images), kwargs))
            if len(base64_images) == 2 and attempts["two"]:
                attempts["two"] = False
                raise self.activity.RequestTooLargeError("413")
            return '{"courses":[]}'

        result = self.activity._extract_chunked_transcript(
            ["page-1", "page-2"], "instance-1", "system", "user", 2,
            model_runner=model_runner, verify=False,
        )

        self.assertEqual(result["courses"], [])
        self.assertEqual([call[0] for call in calls], [
            ["page-1", "page-2"], ["page-1"], ["page-2"]
        ])
        self.assertTrue(all(call[1] == {"log_interaction": False} for call in calls))

    def test_one_image_413_raises(self):
        calls = []

        def model_runner(instance_id, system_prompt, user_prompt, base64_images, **kwargs):
            calls.append(list(base64_images))
            raise self.activity.RequestTooLargeError("413")

        with self.assertRaises(self.activity.RequestTooLargeError):
            self.activity._extract_chunked_transcript(
                ["page-1"], "instance-1", "system", "user", 2,
                model_runner=model_runner, verify=False,
            )
        self.assertEqual(calls, [["page-1"]])

    def test_successful_chunks_are_not_repeated_after_other_chunk_413(self):
        calls = []

        def model_runner(instance_id, system_prompt, user_prompt, base64_images, **kwargs):
            calls.append(list(base64_images))
            if base64_images == ["page-3", "page-4"]:
                raise self.activity.RequestTooLargeError("413")
            return '{"courses":[]}'

        self.activity._extract_chunked_transcript(
            ["page-1", "page-2", "page-3", "page-4"],
            "instance-1", "system", "user", 2,
            model_runner=model_runner, verify=False,
        )

        self.assertEqual(calls, [
            ["page-1", "page-2"],
            ["page-3", "page-4"],
            ["page-3"],
            ["page-4"],
        ])

    def test_persistent_truncation_fails_without_partial_result(self):
        calls = []

        def model_runner(instance_id, system_prompt, user_prompt, base64_images, **kwargs):
            calls.append(base64_images)
            return '{"courses":['

        with self.assertRaises(ValueError):
            self.activity._extract_chunked_transcript(
                ["page-1"], "instance-1", "system", "user", 2,
                model_runner=model_runner, verify=False,
            )

        self.assertEqual(len(calls), 2)

    def test_malformed_non_truncated_response_is_not_retried(self):
        calls = []

        def model_runner(instance_id, system_prompt, user_prompt, base64_images, **kwargs):
            calls.append(base64_images)
            return '{not valid json}'

        with self.assertRaisesRegex(ValueError, "malformed JSON"):
            self.activity._extract_chunked_transcript(
                ["page-1"], "instance-1", "system", "user", 2,
                model_runner=model_runner, verify=False,
            )

        self.assertEqual(len(calls), 1)

    def test_five_pages_with_chunk_size_two_make_three_model_calls(self):
        calls = []
        pages = ["page-1", "page-2", "page-3", "page-4", "page-5"]

        def model_runner(instance_id, system_prompt, user_prompt, base64_images, **kwargs):
            calls.append({
                "instance_id": instance_id,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "base64_images": base64_images,
            })
            return '{"courses":[]}'

        result = self.activity._extract_chunked_transcript(
            pages,
            "instance-1",
            "system",
            "user",
            2,
            model_runner=model_runner,
        )

        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [call["base64_images"] for call in calls],
            [["page-1", "page-2"], ["page-3", "page-4"], ["page-5"]],
        )
        self.assertIn("pages 1, 2", calls[0]["user_prompt"])
        self.assertIn("pages 3, 4", calls[1]["user_prompt"])
        self.assertIn("pages 5", calls[2]["user_prompt"])
        self.assertEqual(result["courses"], [])
        self.assertFalse(any(
            "row_index" in course for course in result["courses"]
        ))


if __name__ == "__main__":
    unittest.main()
