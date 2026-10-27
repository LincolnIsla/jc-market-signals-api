import os
import time
import secrets
from flask import Flask, jsonify, request
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

def ok_auth():
    provided = request.headers.get('Authorization', '')
    return bool(admin) and secrets.compare_digest(provided, 'Bearer ' + admin)

@app.after_request
def headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@app.get('/health')
def health():
    return jsonify(status='ok', environment=mode, connected=access is not None)

@app.before_request
def protect():
    if request.path == '/health':
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
        pending = {'token':result['oauth_token'], 'secret':result['oauth_token_secret'], 'time':time.time()}
        return jsonify(authorize_url=oauth.authorization_url('https://us.etrade.com/e/t/etws/authorize', key=key))
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
        access = {'token':result['oauth_token'], 'secret':result['oauth_token_secret']}
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
        response = oauth.get(api_base + '/v1/accounts/list', headers={'Accept':'application/json'}, timeout=15)
        response.raise_for_status()
        return jsonify(response.json())
    except Exception:
        app.logger.exception('E*TRADE accounts failure')
        return jsonify(error='E*TRADE account request failed; authorization may have expired'), 502
