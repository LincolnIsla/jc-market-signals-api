"""Opt-in E*TRADE production ACCOUNT READER: no live order placement.

This module can only make authenticated GETs to a fixed allowlist of
E*TRADE production account/portfolio/balance paths. It never imports
or calls the existing sandbox order-placement code. The E*TRADE OAuth
consent is not restricted to read-only: safeguards apply in our code.
"""
import os
import secrets
import time
from threading import RLock
from urllib.parse import quote, urlencode

from flask import Blueprint, jsonify, request
from requests_oauthlib import OAuth1Session

API = "https://api.etrade.com"
APP_ORIGIN = "https://jc-market-signals-api.onrender.com"
AUTH_URL = "https://us.etrade.com/e/t/etws/authorize"
FLAG = "ETRADE_PRODUCTION_READONLY_ENABLED"


def objects(value):
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def get_accounts(body):
    response = body.get("AccountListResponse")
    if not isinstance(response, dict) or not isinstance(response.get("Accounts"), dict):
        raise ValueError("Unexpected E*TRADE account response")
    return objects(response["Accounts"].get("Account", []))


def get_balance(body):
    response = body.get("BalanceResponse")
    if not isinstance(response, dict):
        raise ValueError("Unexpected E*TRADE balance response")
    computed = response.get("Computed", response.get("computedBalance"))
    if not isinstance(computed, dict):
        raise ValueError("E*TRADE did not return computed cash balances")
    return {
        "accountMode": response.get("accountMode"),
        "institutionType": response.get("institutionType"),
        "asOfDate": response.get("asOfDate"),
        "settledCashForInvestment": computed.get("settledCashForInvestment"),
        "cashAvailableForInvestment": computed.get("cashAvailableForInvestment"),
        "cashBuyingPower": computed.get("cashBuyingPower"),
        "unSettledCashForInvestment": computed.get("unSettledCashForInvestment"),
    }


def get_positions(body):
    response = body.get("PortfolioResponse")
    if not isinstance(response, dict):
        raise ValueError("Unexpected E*TRADE portfolio response")
    portfolios = objects(response.get("AccountPortfolio", []))
    return {
        "accounts": [{
            "accountEnding": str(item.get("accountId", ""))[-4:],
            "positions": [{
                "symbol": pos.get("Product", {}).get("symbol") if isinstance(pos.get("Product"), dict) else None,
                "securityType": pos.get("Product", {}).get("securityType") if isinstance(pos.get("Product"), dict) else None,
                "quantity": pos.get("quantity"),
                "positionType": pos.get("positionType"),
                "marketValue": pos.get("marketValue")
            } for pos in objects(item.get("Position", []))],
            "nextPageNo": item.get("nextPageNo")
        } for item in portfolios]
    }


def create_reader_blueprint(admin_authorized, oauth_class=OAuth1Session):
    bp = Blueprint("production_reader", __name__, url_prefix="/production-readonly")
    lock = RLock()
    state = {"pending": None, "access": None}
    call_times = []

    def enabled():
        return os.getenv(FLAG) == "true" and bool(
            os.getenv("ETRADE_PRODUCTION_CONSUMER_KEY")
            and os.getenv("ETRADE_PRODUCTION_CONSUMER_SECRET")
        )

    def keys():
        return (
            os.environ["ETRADE_PRODUCTION_CONSUMER_KEY"],
            os.environ["ETRADE_PRODUCTION_CONSUMER_SECRET"],
        )

    @bp.before_request
    def guard():
        if not enabled():
            return jsonify(error="Production account reader is disabled"), 404
        if not admin_authorized():
            return jsonify(error="Administrator authentication required"), 401
        if request.method not in ("GET", "HEAD"):
            # Origin header required, including for same-origin POSTs.
            if request.headers.get("Origin") != APP_ORIGIN:
                return jsonify(error="Request origin not permitted"), 403
        return None

    @bp.after_request
    def safety_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def production_get(path, params=None):
        with lock:
            access = dict(state["access"]) if state["access"] else None
        if not access:
            raise ValueError("Authorize E*TRADE production before reading accounts")
        # Only these three account resources. No dynamic URL, method or host.
        if not (path == "/v1/accounts/list.json"
                or (path.startswith("/v1/accounts/")
                    and (path.endswith("/balance.json") or path.endswith("/portfolio.json"))
                    and path.count("/") == 4)):
            raise ValueError("Disallowed production account endpoint")
        key, secret = keys()
        oauth = oauth_class(
            key, client_secret=secret,
            resource_owner_key=access["token"],
            resource_owner_secret=access["secret"],
        )
        response = oauth.get(
            API + path, params=params,
            headers={"Accept": "application/json"},
            timeout=15, allow_redirects=False
        )
        if response.status_code == 204 and path.endswith("/portfolio.json"):
            return {"PortfolioResponse": {"AccountPortfolio": []}}
        if response.status_code != 200:
            raise ValueError("E*TRADE production account read failed")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected E*TRADE response")
        return data

    def accounts():
        return get_accounts(production_get("/v1/accounts/list.json"))

    def validate_account(account_key):
        if not account_key or len(account_key) > 128:
            raise ValueError("Invalid account key")
        found = next((
            a for a in accounts() if a.get("accountIdKey") == account_key
            and a.get("accountStatus") == "ACTIVE"
            and a.get("institutionType", "BROKERAGE") == "BROKERAGE"
        ), None)
        if not found:
            raise ValueError("Account not in current active E*TRADE brokerage account list")
        return found

    @bp.get("/status")
    def status():
        with lock:
            connected = state["access"] is not None
        return jsonify(mode="read-only", environment="production",
                       connected=connected, orders_enabled=False)

    @bp.post("/connect/start")
    def start():
        now = time.time()
        with lock:
            call_times[:] = [t for t in call_times if now - t < 60]
            if len(call_times) >= 5:
                return jsonify(error="Too many authorization attempts"), 429
            call_times.append(now)
            state["pending"] = None
            state["access"] = None
        key, secret = keys()
        try:
            client = oauth_class(key, client_secret=secret, callback_uri="oob")
            data = client.fetch_request_token(API + "/oauth/request_token")
            token, token_secret = data["oauth_token"], data["oauth_token_secret"]
            if not token or not token_secret:
                raise ValueError("Missing OAuth token")
            with lock:
                state["pending"] = (token, token_secret, time.time())
            return jsonify(
                environment="production", mode="read-only",
                orders_enabled=False,
                authorize_url=AUTH_URL + "?" + urlencode({"key": key, "token": token})
            )
        except Exception:
            return jsonify(error="Unable to start production authorization"), 502

    @bp.post("/connect/finish")
    def finish():
        body = request.get_json(silent=True)
        code = body.get("verifier") if isinstance(body, dict) else None
        if not isinstance(code, str) or not 1 <= len(code.strip()) <= 128:
            return jsonify(error="Enter a valid E*TRADE verification code"), 400
        with lock:
            pending = state["pending"]
            state["pending"] = None
        if not pending or time.time() - pending[2] > 300:
            return jsonify(error="E*TRADE authorization expired; start again"), 400
        key, secret = keys()
        try:
            client = oauth_class(
                key, client_secret=secret,
                resource_owner_key=pending[0],
                resource_owner_secret=pending[1],
                verifier=code.strip()
            )
            data = client.fetch_access_token(API + "/oauth/access_token")
            token, token_secret = data["oauth_token"], data["oauth_token_secret"]
            if not token or not token_secret:
                raise ValueError("Missing OAuth access token")
            with lock:
                state["access"] = {"token": token, "secret": token_secret}
            return jsonify(environment="production", mode="read-only",
                           connected=True, orders_enabled=False)
        except Exception:
            return jsonify(error="Unable to finish production authorization"), 502

    @bp.get("/accounts")
    def account_list():
        try:
            rows = accounts()
            return jsonify(environment="production", mode="read-only",
                           orders_enabled=False,
                           accounts=[{
                               "accountIdKey": a.get("accountIdKey"),
                               "accountEnding": str(a.get("accountId", ""))[-4:],
                               "accountMode": a.get("accountMode"),
                               "accountStatus": a.get("accountStatus"),
                               "institutionType": a.get("institutionType"),
                               "accountType": a.get("accountType"),
                           } for a in rows])
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
        except Exception:
            return jsonify(error="Production account lookup failed"), 502

    @bp.get("/accounts/<account_key>/balance")
    def balance(account_key):
        try:
            account = validate_account(account_key)
            body = production_get(
                "/v1/accounts/" + quote(account_key, safe="") + "/balance.json",
                params={"instType": "BROKERAGE", "realTimeNAV": "true"}
            )
            output = get_balance(body)
            # Do not label margin buying power as spendable cash.
            return jsonify(environment="production", mode="read-only",
                           orders_enabled=False,
                           accountEnding=str(account.get("accountId", ""))[-4:],
                           **output)
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
        except Exception:
            return jsonify(error="Production balance lookup failed"), 502

    @bp.get("/accounts/<account_key>/positions")
    def positions(account_key):
        try:
            account = validate_account(account_key)
            data = production_get(
                "/v1/accounts/" + quote(account_key, safe="") + "/portfolio.json",
                params={"count": "50", "pageNumber": "1"}
            )
            return jsonify(environment="production", mode="read-only",
                           orders_enabled=False,
                           accountEnding=str(account.get("accountId", ""))[-4:],
                           **get_positions(data))
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
        except Exception:
            return jsonify(error="Production portfolio lookup failed"), 502

    return bp
