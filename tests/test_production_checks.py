"""Offline tests using synthetic example balances. Not E*TRADE integration tests."""
import unittest
from production_checks import OrderBlocked, validate_cash_limit_order, calculate_budgeted_buy

ACCOUNT = {
    "accountMode": "CASH", "institutionType": "BROKERAGE",
    "accountStatus": "ACTIVE", "accountIdKey": "testkey", "accountId": "1234"
}
BALANCE = {
    "accountId": "1234",
    "Computed": {
        "settledCashForInvestment": 2000,
        "cashAvailableForInvestment": 2000,
        "cashBuyingPower": 2000,
    },
}
BUY = {
    "symbol": "NVDA", "side": "BUY", "quantity": 2,
    "order_type": "LIMIT", "limit_price": "100.00", "account_id": "testkey",
}


class ProductionChecksTest(unittest.TestCase):

    def test_fixed_budget_rounds_down_to_five_shares(self):
        result = calculate_budgeted_buy("NVDA", ACCOUNT, BALANCE, "185.00", "1000.00")
        self.assertEqual(result["shares"], 5)
        self.assertEqual(result["estimated_purchase_at_limit"], "925.00")
        self.assertEqual(result["cost_allowance_2pct"], "18.5000")
        self.assertEqual(result["order"]["quantity"], 5)
        self.assertFalse(result["orders_enabled"])
        self.assertTrue(result["broker_preview_required"])

    def test_fixed_budget_respects_available_cash(self):
        constrained = {
            **BALANCE, "Computed": {**BALANCE["Computed"],
                                    "cashAvailableForInvestment": "400.00"}
        }
        result = calculate_budgeted_buy("NVDA", ACCOUNT, constrained, "185.00")
        self.assertEqual(result["shares"], 2)

    def test_fixed_budget_insufficient_for_one_share(self):
        with self.assertRaises(OrderBlocked):
            calculate_budgeted_buy("NVDA", ACCOUNT, BALANCE, "990.00")

    def test_fixed_budget_no_fractional_shares(self):
        result = calculate_budgeted_buy("NVDA", ACCOUNT, BALANCE, "98.04")
        self.assertEqual(result["shares"], 10)
        self.assertLessEqual(float(result["estimated_purchase_at_limit"]), 1000)

    def test_fixed_budget_cannot_exceed_1000(self):
        with self.assertRaises(OrderBlocked):
            calculate_budgeted_buy("NVDA", ACCOUNT, BALANCE, "100.00", "1001.00")

    def test_fixed_budget_rejects_missing_balance(self):
        with self.assertRaises(OrderBlocked):
            calculate_budgeted_buy("NVDA", ACCOUNT, {}, "100.00")

    def test_fixed_budget_margin_account_fails(self):
        with self.assertRaises(OrderBlocked):
            calculate_budgeted_buy("NVDA", {**ACCOUNT, "accountMode": "MARGIN"},
                                   BALANCE, "100.00")

    def test_fixed_budget_rejects_invalid_price(self):
        for bad_price in ("0", "-1", "NaN", "1.234", "Infinity"):
            with self.subTest(bad_price=bad_price), self.assertRaises(OrderBlocked):
                calculate_budgeted_buy("NVDA", ACCOUNT, BALANCE, bad_price)

    def test_fixed_budget_preserves_low_budget_cap(self):
        result = calculate_budgeted_buy("NVDA", ACCOUNT, BALANCE, "50.00", "150.00")
        self.assertEqual(result["shares"], 2)
        self.assertEqual(result["requested_budget"], "150.00")

    def test_fixed_budget_rejects_boolean_budget(self):
        with self.assertRaises(OrderBlocked):
            calculate_budgeted_buy("NVDA", ACCOUNT, BALANCE, "50.00", True)

    def test_cash_buy_passes(self):
        self.assertEqual(
            validate_cash_limit_order(BUY, ACCOUNT, BALANCE)["maximum_notional"], "200.00"
        )

    def test_limit_only(self):
        with self.assertRaises(OrderBlocked):
            validate_cash_limit_order({**BUY, "order_type": "MARKET"}, ACCOUNT, BALANCE)

    def test_per_order_cap(self):
        with self.assertRaises(OrderBlocked):
            validate_cash_limit_order({**BUY, "quantity": 11}, ACCOUNT, BALANCE)

    def test_margin_account_blocked(self):
        with self.assertRaises(OrderBlocked):
            validate_cash_limit_order(BUY, {**ACCOUNT, "accountMode": "MARGIN"}, BALANCE)

    def test_missing_balance_blocked(self):
        with self.assertRaises(OrderBlocked):
            validate_cash_limit_order(BUY, ACCOUNT, {})

    def test_unsettled_funds_blocked(self):
        with self.assertRaises(OrderBlocked):
            validate_cash_limit_order(
                BUY, ACCOUNT, {
                    "Computed": {**BALANCE["Computed"], "settledCashForInvestment": 100}
                },
            )

    def test_short_sale_blocked(self):
        with self.assertRaises(OrderBlocked):
            validate_cash_limit_order(
                {**BUY, "side": "SELL"}, ACCOUNT, BALANCE, {"NVDA": 1}
            )

    def test_owned_sell(self):
        self.assertEqual(
            validate_cash_limit_order(
                {**BUY, "side": "SELL"}, ACCOUNT, BALANCE, {"NVDA": 2}
            )["side"], "SELL"
        )

    def test_boolean_quantity_rejected(self):
        with self.assertRaises(OrderBlocked):
            validate_cash_limit_order({**BUY, "quantity": True}, ACCOUNT, BALANCE)

    def test_missing_account_blocked(self):
        with self.assertRaises(OrderBlocked):
            validate_cash_limit_order(BUY, {}, BALANCE)

    def test_balance_account_mismatch(self):
        with self.assertRaises(OrderBlocked):
            validate_cash_limit_order(BUY, ACCOUNT, {**BALANCE, "accountId": "9999"})

    def test_nonfinite_balance_blocked(self):
        with self.assertRaises(OrderBlocked):
            validate_cash_limit_order(
                BUY, ACCOUNT,
                {"Computed": {**BALANCE["Computed"], "cashBuyingPower": "NaN"}},
            )

    def test_funds_buffer(self):
        with self.assertRaises(OrderBlocked):
            validate_cash_limit_order(
                BUY, ACCOUNT,
                {"Computed": {**BALANCE["Computed"], "settledCashForInvestment": 201}},
            )

    def test_sufficient_buffer(self):
        self.assertEqual(
            validate_cash_limit_order(
                BUY, ACCOUNT,
                {"Computed": {**BALANCE["Computed"], "settledCashForInvestment": 204}},
            )["maximum_notional"], "200.00"
        )
