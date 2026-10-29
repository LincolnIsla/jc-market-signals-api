"""Offline production order tests. No broker network access or real orders."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from flask import Flask, request
from production_live import create_live_blueprint, CONFIRMATION, database

ORIGIN = 'https://jc-market-signals.brave-rain-7832.chatgpt.site'
ACCOUNT = {'accountIdKey': 'safe', 'accountId': '123456789',
           'accountStatus': 'ACTIVE', 'accountMode': 'CASH',
           'institutionType': 'BROKERAGE'}
BALANCE = {'accountId': '123456789', 'accountMode': 'CASH',
           'institutionType': 'BROKERAGE', 'Computed': {
               'settledCashForInvestment': 2000,
               'cashAvailableForInvestment': 2000, 'cashBuyingPower': 2000}}
ORDER = {'account_id': 'safe', 'symbol': 'NVDA', 'side': 'BUY',
         'order_type': 'LIMIT', 'limit_price': '100.00', 'quantity': 2}


class Response:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class Broker:
    def __init__(self):
        self.calls = []
        self.account = copy.deepcopy(ACCOUNT)
        self.balance = copy.deepcopy(BALANCE)
        self.open_order = False
        self.place_timeout = False
        self.echo_mismatch = False
        self.negative_settled_bp = False
        self.positions_qty = 3
        self.position_page = False
        self.order_state = 'OPEN'

    def request(self, method, url, **kwargs):
        assert url.startswith('https://api.etrade.com/v1/accounts/')
        assert kwargs['allow_redirects'] is False
        self.calls.append((method, url, kwargs))
        if url.endswith('/list.json'):
            return Response({'AccountListResponse': {'Accounts': {'Account': [self.account]}}})
        if url.endswith('/balance.json'):
            return Response({'BalanceResponse': self.balance})
        if url.endswith('/portfolio.json'):
            return Response({'PortfolioResponse': {'AccountPortfolio': [{
                'accountId': ACCOUNT['accountId'],
                'nextPageNo': 2 if self.position_page and kwargs['params']['pageNumber'] == 1 else None,
                'Position': [{'Product': {'symbol': 'NVDA', 'securityType': 'EQ'},
                              'positionType': 'LONG', 'quantity': self.positions_qty}]}]}})
        if url.endswith('/orders.json'):
            if 'status' in kwargs['params']:
                return Response({'OrdersResponse': {'Order': [{'orderId': 8}] if self.open_order else []}})
            return Response({'OrdersResponse': {'Order': [{'orderId': 44, 'OrderDetail': [{'status': self.order_state}]}]}})
        if method == 'POST' and '/orders/' in url:
            payload = kwargs['json'].get('PreviewOrderRequest', kwargs['json'].get('PlaceOrderRequest'))
            detail = copy.deepcopy(payload['Order'])
            if self.echo_mismatch:
                detail[0]['Instrument'][0]['Product']['symbol'] = 'WRONG'
            common = {'accountId': ACCOUNT['accountId'], 'orderType': 'EQ',
                      'Order': detail, 'totalOrderValue': 200, 'totalCommission': 0,
                      'cashBpDetails': {'settled': {'netBp': -1 if self.negative_settled_bp else 1800,
                                                  'currentOrderImpact': 200}}}
            if url.endswith('/preview.json'):
                common['PreviewIds'] = [{'previewId': 3}]
                return Response({'PreviewOrderResponse': common})
            if self.place_timeout:
                raise TimeoutError('uncertain network outcome')
            common['OrderIds'] = [{'orderId': 44}]
            return Response({'PlaceOrderResponse': common})
        raise AssertionError('Unexpected broker operation')


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.env = patch.dict(os.environ, {
            'ADMIN_TOKEN': 'test-admin', 'ETRADE_LIVE_ORDERS_ENABLED': 'true',
            'ETRADE_LIVE_ACCOUNT_ALLOWLIST': '["safe"]',
            'ETRADE_VERIFIED_CASH_ACCOUNT_KEYS': '["safe"]',
            'LIVE_ORDER_DB_PATH': self.temp.name+'/orders.db'})
        self.env.start()
        self.clock = patch('production_live.datetime')
        mocked = self.clock.start()
        mocked.now.return_value = datetime(2026, 10, 29, 11, 0)
        self.broker = Broker()
        app = Flask(__name__)
        app.register_blueprint(create_live_blueprint(
            lambda: request.headers.get('Authorization') == 'Bearer test-admin',
            lambda: self.broker))
        self.client = app.test_client()
        self.headers = {'Authorization': 'Bearer test-admin', 'Origin': ORIGIN}

    def tearDown(self):
        self.clock.stop()
        self.env.stop()
        self.temp.cleanup()

    def preview(self, order=None):
        return self.client.post('/live-trading/orders/preview', headers=self.headers, json=order or ORDER)

    def submit(self, token, **overrides):
        return self.client.post('/live-trading/orders/submit', headers=self.headers, json={
            'preview_token': token, 'confirm': True,
            'confirmation_text': CONFIRMATION, **overrides})

    def placements(self):
        return [c for c in self.broker.calls if c[1].endswith('/place.json')]

    def test_default_kill_switch(self):
        os.environ.pop('ETRADE_LIVE_ORDERS_ENABLED')
        self.assertEqual(self.preview().status_code, 403)
        self.assertEqual(self.broker.calls, [])

    def test_auth_and_csrf_before_broker(self):
        self.assertEqual(self.client.post('/live-trading/orders/preview',json=ORDER).status_code,401)
        h = {**self.headers, 'Origin': 'https://unexpected.test'}
        self.assertEqual(self.client.post('/live-trading/orders/preview',headers=h,json=ORDER).status_code,403)
        self.assertEqual(self.broker.calls, [])

    def test_requires_allowlist_and_verified_permissions(self):
        for name in ('ETRADE_LIVE_ACCOUNT_ALLOWLIST', 'ETRADE_VERIFIED_CASH_ACCOUNT_KEYS'):
            with patch.dict(os.environ, {name:'[]'}):
                self.assertEqual(self.preview().status_code,409)
        self.assertEqual(self.broker.calls, [])

    def test_classification_mismatch_blocks(self):
        self.broker.balance['accountMode'] = 'MARGIN'
        self.assertEqual(self.preview().status_code,409)
        self.assertFalse(any(c[0]=='POST' for c in self.broker.calls))

    def test_balance_identity_mismatch_blocks(self):
        self.broker.balance['accountId'] = 'other'
        self.assertEqual(self.preview().status_code,409)
        self.assertFalse(any(c[0]=='POST' for c in self.broker.calls))

    def test_open_orders_block(self):
        self.broker.open_order = True
        self.assertEqual(self.preview().status_code,409)
        self.assertFalse(any(c[0]=='POST' for c in self.broker.calls))

    def test_missing_cash_and_insufficient_cash(self):
        self.broker.balance['Computed']['settledCashForInvestment'] = 1
        self.assertEqual(self.preview().status_code,409)
        self.broker.balance['Computed'].pop('settledCashForInvestment')
        self.assertEqual(self.preview().status_code,409)
        self.assertFalse(any(c[0]=='POST' for c in self.broker.calls))

    def test_limit_whole_shares_and_cap(self):
        for override in ({'quantity':True},{'quantity':1.2},{'quantity':11},
                         {'side':'SELL_SHORT'},{'order_type':'MARKET'},
                         {'symbol':'../../other'},{'limit_price':'NaN'}):
            self.assertEqual(self.preview({**ORDER, **override}).status_code,409)
        self.assertFalse(any(c[0]=='POST' for c in self.broker.calls))

    def test_preview_echo_must_match(self):
        self.broker.echo_mismatch = True
        self.assertEqual(self.preview().status_code,409)
        self.assertFalse(self.placements())

    def test_broker_negative_settled_buying_power_blocks(self):
        self.broker.negative_settled_bp = True
        self.assertEqual(self.preview().status_code,409)

    def test_preview_does_not_place_and_requires_exact_confirmation(self):
        r = self.preview()
        self.assertEqual(r.status_code,200,r.json)
        self.assertFalse(self.placements())
        for override in ({'confirm':False},{'confirm':'true'},{'confirmation_text':'OK'}):
            self.assertEqual(self.submit(r.json['preview_token'],**override).status_code,400)
        self.assertFalse(self.placements())

    def test_confirmed_submit_once_and_cash_refreshed(self):
        r = self.preview(); token = r.json['preview_token']
        placed = self.submit(token)
        self.assertEqual(placed.status_code,200,placed.json)
        self.assertEqual(placed.json['status'],'ACCEPTED')
        self.assertNotIn('fill_price',placed.json)
        self.assertEqual(len(self.placements()),1)
        self.assertEqual(self.submit(token).status_code,409)
        self.assertEqual(len(self.placements()),1)
        self.assertEqual(sum(c[1].endswith('/balance.json') for c in self.broker.calls),2)

    def test_submit_rechecks_cash_before_placement(self):
        r = self.preview()
        self.broker.balance['Computed']['settledCashForInvestment'] = 1
        self.assertEqual(self.submit(r.json['preview_token']).status_code,409)
        self.assertFalse(self.placements())

    def test_kill_switch_between_preview_and_submit(self):
        r = self.preview()
        os.environ['ETRADE_LIVE_ORDERS_ENABLED'] = 'false'
        self.assertEqual(self.submit(r.json['preview_token']).status_code,403)
        self.assertFalse(self.placements())

    def test_expired_or_revoked_preview(self):
        r = self.preview()
        with database() as db:
            db.execute('UPDATE live_orders SET expires=0')
        self.assertEqual(self.submit(r.json['preview_token']).status_code,409)
        self.assertFalse(self.placements())

    def test_admin_rotation_revokes_preview(self):
        r = self.preview()
        os.environ['ADMIN_TOKEN'] = 'rotated'
        self.assertEqual(self.submit(r.json['preview_token']).status_code,409)
        self.assertFalse(self.placements())

    def test_unknown_never_retries_and_blocks_next_order(self):
        r = self.preview();token=r.json['preview_token']
        self.broker.place_timeout=True
        self.assertEqual(self.submit(token).json['status'],'UNKNOWN')
        self.assertEqual(self.submit(token).status_code,409)
        self.assertEqual(self.preview().status_code,409)
        self.assertEqual(len(self.placements()),1)

    def test_second_preview_blocked(self):
        self.assertEqual(self.preview().status_code,200)
        self.assertEqual(self.preview().status_code,409)

    def test_sell_no_short_sale(self):
        self.assertEqual(self.preview({**ORDER,'side':'SELL','quantity':4}).status_code,409)
        self.assertEqual(self.preview({**ORDER,'side':'SELL'}).status_code,200)

    def test_holdings_account_mismatch_blocks(self):
        original=self.broker.request
        def mismatched(method,url,**kwargs):
            r=original(method,url,**kwargs)
            if url.endswith('/portfolio.json'):
                r.data['PortfolioResponse']['AccountPortfolio'][0]['accountId']='other'
            return r
        self.broker.request=mismatched
        self.assertEqual(self.preview({**ORDER,'side':'SELL'}).status_code,409)

    def test_accepted_stays_blocked_until_broker_terminal(self):
        r=self.preview();token=r.json['preview_token'];self.submit(token)
        self.assertEqual(self.preview().status_code,409)
        h={**self.headers,'X-Preview-Token':token}
        self.assertEqual(self.client.get('/live-trading/orders/status',headers=h).json['status'],'OPEN')
        self.broker.order_state='EXECUTED'
        self.assertEqual(self.client.get('/live-trading/orders/status',headers=h).json['status'],'EXECUTED')
        self.assertEqual(self.preview().status_code,200)

    def test_non_durable_database_blocks(self):
        os.environ['LIVE_ORDER_DB_PATH']='/tmp/live.sqlite3'
        self.assertEqual(self.preview().status_code,409)
        self.assertFalse(any(c[0]=='POST' for c in self.broker.calls))


if __name__ == '__main__':
    unittest.main()
