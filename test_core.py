import copy
import unittest

from core import amount, export_baseline, new_state, progress, validate_state


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.snap = {"wallet": "example", "uuid": "account-1", "balance": "2.5", "paid": "0"}
        self.state = new_state("example", "session-1", "10", self.snap)

    def test_existing_balance_does_not_count_towards_new_target(self):
        phase, earned = progress(self.state, {**self.snap, "balance": "10"})
        self.assertEqual(phase, "mining")
        self.assertEqual(earned, amount("7.5"))

    def test_exact_target_and_overshoot_stop(self):
        for balance in ("12.5", "12.50001"):
            phase, earned = progress(copy.deepcopy(self.state), {**self.snap, "balance": balance})
            self.assertEqual(phase, "target_reached")
            self.assertGreaterEqual(earned, amount("10"))

    def test_completed_session_never_restarts_after_payout_or_restart(self):
        self.state.update(target_reached=True, earned_doge="10.01")
        self.assertEqual(progress(self.state, {**self.snap, "balance": "0", "paid": "30"})[0], "target_reached")

    def test_regression_or_uncertain_payout_stops_for_review(self):
        progress(self.state, {**self.snap, "balance": "5"})
        for snap in ({**self.snap, "balance": "4.99"}, {**self.snap, "balance": "8", "paid": "3"}):
            with self.assertRaises(ValueError):
                progress(self.state, snap)

    def test_late_previous_payout_cannot_be_miscounted_as_new_earnings(self):
        with self.assertRaises(ValueError):
            progress(self.state, {**self.snap, "balance": "0", "paid": "50"})

    def test_imported_baseline_is_same_for_other_instances(self):
        other_snap = {**self.snap, "balance": "8"}
        other = new_state("example", "session-1", "10", other_snap, export_baseline(self.state))
        self.assertEqual(other["initial_balance"], "2.5")
        self.assertEqual(progress(other, {**other_snap, "balance": "12.5"})[0], "target_reached")

    def test_wrong_wallet_target_and_campaign_cannot_replace_baseline(self):
        for wallet, campaign, target in (("other", "session-1", "10"), ("example", "session-2", "10"), ("example", "session-1", "20")):
            with self.assertRaises(ValueError):
                validate_state(self.state, wallet, campaign, target, self.snap)

    def test_invalid_amounts_rejected(self):
        for value in ("NaN", "Infinity", "-1", "oops"):
            with self.assertRaises(ValueError):
                amount(value)


if __name__ == "__main__":
    unittest.main()
