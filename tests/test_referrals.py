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


class EmailInviteTests(unittest.TestCase):
    def test_outstanding_invites_hold_a_pass(self) -> None:
        """
        Reserving the pass at send time is the *only* thing bounding outbound
        mail to three addresses. If `invited` ever stops consuming a pass, the
        invite form becomes an open relay.
        """
        self.assertIn("invited", referrals._PASS_CONSUMING_STATUSES)

    def test_expired_invites_hand_the_pass_back(self) -> None:
        self.assertNotIn("expired", referrals._PASS_CONSUMING_STATUSES)

    def test_send_caps_are_a_nudge_not_a_campaign(self) -> None:
        self.assertEqual(referrals.MAX_INVITE_SENDS, 2)  # first mail + one reminder
        self.assertGreaterEqual(referrals.MAX_INVITE_SENDS_PER_DAY, referrals.MAX_INVITE_SENDS)

    def test_email_shape_check(self) -> None:
        for good in ("a@b.co", "first.last+tag@studio.example.com"):
            self.assertTrue(referrals._EMAIL_RE.match(good), good)
        for bad in ("", "nope", "a@b", "a b@c.com", "@c.com", "a@.com "):
            self.assertFalse(referrals._EMAIL_RE.match(bad), bad)

    def test_addresses_are_normalized_before_comparison(self) -> None:
        """Duplicate detection and invite matching both key off this."""
        self.assertEqual(referrals._normalize_email("  Friend@Studio.COM "), "friend@studio.com")


class PassAccountingTests(unittest.TestCase):
    """
    Regression cover for the three ways pass accounting went wrong in review.

    These assert on the queries the functions build rather than on live rows —
    the models need Postgres (JSONB, partial indexes), so a real session isn't
    available here. They still catch the specific mistakes that were made: a
    filter being dropped, or a lock going missing.
    """

    def test_signup_banner_tolerates_passes_held_by_invites(self) -> None:
        """
        The friend sent the last pass arrives on a code with zero free — held by
        their own invite. A strict check showed them no banner and then let them
        redeem anyway, which is backwards.
        """
        import inspect

        source = inspect.getsource(referrals.describe_code_for_signup)
        self.assertIn("_has_outstanding_invite", source)

    def test_pass_spending_paths_lock_the_code_row(self) -> None:
        """Without the lock, two requests both see the last pass and both take it."""
        import inspect

        for fn in (referrals.send_email_invite, referrals.redeem_referral_code):
            self.assertIn("_lock_code", inspect.getsource(fn), fn.__name__)

    def test_expired_invites_are_not_claimable(self) -> None:
        """
        `passes_used` stops counting an invite the moment it times out, so
        claiming one afterwards would revive a reservation already given back.
        """
        import inspect

        source = inspect.getsource(referrals._claim_pending_invite)
        self.assertIn("invite_expires_at", source)


if __name__ == "__main__":
    unittest.main()
