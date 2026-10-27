import os
import time
import secrets
from flask import Flask, jsonify, request, Response
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
</style></head><body><main><h1>Connect E*TRADE</h1><p>Test connection only. No buying or selling is enabled.</p><p><small>Use the ADMIN_TOKEN saved in Render, NOT your E*TRADE Consumer Secret. This page does not save your token to browser storage.</small></p><label for="admin">Render ADMIN_TOKEN</label><input id="admin" type="password" autocomplete="off"><button id="start">1. Start E*TRADE authorization</button><section id="verification" class="hidden"><p><a id="authorize" target="_blank" rel="noopener noreferrer">Open E*TRADE authorization page</a></p><p>Approve access at E*TRADE, then enter the verification code below.</p><label for="verifier">E*TRADE verification code</label><input id="verifier" autocomplete="off"><button id="finish">2. Complete connection</button></section><button id="test" class="hidden">3. Test account connection</button><div id="status" role="status" aria-live="polite"></div></main><script>
const el=id=>document.getElementById(id), show=s=>el('status').textContent=s;
async function api(path,method='POST',data){const token=el('admin').value.trim();if(!token)throw Error('Enter Render ADMIN_TOKEN first.');const r=await fetch(path,{method,headers:{Authorization:'Bearer '+token,...(data?{'Content-Type':'application/json'}:{})},body:data?JSON.stringify(data):undefined,cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');return d}
el('start').onclick=async()=>{try{show('Requesting E*TRADE authorization link...');const d=await api('/connect/start');const u=new URL(d.authorize_url);if(u.protocol!=='https:'||u.hostname!=='us.etrade.com')throw Error('Unexpected authorization address');el('authorize').href=u.toString();el('verification').classList.remove('hidden');show('Open the E*TRADE authorization page and approve access.')}catch(e){show(e.message)}};
el('finish').onclick=async()=>{try{await api('/connect/finish','POST',{verifier:el('verifier').value.trim()});el('verifier').value='';el('test').classList.remove('hidden');show('E*TRADE authorization completed. Test account access next.')}catch(e){show(e.message)}};
el('test').onclick=async()=>{try{const d=await api('/accounts','GET');const a=d.Accounts?.Account||[];show('Account API request succeeded. Number of accounts: '+(Array.isArray(a)?a.length:1)+'.')}catch(e){show(e.message)}};
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
    if request.path in ('/health', '/connect-ui'):
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
