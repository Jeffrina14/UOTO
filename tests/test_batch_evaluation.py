import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "testing_docs" / "evaluate_transcripts.py"
SPEC = importlib.util.spec_from_file_location("evaluate_transcripts", MODULE_PATH)
evaluate_transcripts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluate_transcripts)


class BatchEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.input_dir.mkdir()
        self.output_dir.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_pdf_placeholder(self, name):
        path = self.input_dir / name
        path.write_bytes(b"pdf")
        return path

    def _extractor(self, result):
        return {
            "convert": lambda blob_input, content: ["page"],
            "classify": lambda images, instance_id: {
                "document_type": "transcript",
                "jurisdiction": "unknown",
                "contains_results": True,
                "has_course_table": True,
            },
            "load_prompts": lambda: {"system_prompt": "system", "user_prompt": "user"},
            "pages_per_request": lambda: 2,
            "format_context": lambda classification: "context",
            "run_prompt": lambda *args, **kwargs: "{}",
            "extract": lambda *args, **kwargs: result,
        }

    def test_matching_sidecar_json(self):
        pdf = self._write_pdf_placeholder("match.pdf")
        expected = {"document_type": "transcript", "is_academic_record": True,
                    "source_languages": ["English"], "courses": [{
                        "course_code": "ABC1D", "course_name": "English", "grade": "A"
                    }]}
        pdf.with_suffix(".json").write_text(json.dumps(expected), encoding="utf-8")
        record = evaluate_transcripts.evaluate_pdf(
            pdf, self.output_dir / "match.json", self._extractor(expected)
        )
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["reference_comparison"]["mismatch_counts"], {})

    def test_missing_sidecar_json(self):
        pdf = self._write_pdf_placeholder("missing.pdf")
        result = {"document_type": "unknown", "courses": []}
        record = evaluate_transcripts.evaluate_pdf(
            pdf, self.output_dir / "missing.json", self._extractor(result)
        )
        self.assertIsNone(record["reference_comparison"])

    def test_valid_null_fields_are_allowed(self):
        expected = {"document_type": "report", "is_academic_record": True,
                    "source_languages": ["French"], "courses": [{
                        "course_code": None, "course_level": None,
                        "course_name": "Math", "grade": None, "notes": None
                    }]}
        actual = dict(expected)
        comparison = evaluate_transcripts.compare_reference(expected, actual)
        self.assertEqual(comparison["mismatch_counts"], {})

    def test_extra_output_course_is_reported(self):
        expected = {"courses": [{"course_code": "A"}]}
        actual = {"courses": [{"course_code": "A"}, {"course_code": "B"}]}
        comparison = evaluate_transcripts.compare_reference(expected, actual)
        self.assertEqual(len(comparison["extra_rows"]), 1)

    def test_missing_output_course_is_reported(self):
        expected = {"courses": [{"course_code": "A"}, {"course_code": "B"}]}
        actual = {"courses": [{"course_code": "A"}]}
        comparison = evaluate_transcripts.compare_reference(expected, actual)
        self.assertEqual(len(comparison["missing_rows"]), 1)

    def test_field_level_mismatch_is_reported(self):
        expected = {"courses": [{"course_code": "A", "grade": "90"}]}
        actual = {"courses": [{"course_code": "A", "grade": "85"}]}
        comparison = evaluate_transcripts.compare_reference(expected, actual)
        self.assertEqual(comparison["mismatch_counts"]["grade"], 1)

    def test_failed_document_does_not_stop_batch(self):
        first = self._write_pdf_placeholder("first.pdf")
        second = self._write_pdf_placeholder("second.pdf")
        calls = []

        def extract_with_one_failure(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("synthetic failure")
            return {"courses": []}

        def extractor_for_batch():
            return {
                "convert": lambda blob_input, content: ["page"],
                "classify": lambda images, instance_id: {"document_type": "unknown", "contains_results": False},
                "load_prompts": lambda: {"system_prompt": "system", "user_prompt": "user"},
                "pages_per_request": lambda: 2,
                "format_context": lambda classification: "",
                "run_prompt": lambda *args, **kwargs: "{}",
                "extract": extract_with_one_failure,
            }

        original_loader = evaluate_transcripts._load_extractor
        evaluate_transcripts._load_extractor = extractor_for_batch
        try:
            summary = evaluate_transcripts.evaluate_directory(self.input_dir, self.output_dir)
        finally:
            evaluate_transcripts._load_extractor = original_loader
        self.assertEqual(summary["total_files"], 2)
        self.assertEqual(summary["failed_files"], 1)
        self.assertEqual(summary["successful_files"], 1)


if __name__ == "__main__":
    unittest.main()
