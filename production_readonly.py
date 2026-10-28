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

from production_checks import OrderBlocked, calculate_budgeted_buy

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
        # Show only a static connection page without credentials. All data and
        # actions still require the administrator's authenticated API request.
        if request.endpoint == "production_reader.connect_ui":
            return None
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

    @bp.get("/connect-ui")
    def connect_ui():
        from flask import Response
        html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JC Market Signals - E*TRADE Production Account Reader</title>
<style>
body{font:16px system-ui,sans-serif;background:#f4f6fa;color:#18253d;max-width:760px;margin:30px auto;padding:0 18px}
main{background:white;border:1px solid #d7deea;border-radius:14px;padding:24px}
h1{font-size:24px}h2{font-size:18px;margin-top:24px}
label{display:block;margin:14px 0 6px;font-weight:600}
input,select{width:100%;box-sizing:border-box;padding:11px;border:1px solid #9cadc2;border-radius:8px}
button{margin:12px 8px 0 0;padding:11px 14px;border-radius:8px;background:#174c92;color:white;border:0;cursor:pointer}
button:disabled{background:#94a2b5}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf2fa;padding:12px;border-radius:7px}
small{color:#526179}.warn{color:#9b2b23;font-weight:700}a{color:#1354aa}
</style></head><body><main><h1>E*TRADE Production Account Reader</h1>
<p class="warn">READ ONLY. No live buying, selling, or orders are enabled.</p>
<p>This page is for checking production account access before preparing live trading. Use the ADMIN_TOKEN from Render, not your E*TRADE Consumer Secret. The token stays in this open page's memory only.</p>
<label for="token">Render ADMIN_TOKEN</label><input id="token" type="password" autocomplete="off">
<button id="status">Check status</button>
<button id="start">1. Start production authorization</button>
<p id="authorize" hidden><a id="approval" target="_blank" rel="noopener noreferrer">Open E*TRADE authorization</a> and copy the verification code.</p>
<label for="verifier">E*TRADE verification code</label><input id="verifier" autocomplete="off">
<button id="finish">2. Finish authorization</button>
<h2>Read-only account verification</h2>
<button id="accounts">3. Load accounts</button>
<label for="account">Select one account</label><select id="account"><option value="">Load accounts first</option></select>
<button id="balance">Check account balance</button>
<button id="positions">Check holdings</button>
<h2>Whole-share budget calculator — estimate only</h2>
<p><small>Enter your proposed limit price per share. The app will check current settled cash and round DOWN to whole shares with a 2% cost reserve. No trades or broker order previews occur here.</small></p>
<label for="symbol">Stock symbol</label><input id="symbol" value="NVDA" autocomplete="off" maxlength="10">
<label for="limit">Limit price per share ($)</label><input id="limit" value="" placeholder="e.g. 185.00" inputmode="decimal">
<label for="budget">Maximum total budget ($)</label><input id="budget" value="1000.00" inputmode="decimal">
<button id="calculate">Calculate Whole Shares — No Trade</button>
<p><small>No data is stored by this page. Authorization may need to be repeated after Render restarts. Do not use the sandbox credentials or share your secret keys.</small></p>
<pre id="output" role="status" aria-live="polite">Account reader not yet connected.</pre>
</main><script>
const $=id=>document.getElementById(id);
const show=data=>{$('output').textContent=typeof data==='string'?data:JSON.stringify(data,null,2)};
async function api(path,method='GET',body){
 const token=$('token').value.trim();
 if(!token)throw Error('Enter the ADMIN_TOKEN to access this page.');
 const r=await fetch('/production-readonly'+path,{method,cache:'no-store',headers:{'Authorization':'Bearer '+token,...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined,credentials:'omit'});
 const data=await r.json().catch(()=>({error:'Non-JSON server response'}));
 if(!r.ok)throw Error(data.error||'Request failed.');
 return data;
}
function run(fn){return async()=>{try{show('Requesting...');await fn()}catch(e){show(e.message)}}}
$('status').onclick=run(async()=>show(await api('/status')));
$('start').onclick=run(async()=>{
 const d=await api('/connect/start','POST',{start:true});
 const url=new URL(d.authorize_url);
 if(url.protocol!=='https:'||url.hostname!=='us.etrade.com')throw Error('Unexpected E*TRADE authorization address');
 $('approval').href=url.toString();$('authorize').hidden=false;
 show('Open E*TRADE authorization link, approve access, then enter its verification code. This module only reads accounts.');
});
$('finish').onclick=run(async()=>{const d=await api('/connect/finish','POST',{verifier:$('verifier').value.trim()});$('verifier').value='';show(d)});
$('accounts').onclick=run(async()=>{
 const d=await api('/accounts');$('account').replaceChildren();
 const placeholder=new Option('Choose an account','');$('account').add(placeholder);
 for(const a of d.accounts||[]){if(a.accountIdKey){$('account').add(new Option((a.accountMode||'Unknown')+' · ending '+a.accountEnding,a.accountIdKey))}}
 show({...d,accounts:(d.accounts||[]).map(a=>({...a,accountIdKey:'[hidden in display]'}))});
});
function selected(){const id=$('account').value;if(!id)throw Error('Select an account first');return encodeURIComponent(id)}
$('balance').onclick=run(async()=>show(await api('/accounts/'+selected()+'/balance')));
$('positions').onclick=run(async()=>show(await api('/accounts/'+selected()+'/positions')));
$('calculate').onclick=run(async()=>{
 const params=new URLSearchParams({symbol:$('symbol').value.trim().toUpperCase(),limit_price:$('limit').value.trim(),budget:$('budget').value.trim()});
 const result=await api('/accounts/'+selected()+'/size-buy?'+params.toString());
 if(result.order){result.order={...result.order,account_id:'[hidden in display]'}}
 show(result);
});
</script></body></html>"""
        return Response(html, mimetype="text/html", headers={
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
        })

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

    @bp.get("/accounts/<account_key>/size-buy")
    def size_buy(account_key):
        """Calculate, but NEVER preview or submit, an actual E*TRADE order."""
        try:
            symbol = request.args.get("symbol", "")
            price = request.args.get("limit_price", "")
            budget = request.args.get("budget", "1000.00")
            account = validate_account(account_key)
            raw = production_get(
                "/v1/accounts/" + quote(account_key, safe="") + "/balance.json",
                params={"instType": "BROKERAGE", "realTimeNAV": "true"}
            )
            mapped = get_balance(raw)
            # Only verified cash accounts are eligible; never treat margin
            # buying power as settled cash or silently infer a cash account.
            if account.get("accountMode") != "CASH" or mapped.get("accountMode") != "CASH":
                raise OrderBlocked("Account must explicitly be CASH in both account and balance responses")
            if account.get("institutionType") != "BROKERAGE" or mapped.get("institutionType") != "BROKERAGE":
                raise OrderBlocked("Account and balance must both be a verified BROKERAGE account")
            normalized = {
                "accountId": account.get("accountId"),
                "Computed": {key: mapped.get(key) for key in (
                    "settledCashForInvestment",
                    "cashAvailableForInvestment",
                    "cashBuyingPower"
                )}
            }
            proposed = calculate_budgeted_buy(
                symbol, account, normalized,
                price, budget
            )
            proposed.update(
                mode="calculation_only",
                environment="production",
                orders_enabled=False,
                broker_preview_performed=False,
                broker_order_submitted=False,
                pricing_source="USER_ENTERED_LIMIT_PRICE",
                message="Estimate only. No order was previewed or placed."
            )
            return jsonify(proposed)
        except (ValueError, OrderBlocked) as exc:
            return jsonify(
                error=str(exc), mode="calculation_only",
                orders_enabled=False
            ), 400
        except Exception:
            return jsonify(
                error="Unable to verify current E*TRADE cash. No order placed.",
                mode="calculation_only", orders_enabled=False
            ), 502

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
