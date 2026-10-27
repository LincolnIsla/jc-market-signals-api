# JC Market Signals — E*TRADE API

Initial read-only backend. No trading endpoints. Does not contain API keys.

## Render
Create a **Python Web Service** with repository `jc-market-signals-api`.
Build command: `pip install -r requirements.txt`
Start command: `gunicorn app:app`

Set these as secret Render environment variables:
- `ETRADE_CONSUMER_KEY` — matching Sandbox key (test first)
- `ETRADE_CONSUMER_SECRET` — matching Sandbox secret
- `ETRADE_ENV` — `sandbox` (later `production` for live account data)
- `ADMIN_TOKEN` — long random secret used to protect API endpoints

`GET /health` is public. Every other endpoint requires `Authorization: Bearer <ADMIN_TOKEN>`.

## Authorization (server-side test only)
- `POST /connect/start`: returns an E*TRADE authorization URL. Open it and log in at E*TRADE to get a verification code.
- `POST /connect/finish` with JSON `{"verifier":"YOUR_CODE"}`: establishes a session.
- `GET /accounts`: reads E*TRADE accounts in the selected environment.

The OAuth access token is held only in memory, so a server restart clears it. E*TRADE may require another authorization at midnight US Eastern time. The sandbox returns example data, not your account's live holdings. For production use, add session management and persistent encrypted token storage. **Do not expose ADMIN_TOKEN to a browser application or ChatGPT Sites frontend.** A secure server-side authentication/proxy layer is required before connecting Sites. Trading remains disabled.
