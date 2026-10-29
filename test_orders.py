import importlib.util
import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location('backend', Path(__file__).with_name('app.py'))
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)

@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(backend, 'mode', 'sandbox')
    monkeypatch.setattr(backend, 'admin', 'test-only-token')
    monkeypatch.setattr(backend, 'key', 'test-key')
    monkeypatch.setattr(backend, 'secret', 'test-secret')
    monkeypatch.setattr(backend, 'access', {'token': 'test', 'secret': 'test'})
    monkeypatch.setattr(backend, 'ORDER_DB', str(tmp_path / 'orders.db'))
    broker = Mock()
    def respond(method, path, payload=None):
        if 'accounts/list' in path:
            return {'AccountListResponse': {'Accounts': {'Account': [{'accountIdKey': 'abc', 'accountId': '12345678', 'accountDesc': 'Sandbox'}]}}}
        if '/quote/' in path:
            return {'QuoteResponse': {'QuoteData': [{'Product': {'symbol': 'NVDA'}, 'All': {'lastTrade': 150}, 'dateTime': 'Test timestamp'}]}}
        if '/preview' in path:
            return {'PreviewOrderResponse': {'PreviewIds': [{'previewId': 123}], 'totalOrderValue': 150}}
        if '/place' in path:
            return {'PlaceOrderResponse': {'OrderIds': [{'orderId': 456}]}}
        raise AssertionError(path)
    broker.side_effect = respond
    monkeypatch.setattr(backend, 'sandbox_call', broker)
    return backend.app.test_client(), broker

AUTH = {'Authorization': 'Bearer test-only-token', 'Origin': backend.TRADING_ORIGIN}
ORDER = dict(symbol='NVDA', side='BUY', account_id='abc', quantity=1, order_type='MARKET')

def preview(client, **changes):
    return client.post('/trading/orders/preview', json={**ORDER, **changes}, headers=AUTH)

def places(broker):
    return [c for c in broker.call_args_list if '/place' in c.args[1]]

def test_auth_and_csrf(setup):
    client, broker = setup
    assert client.get('/trading/accounts').status_code == 401
    assert client.get('/trading/accounts', headers={**AUTH, 'Origin': 'https://evil.example'}).status_code == 403
    assert not broker.called
    preflight = client.options('/trading/orders/submit', headers={'Origin': backend.TRADING_ORIGIN})
    assert preflight.status_code == 204
    assert preflight.headers['Access-Control-Allow-Origin'] == backend.TRADING_ORIGIN

def test_production_is_blocked(setup, monkeypatch):
    client, broker = setup
    monkeypatch.setattr(backend, 'mode', 'production')
    for path in ['/trading/accounts', '/trading/quote', '/trading/status']:
        assert client.get(path, headers=AUTH).status_code == 403
    assert preview(client).status_code == 403
    assert client.post('/trading/orders/submit', json={'confirm': True, 'preview_token': 'x'}, headers=AUTH).status_code == 403
    assert not broker.called

def test_accounts_quote_and_connection(setup, monkeypatch):
    client, broker = setup
    assert client.get('/trading/accounts', headers=AUTH).json['accounts'][0]['label'].endswith('5678')
    assert client.get('/trading/quote?symbol=NVDA', headers=AUTH).json['price'] == 150
    monkeypatch.setattr(backend, 'access', None)
    assert client.get('/trading/accounts', headers=AUTH).status_code == 409

@pytest.mark.parametrize('change', [{'quantity': 0}, {'quantity': 1.5}, {'quantity': True}, {'side': 'SELL_SHORT'}, {'account_id': 'other'}, {'symbol': '../x'}, {'order_type': 'LIMIT', 'limit_price': -1}, {'order_type': 'LIMIT', 'limit_price': 1.234}, {'order_type': 'LIMIT', 'limit_price': 'NaN'}])
def test_invalid_orders(setup, change):
    client, broker = setup
    assert preview(client, **change).status_code == 400
    assert not places(broker)

def test_preview_is_not_submission_and_confirmation_is_required(setup):
    client, broker = setup
    p = preview(client).json
    assert p['order'] == ORDER
    assert not places(broker)
    assert client.post('/trading/orders/submit', json={'preview_token': p['preview_token']}, headers=AUTH).status_code == 400
    assert not places(broker)

def test_immutable_limit_sell_and_replay_protection(setup):
    client, broker = setup
    p = preview(client, side='SELL', order_type='LIMIT', limit_price=151.25, quantity=2).json
    body = {'preview_token': p['preview_token'], 'confirm': True, 'quantity': 1000, 'side': 'BUY'}
    response = client.post('/trading/orders/submit', json=body, headers=AUTH)
    assert response.json['status'] == 'ACCEPTED'
    sent = places(broker)[0].args[2]['PlaceOrderRequest']
    assert sent['Order'][0]['Instrument'][0]['orderAction'] == 'SELL'
    assert sent['Order'][0]['Instrument'][0]['quantity'] == 2
    assert sent['Order'][0]['limitPrice'] == '151.25'
    assert sent['PreviewIds'] == [{'previewId': 123}]
    assert len(sent['clientOrderId']) == 20
    assert client.post('/trading/orders/submit', json=body, headers=AUTH).status_code == 409
    assert len(places(broker)) == 1

def test_expired_or_forged_preview(setup):
    client, broker = setup
    p = preview(client).json
    with backend.order_db() as db:
        db.execute('UPDATE previews SET expires=0')
    for token in [p['preview_token'], 'forged']:
        assert client.post('/trading/orders/submit', json={'preview_token': token, 'confirm': True}, headers=AUTH).status_code == 409
    assert not places(broker)

@pytest.mark.parametrize('response', [{}, {'PlaceOrderResponse': {}}, {'PlaceOrderResponse': {'OrderIds': [{'orderId': 456}], 'Messages': {'Message': [{'type': 'ERROR', 'description': 'Rejected'}]}}}])
def test_no_false_success_and_no_retry(setup, response):
    client, broker = setup
    p = preview(client).json
    broker.side_effect = lambda *args: response
    body = {'preview_token': p['preview_token'], 'confirm': True}
    result = client.post('/trading/orders/submit', json=body, headers=AUTH)
    assert result.status_code == 502
    assert result.json['broker_confirmed'] is False
    assert client.post('/trading/orders/submit', json=body, headers=AUTH).status_code == 409

def test_timeout_never_retries(setup):
    client, broker = setup
    p = preview(client).json
    broker.side_effect = TimeoutError()
    body = {'preview_token': p['preview_token'], 'confirm': True}
    assert client.post('/trading/orders/submit', json=body, headers=AUTH).status_code == 502
    assert client.post('/trading/orders/submit', json=body, headers=AUTH).status_code == 409

def test_transport_uses_sandbox_literal_host_no_redirects(setup, monkeypatch):
    client, broker = setup
    # Exercise original transport, not the mocked helper.
    source_spec = importlib.util.spec_from_file_location('transport', Path(__file__).with_name('app.py'))
    transport = importlib.util.module_from_spec(source_spec)
    source_spec.loader.exec_module(transport)
    transport.key, transport.secret, transport.access = 'x', 'y', {'token': 'z', 'secret': 'w'}
    transport.mode = 'sandbox'
    session = Mock()
    session.request.return_value.status_code = 200
    session.request.return_value.json.return_value = {}
    monkeypatch.setattr(transport, 'OAuth1Session', Mock(return_value=session))
    transport.sandbox_call('POST', '/v1/accounts/abc/orders/place.json', {})
    assert session.request.call_args.args[1].startswith('https://apisb.etrade.com/')
    assert session.request.call_args.kwargs['allow_redirects'] is False

