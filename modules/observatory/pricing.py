"""Versioned price table and Decimal cost estimation.

Rules enforced here:
- Only API-reported usage is billable. Unknown usage is never treated as 0.
- Missing price is ``price_not_configured``, never "free".
- Cached input is a subset of input and is never added on top of it again.
- Amounts freeze the price version and unit snapshot at estimation time.
- Different currencies are never summed; the UI groups by currency.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from .util import to_int, utc_now

BILLING_MODES = ("per_token", "subscription_or_quota", "unknown")
UNIT_PER_MILLION = "per_million_tokens"
CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")
MONEY_QUANT = Decimal("0.000001")
MILLION = Decimal(1000000)


def parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite() or result < 0:
        return None
    return result


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return text or "0"


def _select_price(
    conn: sqlite3.Connection, provider_id: str, model: str, input_tokens: int
) -> sqlite3.Row | None:
    rows = conn.execute(
        "SELECT * FROM price_version WHERE is_active=1 AND provider_id=? "
        "AND (model=? OR model='*') AND min_input_tokens<=? "
        "ORDER BY (model=?) DESC, min_input_tokens DESC, effective_at DESC",
        (provider_id, model, input_tokens, model),
    ).fetchall()
    return rows[0] if rows else None


def resolve_cost(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    result_id: str,
    provider_id: str,
    provider_model: str,
    usage_source: str | None,
    input_other: int | None,
    input_cached: int | None,
    output: int | None,
    ts: float,
) -> dict:
    """Return a cost_estimate row payload for one request."""
    provider_id = (provider_id or "").strip()
    provider_model = (provider_model or "").strip()
    if usage_source != "api_reported" or None in (input_other, input_cached, output):
        return {
            "request_id": request_id,
            "result_id": result_id,
            "price_version_id": None,
            "amount": None,
            "currency": None,
            "status": "unknown_usage",
            "reason": "模型未返回完整 usage（input_other/input_cached/output 至少一项未知），不按 0 计费。",
            "snapshot": {
                "usage_source": usage_source,
                "input_other": input_other,
                "input_cached": input_cached,
                "output": output,
            },
        }

    input_total = int(input_other) + int(input_cached)
    price = _select_price(conn, provider_id, provider_model, input_total)
    if price is None:
        return {
            "request_id": request_id,
            "result_id": result_id,
            "price_version_id": None,
            "amount": None,
            "currency": None,
            "status": "price_not_configured",
            "reason": "该 provider/model 未配置适用价格；未知单价不等于免费。",
            "snapshot": {
                "provider_id": provider_id,
                "model": provider_model,
                "input_tokens": input_total,
            },
        }

    billing_mode = price["billing_mode"] or "unknown"
    currency = (price["currency"] or "").upper()
    snapshot = {
        "price_version_id": price["price_version_id"],
        "provider_id": price["provider_id"],
        "model": price["model"],
        "tier_label": price["tier_label"],
        "min_input_tokens": price["min_input_tokens"],
        "currency": currency,
        "unit": price["unit"],
        "billing_mode": billing_mode,
        "input_price": price["input_price"],
        "cached_input_price": price["cached_input_price"],
        "output_price": price["output_price"],
        "effective_at": price["effective_at"],
    }
    if billing_mode == "subscription_or_quota":
        return {
            "request_id": request_id,
            "result_id": result_id,
            "price_version_id": price["price_version_id"],
            "amount": None,
            "currency": currency or None,
            "status": "subscription_or_quota",
            "reason": "订阅/额度制，不折算为单条消息金额。",
            "snapshot": snapshot,
        }
    if billing_mode != "per_token":
        return {
            "request_id": request_id,
            "result_id": result_id,
            "price_version_id": price["price_version_id"],
            "amount": None,
            "currency": currency or None,
            "status": "unknown_billing_mode",
            "reason": "计费模式未知，不显示金额。",
            "snapshot": snapshot,
        }

    input_price = parse_decimal(price["input_price"])
    cached_price = parse_decimal(price["cached_input_price"])
    output_price = parse_decimal(price["output_price"])
    missing = []
    if int(input_other) > 0 and input_price is None:
        missing.append("input_price")
    if int(input_cached) > 0 and cached_price is None:
        missing.append("cached_input_price")
    if output_price is None:
        missing.append("output_price")
    if not CURRENCY_RE.match(currency or ""):
        missing.append("currency")
    if missing:
        return {
            "request_id": request_id,
            "result_id": result_id,
            "price_version_id": price["price_version_id"],
            "amount": None,
            "currency": currency or None,
            "status": "incomplete_price",
            "reason": "缺少对应分项单价：" + ", ".join(missing),
            "snapshot": snapshot,
        }

    amount = Decimal(0)
    if input_price is not None:
        amount += Decimal(int(input_other)) / MILLION * input_price
    if cached_price is not None and int(input_cached) > 0:
        amount += Decimal(int(input_cached)) / MILLION * cached_price
    amount += Decimal(int(output)) / MILLION * output_price
    amount = amount.quantize(MONEY_QUANT)
    return {
        "request_id": request_id,
        "result_id": result_id,
        "price_version_id": price["price_version_id"],
        "amount": _decimal_text(amount),
        "currency": currency,
        "status": "estimated",
        "reason": "按已配置单价估算，非供应商账单。",
        "snapshot": snapshot,
    }


def list_prices(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM price_version ORDER BY provider_id, model, min_input_tokens, "
        "created_at DESC"
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["prices_complete"] = all(
            item.get(key) is not None
            for key in ("input_price", "cached_input_price", "output_price")
        )
        result.append(item)
    return result


def validate_price_payload(payload: Any) -> tuple[dict, list[str]]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return {}, ["payload must be an object"]
    provider_id = str(payload.get("provider_id") or "").strip()
    model = str(payload.get("model") or "").strip()
    if not provider_id or len(provider_id) > 120:
        errors.append("provider_id 必填且不超过 120 字符")
    if not model or len(model) > 200:
        errors.append("model 必填且不超过 200 字符（长上下文档可用 * 通配）")
    currency = str(payload.get("currency") or "").strip().upper()
    if not CURRENCY_RE.match(currency):
        errors.append("currency 必须是 3 位字母代码，例如 CNY / USD")
    billing_mode = str(payload.get("billing_mode") or "per_token").strip()
    if billing_mode not in BILLING_MODES:
        errors.append("billing_mode 必须是 per_token / subscription_or_quota / unknown")
    min_input = to_int(payload.get("min_input_tokens", 0))
    if min_input is None or min_input < 0:
        errors.append("min_input_tokens 必须是非负整数")
        min_input = 0
    prices = {}
    for key in ("input_price", "cached_input_price", "output_price"):
        value = payload.get(key)
        if value is None or value == "":
            prices[key] = None
            continue
        parsed = parse_decimal(value)
        if parsed is None:
            errors.append(f"{key} 必须是非负十进制数字")
        else:
            prices[key] = _decimal_text(parsed)
    tier_label = str(payload.get("tier_label") or "").strip()[:80] or None
    note = str(payload.get("note") or "").strip()[:500] or None
    if tier_label is None:
        tier_label = "长上下文" if min_input > 0 else "标准"
    cleaned = {
        "provider_id": provider_id,
        "model": model,
        "tier_label": tier_label,
        "min_input_tokens": min_input,
        "currency": currency,
        "unit": UNIT_PER_MILLION,
        "billing_mode": billing_mode,
        "input_price": prices["input_price"],
        "cached_input_price": prices["cached_input_price"],
        "output_price": prices["output_price"],
        "note": note,
    }
    return cleaned, errors


def save_price(conn: sqlite3.Connection, payload: Any) -> dict:
    cleaned, errors = validate_price_payload(payload)
    if errors:
        raise ValueError("；".join(errors))
    now = utc_now()
    version_id = uuid.uuid4().hex
    conn.execute(
        "UPDATE price_version SET is_active=0 WHERE is_active=1 "
        "AND provider_id=? AND model=? AND min_input_tokens=?",
        (cleaned["provider_id"], cleaned["model"], cleaned["min_input_tokens"]),
    )
    conn.execute(
        "INSERT INTO price_version(price_version_id, provider_id, model, tier_label, "
        "min_input_tokens, currency, unit, billing_mode, input_price, "
        "cached_input_price, output_price, effective_at, created_at, is_active, note) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            version_id,
            cleaned["provider_id"],
            cleaned["model"],
            cleaned["tier_label"],
            cleaned["min_input_tokens"],
            cleaned["currency"],
            cleaned["unit"],
            cleaned["billing_mode"],
            cleaned["input_price"],
            cleaned["cached_input_price"],
            cleaned["output_price"],
            now,
            now,
            cleaned["note"],
        ),
    )
    row = conn.execute(
        "SELECT * FROM price_version WHERE price_version_id=?", (version_id,)
    ).fetchone()
    return dict(row) if row else {"price_version_id": version_id}
