"""Offline tests: invented sample data only; no E*TRADE calls."""
import unittest

from live_cash_preflight import (
    CashOrderBlocked, assess_cash_buy, whole_share_estimate,
)


ACCOUNT = {
    "accountIdKey": "synthetic-key",
    "accountId": "1234",
    "accountMode": "CASH",
    "accountStatus": "ACTIVE",
    "institutionType": "BROKERAGE",
    "accountType": "INDIVIDUAL",
}
BALANCE = {
    "accountId": "1234",
    "accountMode": "CASH",
    "institutionType": "BROKERAGE",
    "settledCashForInvestment": "1200.00",
    "cashAvailableForInvestment": "1200.00",
    "marginBuyingPower": "1000000.00",
}


def check(**overrides):
    args = dict(
        symbol="NVDA", limit_price="185.00", budget="1000.00",
        account=dict(ACCOUNT), balance=dict(BALANCE),
        known_account_keys={"synthetic-key"},
        account_permissions_verified=True,
        open_orders_verified=True,
        outstanding_buy_reservations="0",
    )
    args.update(overrides)
    return assess_cash_buy(**args)


class CashOnlyPreflightTests(unittest.TestCase):
    def test_budget_rounds_down_to_five_shares(self):
        est = whole_share_estimate("185.00", "1000.00")
        self.assertEqual(est["shares"], 5)
        self.assertEqual(est["notional"], 925)
        self.assertEqual(est["reserved"], 943.50)

    def test_production_reader_does_not_place_orders(self):
        r = check()
        self.assertEqual(r["estimated_shares"], 5)
        self.assertEqual(r["estimated_with_2pct_allowance"], "943.50")
        self.assertEqual(r["blockers"], [])
        self.assertTrue(r["ready_for_broker_preview"])
        self.assertFalse(r["broker_preview_performed"])
        self.assertFalse(r["orders_enabled"])
        self.assertFalse(r["order_submitted"])
        self.assertTrue(r["requires_separate_trade_confirmation"])
        self.assertNotIn("order", r)

    def test_actual_user_case_list_cash_balance_margin_stays_blocked(self):
        r = check(balance={**BALANCE, "accountMode": "MARGIN"})
        self.assertIn("cash_mode_conflict_or_unverified", r["blockers"])
        self.assertFalse(r["ready_for_broker_preview"])
        self.assertFalse(r["orders_enabled"])

    def test_budget_estimate_ignores_large_margin_buying_power(self):
        b = {**BALANCE,
             "settledCashForInvestment": "200",
             "cashAvailableForInvestment": "200",
             "marginBuyingPower": "500000"}
        r = check(balance=b)
        self.assertEqual(r["estimated_shares"], 5)
        self.assertIn("insufficient_confirmed_cash_plus_buffer", r["blockers"])

    def test_unknown_cash_balance_denied(self):
        b = {**BALANCE, "settledCashForInvestment": None}
        r = check(balance=b)
        self.assertIn("missing_or_invalid_settledCashForInvestment", r["blockers"])
        self.assertIsNone(r["spendable_cash_after_reservations"])

    def test_unknown_account_mode_denied(self):
        self.assertIn(
            "cash_mode_conflict_or_unverified",
            check(balance={**BALANCE, "accountMode": None})["blockers"],
        )

    def test_list_margin_denied_even_if_balance_cash(self):
        self.assertIn(
            "cash_mode_conflict_or_unverified",
            check(account={**ACCOUNT, "accountMode": "MARGIN"})["blockers"],
        )

    def test_explicit_broker_permission_confirmation_required(self):
        r = check(account_permissions_verified=False)
        self.assertIn("broker_cash_only_permissions_not_verified", r["blockers"])

    def test_open_order_verification_required(self):
        r = check(open_orders_verified=False, outstanding_buy_reservations=None)
        self.assertIn("open_order_reservations_not_verified", r["blockers"])
        self.assertIn("outstanding_buy_reservations_unknown", r["blockers"])

    def test_pending_orders_subtract_from_cash(self):
        r = check(outstanding_buy_reservations="300")
        self.assertEqual(r["spendable_cash_after_reservations"], "900.00")
        self.assertIn("insufficient_confirmed_cash_plus_buffer", r["blockers"])

    def test_account_balance_mismatch_denied(self):
        r = check(balance={**BALANCE, "accountId": "9999"})
        self.assertIn("account_balance_identity_not_verified", r["blockers"])

    def test_missing_balance_id_denied(self):
        b = dict(BALANCE)
        del b["accountId"]
        r = check(balance=b)
        self.assertIn("account_balance_identity_not_verified", r["blockers"])

    def test_explicit_account_allowlist_required(self):
        with self.assertRaises(CashOrderBlocked):
            check(known_account_keys=set())

    def test_institution_mismatch_denied(self):
        r = check(balance={**BALANCE, "institutionType": None})
        self.assertIn("brokerage_classification_unverified", r["blockers"])

    def test_inactive_account_denied(self):
        r = check(account={**ACCOUNT, "accountStatus": "CLOSED"})
        self.assertIn("account_not_confirmed_active", r["blockers"])

    def test_budget_does_not_allow_fractional_shares(self):
        self.assertEqual(whole_share_estimate("98.04", "1000.00")["shares"], 9)

    def test_invalid_price_or_budget_fails(self):
        for price in ("NaN", "Infinity", "0", "-3", "1.234"):
            with self.subTest(price=price), self.assertRaises(CashOrderBlocked):
                whole_share_estimate(price, "1000")
        for budget in ("NaN", "1000.01", "0", "-1", "1.001"):
            with self.subTest(budget=budget), self.assertRaises(CashOrderBlocked):
                whole_share_estimate("10.00", budget)

    def test_no_shares_if_price_exceeds_budget(self):
        with self.assertRaises(CashOrderBlocked):
            whole_share_estimate("999.00", "1000.00")

    def test_numeric_account_spoofing_rejected(self):
        with self.assertRaises(CashOrderBlocked):
            check(known_account_keys={"other"})

    def test_invalid_reservations_block(self):
        r = check(outstanding_buy_reservations="not-a-number")
        self.assertIn("outstanding_buy_reservations_invalid", r["blockers"])

    def test_no_liquidity_from_unsettled_cash(self):
        b = {**BALANCE,
             "settledCashForInvestment": "0.00",
             "cashAvailableForInvestment": "10000.00",
             "unSettledCashForInvestment": "10000.00"}
        r = check(balance=b)
        self.assertIn("insufficient_confirmed_cash_plus_buffer", r["blockers"])


if __name__ == "__main__":
    unittest.main()
