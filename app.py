import os
import time
import secrets
from flask import Flask, jsonify, request, Response
from urllib.parse import urlencode
from requests_oauthlib import OAuth1Session

app = Flask(__name__)
key = os.getenv('ETRADE_CONSUMER_KEY', '')
secret = os.getenv('ETRADE_CONSUMER_SECRET', '')
admin = os.getenv('ADMIN_TOKEN', '')
mode = os.getenv('ETRADE_ENV', 'sandbox')
api_base = 'https://apisb.etrade.com' if mode == 'sandbox' else 'https://api.etrade.com'
auth_base = 'https://api.etrade.com'
pending = None
access = None

PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>E*TRADE Connection | JC Market Signals</title><style>
body{font:16px system-ui,sans-serif;max-width:650px;margin:3rem auto;padding:0 1rem;background:#f5f7fb;color:#18243a}main{background:white;border:1px solid #dce2ec;border-radius:14px;padding:25px}label{display:block;font-weight:600;margin:18px 0 6px}input{box-sizing:border-box;width:100%;padding:12px;border:1px solid #a8b5c6;border-radius:8px;font-size:16px}button{background:#15518d;color:white;border:0;border-radius:8px;padding:12px 17px;font-size:16px;margin-top:15px;cursor:pointer}#status{margin-top:18px;white-space:pre-wrap;overflow-wrap:anywhere}small{color:#526174}.hidden{display:none}a{color:#15518d}
</style></head><body><main><h1>Connect E*TRADE</h1><p>Sandbox connection. Orders require separate review and confirmation in JC Market Signals. Real-money trading is disabled.</p><p><small>Use the ADMIN_TOKEN saved in Render, NOT your E*TRADE Consumer Secret. This page does not save your token to browser storage.</small></p><label for="admin">Render ADMIN_TOKEN</label><input id="admin" type="password" autocomplete="off"><button id="start">1. Start E*TRADE authorization</button><section id="verification" class="hidden"><p><a id="authorize" target="_blank" rel="noopener noreferrer">Open E*TRADE authorization page</a></p><p>Approve access at E*TRADE, then enter the verification code below.</p><label for="verifier">E*TRADE verification code</label><input id="verifier" autocomplete="off"><button id="finish">2. Complete connection</button></section><button id="test" class="hidden">3. Test account connection</button><div id="status" role="status" aria-live="polite"></div></main><script>
const el=id=>document.getElementById(id), show=s=>el('status').textContent=s;
async function api(path,method='POST',data){const token=el('admin').value.trim();if(!token)throw Error('Enter Render ADMIN_TOKEN first.');const r=await fetch(path,{method,headers:{Authorization:'Bearer '+token,...(data?{'Content-Type':'application/json'}:{})},body:data?JSON.stringify(data):undefined,cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');return d}
el('start').onclick=async()=>{try{show('Requesting E*TRADE authorization link...');const d=await api('/connect/start');const u=new URL(d.authorize_url);if(u.protocol!=='https:'||u.hostname!=='us.etrade.com')throw Error('Unexpected authorization address');el('authorize').href=u.toString();el('verification').classList.remove('hidden');show('Open the E*TRADE authorization page and approve access.')}catch(e){show(e.message)}};
el('finish').onclick=async()=>{try{await api('/connect/finish','POST',{verifier:el('verifier').value.trim()});el('verifier').value='';el('test').classList.remove('hidden');show('E*TRADE authorization completed. Test account access next.')}catch(e){show(e.message)}};
el('test').onclick=async()=>{try{const d=await api('/accounts','GET');const a=d.AccountListResponse?.Accounts?.Account||[];[];show('Account API request succeeded. Number of accounts: '+(Array.isArray(a)?a.length:1)+'.')}catch(e){show(e.message)}};
</script></body></html>'''

def ok_auth():
    supplied = request.headers.get('Authorization', '')
    return bool(admin) and secrets.compare_digest(supplied, 'Bearer ' + admin)

@app.after_request
def headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@app.get('/health')
def health():
    return jsonify(status='ok', environment=mode, connected=access is not None)

@app.get('/connect-ui')
def connect_ui():
    return Response(PAGE, mimetype='text/html', headers={
        'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        'Referrer-Policy': 'no-referrer',
    })

@app.before_request
def protect():
    if request.path in ('/health', '/connect-ui') or request.path.startswith(('/trading/', '/auth/', '/production-readonly/')):
        return None
    if not ok_auth():
        return jsonify(error='Unauthorized'), 401
    if not key or not secret:
        return jsonify(error='E*TRADE credentials not set in Render'), 503

@app.post('/connect/start')
def start():
    global pending
    try:
        oauth = OAuth1Session(key, client_secret=secret, callback_uri='oob')
        result = oauth.fetch_request_token(auth_base + '/oauth/request_token')
        pending = {'token': result['oauth_token'], 'secret': result['oauth_token_secret'], 'time': time.time()}
        return jsonify(authorize_url='https://us.etrade.com/e/t/etws/authorize?' + urlencode({'key': key, 'token': result['oauth_token']}))
    except Exception:
        app.logger.exception('E*TRADE request-token failure')
        return jsonify(error='Unable to begin E*TRADE authorization'), 502

@app.post('/connect/finish')
def finish():
    global pending, access
    verifier = (request.get_json(silent=True) or {}).get('verifier', '')
    if not isinstance(verifier, str) or not verifier.strip():
        return jsonify(error='Verification code required'), 400
    if not pending or time.time() - pending['time'] > 600:
        return jsonify(error='Start authorization again'), 400
    try:
        oauth = OAuth1Session(key, client_secret=secret, resource_owner_key=pending['token'], resource_owner_secret=pending['secret'], verifier=verifier.strip())
        result = oauth.fetch_access_token(auth_base + '/oauth/access_token')
        access = {'token': result['oauth_token'], 'secret': result['oauth_token_secret']}
        pending = None
        return jsonify(connected=True)
    except Exception:
        app.logger.exception('E*TRADE access-token failure')
        return jsonify(error='Unable to finish E*TRADE authorization'), 502

@app.get('/accounts')
def accounts():
    if not access:
        return jsonify(error='Connect E*TRADE first'), 409
    try:
        oauth = OAuth1Session(key, client_secret=secret, resource_owner_key=access['token'], resource_owner_secret=access['secret'])
        response = oauth.get(api_base + '/v1/accounts/list', headers={'Accept': 'application/json'}, timeout=15)
        response.raise_for_status()
        return jsonify(response.json())
    except Exception:
        app.logger.exception('E*TRADE accounts failure')
        return jsonify(error='E*TRADE account request failed; authorization may have expired'), 502


# Sandbox order API v1. No route below can target the production broker API.
import json
import math
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

TRADING_ORIGIN = 'https://jc-market-signals.brave-rain-7832.chatgpt.site'
SANDBOX_API = 'https://apisb.etrade.com'
PREVIEW_TTL = 120
ORDER_DB = os.getenv('ORDER_DB_PATH', '/tmp/jc-sandbox-orders.sqlite3')

def order_db():
    db = sqlite3.connect(ORDER_DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS previews (token TEXT PRIMARY KEY, expires REAL NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL)')
    return db

@app.after_request
def trading_headers(response):
    if request.headers.get('Origin') == TRADING_ORIGIN:
        response.headers['Access-Control-Allow-Origin'] = TRADING_ORIGIN
        response.headers['Vary'] = 'Origin'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
    if request.path.startswith('/trading/'):
        response.headers['Cache-Control'] = 'no-store'
    return response

# Run before the existing authentication guard, including OPTIONS preflights.
def trading_guard():
    if not request.path.startswith('/trading/'):
        return None
    origin = request.headers.get('Origin')
    if origin and origin != TRADING_ORIGIN:
        return jsonify(error='Origin is not allowed', environment='sandbox'), 403
    if request.method == 'OPTIONS':
        return Response(status=204)
    if mode != 'sandbox':
        return jsonify(error='Trading blocked: ETRADE_ENV must be sandbox. Production trading is disabled.', environment=mode), 403
    if request.path == '/trading/status':
        return None
    if not ok_auth():
        return jsonify(error='Unauthorized: enter the Render ADMIN_TOKEN', environment='sandbox'), 401
    if not key or not secret:
        return jsonify(error='Sandbox credentials are not configured in Render', environment='sandbox'), 503
    if not access:
        return jsonify(error='Connect E*TRADE sandbox first; authorization may have expired', environment='sandbox'), 409
    return None
app.before_request_funcs[None].insert(0, trading_guard)

@app.get('/trading/status')
def trading_status():
    return jsonify(environment='sandbox', connected=access is not None, orders_enabled=True, version=1)

# Existing guard already enforces the same auth. All brokerage calls here use
# a literal sandbox host and disable redirects (including credential forwarding).
def sandbox_call(method, path, payload=None):
    if mode != 'sandbox':
        raise ValueError('Production trading is disabled')
    oauth = OAuth1Session(key, client_secret=secret, resource_owner_key=access['token'], resource_owner_secret=access['secret'])
    response = oauth.request(method, SANDBOX_API + path, json=payload,
                             headers={'Accept': 'application/json'}, timeout=18, allow_redirects=False)
    if not 200 <= response.status_code < 300:
        raise ValueError('E*TRADE sandbox rejected the request or authorization expired. Check sandbox orders before retrying a submission.')
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError('Invalid E*TRADE sandbox response')
    return data

def many(value):
    return value if isinstance(value, list) else [value] if isinstance(value, dict) else []

def broker_messages(data):
    result = []
    for group in ('Messages', 'messageList', 'messages'):
        block = data.get(group, {})
        for message in many(block.get('Message', block.get('message', [])) if isinstance(block, dict) else block):
            if str(message.get('type', '')).upper() == 'ERROR':
                raise ValueError('E*TRADE rejected the sandbox order: ' + str(message.get('description', 'Order validation failed'))[:500])
            result.append(str(message.get('description', message.get('text', '')))[:500])
    return result

def sandbox_accounts():
    data = sandbox_call('GET', '/v1/accounts/list.json')
    container = data.get('AccountListResponse', data)
    accounts = container.get('Accounts', {}).get('Account', [])
    return [a for a in many(accounts) if isinstance(a.get('accountIdKey'), str) and a['accountIdKey'] and a.get('accountStatus', 'ACTIVE') == 'ACTIVE']

@app.get('/trading/accounts')
def trading_accounts():
    try:
        accounts = sandbox_accounts()
        return jsonify(environment='sandbox', accounts=[{'id': a['accountIdKey'], 'label': str(a.get('accountDesc', a.get('accountName', 'Sandbox account'))) + ' · ending ' + str(a.get('accountId', ''))[-4:]} for a in accounts])
    except Exception as e:
        return jsonify(environment='sandbox', error=str(e) if isinstance(e, ValueError) else 'Unable to load sandbox accounts'), 502

@app.get('/trading/quote')
def trading_quote():
    symbol = request.args.get('symbol', '').upper()
    if not re.fullmatch(r'[A-Z][A-Z.]{0,9}', symbol):
        return jsonify(environment='sandbox', error='Invalid stock symbol'), 400
    try:
        data = sandbox_call('GET', '/v1/market/quote/' + quote(symbol, safe='') + '.json')
        quotes = many(data.get('QuoteResponse', {}).get('QuoteData', []))
        match = next((q for q in quotes if q.get('Product', {}).get('symbol') == symbol), None)
        price = float(match.get('All', {}).get('lastTrade', 0)) if match else 0
        if not math.isfinite(price) or price <= 0:
            raise ValueError('E*TRADE did not return a valid sandbox price for this symbol')
        return jsonify(environment='sandbox', symbol=symbol, price=price, as_of=str(match.get('dateTime', 'Timestamp unavailable')), quote_status=str(match.get('quoteStatus', 'Sandbox test quote')))
    except Exception as e:
        return jsonify(environment='sandbox', error=str(e) if isinstance(e, ValueError) else 'Unable to load current sandbox quote'), 502

def validate_order(data):
    if not isinstance(data, dict):
        raise ValueError('Order object required')
    symbol = data.get('symbol', '')
    side = data.get('side')
    account = data.get('account_id')
    quantity = data.get('quantity')
    price_type = data.get('order_type')
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z][A-Z.]{0,9}', symbol):
        raise ValueError('Invalid stock symbol')
    if side not in ('BUY', 'SELL') or price_type not in ('MARKET', 'LIMIT'):
        raise ValueError('Only BUY/SELL and MARKET/LIMIT are supported')
    if type(quantity) is not int or not 1 <= quantity <= 1000000:
        raise ValueError('Quantity must be a positive whole number, at most 1,000,000 shares')
    if not isinstance(account, str) or account not in {a['accountIdKey'] for a in sandbox_accounts()}:
        raise ValueError('Select an active sandbox account returned by E*TRADE')
    normalized = dict(symbol=symbol, side=side, account_id=account, quantity=quantity, order_type=price_type)
    detail = {'allOrNone': False, 'priceType': price_type, 'orderTerm': 'GOOD_FOR_DAY', 'marketSession': 'REGULAR', 'Instrument': [{'Product': {'securityType': 'EQ', 'symbol': symbol}, 'orderAction': side, 'quantityType': 'QUANTITY', 'quantity': quantity}]}
    if price_type == 'LIMIT':
        try:
            price = Decimal(str(data.get('limit_price')))
        except InvalidOperation:
            raise ValueError('A positive limit price is required')
        if not price.is_finite() or price <= 0 or price > 10000000 or price.as_tuple().exponent < -2:
            raise ValueError('Limit price must be positive with at most two decimals')
        normalized['limit_price'] = float(price)
        detail['limitPrice'] = format(price, 'f')
    return normalized, detail

@app.post('/trading/orders/preview')
def preview_order():
    try:
        normalized, detail = validate_order(request.get_json(silent=True))
        client_id = secrets.token_hex(10)  # E*TRADE limit: 20 alphanumeric chars.
        broker_request = {'orderType': 'EQ', 'clientOrderId': client_id, 'Order': [detail]}
        response = sandbox_call('POST', '/v1/accounts/' + quote(normalized['account_id'], safe='') + '/orders/preview.json', {'PreviewOrderRequest': broker_request})
        preview = response.get('PreviewOrderResponse')
        if not isinstance(preview, dict):
            raise ValueError('E*TRADE did not confirm an order preview')
        messages = broker_messages(preview)
        ids = many(preview.get('PreviewIds', []))
        if not ids or not all(str(i.get('previewId', '')).isdigit() for i in ids):
            raise ValueError('E*TRADE did not return a valid preview ID')
        broker_request['PreviewIds'] = ids
        token = secrets.token_urlsafe(32)
        expires = time.time() + PREVIEW_TTL
        saved = {'account_id': normalized['account_id'], 'broker_request': broker_request, 'order': normalized}
        with order_db() as db:
            db.execute('DELETE FROM previews WHERE expires < ?', (time.time() - 86400,))
            db.execute('INSERT INTO previews VALUES (?,?,?,?)', (token, expires, json.dumps(saved), 'PREVIEWED'))
        value = preview.get('totalOrderValue')
        try:
            estimate = float(value)
            if not math.isfinite(estimate):
                estimate = None
        except (ValueError, TypeError):
            estimate = None
        return jsonify(environment='sandbox', preview_token=token, expires_at=expires, order=normalized, estimated_value=estimate, messages=messages)
    except ValueError as e:
        return jsonify(environment='sandbox', error=str(e)), 400
    except Exception:
        return jsonify(environment='sandbox', error='Unable to preview the order. No order was submitted.'), 502

@app.post('/trading/orders/submit')
def submit_order():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or data.get('confirm') is not True or not isinstance(data.get('preview_token'), str):
        return jsonify(environment='sandbox', error='Explicit confirmation of a sandbox preview is required'), 400
    token = data['preview_token']
    # Claim the immutable preview before making a network request. Replays,
    # double clicks, concurrent workers and uncertain responses cannot re-place it.
    with order_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM previews WHERE token = ?', (token,)).fetchone()
        if not row or row['state'] != 'PREVIEWED' or row['expires'] <= time.time():
            return jsonify(environment='sandbox', error='Preview expired or already submitted. Check E*TRADE sandbox orders before creating another.'), 409
        db.execute("UPDATE previews SET state='SUBMITTING' WHERE token=?", (token,))
        saved = json.loads(row['payload'])
    try:
        response = sandbox_call('POST', '/v1/accounts/' + quote(saved['account_id'], safe='') + '/orders/place.json', {'PlaceOrderRequest': saved['broker_request']})
        placed = response.get('PlaceOrderResponse')
        if not isinstance(placed, dict):
            raise ValueError('No E*TRADE order acceptance received. Check sandbox orders before retrying.')
        messages = broker_messages(placed)
        ids = many(placed.get('OrderIds', []))
        order_id = next((str(i.get('orderId')) for i in ids if str(i.get('orderId', '')).isdigit()), None)
        if not order_id:
            raise ValueError('No E*TRADE order ID received. Status is unknown; check sandbox orders before retrying.')
        with order_db() as db:
            db.execute("UPDATE previews SET state='ACCEPTED' WHERE token=?", (token,))
        # A place response with broker order ID proves acceptance, not a fill.
        return jsonify(environment='sandbox', broker_confirmed=True, status='ACCEPTED', order_id=order_id, messages=messages)
    except Exception as e:
        with order_db() as db:
            db.execute("UPDATE previews SET state='UNKNOWN' WHERE token=?", (token,))
        return jsonify(environment='sandbox', broker_confirmed=False, error=str(e) if isinstance(e, ValueError) else 'Sandbox submission status unknown. Check E*TRADE orders before trying again.'), 502


# Browser device sessions. Requires a durable, operator-configured SQLite path.
import hashlib
from pathlib import Path

DEVICE_COOKIE = '__Host-jc_device'
SESSION_DB_PATH = os.getenv('AUTH_SESSION_DB_PATH', '')
DEVICE_LIFETIME = 30 * 86400
DEVICE_IDLE = 7 * 86400
_original_ok_auth = ok_auth

def session_db():
    path = Path(SESSION_DB_PATH)
    if not SESSION_DB_PATH or not path.is_absolute() or str(path).startswith('/tmp/') or not path.parent.is_dir():
        raise RuntimeError('Configure AUTH_SESSION_DB_PATH on a persistent Render disk before enabling device login')
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS device_sessions (id_hash TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL, touched REAL NOT NULL, idle INTEGER NOT NULL, generation TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS device_login_attempts (at REAL NOT NULL)')
    return db

def session_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()

def auth_generation():
    return hashlib.sha256(('jc-device-generation:' + admin).encode()).hexdigest()

def valid_device():
    token = request.cookies.get(DEVICE_COOKIE, '')
    if not token or len(token) > 128 or not admin:
        return False
    try:
        now = time.time()
        with session_db() as db:
            row = db.execute('SELECT * FROM device_sessions WHERE id_hash=?', (session_hash(token),)).fetchone()
            if not row or row['expires'] <= now or row['touched'] + row['idle'] <= now or row['generation'] != auth_generation():
                return False
            if request.method not in ('GET', 'HEAD') and request.headers.get('Origin') != TRADING_ORIGIN:
                return False
            db.execute('UPDATE device_sessions SET touched=? WHERE id_hash=?', (now, session_hash(token)))
        return True
    except (RuntimeError, sqlite3.Error):
        return False

def ok_auth():
    return _original_ok_auth() or valid_device()

def expire_device(response):
    response.delete_cookie(DEVICE_COOKIE, path='/', secure=True, httponly=True, samesite='Lax')
    return response

@app.before_request
def device_origin_guard():
    if request.path.startswith('/auth/') and request.method not in ('GET', 'HEAD'):
        if request.headers.get('Origin') != TRADING_ORIGIN:
            return jsonify(environment='sandbox', error='Origin is not allowed'), 403
    return None

@app.get('/auth/session')
def device_status():
    try:
        with session_db():
            pass
    except (RuntimeError, sqlite3.Error):
        return jsonify(environment='sandbox', available=False, authenticated=False,
                       error='Remember This Device requires the backend session update and durable session storage'), 503
    return jsonify(environment='sandbox', available=True, authenticated=valid_device())

@app.post('/auth/login')
def device_login():
    if mode != 'sandbox':
        return jsonify(environment=mode, error='Production trading remains disabled'), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or type(body.get('remember_device')) is not bool:
        return jsonify(environment='sandbox', error='Choose whether to remember this device'), 400
    try:
        with session_db() as db:
            now = time.time()
            db.execute('DELETE FROM device_login_attempts WHERE at < ?', (now - 60,))
            if db.execute('SELECT count(*) FROM device_login_attempts').fetchone()[0] >= 30:
                return jsonify(environment='sandbox', error='Too many sign-in attempts. Try again in a minute.'), 429
            db.execute('INSERT INTO device_login_attempts VALUES (?)', (now,))
    except (RuntimeError, sqlite3.Error):
        return jsonify(environment='sandbox', error='Backend device login needs durable AUTH_SESSION_DB_PATH storage'), 503
    if not _original_ok_auth():
        return jsonify(environment='sandbox', error='Sign-in failed'), 401
    token = secrets.token_urlsafe(48)
    remember = body['remember_device']
    lifetime = DEVICE_LIFETIME if remember else 8 * 3600
    idle = DEVICE_IDLE if remember else 8 * 3600
    old = request.cookies.get(DEVICE_COOKIE, '')
    with session_db() as db:
        db.execute('DELETE FROM device_sessions WHERE expires <= ? OR touched + idle <= ?', (now, now))
        if old:
            db.execute('DELETE FROM device_sessions WHERE id_hash=?', (session_hash(old),))
        db.execute('INSERT INTO device_sessions VALUES (?,?,?,?,?,?)', (session_hash(token), now, now + lifetime, now, idle, auth_generation()))
    response = jsonify(environment='sandbox', authenticated=True, remembered=remember, expires_at=now + lifetime)
    response.set_cookie(DEVICE_COOKIE, token, max_age=lifetime if remember else None,
                        path='/', secure=True, httponly=True, samesite='Lax')
    return response

@app.post('/auth/logout')
def device_logout():
    token = request.cookies.get(DEVICE_COOKIE, '')
    try:
        with session_db() as db:
            db.execute('DELETE FROM device_sessions WHERE id_hash=?', (session_hash(token),))
    except (RuntimeError, sqlite3.Error):
        return jsonify(environment='sandbox', error='Could not revoke server session. Retry Sign Out.'), 503
    return expire_device(jsonify(environment='sandbox', authenticated=False))

@app.post('/auth/logout-all')
def device_logout_all():
    if not ok_auth():
        return jsonify(environment='sandbox', error='Sign in first'), 401
    with session_db() as db:
        db.execute('DELETE FROM device_sessions')
    return expire_device(jsonify(environment='sandbox', authenticated=False))

# Production ACCOUNT READER only. The static setup page remains reachable
# even when credentials or feature flags have not yet been configured.
# All production account-data and OAuth endpoints remain disabled by default
# in the blueprint's before_request guard. No production trading endpoints.
from production_readonly import create_reader_blueprint
app.register_blueprint(create_reader_blueprint(ok_auth))
