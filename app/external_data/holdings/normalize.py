from __future__ import annotations

from decimal import Decimal, InvalidOperation


def clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.strip().split())
    return cleaned or None


def parse_nonnegative_decimal(value: str | None, *, field: str) -> Decimal | None:
    cleaned = clean_optional_text(value)
    if cleaned is None:
        return None
    try:
        parsed = Decimal(cleaned.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a decimal: {value!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field} must be a finite nonnegative decimal")
    return parsed


def kodex_percent_to_proportion(value: str | None) -> Decimal | None:
    """KODEX `ratio` is documented as percent points excluding cash assets."""

    percent_points = parse_nonnegative_decimal(value, field="ratio")
    if percent_points is None:
        return None
    if percent_points > 100:
        raise ValueError("ratio cannot exceed 100 percent points")
    return percent_points / Decimal("100")
