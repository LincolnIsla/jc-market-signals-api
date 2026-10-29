import hashlib
import importlib.util
from pathlib import Path
from unittest.mock import Mock
import pytest

spec=importlib.util.spec_from_file_location('sessionbackend',Path(__file__).with_name('app.py'))
b=importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
ORIGIN={'Origin':b.TRADING_ORIGIN}
AUTH={**ORIGIN,'Authorization':'Bearer test-login-secret'}

@pytest.fixture
def client(monkeypatch,tmp_path):
    monkeypatch.setattr(b,'admin','test-login-secret')
    monkeypatch.setattr(b,'mode','sandbox')
    monkeypatch.setattr(b,'key','test')
    monkeypatch.setattr(b,'secret','test')
    monkeypatch.setattr(b,'access',{'token':'test','secret':'test'})
    monkeypatch.setattr(b,'SESSION_DB_PATH',str(tmp_path/'sessions.db'))
    monkeypatch.setattr(b,'sandbox_accounts',lambda:[{'accountIdKey':'abc','accountId':'1234'}])
    return b.app.test_client()

def login(c,remember=True):
    return c.post('/auth/login',json={'remember_device':remember},headers=AUTH,base_url='https://site.test')

def test_secure_persistent_cookie_and_hashed_records(client):
    r=login(client)
    assert r.status_code==200
    cookie=r.headers['Set-Cookie']
    for expected in ['__Host-jc_device=','Secure','HttpOnly','SameSite=Lax','Path=/','Max-Age=2592000']:
        assert expected in cookie
    assert 'Domain=' not in cookie
    assert 'test-login-secret' not in cookie
    assert 'token' not in r.json
    raw=cookie.split(';')[0].split('=',1)[1]
    with b.session_db() as db:
        row=db.execute('SELECT * FROM device_sessions').fetchone()
        assert row['id_hash']==hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in str(dict(row))
    assert client.get('/auth/session',base_url='https://site.test').json['authenticated'] is True
    assert client.get('/trading/accounts',base_url='https://site.test').status_code==200

def test_browser_restart_restores_cookie_but_logout_revokes_copy(client):
    r=login(client);raw=r.headers['Set-Cookie'].split(';')[0].split('=',1)[1]
    reopened=b.app.test_client();reopened.set_cookie(b.DEVICE_COOKIE,raw,domain='site.test')
    assert reopened.get('/auth/session',base_url='https://site.test').json['authenticated'] is True
    assert client.post('/auth/logout',json={},headers=ORIGIN,base_url='https://site.test').status_code==200
    assert reopened.get('/auth/session',base_url='https://site.test').json['authenticated'] is False
    assert reopened.get('/trading/accounts',base_url='https://site.test').status_code==401

def test_csrf_and_login_validation(client):
    assert client.post('/auth/login',json={'remember_device':True},headers={'Authorization':'Bearer test-login-secret'}).status_code==403
    assert client.post('/auth/login',json={'remember_device':True},headers=ORIGIN).status_code==401
    assert client.post('/auth/login',json={},headers=AUTH).status_code==400
    login(client)
    assert client.post('/auth/logout',json={},headers={'Origin':'https://evil.test'},base_url='https://site.test').status_code==403
    assert client.post('/trading/orders/submit',json={'confirm':True,'preview_token':'forged'},base_url='https://site.test').status_code==401

def test_idle_expiry_absolute_expiry_and_admin_rotation(client,monkeypatch):
    for sql in ['UPDATE device_sessions SET touched=0','UPDATE device_sessions SET expires=0']:
        login(client)
        with b.session_db() as db:db.execute(sql)
        assert client.get('/auth/session',base_url='https://site.test').json['authenticated'] is False
    login(client);monkeypatch.setattr(b,'admin','rotated')
    assert client.get('/auth/session',base_url='https://site.test').json['authenticated'] is False

def test_all_devices_and_rotation(client):
    r=login(client);old=r.headers['Set-Cookie'].split(';')[0].split('=',1)[1]
    login(client)
    other=b.app.test_client();other.set_cookie(b.DEVICE_COOKIE,old,domain='site.test')
    assert other.get('/auth/session',base_url='https://site.test').json['authenticated'] is False
    r=login(other)
    assert client.post('/auth/logout-all',json={},headers=ORIGIN,base_url='https://site.test').status_code==200
    assert other.get('/auth/session',base_url='https://site.test').json['authenticated'] is False

def test_storage_required_session_cookie_and_rate_limit(client,monkeypatch):
    assert 'Max-Age' not in login(client,False).headers['Set-Cookie']
    with b.session_db() as db:
        db.executemany('INSERT INTO device_login_attempts VALUES (?)',[(b.time.time(),)]*30)
    assert login(client).status_code==429
    monkeypatch.setattr(b,'SESSION_DB_PATH','')
    assert client.get('/auth/session').status_code==503
    assert login(client).status_code==503

def test_production_login_uses_secure_session(client,monkeypatch):
    monkeypatch.setattr(b,'mode','production')
    assert login(client).status_code==200

