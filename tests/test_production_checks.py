"""Offline tests using synthetic example balances. Not E*TRADE integration tests."""
import unittest
from production_checks import OrderBlocked, validate_cash_limit_order

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
