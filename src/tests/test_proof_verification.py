import unittest

from proof_verification import combine_proof_verdict


class CombineProofVerdictTests(unittest.TestCase):
    def setUp(self):
        self.valid_evidence = {
            "detected_code": "ab-123",
            "item_matches": True,
            "product_condition": "GOOD",
            "product_issues": [],
            "confidence": "HIGH",
            "reasoning": "The item matches the agreed model and appears undamaged.",
        }

    def test_releases_only_when_product_matches_and_looks_good(self):
        verdict = combine_proof_verdict(self.valid_evidence, "AB123")

        self.assertEqual(verdict["decision"], "RELEASE")
        self.assertEqual(verdict["product_condition"], "GOOD")
        self.assertEqual(verdict["product_issues"], [])

    def test_potential_defect_requires_buyer_review(self):
        evidence = {
            **self.valid_evidence,
            "product_condition": "ISSUES",
            "product_issues": ["cracked screen"],
        }

        verdict = combine_proof_verdict(evidence, "AB123")

        self.assertEqual(verdict["decision"], "ASK_BUYER")
        self.assertIn("cracked screen", verdict["reason"])

    def test_product_mismatch_requires_buyer_review(self):
        evidence = {**self.valid_evidence, "item_matches": False}

        verdict = combine_proof_verdict(evidence, "AB123")

        self.assertEqual(verdict["decision"], "ASK_BUYER")
        self.assertIn("does not clearly match", verdict["reason"])

    def test_unclear_condition_requires_buyer_review(self):
        evidence = {**self.valid_evidence, "product_condition": "UNCLEAR"}

        verdict = combine_proof_verdict(evidence, "AB123")

        self.assertEqual(verdict["decision"], "ASK_BUYER")

    def test_missing_condition_fails_closed(self):
        evidence = dict(self.valid_evidence)
        del evidence["product_condition"]

        verdict = combine_proof_verdict(evidence, "AB123")

        self.assertEqual(verdict["decision"], "ASK_BUYER")
        self.assertEqual(verdict["product_condition"], "UNCLEAR")


if __name__ == "__main__":
    unittest.main()
