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

    def test_status_output_is_directly_below_button(self):
        page = self.client.get("/production-readonly/connect-ui")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        status = html.find('<button id="status">Check status</button>')
        output = html.find('<pre id="output" role="status"')
        authorize = html.find('<button id="start">')
        self.assertGreaterEqual(status, 0)
        self.assertGreater(output, status)
        self.assertGreater(authorize, output)
        self.assertIn("Connection messages appear here", html)

    def test_static_buttons_script_loads_without_credentials(self):
        with patch.dict(os.environ, {
            "ETRADE_PRODUCTION_READONLY_ENABLED": "false",
            "ETRADE_PRODUCTION_CONSUMER_KEY": "",
            "ETRADE_PRODUCTION_CONSUMER_SECRET": "",
        }):
            page = self.client.get("/production-readonly/connect-ui")
            self.assertEqual(page.status_code, 200)
            self.assertIn(b'<script defer src="/production-readonly/connect-ui.js"></script>', page.data)
            self.assertNotIn(b'<script>', page.data)
            self.assertIn("script-src 'self'", page.headers.get("Content-Security-Policy"))
            self.assertIn(b"Loading page controls", page.data)
            script = self.client.get("/production-readonly/connect-ui.js")
            self.assertEqual(script.status_code, 200)
            self.assertIn("javascript", script.mimetype)
            self.assertIn(b"Controls loaded", script.data)
            self.assertIn(b"$('status').onclick", script.data)
            self.assertNotIn(b"testsecret", script.data)
            blocked = self.client.get("/production-readonly/status", headers=self.auth)
            self.assertEqual(blocked.status_code, 200)
            self.assertFalse(blocked.json["ready"])
            self.assertEqual(set(blocked.json["missing_settings"]), set((
                "ETRADE_PRODUCTION_READONLY_ENABLED",
                "ETRADE_PRODUCTION_CONSUMER_KEY",
                "ETRADE_PRODUCTION_CONSUMER_SECRET",
            )))

    def test_static_setup_page_available_without_enable_flag_or_keys(self):
        with patch.dict(os.environ, {
            "ETRADE_PRODUCTION_READONLY_ENABLED": "false",
            "ETRADE_PRODUCTION_CONSUMER_KEY": "",
            "ETRADE_PRODUCTION_CONSUMER_SECRET": ""
        }):
            response = self.client.get("/production-readonly/connect-ui")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"ETRADE_PRODUCTION_READONLY_ENABLED", response.data)
            self.assertIn(b"READ ONLY", response.data)
            self.assertNotIn(b"testsecret", response.data)
            status = self.client.get("/production-readonly/status", headers=self.auth)
            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.json.get("ready", True))
            self.assertFalse(status.json.get("orders_enabled", False))
            oauth_start = self.client.post(
                "/production-readonly/connect/start",
                headers={**self.auth, "Origin": ORIGIN}
            )
            self.assertEqual(oauth_start.status_code, 404)

    def test_disabled_without_flag(self):
        with patch.dict(os.environ, {"ETRADE_PRODUCTION_READONLY_ENABLED": "false"}):
            r = self.client.get("/production-readonly/status", headers=self.auth)
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json["ready"])
            self.assertEqual(r.json["missing_settings"], ["ETRADE_PRODUCTION_READONLY_ENABLED"])
            refused = self.client.get("/production-readonly/accounts", headers=self.auth)
            self.assertEqual(refused.status_code, 404)

    def test_missing_one_production_key_is_diagnosed_without_leaking_values(self):
        with patch.dict(os.environ, {"ETRADE_PRODUCTION_CONSUMER_SECRET": ""}):
            r = self.client.get("/production-readonly/status", headers=self.auth)
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json["ready"])
            self.assertEqual(r.json["missing_settings"], ["ETRADE_PRODUCTION_CONSUMER_SECRET"])
            self.assertNotIn("testkey", r.get_data(as_text=True))
            self.assertNotIn("testsecret", r.get_data(as_text=True))
            unauthenticated = self.client.get("/production-readonly/status")
            self.assertEqual(unauthenticated.status_code, 401)
            self.assertNotIn("missing_settings", unauthenticated.get_data(as_text=True))

    def test_correct_flag_both_keys_readonly_ready(self):
        r = self.client.get("/production-readonly/status", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json["ready"])
        self.assertEqual(r.json["missing_settings"], [])
        self.assertFalse(r.json["connected"])
        self.assertFalse(r.json["orders_enabled"])

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


    def test_size_buy_rounds_down_and_never_trades(self):
        self.connect()
        url = "/production-readonly/accounts/safe-account/size-buy"
        response = self.client.get(url, headers=self.auth,
            query_string={"symbol": "NVDA", "limit_price": "185.00", "budget": "1000.00"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["shares"], 2)  # only $500 settled in mocked account
        self.assertEqual(response.json["estimated_purchase_at_limit"], "370.00")
        self.assertEqual(response.json["cost_allowance_2pct"], "7.4000")
        self.assertFalse(response.json["orders_enabled"])
        self.assertFalse(response.json["broker_preview_performed"])
        self.assertFalse(response.json["broker_order_submitted"])
        self.assertTrue(response.json["requires_explicit_confirmation"])
        self.assertFalse(any("/orders/" in str(item) for item in FakeOAuth.calls))

    def test_size_buy_rejects_expensive_shares(self):
        self.connect()
        response = self.client.get(
            "/production-readonly/accounts/safe-account/size-buy",
            headers=self.auth,
            query_string={"symbol": "NVDA", "limit_price": "600.00", "budget": "1000.00"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["orders_enabled"])

    def test_size_buy_requires_admin_auth(self):
        response = self.client.get(
            "/production-readonly/accounts/safe-account/size-buy",
            query_string={"symbol": "NVDA", "limit_price": "100.00"}
        )
        self.assertEqual(response.status_code, 401)

    def test_size_buy_requires_cash_account_and_cash_balance(self):
        self.connect()
        with patch("production_readonly.get_balance", return_value={
            "accountMode": "MARGIN", "institutionType": "BROKERAGE",
            "settledCashForInvestment": 500,
            "cashAvailableForInvestment": 1000, "cashBuyingPower": 1000
        }):
            response = self.client.get(
                "/production-readonly/accounts/safe-account/size-buy",
                headers=self.auth,
                query_string={"symbol": "NVDA", "limit_price": "100.00"}
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("CASH", response.json["error"])

    def test_size_buy_rejects_too_large_budget(self):
        self.connect()
        response = self.client.get(
            "/production-readonly/accounts/safe-account/size-buy",
            headers=self.auth,
            query_string={"symbol": "NVDA", "limit_price": "100.00", "budget": "1000.01"}
        )
        self.assertEqual(response.status_code, 400)

    def test_balance_missing_computed_is_rejected(self):
        with self.assertRaises(ValueError):
            get_balance({"BalanceResponse": {}})

    def test_positions_malformed_is_rejected(self):
        with self.assertRaises(ValueError):
            get_positions({"PortfolioResponse": "not a dictionary"})

if __name__ == "__main__":
    unittest.main()
