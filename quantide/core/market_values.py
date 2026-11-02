"""Normalization of scalar values received from market-data publishers."""


def float_or_none(value):
    """Parse an optional numeric quote field without inventing a zero price."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
