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
