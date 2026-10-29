# Real-money order integration

This implementation is **disabled by default**. Installing or opening the
page never enables trading and never submits an order. Existing sandbox
credentials and endpoints remain separate.

## Activation prerequisites

1. Authorize the separate production connection on the live-order page.
2. Select the intended account and run **Check account — no trade**.
3. Verify the actual production account, balance, settled-cash, open-order,
   holdings and broker-preview response mappings. Tests use synthetic data;
   they do not establish compatibility with a particular live account.
4. Resolve any CASH/MARGIN discrepancy with E*TRADE before activation. This
   first release intentionally refuses mismatching classifications. A
   positive cash balance is not evidence of cash-only broker permissions.
5. Configure the exact approved account keys in Render, as JSON arrays:
   `ETRADE_LIVE_ACCOUNT_ALLOWLIST` and `ETRADE_VERIFIED_CASH_ACCOUNT_KEYS`.
   The second list means cash-only broker permissions were independently
   verified. Never populate it merely to bypass a diagnostic failure.
6. `LIVE_ORDER_DB_PATH` defaults to
   `/var/data/jc-live-orders.sqlite3` on the existing persistent disk.
7. Only after validation, set `ETRADE_LIVE_ORDERS_ENABLED=true` to allow
   broker previews and explicit placements. Remove or set this flag to
   `false` to disable both routes. The flag is checked again at submission.

The settings above are deliberately **not** provided by the browser or set
by the account-check button. Do not change `ETRADE_ENV` or sandbox keys.
Production OAuth is shared with the existing production reader, stays on
the server, and requires reconnection after a Render restart.

## Order behavior

- Stock limit orders only, whole shares, regular session, good for day.
- $1,000 maximum notional per order. BUY broker-estimated cost plus
  commission must also fit within $1,000 and verified settled cash.
- Available settled cash is checked at both preview and placement, with
  the existing conservative 2% affordability reserve.
- SELL requires sufficient verified long stock holdings. Holdings pages
  are fetched up to a bounded limit; missing or malformed pages block.
- Any OPEN or CANCEL_REQUESTED broker order blocks a new order. This is
  deliberately conservative about reservations and external activity.
- The broker preview must echo the exact account, symbol, action, whole
  quantity, limit, session and term. BUY previews require settled cash
  buying-power information with no negative balance.
- The user must review the immutable preview, check the real-money
  confirmation box, and click **Confirm & Place Real-Money Order**.
- Durable SQLite atomically claims each preview once across workers.
  A timeout after placement starts becomes UNKNOWN and cannot be retried.
- ACCEPTED means broker acceptance, not a fill. Broker history must
  confirm a terminal status before this account can place another order.
- Cancel/manage orders directly at E*TRADE in this release.

## Operational limits

UNKNOWN and interrupted PREPARING/SUBMITTING records keep the account
blocked until an operator reconciles them against E*TRADE. Never clear them
on a timer or infer success from matching symbol, amount and time. The API
documentation says clientOrderId is not present in API responses, so an
UNKNOWN placement cannot be safely resolved by guessing at that field.

Regular-session timing uses Eastern weekday hours. The broker may reject
orders on holidays or early-close days; warnings are displayed, and orders
are always marked REGULAR and GOOD_FOR_DAY. The app does not claim to
implement a full exchange calendar, automatic execution, stop orders or
profit guarantees. Scanner prices never prefill the live limit price.

## Validation

`python -m unittest discover -s tests -p 'test_production*.py'`

`python -m pytest -q test_orders.py test_sessions.py --basetemp=<workspace-dir>`

Official E*TRADE references reviewed for this implementation:

- https://apisb.etrade.com/docs/api/order/api-order-v1.html
- https://apisb.etrade.com/docs/api/account/api-balance-v1.html
- https://apisb.etrade.com/docs/api/account/api-portfolio-v1.html
