from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .image_fetch import MAX_URL_LENGTH as _IMAGE_URL_MAX_LENGTH


_ASPECTS_MAX_BYTES = 16_384
_HTML_LIKE_TAG = re.compile(r"<\s*(?:/?[A-Za-z][^<>]*|![^<>]*|\?[^<>]*)>")

# Non-deprecated values documented by eBay's Inventory API ConditionEnum.
_DRAFT_CONDITIONS = frozenset(
    {
        "NEW",
        "LIKE_NEW",
        "NEW_OTHER",
        "NEW_WITH_DEFECTS",
        "CERTIFIED_REFURBISHED",
        "EXCELLENT_REFURBISHED",
        "VERY_GOOD_REFURBISHED",
        "GOOD_REFURBISHED",
        "SELLER_REFURBISHED",
        "USED_EXCELLENT",
        "USED_VERY_GOOD",
        "USED_GOOD",
        "USED_ACCEPTABLE",
        "FOR_PARTS_OR_NOT_WORKING",
        "PRE_OWNED_EXCELLENT",
        "PRE_OWNED_FAIR",
    }
)


class ValidationError(ValueError):
    def __init__(self, errors: Mapping[str, str]) -> None:
        super().__init__("Please correct the highlighted fields")
        self.errors = dict(errors)


def _one(form: Mapping[str, List[str]], name: str) -> str:
    values = form.get(name, [])
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        return ""
    return values[0]


def _money(raw: str, field: str, errors: Dict[str, str]) -> str:
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        errors[field] = "Enter a valid decimal amount."
        return raw
    if not value.is_finite() or value < Decimal("0.01") or value > Decimal("999999999.99"):
        errors[field] = "Enter an amount from 0.01 to 999999999.99."
        return raw
    if value.as_tuple().exponent < -2:
        errors[field] = "Use no more than two decimal places."
        return raw
    return format(value.quantize(Decimal("0.01")), "f")


def _sku(raw: str, errors: Dict[str, str]) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,49}", raw):
        errors["sku"] = (
            "SKU must contain 1–50 ASCII letters, numbers, periods, underscores or hyphens "
            "and must start with a letter or number."
        )
    return raw


def _has_unsafe_unicode(raw: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cs"} for char in raw)


def _plain_text(
    raw: str,
    field: str,
    minimum: int,
    maximum: int,
    errors: Dict[str, str],
) -> str:
    label = field.capitalize()
    normalized = raw.strip()
    if not minimum <= len(normalized) <= maximum:
        errors[field] = f"{label} must contain {minimum}–{maximum} characters."
    elif _has_unsafe_unicode(raw) or _HTML_LIKE_TAG.search(raw):
        errors[field] = f"{label} must be plain text without HTML or control characters."
    return normalized


def _draft_money(raw: str, errors: Dict[str, str]) -> str:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", raw):
        errors["price"] = "Enter a positive decimal amount with no more than two decimal places."
        return raw
    return _money(raw, "price", errors)


class _DuplicateJsonKey(ValueError):
    pass


def _unique_json_object(pairs: List[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise _DuplicateJsonKey(name)
        result[name] = value
    return result


def _aspects(raw: str, errors: Dict[str, str]) -> Dict[str, List[str]]:
    try:
        raw_size = len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        errors["aspects"] = "Aspects must contain valid Unicode text."
        return {}
    if raw_size > _ASPECTS_MAX_BYTES:
        errors["aspects"] = "Aspects JSON must not exceed 16384 UTF-8 bytes."
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw, object_pairs_hook=_unique_json_object)
    except _DuplicateJsonKey:
        errors["aspects"] = "Aspect names must not be duplicated."
        return {}
    except (json.JSONDecodeError, RecursionError):
        errors["aspects"] = 'Use JSON such as {"Brand":["Example"]}.'
        return {}
    if not isinstance(parsed, dict) or len(parsed) > 50:
        errors["aspects"] = "Aspects must be a JSON object with at most 50 names."
        return {}
    normalized: Dict[str, List[str]] = {}
    for name, values in parsed.items():
        normalized_name = name.strip()
        if (
            not 1 <= len(normalized_name) <= 40
            or _has_unsafe_unicode(normalized_name)
        ):
            errors["aspects"] = (
                "Every aspect name must contain 1–40 characters and no control characters."
            )
            return {}
        if normalized_name in normalized:
            errors["aspects"] = "Aspect names must not be duplicated."
            return {}
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or not values or len(values) > 30:
            errors["aspects"] = "Every aspect must contain 1–30 text values."
            return {}
        normalized_values: List[str] = []
        seen_values = set()
        for value in values:
            if not isinstance(value, str):
                errors["aspects"] = "Every aspect value must be text."
                return {}
            normalized_value = value.strip()
            if (
                not 1 <= len(normalized_value) <= 50
                or _has_unsafe_unicode(normalized_value)
            ):
                errors["aspects"] = (
                    "Every aspect value must contain 1–50 characters and no control characters."
                )
                return {}
            if normalized_value in seen_values:
                errors["aspects"] = "Aspect values must not be duplicated."
                return {}
            seen_values.add(normalized_value)
            normalized_values.append(normalized_value)
        normalized[normalized_name] = normalized_values
    return normalized


def validate_draft(form: Mapping[str, List[str]], currency: str) -> Dict[str, Any]:
    allowed = {
        "csrf_token",
        "sku",
        "title",
        "description",
        "condition",
        "quantity",
        "price",
        "category_id",
        "aspects",
    }
    errors: Dict[str, str] = {}
    unknown = set(form) - allowed
    repeated_or_malformed = any(
        not isinstance(values, list)
        or len(values) != 1
        or not isinstance(values[0], str)
        for values in form.values()
    )
    if unknown or repeated_or_malformed:
        errors["form"] = "The form contained unsupported, repeated or malformed fields."

    sku = _sku(_one(form, "sku"), errors)
    title = _plain_text(_one(form, "title"), "title", 1, 80, errors)
    description = _plain_text(
        _one(form, "description"), "description", 1, 4000, errors
    )
    condition = _one(form, "condition").strip().upper()
    if condition not in _DRAFT_CONDITIONS:
        errors["condition"] = "Choose a supported eBay Inventory API condition."
    category_id = _one(form, "category_id").strip()
    if not re.fullmatch(r"[0-9]{1,12}", category_id):
        errors["category_id"] = "Enter a numeric eBay category ID."
    raw_quantity = _one(form, "quantity").strip()
    if not re.fullmatch(r"[0-9]{1,6}", raw_quantity):
        quantity = 0
        errors["quantity"] = "Quantity must be an integer from 1 to 999999."
    else:
        quantity = int(raw_quantity)
        if not 1 <= quantity <= 999999:
            errors["quantity"] = "Quantity must be an integer from 1 to 999999."
    price = _draft_money(_one(form, "price").strip(), errors)
    aspects = _aspects(_one(form, "aspects"), errors)

    if errors:
        raise ValidationError(errors)
    return {
        "sku": sku,
        "title": title,
        "description": description,
        "condition": condition,
        "quantity": quantity,
        "price": price,
        "currency": currency,
        "category_id": category_id,
        "aspects": aspects,
    }


def validate_new_price(raw: str) -> str:
    errors: Dict[str, str] = {}
    value = _money(raw.strip(), "new_price", errors)
    if errors:
        raise ValidationError(errors)
    return value


def validate_price_change_form(form: Mapping[str, List[str]]) -> Dict[str, str]:
    """Validate a price-change form: which Offer/SKU, and the new price."""

    errors: Dict[str, str] = {}
    offer_id = _one(form, "offer_id").strip()
    if not re.fullmatch(r"[^\x00-\x20]{1,100}", offer_id):
        errors["offer_id"] = "Offer ID is invalid."
    sku = _one(form, "sku").strip()
    if not 1 <= len(sku) <= 50:
        errors["sku"] = "SKU is invalid."
    new_price = _money(_one(form, "new_price").strip(), "new_price", errors)
    if errors:
        raise ValidationError(errors)
    return {"offer_id": offer_id, "sku": sku, "new_price": new_price}


def _optional_percent(raw: str, field: str, errors: Dict[str, str]) -> Optional[str]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        errors[field] = "Enter a valid percentage, or leave blank to use the default."
        return None
    if not value.is_finite() or value < Decimal("0.1") or value > Decimal("100"):
        errors[field] = "Enter a percentage from 0.1 to 100, or leave blank to use the default."
        return None
    return format(value.normalize(), "f")


def validate_price_limit_form(form: Mapping[str, List[str]]) -> Dict[str, Optional[str]]:
    """Validate a per-SKU price-safety-limit form: required floor price, optional
    per-SKU override of the global max price-change percentage."""

    errors: Dict[str, str] = {}
    sku = _one(form, "sku").strip()
    _sku(sku, errors)
    floor_price = _money(_one(form, "floor_price").strip(), "floor_price", errors)
    max_change_percent = _optional_percent(
        _one(form, "max_change_percent"), "max_change_percent", errors
    )
    if errors:
        raise ValidationError(errors)
    return {
        "sku": sku,
        "floor_price": floor_price,
        "max_change_percent": max_change_percent,
    }


def validate_quantity_change_form(form: Mapping[str, List[str]]) -> Dict[str, Any]:
    """Validate a quantity-change form: which Offer/SKU, and the new quantity."""

    errors: Dict[str, str] = {}
    offer_id = _one(form, "offer_id").strip()
    if not re.fullmatch(r"[^\x00-\x20]{1,100}", offer_id):
        errors["offer_id"] = "Offer ID is invalid."
    sku = _one(form, "sku").strip()
    if not 1 <= len(sku) <= 50:
        errors["sku"] = "SKU is invalid."
    raw_quantity = _one(form, "new_quantity").strip()
    if not re.fullmatch(r"[0-9]{1,6}", raw_quantity):
        new_quantity = 0
        errors["new_quantity"] = "Quantity must be a whole number from 0 to 999999."
    else:
        new_quantity = int(raw_quantity)
        if not 0 <= new_quantity <= 999999:
            errors["new_quantity"] = "Quantity must be a whole number from 0 to 999999."
    if errors:
        raise ValidationError(errors)
    return {"offer_id": offer_id, "sku": sku, "new_quantity": new_quantity}


def validate_image_add_form(form: Mapping[str, List[str]]) -> Dict[str, str]:
    """Validate an image-add form: which Offer/SKU, and the candidate image URL.

    This performs only synchronous, no-network format checks -- a plain
    https:// address of reasonable length with no control characters.
    Whether the URL is actually reachable, is really an image, and is an
    acceptable size is checked separately and live by
    ``image_fetch.check_image_url`` in the service layer; keeping that
    live network check out of form validation keeps validation itself a
    pure function and easy to test.
    """

    errors: Dict[str, str] = {}
    offer_id = _one(form, "offer_id").strip()
    if not re.fullmatch(r"[^\x00-\x20]{1,100}", offer_id):
        errors["offer_id"] = "Offer ID is invalid."
    sku = _one(form, "sku").strip()
    if not 1 <= len(sku) <= 50:
        errors["sku"] = "SKU is invalid."
    image_url = _one(form, "image_url").strip()
    if (
        not 1 <= len(image_url) <= _IMAGE_URL_MAX_LENGTH
        or _has_unsafe_unicode(image_url)
        or not image_url.startswith("https://")
    ):
        errors["image_url"] = "Enter a valid https:// image address."
    if errors:
        raise ValidationError(errors)
    return {"offer_id": offer_id, "sku": sku, "image_url": image_url}


def display_form_values(form: Mapping[str, Iterable[str]]) -> Dict[str, str]:
    return {name: next(iter(values), "") for name, values in form.items()}
