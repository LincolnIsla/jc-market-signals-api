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
