# JC Market Signals: staged production-trading guardrails

**Status: NOT LIVE. No code here places real-money orders.**

This branch includes a standalone Python module, `production_checks.py`,
for *future* production order validation and offline unit tests. It is not
imported by `app.py`. Render deploys only the `main` branch. The working
E*TRADE Sandbox remains unchanged.

## Offline test

```
python -m unittest discover -s tests -p 'test_production_checks.py' -v
```

Synthetic test account and balance fields are examples, not proof of
E*TRADE production response compatibility.

## Fail-closed checks

- Account must be verified CASH, active BROKERAGE, account IDs must agree.
- Long-only, whole-share stock limit orders during regular trading hours.
- Notional maximum of $1,000 per order, on both buy and sell.
- Buying requires broker-confirmed settled cash, cash available to invest
  and cash buying power, with a 2% cost allowance.
- Selling requires verified available shares (no short sales).
- Missing, malformed, inconsistent, or non-finite data blocks trading.

## Before real-money trading is enabled

1. Keep `ETRADE_ENV=sandbox` and the sandbox credentials unchanged. Introduce
   a *separately configured* production connection; never reuse sandbox OAuth.
2. Verify E*TRADE's actual production read-only account, balance, and positions
   structures. Map fields explicitly; never assume the sample shapes above.
3. Use a separate, secure production account allowlist and access controls;
   do not let the browser choose arbitrary brokerage account identifiers.
4. Acquire fresh broker balances and available positions immediately before
   **both** preview and placement. Handle open orders and reserved funds.
5. Call broker production preview and verify the exact account, side, symbol,
   quantity, limit and warnings. Preview must never place an order.
6. Require an explicit, separate confirmation and a server-revocable kill switch
   defaulting to **OFF**; require fresh authorization for the live order.
7. Persist order/idempotency state on /var/data, reconcile UNKNOWN outcomes
   with E*TRADE order history, and never blindly retry network timeouts.
8. Address cross-site authentication/session cookies and CSRF protections.
   Avoid storing admin tokens in localStorage or logging secrets.
9. Add order-history reconciliation, confirmed fill statuses, cancellation,
   daily loss limits, exposure checks, and broker/cash-account settlement rules.
10. Run offline, sandbox, and broker-approved production read-only tests and
    obtain explicit approval before enabling any production order placement.

The existing backend currently uses a literal Sandbox API host and blocks
`ETRADE_ENV=production`. Changing that variable alone does not activate live
trading; it disables orders.

Official API reference: https://developer.etrade.com/getting-started

## Staged production account reader (not deployed)

The development branch also includes `production_readonly.py` and
`tests/test_production_readonly.py`. The reader is registered by `app.py`
**only** when `ETRADE_PRODUCTION_READONLY_ENABLED=true` and the code is
actually deployed. It is currently not present in the Render `main` deploy.

- `ETRADE_PRODUCTION_CONSUMER_KEY`: the separate **production** application key.
- `ETRADE_PRODUCTION_CONSUMER_SECRET`: its production secret.
- `ETRADE_PRODUCTION_READONLY_ENABLED`: default absent/false, so no routes.
- Do NOT change `ETRADE_ENV=sandbox`, `ETRADE_CONSUMER_KEY`, or
  `ETRADE_CONSUMER_SECRET` for the existing Sandbox application.
- Do NOT put credentials in chat, browser localStorage, GitHub or query URLs.

Once validated and deployed for read-only testing, use the same-origin UI:
`https://jc-market-signals-api.onrender.com/production-readonly/connect-ui`.
It requires the Render ADMIN_TOKEN when making requests and gives options to
authorize production separately, list accounts, inspect balance and holdings.

Important: **The E*TRADE OAuth approval itself is not read-only**. Our
application limits its own broker REST requests to GET account/list,
balance and portfolio; it has no production preview or place endpoints.
Sessions are in memory and will be lost on app restarts. The account reader
exposes a first page of portfolio positions only; it must never be used
directly for sell validation until pagination and pending orders are handled.

Offline unit tests (mock broker, no network):
```
python -m unittest discover -s tests -p 'test_production*.py' -v
```
The GitHub workflow stages the same tests on the development branch.
A passing test is **not** evidence of broker production compatibility.

## Fixed-dollar BUY sizing (staged, not connected to a trading route)

\`production_checks.calculate_budgeted_buy(..., budget="1000.00")\`
creates a proposed whole-share **limit BUY**; it does not place or
preview anything with the broker. It rounds DOWN using exact decimal
arithmetic.

For example, a $1,000 budget and $185.00 limit price yields **5 shares**,
$925 at the limit, and an additional $18.50 (2%) allowance. The remainder
of the spending budget is untouched. If 1 share cannot fit inside both
budget and available settled cash after the allowance, the order is
blocked instead of rounded up.

Budget is a ceiling on the *estimated gross cost including the 2%
allowance*, whereas the existing $1,000 order-notional limit is
separate. The allowance is a conservative reserve, not an actual broker
fee quote. Prices must be rounded to no more than two decimals; inputs
cannot demand fractional shares.

This calculator is not wired into the live website or backend routes.
Before any future LIVE use, recalculate with up-to-date limit price,
cash/positions and open orders immediately before preview and confirm,
and compare the E*TRADE preview's actual total and account before any
placement. Never buy on the basis of stale scanner prices.

## Read-only account-screen budget calculator

The staged /production-readonly/connect-ui page now includes a
Calculate Whole Shares — No Trade button. It accepts a user-entered
ticker, limit price and budget (default $1,000), retrieves fresh
E*TRADE production account/balance data through the GET-only reader,
and calls calculate_budgeted_buy.

- Refuses to calculate unless both account and balance explicitly
  classify the account as CASH and BROKERAGE.
- Uses verified settled cash, available investment cash and cash
  buying power, with a 2% estimated cost allowance.
- Displays proposed whole shares, estimated total, reserve and
  remaining budget. It is an estimate, NOT an E*TRADE order preview.
- No production order API is called, and there are no production BUY,
  SELL, preview, or submit routes in this module.
- User manually supplies a limit price; the calculator does not
  fetch or imply a live stock quote.

This branch is NOT deployed. Do not merge or enable production access
without reviewing authentication, logging/privacy, actual brokerage
data mappings, and user-facing workflow.