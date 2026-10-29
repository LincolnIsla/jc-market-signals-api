"""Opt-in production limit orders. Disabled unless all operator gates pass.

No order is sent by installation, account checks or preview. Placement is a
separate confirmed POST using a durable, single-use server-side preview.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request
from production_checks import validate_cash_limit_order

API = 'https://api.etrade.com'
ORIGINS = {'https://jc-market-signals-api.onrender.com',
           'https://jc-market-signals.brave-rain-7832.chatgpt.site'}
TTL = 120
CONFIRMATION = 'PLACE REAL MONEY ORDER'
ACTIVE = ('PREPARING', 'PREVIEWED', 'SUBMITTING', 'UNKNOWN', 'ACCEPTED')


def records(value):
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(v, dict) for v in value):
        return value
    raise ValueError('Malformed broker record list')


def decimal(value):
    if value is None or isinstance(value, bool):
        raise ValueError('Missing broker amount')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Invalid broker amount') from None
    if not result.is_finite() or result < 0:
        raise ValueError('Invalid broker amount')
    return result


def setting_accounts(name):
    try:
        value = json.loads(os.getenv(name, '[]'))
    except ValueError:
        return set()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        return set()
    return set(value)


def database():
    path = Path(os.getenv('LIVE_ORDER_DB_PATH') or '/var/data/jc-live-orders.sqlite3')
    if not path.is_absolute() or str(path).startswith('/tmp/') or not path.parent.is_dir():
        raise ValueError('Live orders require a persistent disk database')
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS live_orders ('
               'token_hash TEXT PRIMARY KEY, account_key TEXT NOT NULL, '
               'created REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL, '
               'generation TEXT NOT NULL, payload TEXT NOT NULL, broker_id TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS live_cash_bindings ('
               'ending TEXT PRIMARY KEY, account_key TEXT NOT NULL UNIQUE, '
               'account_id_hash TEXT NOT NULL)')
    return db


def generation():
    return hashlib.sha256(os.getenv('ADMIN_TOKEN', '').encode()).hexdigest()


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def messages(body):
    output = []
    for group in ('Messages', 'messages', 'messageList'):
        value = body.get(group)
        if value is None:
            continue
        if isinstance(value, dict):
            value = value.get('Message', value.get('message', []))
        for item in records(value):
            if str(item.get('type', '')).upper() == 'ERROR':
                raise ValueError('Broker rejected this order preview')
            output.append(str(item.get('description', 'Broker message'))[:500])
    return output


def detail_for(order):
    return {'allOrNone': False, 'priceType': 'LIMIT',
            'limitPrice': order['limit_price'], 'orderTerm': 'GOOD_FOR_DAY',
            'marketSession': 'REGULAR', 'Instrument': [{
                'Product': {'symbol': order['symbol'], 'securityType': 'EQ'},
                'orderAction': order['side'], 'quantityType': 'QUANTITY',
                'quantity': order['quantity']}]}


def verify_echo(body, order, account):
    if body.get('orderType') != 'EQ' or str(body.get('accountId')) != str(account['accountId']):
        raise ValueError('Broker returned a different account or security type')
    details = records(body.get('Order', body.get('order')))
    if len(details) != 1:
        raise ValueError('Only a single stock order is supported')
    detail = details[0]
    instruments = records(detail.get('Instrument'))
    if len(instruments) != 1:
        raise ValueError('Unexpected broker order legs')
    instrument = instruments[0]
    product = instrument.get('Product', {})
    if (product.get('securityType') != 'EQ' or product.get('symbol') != order['symbol']
            or instrument.get('orderAction') != order['side']
            or decimal(instrument.get('quantity')) != order['quantity']
            or detail.get('priceType') != 'LIMIT'
            or decimal(detail.get('limitPrice')) != decimal(order['limit_price'])
            or detail.get('orderTerm') != 'GOOD_FOR_DAY'
            or detail.get('marketSession') != 'REGULAR'):
        raise ValueError('Broker preview does not match your exact order')
    return messages(body) + messages(detail)


def create_live_blueprint(authorized, broker_factory):
    """broker_factory shares the separately authorized production OAuth client."""
    bp = Blueprint('production_live', __name__, url_prefix='/live-trading')

    def enabled():
        return os.getenv('ETRADE_LIVE_ORDERS_ENABLED') == 'true'

    @bp.before_request
    def guard():
        if not authorized():
            return jsonify(environment='production', error='Sign in first'), 401
        if request.method not in ('GET', 'HEAD') and request.headers.get('Origin') not in ORIGINS:
            return jsonify(environment='production', error='Request origin not permitted'), 403
        if request.endpoint not in ('production_live.status', 'production_live.accounts', 'production_live.readiness') and not enabled():
            return jsonify(environment='production', error='Live submission is disabled pending account verification'), 403

    @bp.after_request
    def no_cache(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def broker(method, account_key=None, resource='list', params=None, payload=None):
        # Caller cannot supply arbitrary hosts or paths, and redirects are never followed.
        if method == 'GET' and resource == 'list':
            path = '/v1/accounts/list.json'
        elif method == 'GET' and resource in ('balance', 'portfolio', 'orders'):
            path = '/v1/accounts/' + quote(account_key, safe='') + '/' + resource + '.json'
        elif method == 'POST' and resource in ('preview', 'place'):
            path = '/v1/accounts/' + quote(account_key, safe='') + '/orders/' + resource + '.json'
        else:
            raise ValueError('Unsupported broker operation')
        client = broker_factory()
        response = client.request(method, API + path, params=params, json=payload,
                                  headers={'Accept': 'application/json'},
                                  timeout=18, allow_redirects=False)
        if response.status_code == 204 and resource in ('portfolio', 'orders'):
            return {'PortfolioResponse': {'AccountPortfolio': []}} if resource == 'portfolio' else {'OrdersResponse': {'Order': []}}
        if response.status_code != 200:
            raise ValueError('E*TRADE request failed; check connection and broker order history')
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError('Malformed E*TRADE response')
        return data

    def account_list():
        data = broker('GET').get('AccountListResponse')
        if not isinstance(data, dict) or not isinstance(data.get('Accounts'), dict):
            raise ValueError('Account list unavailable')
        return records(data['Accounts'].get('Account', []))

    def funding_policy(account, all_accounts):
        """Explicit cash-FUNDED policy, not a claim about borrowing permissions.

        Resolve only one broker-owned account matching the configured ending,
        then bind its complete broker key and ID hash durably. Later sessions
        cannot silently replace it with another account having the same ending.
        """
        endings = {v for v in setting_accounts('ETRADE_CASH_FUNDED_ACCOUNT_ENDINGS')
                   if re.fullmatch(r'[0-9]{4}', v)}
        account_id = str(account.get('accountId', ''))
        ending = account_id[-4:]
        if ending not in endings:
            return False
        if not re.fullmatch(r'[0-9]{4,20}', account_id):
            raise ValueError('Broker account identity is invalid')
        matches = [a for a in all_accounts if str(a.get('accountId', ''))[-4:] == ending]
        if len(matches) != 1 or matches[0].get('accountIdKey') != account.get('accountIdKey'):
            raise ValueError('Account ending is ambiguous. Enable the exact account key instead')
        if (account.get('accountMode') != 'CASH' or account.get('institutionType') != 'BROKERAGE'
                or account.get('accountStatus') != 'ACTIVE'):
            raise ValueError('Cash-funded policy requires an active CASH brokerage account in the account list')
        key = account.get('accountIdKey')
        if not isinstance(key, str) or not key:
            raise ValueError('Broker account key is missing')
        identity = hashlib.sha256(account_id.encode()).hexdigest()
        with database() as db:
            db.execute('BEGIN IMMEDIATE')
            bound = db.execute('SELECT * FROM live_cash_bindings WHERE ending=?', (ending,)).fetchone()
            if bound is None:
                db.execute('INSERT INTO live_cash_bindings VALUES (?,?,?)', (ending, key, identity))
            elif bound['account_key'] != key or bound['account_id_hash'] != identity:
                raise ValueError('Configured account identity changed. Operator review required')
        return True

    def verified_account(key):
        explicitly_allowed = key in setting_accounts('ETRADE_LIVE_ACCOUNT_ALLOWLIST')
        if not explicitly_allowed and not setting_accounts('ETRADE_CASH_FUNDED_ACCOUNT_ENDINGS'):
            raise ValueError('Account has not been enabled on the server')
        if (not setting_accounts('ETRADE_CASH_FUNDED_ACCOUNT_ENDINGS')
                and key not in setting_accounts('ETRADE_VERIFIED_CASH_ACCOUNT_KEYS')):
            raise ValueError('Broker cash-only permissions have not been verified')
        all_accounts = account_list()
        rows = [a for a in all_accounts if a.get('accountIdKey') == key]
        if len(rows) != 1 or rows[0].get('accountId') is None:
            raise ValueError('Active account identity unavailable')
        funded = funding_policy(rows[0], all_accounts)
        if not funded and (not explicitly_allowed or key not in setting_accounts('ETRADE_VERIFIED_CASH_ACCOUNT_KEYS')):
            raise ValueError('Account has no configured cash-funded order policy')
        return {**rows[0], '_cash_funded_policy': funded}

    def reject_open_orders(key):
        # Conservative first release: no new order while ANY open/cancel-pending
        # order exists, so shares/funds cannot be reserved twice by this flow.
        for state in ('OPEN', 'CANCEL_REQUESTED'):
            container = broker('GET', key, 'orders', params={'status': state, 'count': 1}).get('OrdersResponse')
            if not isinstance(container, dict):
                raise ValueError('Open-order reservations could not be verified')
            if records(container.get('Order', [])) or container.get('marker') or container.get('next'):
                raise ValueError('An open or cancel-pending order exists. Resolve it at E*TRADE first')

    def positions(key, account):
        result = {}
        for page in range(1, 21):
            data = broker('GET', key, 'portfolio', params={'count': 100, 'pageNumber': page}).get('PortfolioResponse')
            if not isinstance(data, dict):
                raise ValueError('Holdings unavailable')
            groups = records(data.get('AccountPortfolio', []))
            next_page = None
            for group in groups:
                if str(group.get('accountId')) != str(account['accountId']):
                    raise ValueError('Holdings account mismatch')
                for pos in records(group.get('Position', [])):
                    product = pos.get('Product', {})
                    if product.get('securityType') == 'EQ' and pos.get('positionType') == 'LONG':
                        qty = decimal(pos.get('quantity'))
                        if qty != qty.to_integral_value():
                            raise ValueError('Fractional holdings require manual broker review')
                        symbol = product.get('symbol')
                        result[symbol] = result.get(symbol, 0) + int(qty)
                n = group.get('nextPageNo')
                if n not in (None, '', 0, '0'):
                    if str(n) != str(page + 1):
                        raise ValueError('Unexpected holdings pagination')
                    next_page = page + 1
            if next_page is None:
                return result
        raise ValueError('Holdings pagination limit exceeded')

    def preflight(order):
        now = datetime.now(ZoneInfo('America/New_York'))
        if now.weekday() >= 5 or not ('09:30' <= now.strftime('%H:%M') < '16:00'):
            raise ValueError('Live orders are supported during regular weekday market hours only')
        if not isinstance(order, dict) or not isinstance(order.get('account_id'), str):
            raise ValueError('Select an account and enter the complete order')
        key = order['account_id']
        account = verified_account(key)
        balance = broker('GET', key, 'balance', params={'instType': 'BROKERAGE', 'realTimeNAV': 'true'}).get('BalanceResponse')
        if not isinstance(balance, dict) or balance.get('accountId') is None:
            raise ValueError('Broker balance identity is missing')
        funded = account['_cash_funded_policy']
        if (balance.get('institutionType') not in (None, '', 'BROKERAGE')
                or (not funded and balance.get('institutionType') != 'BROKERAGE')
                or balance.get('accountMode') not in (('CASH', 'MARGIN') if funded else ('CASH',))):
            raise ValueError('CASH/MARGIN classification is not verified; live trading remains blocked')
        reject_open_orders(key)
        owned = positions(key, account) if order.get('side') == 'SELL' else None
        clean = validate_cash_limit_order(order, account, balance, owned)
        return clean, account, balance

    def verify_cost(body, clean, balance):
        total = decimal(body.get('totalOrderValue'))
        commission = decimal(body.get('totalCommission'))
        if clean['side'] == 'BUY':
            notional = decimal(clean['maximum_notional'])
            computed = balance.get('Computed', balance.get('computedBalance', {}))
            cash = min(decimal(computed.get('settledCashForInvestment')),
                       decimal(computed.get('cashAvailableForInvestment')))
            # Reserve the larger of notional+commission and broker total.
            reserve = max(total, notional + commission)
            settled = body.get('cashBpDetails', {}).get('settled', {})
            impact = decimal(settled.get('currentOrderImpact'))
            decimal(settled.get('netBp'))
            if impact < notional or reserve > cash or reserve > 1000:
                raise ValueError('Broker preview exceeds verified settled cash or $1,000 total cost')
        return str(total), str(commission)

    @bp.get('/status')
    def status():
        storage = True
        try:
            with database():
                pass
        except (ValueError, sqlite3.Error):
            storage = False
        return jsonify(environment='production', orders_enabled=enabled() and storage,
                       configured_enabled=enabled(), durable_storage=storage,
                       max_order_dollars=1000, limit_orders_only=True,
                       message='LIVE: every order requires broker preview and explicit confirmation.' if enabled() and storage else 'Live submission is disabled pending verified cash account setup.')

    @bp.get('/accounts')
    def accounts():
        try:
            allowed = setting_accounts('ETRADE_LIVE_ACCOUNT_ALLOWLIST')
            verified = setting_accounts('ETRADE_VERIFIED_CASH_ACCOUNT_KEYS')
            endings = setting_accounts('ETRADE_CASH_FUNDED_ACCOUNT_ENDINGS')
            return jsonify(environment='production', accounts=[{
                'id': a.get('accountIdKey'), 'ending': str(a.get('accountId', ''))[-4:],
                'mode': a.get('accountMode'), 'status': a.get('accountStatus'),
                'enabled_for_live': (a.get('accountIdKey') in allowed & verified
                                     or str(a.get('accountId', ''))[-4:] in endings),
            } for a in account_list()])
        except Exception as exc:
            return jsonify(environment='production', error=str(exc) if isinstance(exc, ValueError) else 'Unable to load production accounts'), 409

    @bp.post('/account-check')
    def readiness():
        """Read-only setup check, available before the live kill switch is enabled."""
        data = request.get_json(silent=True) or {}
        key = data.get('account_id') if isinstance(data, dict) else None
        try:
            all_accounts = account_list()
            account = next((a for a in all_accounts if a.get('accountIdKey') == key), None)
            if not account or not isinstance(key, str) or not key:
                raise ValueError('Select an account from E*TRADE')
            raw = broker('GET', key, 'balance', params={'instType':'BROKERAGE','realTimeNAV':'true'}).get('BalanceResponse')
            if not isinstance(raw, dict):
                raise ValueError('Broker balance unavailable')
            blockers = []
            funded = funding_policy(account, all_accounts)
            if (account.get('accountMode') != 'CASH'
                    or raw.get('accountMode') not in (('CASH','MARGIN') if funded else ('CASH',))):
                blockers.append('CASH/MARGIN classification requires broker verification')
            if account.get('accountStatus') != 'ACTIVE':
                blockers.append('Account status from E*TRADE: ' + str(account.get('accountStatus') or 'not provided') + ' (expected ACTIVE)')
            if account.get('institutionType') != 'BROKERAGE':
                blockers.append('Account-list institution type: ' + str(account.get('institutionType') or 'not provided') + ' (expected BROKERAGE)')
            if raw.get('institutionType') not in ((None, '', 'BROKERAGE') if funded else ('BROKERAGE',)):
                blockers.append('Balance institution type: ' + str(raw.get('institutionType') or 'not provided') + ' (expected BROKERAGE)')
            if raw.get('accountId') is None or str(raw.get('accountId')) != str(account.get('accountId')):
                blockers.append('Balance account identity not verified')
            computed = raw.get('Computed', raw.get('computedBalance', {}))
            try:
                decimal(computed.get('settledCashForInvestment'))
                decimal(computed.get('cashAvailableForInvestment'))
                decimal(computed.get('cashBuyingPower'))
            except (ValueError, AttributeError):
                blockers.append('Settled cash fields are missing or invalid')
            try:
                reject_open_orders(key)
            except ValueError as exc:
                blockers.append(str(exc))
            if not funded and key not in setting_accounts('ETRADE_LIVE_ACCOUNT_ALLOWLIST'):
                blockers.append('Account not yet enabled on server')
            if not funded and key not in setting_accounts('ETRADE_VERIFIED_CASH_ACCOUNT_KEYS'):
                blockers.append('Cash-only broker permissions not yet verified')
            return jsonify(environment='production', account_ending=str(account.get('accountId',''))[-4:],
                           account_list_mode=account.get('accountMode'), balance_mode=raw.get('accountMode'),
                           blockers=blockers, ready_for_live_configuration=not blockers,
                           orders_enabled=enabled(), order_submitted=False,
                           cash_funded_policy=funded,
                           message=('Cash-funded orders only: fresh settled cash and a verified broker preview are required. '
                                    if funded else '') + 'Account check only. No order was previewed or placed.')
        except Exception as exc:
            return jsonify(environment='production', order_submitted=False,
                           error=str(exc) if isinstance(exc, ValueError) else 'Account verification unavailable'), 409

    @bp.post('/orders/preview')
    def preview():
        token = secrets.token_urlsafe(48)
        digest = token_hash(token)
        claimed = False
        try:
            order = request.get_json(silent=True)
            clean, account, balance = preflight(order)
            now = time.time()
            with database() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE live_orders SET state='EXPIRED' WHERE state='PREVIEWED' AND expires<=?", (now,))
                busy = db.execute("SELECT 1 FROM live_orders WHERE account_key=? AND state IN ('PREPARING','PREVIEWED','SUBMITTING','UNKNOWN','ACCEPTED')", (clean['account_id'],)).fetchone()
                if busy:
                    raise ValueError('An earlier order is pending. Check its status before creating another')
                db.execute('INSERT INTO live_orders VALUES (?,?,?,?,?,?,?,?)',
                           (digest, clean['account_id'], now, now + TTL, 'PREPARING', generation(), '{}', None))
                claimed = True
            payload = {'orderType': 'EQ', 'clientOrderId': secrets.token_hex(10),
                       'Order': [detail_for(clean)]}
            body = broker('POST', clean['account_id'], 'preview', payload={'PreviewOrderRequest': payload}).get('PreviewOrderResponse')
            if not isinstance(body, dict):
                raise ValueError('Broker did not return a preview')
            warning = verify_echo(body, clean, account)
            total, commission = verify_cost(body, clean, balance)
            ids = records(body.get('PreviewIds', body.get('previewIds')))
            if not ids or any(not re.fullmatch(r'[0-9]+', str(v.get('previewId', ''))) for v in ids):
                raise ValueError('Broker preview ID missing')
            payload['PreviewIds'] = ids
            saved = {'order': clean, 'request': payload, 'account_id': str(account['accountId']),
                     'total': total, 'commission': commission, 'warnings': warning,
                     'preview': body}
            with database() as db:
                db.execute("UPDATE live_orders SET state='PREVIEWED',payload=? WHERE token_hash=?", (json.dumps(saved), digest))
            return jsonify(environment='production', preview_token=token, expires_at=now+TTL,
                           order=clean, account_ending=str(account['accountId'])[-4:],
                           estimated_value=total, commission=commission, messages=warning,
                           requires_confirmation=True, submitted=False)
        except Exception as exc:
            if claimed:
                with database() as db:
                    db.execute("UPDATE live_orders SET state='BLOCKED' WHERE token_hash=?", (digest,))
            return jsonify(environment='production', submitted=False,
                           error=str(exc) if isinstance(exc, ValueError) else 'Preview unavailable. No order was placed.'), 409

    @bp.post('/orders/submit')
    def submit():
        data = request.get_json(silent=True)
        if (not isinstance(data, dict) or data.get('confirm') is not True
                or data.get('confirmation_text') != CONFIRMATION
                or not isinstance(data.get('preview_token'), str)
                or len(data['preview_token']) > 128):
            return jsonify(environment='production', error='Explicit confirmation of the exact real-money preview is required'), 400
        digest = token_hash(data['preview_token'])
        try:
            with database() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM live_orders WHERE token_hash=?', (digest,)).fetchone()
                if not row or row['state'] != 'PREVIEWED' or row['expires'] <= time.time() or row['generation'] != generation():
                    return jsonify(environment='production', error='Preview expired or already used. Check E*TRADE before retrying.'), 409
                db.execute("UPDATE live_orders SET state='SUBMITTING' WHERE token_hash=?", (digest,))
                saved = json.loads(row['payload'])
            try:
                clean, account, balance = preflight(saved['order'])
                if clean != saved['order'] or str(account['accountId']) != saved['account_id']:
                    raise ValueError('Account or order details changed. Create a new preview.')
                verify_cost(saved['preview'], clean, balance)
                if not enabled() or row['expires'] <= time.time():
                    raise ValueError('Live submission was disabled or preview expired')
            except Exception:
                with database() as db:
                    db.execute("UPDATE live_orders SET state='BLOCKED' WHERE token_hash=?", (digest,))
                raise
            # Once this call begins, any error is UNKNOWN; NEVER retry it.
            try:
                placed = broker('POST', clean['account_id'], 'place', payload={'PlaceOrderRequest': saved['request']}).get('PlaceOrderResponse')
                if not isinstance(placed, dict):
                    raise ValueError('No broker acceptance response')
                verify_echo(placed, clean, account)
                ids = records(placed.get('OrderIds'))
                if len(ids) != 1 or not re.fullmatch(r'[0-9]+', str(ids[0].get('orderId', ''))):
                    raise ValueError('Broker order ID missing')
                broker_id = str(ids[0]['orderId'])
                with database() as db:
                    db.execute("UPDATE live_orders SET state='ACCEPTED',broker_id=? WHERE token_hash=?", (broker_id,digest))
                return jsonify(environment='production', broker_confirmed=True, status='ACCEPTED',
                               order_id=broker_id, message='Accepted by E*TRADE. This is not a confirmed fill. Manage or cancel the order at E*TRADE.')
            except Exception:
                with database() as db:
                    db.execute("UPDATE live_orders SET state='UNKNOWN' WHERE token_hash=?", (digest,))
                return jsonify(environment='production', broker_confirmed=False, status='UNKNOWN',
                               error='Submission outcome is unknown. Do not retry. Check E*TRADE order history; new orders for this account are blocked.'), 502
        except Exception as exc:
            return jsonify(environment='production', broker_confirmed=False,
                           error=str(exc) if isinstance(exc, ValueError) else 'Submission blocked. Check order history before retrying.'), 409

    @bp.get('/orders/status')
    def order_status():
        token = request.headers.get('X-Preview-Token', '')
        if not token or len(token) > 128:
            return jsonify(environment='production', error='Preview token required'), 400
        with database() as db:
            row = db.execute('SELECT * FROM live_orders WHERE token_hash=? AND generation=?', (token_hash(token), generation())).fetchone()
        if not row:
            return jsonify(environment='production', error='Order session unavailable'), 404
        # Do not infer UNKNOWN success from symbol/size/time guesses. E*TRADE
        # documents that clientOrderId is not emitted in API responses.
        if row['state'] != 'ACCEPTED':
            return jsonify(environment='production', status=row['state'], order_id=row['broker_id'])
        key = row['account_key']
        try:
            verified_account(key)
            marker = None
            for _ in range(20):
                params = {'count': 100}
                if marker:
                    params['marker'] = marker
                body = broker('GET', key, 'orders', params=params).get('OrdersResponse')
                if not isinstance(body, dict):
                    raise ValueError('Order history unavailable')
                for item in records(body.get('Order', [])):
                    if str(item.get('orderId')) == row['broker_id']:
                        details = records(item.get('OrderDetail'))
                        states = {d.get('status') for d in details}
                        terminal = len(states) == 1 and next(iter(states)) in {'EXECUTED','CANCELLED','EXPIRED','REJECTED'}
                        state = next(iter(states)) if len(states) == 1 else 'ACCEPTED'
                        if terminal:
                            with database() as db:
                                db.execute('UPDATE live_orders SET state=? WHERE token_hash=?', (state, row['token_hash']))
                        return jsonify(environment='production', status=state, order_id=row['broker_id'], broker_confirmed=True)
                next_marker = body.get('marker')
                if not next_marker or next_marker == marker:
                    break
                marker = next_marker
            raise ValueError('Order not found in bounded broker history; check E*TRADE directly')
        except Exception as exc:
            return jsonify(environment='production', status='ACCEPTED', order_id=row['broker_id'],
                           error=str(exc) if isinstance(exc, ValueError) else 'Unable to reconcile order history'), 409

    return bp
