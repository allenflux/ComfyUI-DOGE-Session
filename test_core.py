import copy
import json
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

    def test_target_zero_is_continuous_and_positive_limits_are_unchanged(self):
        for target in ("0", "0.00000001", "1000"):
            state = new_state("example", "session-1", target, self.snap)
            self.assertEqual(amount(state["target_doge"]), amount(target))
        for target in ("-1", "0.000000001", "1000.00000001", "NaN", "Infinity", "oops"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                new_state("example", "session-1", target, self.snap)

    def test_continuous_never_completes_even_above_positive_target_limit(self):
        state = new_state("example", "session-1", "0", self.snap)
        for balance, expected in (("2.5", "0"), ("12.5", "10"), ("10002.5", "10000")):
            phase, earned = progress(state, {**self.snap, "balance": balance})
            self.assertEqual(phase, "mining")
            self.assertEqual(earned, amount(expected))
            self.assertFalse(state["target_reached"])

    def test_continuous_payout_reset_preserves_earnings(self):
        initial = {**self.snap, "paid": "60"}
        state = new_state("example", "session-1", "0", initial)
        before_payout = {**initial, "balance": "32.5"}
        after_payout = {**initial, "balance": "2.5", "paid": "90"}
        self.assertEqual(progress(state, before_payout), ("mining", amount("30")))
        self.assertEqual(progress(state, after_payout), ("mining", amount("30")))
        self.assertEqual(state["initial_balance"], "2.5")
        self.assertEqual(state["initial_paid"], "60")

    def test_continuous_estimate_recovers_from_out_of_order_payout_data(self):
        state = new_state("example", "session-1", "0", self.snap)
        observations = (
            ({"balance": "32.5", "paid": "0"}, "30"),
            ({"balance": "32.5", "paid": "30"}, "60"),
            ({"balance": "2.5", "paid": "30"}, "30"),
            ({"balance": "0", "paid": "0"}, "0"),
            ({"balance": "2.6", "paid": "30"}, "30.1"),
        )
        for values, expected in observations:
            self.assertEqual(progress(state, {**self.snap, **values}), ("mining", amount(expected)))

    def test_continuous_restart_and_import_reuse_original_baseline(self):
        initial = {**self.snap, "paid": "60"}
        state = new_state("example", "session-1", "0", initial)
        after_payout = {**initial, "balance": "4.5", "paid": "90"}
        progress(state, after_payout)
        restarted = json.loads(json.dumps(state))
        imported = new_state("example", "session-1", "0", after_payout, export_baseline(state))
        for restored in (restarted, imported):
            validate_state(restored, "example", "session-1", "0", after_payout)
            self.assertEqual(restored["initial_balance"], "2.5")
            self.assertEqual(restored["initial_paid"], "60")
            self.assertEqual(progress(restored, after_payout), ("mining", amount("32")))
        with self.assertRaises(ValueError):
            new_state("example", "session-1", "10", after_payout, export_baseline(state))

    def test_continuous_still_rejects_invalid_pool_amounts(self):
        state = new_state("example", "session-1", "0", self.snap)
        for field in ("balance", "paid"):
            for value in ("NaN", "Infinity", "-1", "oops"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    progress(state, {**self.snap, field: value})


if __name__ == "__main__":
    unittest.main()
