import unittest

from pipeline.pipelineUtils.transcript_parser import parse_transcript_response


class TranscriptParserTests(unittest.TestCase):
    def test_valid_transcript_json(self):
        result = parse_transcript_response(
            '{"student_name":"Ada Lovelace","courses":[{"course_name":"Mathematics"}]}'
        )
        self.assertEqual(result["student_name"], "Ada Lovelace")
        self.assertEqual(result["courses"][0]["course_name"], "Mathematics")
        self.assertEqual(set(result["courses"][0]), {
            "institution",
            "year_or_session",
            "course_level",
            "course_name",
            "course_code",
            "grade",
            "notes",
        })

    def test_extra_bilingual_fields_are_removed(self):
        result = parse_transcript_response(
            '{"course_name_en":"History", "courses":[{"course_name":"History",'
            '"course_name_original":"Історія"}]}'
        )
        self.assertNotIn("course_name_en", result)
        self.assertNotIn("course_name_original", result["courses"][0])

    def test_fenced_json(self):
        result = parse_transcript_response(
            '```json\n{"document_type":"transcript"}\n```'
        )
        self.assertEqual(result["document_type"], "transcript")

    def test_thinking_tags(self):
        result = parse_transcript_response(
            '<think>private reasoning</think>{"is_academic_record":true}'
        )
        self.assertTrue(result["is_academic_record"])

    def test_trailing_commas(self):
        result = parse_transcript_response(
            '{"student_name":"Ada", "courses":[{"grade":"A",},],}'
        )
        self.assertEqual(result["courses"][0]["grade"], "A")

    def test_empty_response(self):
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            parse_transcript_response("  ")

    def test_malformed_json(self):
        with self.assertRaisesRegex(ValueError, "malformed JSON"):
            parse_transcript_response('{"student_name":"Ada"')

    def test_top_level_array(self):
        with self.assertRaisesRegex(ValueError, "top-level value"):
            parse_transcript_response('[{"student_name":"Ada"}]')

    def test_missing_optional_fields_receive_defaults(self):
        result = parse_transcript_response("{}")
        self.assertIsNone(result["student_name"])
        self.assertEqual(result["source_languages"], [])
        self.assertFalse(result["is_academic_record"])
        self.assertEqual(result["courses"], [])

    def test_courses_must_be_a_list(self):
        with self.assertRaisesRegex(ValueError, "courses must be a JSON array"):
            parse_transcript_response('{"courses":{}}')

    def test_invalid_course_row(self):
        with self.assertRaisesRegex(ValueError, "Course row at index 0"):
            parse_transcript_response('{"courses":["not a course"]}')

    def test_null_like_value_normalization(self):
        result = parse_transcript_response(
            '{"student_name":"N/A","courses":[{"grade":"None","notes":" null "}]}'
        )
        self.assertIsNone(result["student_name"])
        self.assertIsNone(result["courses"][0]["grade"])
        self.assertIsNone(result["courses"][0]["notes"])

    def test_preserves_result_status_wording(self):
        result = parse_transcript_response(
            '{"courses":[{"notes":"Predicted and mock result; current course; transferred credit; final result"}]}'
        )
        self.assertEqual(
            result["courses"][0]["notes"],
            "Predicted and mock result; current course; transferred credit; final result",
        )


if __name__ == "__main__":
    unittest.main()
