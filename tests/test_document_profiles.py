import unittest

from pipeline.pipelineUtils.document_profiles import (
    PROFILE_EBF,
    PROFILE_SUS,
    PROFILE_UNKNOWN,
    normalize_profile_result,
    profile_for_blob,
)


class DocumentProfileTests(unittest.TestCase):
    def test_routes_sus_filename(self):
        self.assertEqual(
            profile_for_blob("bronze/1012120036_SUS_transcript1_redacted.pdf"),
            PROFILE_SUS,
        )

    def test_routes_ebf_filename(self):
        self.assertEqual(
            profile_for_blob("1013061927_EBF_transcript1_redacted.pdf"),
            PROFILE_EBF,
        )

    def test_matching_is_case_insensitive(self):
        self.assertEqual(profile_for_blob("sample_ebf_file.pdf"), PROFILE_EBF)

    def test_partial_tokens_do_not_match(self):
        self.assertEqual(profile_for_blob("1012120036_SUSAN_transcript.pdf"), PROFILE_UNKNOWN)

    def test_unknown_files_are_explicit(self):
        self.assertEqual(profile_for_blob("transcript_without_profile.pdf"), PROFILE_UNKNOWN)

    def test_profile_normalization_preserves_extracted_institution(self):
        result = normalize_profile_result(
            {
                "institution_context": ["Braintree High School"],
                "courses": [{"institution": "Braintree High School"}],
            },
            PROFILE_SUS,
        )

        self.assertEqual(result["institution_context"], ["Braintree High School"])
        self.assertEqual(result["courses"][0]["institution"], "Braintree High School")

    def test_normalization_corrects_verified_issuer_ocr_variant(self):
        result = normalize_profile_result(
            {
                "institution_details": [{"name": "École Française Internationale de Djedda"}],
                "courses": [{"institution": "École Française Internationale de Djedda"}],
            },
            PROFILE_EBF,
        )

        expected = "École Française Internationale de Djeddah"
        self.assertEqual(result["institution_details"][0]["name"], expected)
        self.assertEqual(result["courses"][0]["institution"], expected)

    def test_normalization_corrects_repeated_letter_issuer_variant(self):
        result = normalize_profile_result(
            {"courses": [{"institution": "École Française Internationale de Ddeddah"}]},
            PROFILE_EBF,
        )

        self.assertEqual(
            result["courses"][0]["institution"],
            "École Française Internationale de Djeddah",
        )


if __name__ == "__main__":
    unittest.main()