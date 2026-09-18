import unittest

from pipeline.pipelineUtils.document_profiles import normalize_profile_result


class ProfileNormalizationTests(unittest.TestCase):
    def test_sus_sets_metadata_and_preserves_institution(self):
        result = normalize_profile_result(
            {"courses": [{"institution": "Barrington High School Transcript",
                           "course_name": "COUMONCORE ECONOMICS",
                           "grade": "A", "credits": None}]},
            "SUS",
        )
        self.assertEqual(result["source_languages"], ["English"])
        self.assertTrue(result["is_academic_record"])
        self.assertEqual(result["courses"][0]["institution"], "Barrington High School Transcript")
        self.assertEqual(result["courses"][0]["course_name"], "CONSUMER ECONOMICS")
        self.assertEqual(result["courses"][0]["credits"], "")

    def test_ebf_preserves_institution_and_normalizes_known_label(self):
        result = normalize_profile_result(
            {"courses": [{"institution": "LYAUTEY", "course_name": "OPITION MATH",
                           "grade": None}]},
            "EBF",
        )
        self.assertEqual(result["institution_context"], [])
        self.assertEqual(result["courses"][0]["institution"], "LYAUTEY")
        self.assertEqual(result["courses"][0]["course_name"], "OPTION MATH")
        self.assertEqual(result["courses"][0]["grade"], "")


if __name__ == "__main__":
    unittest.main()
