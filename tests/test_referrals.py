import unittest

from app.services import referrals


class ReferralCodeTests(unittest.TestCase):
    def test_code_avoids_ambiguous_characters(self) -> None:
        """Codes get read down a phone line, so O/0 and I/1/L must not appear."""
        for _ in range(200):
            code = referrals._generate_code()
            self.assertEqual(len(code), referrals._CODE_LENGTH)
            self.assertTrue(set(code) <= set(referrals._CODE_ALPHABET))
            self.assertFalse(set(code) & set("O0I1L"))

    def test_link_points_at_signup_with_the_code(self) -> None:
        link = referrals.build_referral_link("ABCD2345")
        self.assertTrue(link.endswith("/signup?ref=ABCD2345"))


class ReferralTermsTests(unittest.TestCase):
    def test_guest_pass_beats_the_standard_trial(self) -> None:
        """
        The whole promise of a guest pass is that it is better than what anyone
        gets by walking in off the street. If billing's standard trial is ever
        raised past the pass, the panel starts advertising a downgrade — so the
        two numbers are pinned together here rather than left to chance.
        """
        from app.api.routes.billing import TRIAL_DAYS

        self.assertGreater(referrals.PASS_TRIAL_DAYS, TRIAL_DAYS)

    def test_terms_match_the_constants_they_describe(self) -> None:
        """The API serves `terms` verbatim; the UI promises exactly this."""
        self.assertEqual(
            referrals.REFERRAL_TERMS["pass_trial_days"], referrals.PASS_TRIAL_DAYS
        )
        self.assertEqual(
            referrals.REFERRAL_TERMS["invitee_signup_credits"],
            referrals.INVITEE_SIGNUP_CREDITS,
        )
        self.assertEqual(
            referrals.REFERRAL_TERMS["referrer_reward_credits"],
            referrals.REFERRER_REWARD_CREDITS,
        )
        self.assertEqual(referrals.REFERRAL_TERMS["passes_per_user"], referrals.DEFAULT_PASSES)

    def test_voided_referrals_return_their_pass(self) -> None:
        self.assertNotIn("void", referrals._PASS_CONSUMING_STATUSES)


if __name__ == "__main__":
    unittest.main()
