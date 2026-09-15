import unittest

from pipeline.pipelineUtils.document_profiles import (
    PROFILE_EBF,
    PROFILE_SUS,
    PROFILE_UNKNOWN,
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


if __name__ == "__main__":
    unittest.main()