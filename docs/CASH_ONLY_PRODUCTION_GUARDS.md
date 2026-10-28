# Cash-only production preflight (staging only — NO LIVE TRADES)

This branch adds a **pure offline validator**, \`live_cash_preflight.py\`, and
tests. No production order endpoints, API calls, authorization changes, Render
environment changes or application UI changes are included.

## Account mode discrepancy observed

The user has independently stated their E*TRADE brokerage account is cash-only
and a margin order was rejected. E*TRADE's live Account List API reports
\`accountMode=CASH\`, while its Balance API reports \`accountMode=MARGIN\`
both with and without \`accountType=INDIVIDUAL\`.

**The application must not resolve this contradiction by taking the user's
statement as programmatic evidence of cash-only permissions.** The current
read-only whole-share estimator can operate without verifying available cash,
but live trading must remain disabled until an independently verified broker
cash-only permission check is integrated and the discrepancy is understood.
The new preflight still blocks on the mode conflict.

## Guardrails built and tested offline

- Explicit server-side account allowlist and exact account ID match.
- Account and balance both identify an active cash brokerage account.
- An independently verified broker cash-only-permission signal.
- A fresh, complete broker open-order reservation reconciliation.
- Only settled cash and cash available for investment count toward buys;
  \`marginBuyingPower\` is deliberately ignored.
- Up to **$1,000 per order** and a **2% allowance** for costs.
- Limit-price calculations and whole shares (always rounded down).
- Fail closed on missing, stale, conflicting or invalid data.
- The checker only returns \`ready_for_broker_preview\`; it **cannot**
  place an order or approve a production trade.

## Unfinished prerequisites before production orders

1. Confirm with E*TRADE developer/account support which broker field
   authoritatively expresses margin privileges. Do not infer from
   \`accountType=INDIVIDUAL\` or margin-capable buying-power figures.
2. Fetch fresh read-only balances, cash restrictions and pending open
   order/settlement reservations before every individual order preview.
3. Broker **preview** and validation of all submitted fields, errors,
   warnings and fees (separate from actual order placement).
4. User-specific, contemporaneous affirmative confirmation for EACH order,
   with price, symbol, account, share count and maximum spend displayed.
5. Server kill switch default OFF, durable per-order idempotency, market
   session enforcement, order status reconciliation after ambiguous results,
   and explicit no-margin/no-short/no-options controls.
6. Session, origin, CSRF, replay, rate-limit, account permission and log
   security reviews. E*TRADE OAuth consent alone is not a read-only scope.
7. Respect the latest E*TRADE API agreement governing automated order
   generation. No automatic placement from alerts or algorithms.
8. Validate all schemas against authorized real broker read-only responses
   (without placing orders), then separately verify any order flow in
   the Sandbox.

## Run offline tests

\`\`\`bash
python -m unittest discover -s tests -p 'test_live_cash_preflight.py' -v
\`\`\`

Test data is synthetic and does not verify E*TRADE's real production
response fields or legal/account permissions. The existing Render backend
continues to be read-only for production and to limit its order endpoints
to Sandbox. **Do not merge this branch merely as a step toward enabling
real-money trading**; it needs review and end-to-end integration first.

E*TRADE API overview: https://developer.etrade.com/getting-started/
API developer agreement: https://developer.etrade.com/support/terms-of-use
