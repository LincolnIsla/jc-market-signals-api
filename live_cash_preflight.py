"""Offline E*TRADE cash-only BUY preflight, without broker order calls.

This is a pure validator. It never makes requests or creates a broker order.
A 'ready_for_preview' result is NOT permission to place an order. Preview,
fresh balance checks, per-order confirmation, and submission safeguards are
distinct later features that must be tested before deployment.

Do not trust account mode supplied by the browser. Caller must fetch account
and balance fresh from E*TRADE using SERVER-SIDE production OAuth.
"""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import re

MAX_DOLLARS = Decimal("1000.00")
RESERVE_FACTOR = Decimal("1.02")


class CashOrderBlocked(ValueError):
    pass


def money(value, label):
    if value is None or isinstance(value, bool):
        raise CashOrderBlocked(f"{label} is missing or invalid")
    try:
        num = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise CashOrderBlocked(f"{label} is missing or invalid") from exc
    if not num.is_finite() or num < 0:
        raise CashOrderBlocked(f"{label} is missing or invalid")
    return num


def whole_share_estimate(limit_price, budget):
    """Pure mathematical estimate, not verified affordability."""
    price = money(limit_price, "Limit price")
    max_spend = money(budget, "Budget")
    if price <= 0 or price.as_tuple().exponent < -2:
        raise CashOrderBlocked("Limit price must be positive, with at most two decimals")
    if max_spend <= 0 or max_spend > MAX_DOLLARS or max_spend.as_tuple().exponent < -2:
        raise CashOrderBlocked("Budget must be $0.01 to $1,000.00")
    shares = int((max_spend / (price * RESERVE_FACTOR)).to_integral_value(
        rounding=ROUND_FLOOR
    ))
    if shares < 1:
        raise CashOrderBlocked("Budget cannot cover one whole share plus buffer")
    return {
        "shares": shares,
        "price": price,
        "notional": shares * price,
        "reserved": shares * price * RESERVE_FACTOR,
        "budget": max_spend,
    }


def assess_cash_buy(
    *,
    symbol,
    limit_price,
    budget,
    account,
    balance,
    known_account_keys,
    account_permissions_verified=False,
    open_orders_verified=False,
    outstanding_buy_reservations=None,
):
    """Fail-closed staging check for a proposed BUY; NEVER places an order.

    Parameters account/ balance must be normalized backend-only E*TRADE data.
    known_account_keys must be an independently configured server allowlist.
    account_permissions_verified means the broker separately confirmed the
    account cannot borrow on margin; user's verbal report is not enough.
    open_orders_verified means all live/open BUY reservations were fetched.
    No such verification is currently implemented in the production reader.
    """
    if not isinstance(account, dict) or not isinstance(balance, dict):
        raise CashOrderBlocked("Fresh broker account and balance data required")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z.]{0,9}", symbol):
        raise CashOrderBlocked("Invalid stock symbol")
    maths = whole_share_estimate(limit_price, budget)
    key = account.get("accountIdKey")
    if (not isinstance(key, str) or not key
            or not isinstance(known_account_keys, (set, frozenset))
            or key not in known_account_keys):
        raise CashOrderBlocked("Account is not server-allowlisted")

    blockers = []
    list_mode = account.get("accountMode")
    balance_mode = balance.get("accountMode")
    if list_mode != "CASH" or balance_mode != "CASH":
        blockers.append("cash_mode_conflict_or_unverified")
    if account.get("accountStatus") != "ACTIVE":
        blockers.append("account_not_confirmed_active")
    if (account.get("institutionType") != "BROKERAGE"
            or balance.get("institutionType") != "BROKERAGE"):
        blockers.append("brokerage_classification_unverified")
    account_id = account.get("accountId")
    if (account_id is None or balance.get("accountId") is None
            or str(account_id) != str(balance["accountId"])):
        blockers.append("account_balance_identity_not_verified")
    if account_permissions_verified is not True:
        blockers.append("broker_cash_only_permissions_not_verified")
    if open_orders_verified is not True:
        blockers.append("open_order_reservations_not_verified")

    available_fields = {}
    for input_name in ("settledCashForInvestment", "cashAvailableForInvestment"):
        try:
            available_fields[input_name] = money(balance.get(input_name), input_name)
        except CashOrderBlocked:
            blockers.append("missing_or_invalid_" + input_name)

    if outstanding_buy_reservations is None:
        blockers.append("outstanding_buy_reservations_unknown")
    else:
        try:
            reservation = money(outstanding_buy_reservations, "Outstanding buy reservations")
        except CashOrderBlocked:
            blockers.append("outstanding_buy_reservations_invalid")
            reservation = None
    if len(available_fields) == 2:
        spendable = min(available_fields.values())
        # The BUY reservations amount is subtracted regardless of whether the
        # broker cashAvailable field might include the same orders. Conservative.
        if outstanding_buy_reservations is not None and reservation is not None:
            spendable = max(Decimal("0"), spendable - reservation)
        if maths["reserved"] > spendable:
            blockers.append("insufficient_confirmed_cash_plus_buffer")
    else:
        spendable = None

    # Under no condition does a preflight result authorize live placement.
    # The production reader still does not have live order endpoints.
    return {
        "environment": "production",
        "mode": "offline_preflight_only",
        "symbol": symbol,
        "requested_budget": f"{maths['budget']:.2f}",
        "estimated_shares": maths["shares"],
        "limit_price": f"{maths['price']:.2f}",
        "estimated_limit_notional": f"{maths['notional']:.2f}",
        "estimated_with_2pct_allowance": f"{maths['reserved']:.2f}",
        "spendable_cash_after_reservations": (
            f"{spendable:.2f}" if spendable is not None else None
        ),
        "blockers": list(dict.fromkeys(blockers)),
        "ready_for_broker_preview": not bool(blockers),
        "broker_preview_performed": False,
        "order_submitted": False,
        "orders_enabled": False,
        "requires_separate_trade_confirmation": True,
    }
