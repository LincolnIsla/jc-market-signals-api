"""Mocked read-only production integration tests; no brokerage network calls."""
import os
import unittest
from unittest.mock import patch
from flask import Flask, request
from production_readonly import create_reader_blueprint, get_balance, get_positions

VALID = {
    "ETRADE_PRODUCTION_READONLY_ENABLED": "true",
    "ETRADE_PRODUCTION_CONSUMER_KEY": "testkey",
    "ETRADE_PRODUCTION_CONSUMER_SECRET": "testsecret",
}
ORIGIN = "https://jc-market-signals-api.onrender.com"

class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
    def json(self):
        return self.payload

class FakeOAuth:
    calls = []
    def __init__(self, *args, **kwargs):
        self.calls.append(("init", kwargs))
    def fetch_request_token(self, url):
        self.calls.append(("request", url))
        return {"oauth_token": "request", "oauth_token_secret": "secret"}
    def fetch_access_token(self, url):
        self.calls.append(("access", url))
        return {"oauth_token": "access", "oauth_token_secret": "access-secret"}
    def get(self, url, *, params=None, headers=None, timeout=None, allow_redirects=None):
        self.calls.append(("get", url, params))
        assert allow_redirects is False
        assert url.startswith("https://api.etrade.com/")
        assert "/orders/" not in url, "production trading endpoint MUST NOT be reachable"
        if url.endswith("/v1/accounts/list.json"):
            return FakeResponse({"AccountListResponse": {"Accounts": {"Account": [{
                "accountIdKey": "safe-account", "accountId": "123456789",
                "accountStatus": "ACTIVE", "accountMode": "CASH",
                "institutionType": "BROKERAGE", "accountType": "BROKER"
            }]}}})
        if url.endswith("/balance.json"):
            assert params == {"instType": "BROKERAGE", "realTimeNAV": "true"}
            return FakeResponse({"BalanceResponse": {"accountMode": "CASH", "institutionType": "BROKERAGE", "Computed": {
                "settledCashForInvestment": 500, "cashAvailableForInvestment": 1000,
                "cashBuyingPower": 1000
            }}})
        if url.endswith("/portfolio.json"):
            return FakeResponse({"PortfolioResponse": {"AccountPortfolio": [{
                "accountId": "123456789", "Position": [
                    {"Product": {"symbol": "NVDA", "securityType": "EQ"},
                     "quantity": 2, "positionType": "LONG", "marketValue": 300}
                ]
            }]}})
        raise AssertionError("Unlisted production path: " + url)

class ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        FakeOAuth.calls = []
        self.environ = patch.dict(os.environ, VALID)
        self.environ.start()
        self.app = Flask(__name__)
        self.app.register_blueprint(create_reader_blueprint(
            lambda: request.headers.get("Authorization") == "Bearer ADMINTEST",
            oauth_class=FakeOAuth
        ))
        self.client = self.app.test_client()
        self.auth = {"Authorization": "Bearer ADMINTEST"}

    def tearDown(self):
        self.environ.stop()

    def connect(self):
        r = self.client.post("/production-readonly/connect/start", headers={
            **self.auth, "Origin": ORIGIN
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn("us.etrade.com", r.json["authorize_url"])
        r = self.client.post("/production-readonly/connect/finish", headers={
            **self.auth, "Origin": ORIGIN
        }, json={"verifier": "12345"})
        self.assertEqual(r.status_code, 200)

    def test_disabled_without_flag(self):
        with patch.dict(os.environ, {"ETRADE_PRODUCTION_READONLY_ENABLED": "false"}):
            r = self.client.get("/production-readonly/status", headers=self.auth)
            self.assertEqual(r.status_code, 404)

    def test_admin_auth_required(self):
        r = self.client.get("/production-readonly/status")
        self.assertEqual(r.status_code, 401)

    def test_csrf_origin_required(self):
        r = self.client.post("/production-readonly/connect/start", headers=self.auth)
        self.assertEqual(r.status_code, 403)

    def test_status_never_enables_order_placement(self):
        r = self.client.get("/production-readonly/status", headers=self.auth)
        self.assertEqual(r.json["orders_enabled"], False)
        self.assertEqual(r.json["connected"], False)

    def test_oauth_and_account_list(self):
        self.connect()
        r = self.client.get("/production-readonly/accounts", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["accounts"][0]["accountEnding"], "6789")
        self.assertNotIn("accountId", r.json["accounts"][0])

    def test_balance_is_broker_cash_not_margin_power(self):
        self.connect()
        r = self.client.get("/production-readonly/accounts/safe-account/balance", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["settledCashForInvestment"], 500)
        self.assertNotIn("marginBuyingPower", r.json)

    def test_positions_and_no_order_calls(self):
        self.connect()
        r = self.client.get("/production-readonly/accounts/safe-account/positions", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["accounts"][0]["positions"][0]["symbol"], "NVDA")
        for method, *args in FakeOAuth.calls:
            self.assertIn(method, ("init", "request", "access", "get"))

    def test_invalid_account_rejected_without_dynamic_broker_url(self):
        self.connect()
        r = self.client.get("/production-readonly/accounts/other/balance", headers=self.auth)
        self.assertEqual(r.status_code, 409)
        self.assertFalse(any("/other/" in str(c) for c in FakeOAuth.calls))

    def test_post_orders_route_does_not_exist(self):
        self.connect()
        r = self.client.post("/production-readonly/orders/place", headers={
            **self.auth, "Origin": ORIGIN
        }, json={"symbol": "NVDA"})
        self.assertEqual(r.status_code, 404)

    def test_balance_missing_computed_is_rejected(self):
        with self.assertRaises(ValueError):
            get_balance({"BalanceResponse": {}})

    def test_positions_malformed_is_rejected(self):
        with self.assertRaises(ValueError):
            get_positions({"PortfolioResponse": "not a dictionary"})

if __name__ == "__main__":
    unittest.main()
