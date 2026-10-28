# JC Market Signals sandbox orders

The Sites frontend calls only https://jc-market-signals-api.onrender.com for accounts, quotes, preview and submission. This patch preserves the existing E*TRADE OAuth connection flow.

## Deploy the backend

Replace app.py in LincolnIsla/jc-market-signals-api with the included app.py and deploy the main branch on the existing Render service. No dependency changes are required. Keep ETRADE_ENV=sandbox, the sandbox ETRADE_CONSUMER_KEY/ETRADE_CONSUMER_SECRET, and your existing ADMIN_TOKEN in Render. Never put these values in source files or chat.

The deployment restarts the process, so reconnect E*TRADE using /connect-ui. This preserves the existing in-memory OAuth behavior: reconnect after subsequent process restarts as well. Current service runs one instance; do not scale instances or increase Gunicorn workers without moving OAuth sessions and preview storage into a shared store. Preview claims are shared through SQLite within the current instance. They expire after two minutes and become invalid after a deploy. No automatic retries of a submission are made. If a request has an uncertain outcome, inspect E*TRADE sandbox order history before placing another.

## Use the feature

1. Open JC Market Signals and run a connected-data stock scan.
2. Click BUY or SELL. Enter the Render ADMIN_TOKEN in the order window (never the broker consumer secret).
3. Load sandbox accounts and price, select your account, enter whole shares and Market or Limit.
4. Choose Review Order to obtain an E*TRADE preview. No order is submitted yet.
5. Review the details, check the explicit confirmation box, and choose Confirm & Submit Sandbox Order.

A valid broker order ID from PlaceOrderResponse is shown as ACCEPTED, never as an executed fill. Errors, missing broker confirmation, and timeouts never show success. Production mode is blocked at both layers. Fictional sample scan cards open the window but cannot preview or submit orders.

## Verification

20 backend tests pass with mock broker responses. They cover auth, allowed origin/preflight, production blocking, quantity/limit/account validation, preview/confirm separation, immutable order details, preview expiry, replay blocking, no false acceptance, and timeout handling. Frontend behavior tests and existing backtest checks also pass. No order has been sent to E*TRADE as part of verification.

Install pytest separately for development, then run `pytest -q tests/test_orders.py`.
