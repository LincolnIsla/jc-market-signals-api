"""Fail-closed validation helpers for a future E*TRADE production integration.

NOT a live broker integration. Never treat success as broker approval.
Actual E*TRADE response fields must be mapped and verified against
production read-only account/balance/position responses before use.
"""
from decimal import Decimal, InvalidOperation
import re

MAX_NOTIONAL = Decimal("1000.00")
CASH_BUFFER = Decimal("1.02")


class OrderBlocked(ValueError):
    pass


def amount(value, field):
    if isinstance(value, bool) or value is None:
        raise OrderBlocked(f"Missing or invalid {field}")
    try:
        val = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        raise OrderBlocked(f"Missing or invalid {field}") from None
    if not val.is_finite() or val < 0:
        raise OrderBlocked(f"Missing or invalid {field}")
    return val


def validate_cash_limit_order(order, account, balance, long_positions=None):
    """Pre-flight only. Revalidate at both preview and placement.

    Only explicitly verified cash brokerage accounts, long stock, whole
    shares and limit orders are allowed. Missing/inconsistent data blocks
    the request; the E*TRADE broker preview is still mandatory.
    """
    if not isinstance(order, dict) or not isinstance(account, dict) or not isinstance(balance, dict):
        raise OrderBlocked("Broker account and balance information is required")
    if account.get("accountMode") != "CASH" or account.get("institutionType") != "BROKERAGE":
        raise OrderBlocked("Only active, cash-only brokerage accounts are supported")
    if account.get("accountStatus") != "ACTIVE":
        raise OrderBlocked("The brokerage account is not active")
    account_key = account.get("accountIdKey")
    if not isinstance(account_key, str) or not account_key or order.get("account_id") != account_key:
        raise OrderBlocked("Selected account does not match an active cash account")
    if balance.get("accountId") is not None and str(balance["accountId"]) != str(account.get("accountId")):
        raise OrderBlocked("Balance does not belong to selected account")
    symbol = order.get("symbol")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z.]{0,9}", symbol):
        raise OrderBlocked("Invalid stock symbol")
    if order.get("side") not in ("BUY", "SELL"):
        raise OrderBlocked("Only BUY and SELL (not short sales) are supported")
    if order.get("order_type") != "LIMIT":
        raise OrderBlocked("Live trading requires a limit order")
    qty = order.get("quantity")
    if type(qty) is not int or not 1 <= qty <= 1000000:
        raise OrderBlocked("Quantity must be a positive whole number")
    limit_price = amount(order.get("limit_price"), "limit price")
    if limit_price == 0 or limit_price.as_tuple().exponent < -2:
        raise OrderBlocked("Limit price must be positive with at most two decimals")
    notional = limit_price * qty
    if notional > MAX_NOTIONAL:
        raise OrderBlocked("The $1,000 per-order maximum would be exceeded")
    if order["side"] == "BUY":
        computed = balance.get("Computed") or balance.get("computedBalance")
        if not isinstance(computed, dict):
            raise OrderBlocked("Current E*TRADE settled-cash information is missing")
        settled = amount(computed.get("settledCashForInvestment"), "settled cash")
        available = amount(computed.get("cashAvailableForInvestment"), "available investment cash")
        buying_power = amount(computed.get("cashBuyingPower"), "cash buying power")
        if notional * CASH_BUFFER > min(settled, available, buying_power):
            raise OrderBlocked("Insufficient verified settled cash with 2% cost buffer")
    else:
        if not isinstance(long_positions, dict):
            raise OrderBlocked("Verified long positions are required to sell")
        owned = long_positions.get(symbol)
        if type(owned) is not int or owned < qty:
            raise OrderBlocked("Insufficient verified available long shares; short selling disabled")
    return {
        "symbol": symbol,
        "side": order["side"],
        "quantity": qty,
        "limit_price": str(limit_price),
        "maximum_notional": str(notional),
        "account_id": account_key,
        "order_type": "LIMIT",
        "market_session": "REGULAR",
        "order_term": "GOOD_FOR_DAY",
        "cash_only": True,
    }


def calculate_budgeted_buy(symbol, account, balance, limit_price, budget="1000.00"):
    """Propose a whole-share BUY that fits both the budget and settled cash.

    The budget is a per-order *ceiling*, not a promise to spend it all.
    Both the order's limit-price notional and a 2% cost allowance fit
    within that ceiling. This does NOT place, preview or approve a trade.
    Broker data must be verified and refreshed at preview and placement.
    """
    requested = amount(budget, "purchase budget")
    if requested <= 0 or requested > MAX_NOTIONAL or requested.as_tuple().exponent < -2:
        raise OrderBlocked("Budget must be between $0.01 and $1,000, with at most two decimals")
    price = amount(limit_price, "limit price")
    if price <= 0 or price > Decimal("10000000") or price.as_tuple().exponent < -2:
        raise OrderBlocked("Limit price must be positive with at most two decimals")
    if not isinstance(balance, dict):
        raise OrderBlocked("Verified account balances are required")
    computed = balance.get("Computed") or balance.get("computedBalance")
    if not isinstance(computed, dict):
        raise OrderBlocked("Verified settled cash is required")
    settled = amount(computed.get("settledCashForInvestment"), "settled cash")
    available = amount(computed.get("cashAvailableForInvestment"), "available investment cash")
    buying_power = amount(computed.get("cashBuyingPower"), "cash buying power")
    spendable = min(requested, settled, available, buying_power)
    # Decimal avoids float-rounding errors and never rounds a share up.
    shares = int(spendable // (price * CASH_BUFFER))
    if shares < 1:
        raise OrderBlocked("Not enough budget or settled cash for one whole share")
    if shares > 1000000:
        raise OrderBlocked("Share quantity is over the permitted maximum")
    if not isinstance(account, dict):
        raise OrderBlocked("An active cash account is required")
    order = {
        "symbol": symbol,
        "side": "BUY",
        "quantity": shares,
        "order_type": "LIMIT",
        "limit_price": str(price),
        "account_id": account.get("accountIdKey"),
    }
    validated = validate_cash_limit_order(order, account, balance)
    notional = price * shares
    estimated_reserve = notional * CASH_BUFFER
    return {
        "order": validated,
        "requested_budget": str(requested),
        "shares": shares,
        "limit_price": str(price),
        "estimated_purchase_at_limit": str(notional),
        "cost_allowance_2pct": str(estimated_reserve - notional),
        "budget_remaining_after_allowance": str(requested - estimated_reserve),
        "broker_preview_required": True,
        "requires_explicit_confirmation": True,
        "orders_enabled": False,
    }


def estimate_whole_shares_budget_only(symbol, limit_price, budget="1000.00"):
    """Unverified math-only estimate; does not inspect broker buying power.

    Used when E*TRADE classifies the selected account as margin-enabled,
    or does not provide an explicit CASH classification in both responses.
    This function NEVER emits an executable order or trading approval.
    """
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z.]{0,9}", symbol):
        raise OrderBlocked("Invalid stock symbol")
    requested = amount(budget, "purchase budget")
    if requested <= 0 or requested > MAX_NOTIONAL or requested.as_tuple().exponent < -2:
        raise OrderBlocked("Budget must be between $0.01 and $1,000, with at most two decimals")
    price = amount(limit_price, "limit price")
    if price <= 0 or price > Decimal("10000000") or price.as_tuple().exponent < -2:
        raise OrderBlocked("Limit price must be positive with at most two decimals")
    shares = int(requested // (price * CASH_BUFFER))
    if shares < 1:
        raise OrderBlocked("Budget is too small for one whole share at that limit price with the 2% cost allowance")
    if shares > 1000000:
        raise OrderBlocked("Share quantity is over the permitted maximum")
    notional = price * shares
    allowance = notional * (CASH_BUFFER - Decimal("1"))
    remaining = requested - notional * CASH_BUFFER
    return {
        "symbol": symbol,
        "shares": shares,
        "requested_budget": f"{requested:.2f}",
        "limit_price": f"{price:.2f}",
        "estimated_purchase_at_limit": f"{notional:.2f}",
        "cost_allowance_2pct": f"{allowance:.2f}",
        "budget_remaining_after_allowance": f"{remaining:.2f}",
        "mode": "budget_only_estimate",
        "cash_verified": False,
        "trading_eligible": False,
        "broker_preview_performed": False,
        "broker_order_submitted": False,
        "orders_enabled": False,
        "warning": "MATHEMATICAL ESTIMATE ONLY. Cash availability has NOT been verified. Do not use this result to place a trade or borrow on margin.",
    }
