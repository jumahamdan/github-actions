import unittest

from shared_ci.common import PipelineError
from shared_ci.release import draft_from_prs, render


class ReleaseTests(unittest.TestCase):
    def test_public_notes_cannot_include_internal_section(self):
        notes = {"schema": 1, "product": "example", "version": "1.0.0", "build": "1", "sections": [
            {"title": "What's new", "body": "A clearer sign-in screen.", "audiences": ["public", "internal"]},
            {"title": "Operations", "body": "Internal recovery instructions.", "audiences": ["internal"]}]}
        self.assertNotIn("recovery", render(notes, "public"))
        self.assertIn("recovery", render(notes, "internal"))
        notes["sections"][0]["body"] = "TODO"
        with self.assertRaises(PipelineError):
            render(notes, "public")

    def test_draft_deduplicates_promotions_and_requires_changes(self):
        rows = [{"number": 1, "title": "Fix", "category": "Fixes"},
                {"number": 1, "title": "Fix", "category": "Fixes"},
                {"number": 2, "title": "Promote", "category": "Other", "promotion": True}]
        result = draft_from_prs(rows, previous_tag="v1", candidate_sha="a" * 40)
        self.assertEqual(result["categories"], {"Fixes": ["- Fix (#1)"]})
        with self.assertRaises(PipelineError):
            draft_from_prs([], previous_tag="v1", candidate_sha="a" * 40)


if __name__ == "__main__":
    unittest.main()
