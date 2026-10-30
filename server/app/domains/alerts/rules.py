"""Side-effect free evaluation shared by saved rules and previews."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.domains.games.pricing import REGION_TO_CURRENCY


RULE_TYPES = ("price", "pct", "historic_low", "new_low")
WINDOWS = (None, 90, 180, 365)
SCOPES = ("followed", "wishlist", "owned", "manual", "catalog")
DEFAULT_FEATURED = {
    "scope": ["followed", "wishlist"], "minDiscount": 50, "minSavings": None,
    "maxPrice": None, "lowMode": "any", "allowFullPrice": False,
    "minPositiveRate": None, "minReviews": None, "missingReviews": "include",
    "includeOwned": False, "includeIgnored": False, "sort": "discount",
    "windowDays": None,
}


def integer_amount(value, name: str, *, maximum: int | None = None) -> int:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name}必须是整数") from None
    if not number.is_finite() or number != number.to_integral_value() or number < 0:
        raise ValueError(f"{name}必须是非负整数")
    result = int(number)
    if maximum is not None and result > maximum:
        raise ValueError(f"{name}超过允许范围")
    return result


def validate_rule(raw: dict) -> dict:
    rule = dict(raw)
    if int(rule.get("appid") or 0) <= 0:
        raise ValueError("请选择游戏")
    region = str(rule.get("region") or "").strip().upper()
    if region not in REGION_TO_CURRENCY:
        raise ValueError("请选择有效的提醒地区")
    rule["region"] = region
    kind = rule.get("targetType", "price")
    if kind not in RULE_TYPES:
        raise ValueError("未知提醒类型")
    config = dict(rule.get("config") or {})
    if config.get("windowDays") not in WINDOWS:
        raise ValueError("历史比较范围仅支持全部本地历史或90、180、365天")
    if config.get("currency"):
        config["currency"] = str(config["currency"]).upper()
        if config["currency"] not in set(REGION_TO_CURRENCY.values()):
            raise ValueError("未知价格币种")
    if kind in ("price", "pct"):
        rule["targetValue"] = integer_amount(rule.get("targetValue"), "目标值", maximum=100 if kind == "pct" else None)
    mode = rule.get("repeatMode", "once")
    if mode not in ("once", "cooldown", "always"):
        raise ValueError("未知重复策略")
    if rule.get("repeatHours") is not None:
        rule["repeatHours"] = max(1, integer_amount(rule["repeatHours"], "重复间隔"))
    rule["config"] = config or None
    return rule


def validate_featured(raw: dict | None) -> dict:
    config = {**DEFAULT_FEATURED, **{key: value for key, value in (raw or {}).items() if key in DEFAULT_FEATURED}}
    scope = config["scope"]
    if not isinstance(scope, list) or not scope or any(value not in SCOPES for value in scope):
        raise ValueError("请选择有效的精选来源范围")
    if config.get("windowDays") not in WINDOWS:
        raise ValueError("无效的历史比较范围")
    for name, maximum in (("minDiscount", 100), ("minPositiveRate", 100), ("minReviews", None),
                          ("minSavings", None), ("maxPrice", None)):
        if config.get(name) is not None:
            config[name] = integer_amount(config[name], name, maximum=maximum)
    if config["lowMode"] not in ("any", "low", "new_low"):
        raise ValueError("无效的低价条件")
    if config["missingReviews"] not in ("include", "exclude"):
        raise ValueError("无效的缺失评价处理方式")
    if config["sort"] not in ("discount", "price", "savings", "quality", "followed", "wishlist"):
        raise ValueError("无效的精选排序方式")
    for name in ("allowFullPrice", "includeOwned", "includeIgnored"):
        if not isinstance(config[name], bool):
            raise ValueError(f"{name}必须为开关值")
    return config


def _result(current: dict, history: dict, window: int | None) -> dict:
    comparison = {**history, "windowDays": window, "complete": False}
    return {"matched": True, "conditions": [], "exclusions": [], "comparison": comparison,
            "observed": dict(current), "labels": []}


def _condition(result: dict, code: str, passed: bool, actual=None, expected=None) -> None:
    item = {"code": code, "passed": bool(passed), "actual": actual, "expected": expected}
    result["conditions"].append(item)
    if not passed:
        result["matched"] = False
        result["exclusions"].append(item)


def _current_conditions(result: dict) -> None:
    cur = result["observed"]
    _condition(result, "price_available", cur.get("status") == "ok" and cur.get("price") is not None,
               cur.get("status"), "ok")
    _condition(result, "known_currency", bool(cur.get("currency")), cur.get("currency"), "observed currency")
    _condition(result, "known_product", bool(cur.get("subId")) and cur.get("currentKnown", True),
               cur.get("subId"), "identified standard Steam observation")
    _condition(result, "observation_time", bool(cur.get("observedAt")), cur.get("observedAt"), "observation timestamp")


def _low_condition(result: dict, mode: str) -> None:
    cur = result["observed"].get("price")
    low = result["comparison"].get("baselineLow")
    _condition(result, "no_baseline", low is not None, low, "prior comparable positive price")
    if low is not None:
        matches = cur is not None and cur > 0 and (cur < low if mode == "new_low" else cur <= low)
        _condition(result, mode, matches, cur, low)


def evaluate_rule(rule: dict, current: dict | None, history: dict | None) -> dict:
    config = rule.get("config") or {}
    result = _result(current or {}, history or {}, config.get("windowDays"))
    _current_conditions(result)
    kind = rule.get("targetType", "price")
    if kind == "price":
        currency = config.get("currency")
        result["observed"]["thresholdBasis"] = "native" if currency else "legacy_cny"
        if currency:
            _condition(result, "currency_mismatch", currency == (current or {}).get("currency"),
                       (current or {}).get("currency"), currency)
        amount = (current or {}).get("price" if currency else "cnyFen")
        target = rule.get("targetValue")
        _condition(result, "target_price", amount is not None and target is not None and amount <= target,
                   amount, target)
    elif kind == "pct":
        amount = (current or {}).get("discount")
        target = rule.get("targetValue")
        _condition(result, "target_discount", amount is not None and target is not None and amount >= target,
                   amount, target)
    elif kind in ("historic_low", "new_low"):
        _low_condition(result, "low" if kind == "historic_low" else kind)
    else:
        _condition(result, "rule_type", False, kind, RULE_TYPES)
    if result["matched"]:
        result["labels"] = [kind]
    return result


def evaluate_featured(config: dict, current: dict | None, history: dict | None, membership: dict) -> dict:
    config = validate_featured(config)
    result = _result(current or {}, history or {}, config.get("windowDays"))
    _current_conditions(result)
    cur = current or {}
    sources = set(membership.get("sources") or [])
    available = {"followed": "favorite" in sources, "manual": "manual" in sources,
                 "wishlist": "family_wishlist" in sources or membership.get("wishlisted", False),
                 "owned": membership.get("owned", False), "catalog": True}
    _condition(result, "scope", any(available[s] for s in config["scope"]),
               [key for key, value in available.items() if value], config["scope"])
    _condition(result, "owned", config["includeOwned"] or not membership.get("owned", False),
               bool(membership.get("owned")), config["includeOwned"])
    _condition(result, "ignored", config["includeIgnored"] or not membership.get("ignored", False),
               bool(membership.get("ignored")), config["includeIgnored"])
    discount = cur.get("discount")
    _condition(result, "full_price", config["allowFullPrice"] or (discount is not None and discount > 0),
               discount, config["allowFullPrice"])
    if config["minDiscount"] is not None:
        _condition(result, "min_discount", discount is not None and discount >= config["minDiscount"],
                   discount, config["minDiscount"])
    savings = cur.get("originalPrice") - cur["price"] if cur.get("originalPrice") is not None and cur.get("price") is not None else None
    for code, value, threshold, direction in (("min_savings", savings, config["minSavings"], "min"),
                                               ("max_price", cur.get("price"), config["maxPrice"], "max")):
        if threshold is not None:
            _condition(result, code, value is not None and (value >= threshold if direction == "min" else value <= threshold),
                       value, threshold)
    rate, count = cur.get("positiveRate"), cur.get("reviewCount")
    missing = rate is None or count is None or count == 0
    _condition(result, "review_data_missing", not missing or config["missingReviews"] == "include",
               missing, config["missingReviews"])
    for code, value, threshold in (("min_positive_rate", rate / 100 if rate is not None else None, config["minPositiveRate"]),
                                    ("min_reviews", count, config["minReviews"])):
        if threshold is not None:
            _condition(result, code, value is not None and value >= threshold, value, threshold)
    if config["lowMode"] != "any":
        _low_condition(result, config["lowMode"])
    if result["matched"]:
        result["labels"] = ["featured"]
    result["config"] = config
    result["membership"] = {**membership, "sources": sorted(sources)}
    return result
