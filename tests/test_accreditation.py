import unittest

from pipeline.pipelineUtils.accreditation import (
    accreditation_enabled,
    build_accreditation_summary,
    evaluate_institution,
)


class AccreditationTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(accreditation_enabled({}))

    def test_same_entity_match(self):
        result = evaluate_institution(
            {"name": "Example University", "country": "UK", "website": "example.edu"},
            lambda query: [{
                "name": "Example University",
                "country": "UK",
                "website": "example.edu",
                "directory": True,
            }],
        )
        self.assertEqual(result["status"], "found_in_directory")
        self.assertTrue(result["RECOGNIZED"])

    def test_name_collision_requires_same_entity_context(self):
        result = evaluate_institution(
            {"name": "Example University", "country": "UK", "website": "uk.example"},
            lambda query: [{
                "name": "Example University",
                "country": "US",
                "website": "us.example",
                "directory": True,
            }],
        )
        self.assertEqual(result["status"], "unverified")
        self.assertTrue(result["NEEDS_REVIEW"])

    def test_summary_disclaimer(self):
        summary = build_accreditation_summary([])
        self.assertFalse(summary["ACCREDITATION_ENABLED"])
        self.assertIn("not official verification", summary["disclaimer"])


if __name__ == "__main__":
    unittest.main()
