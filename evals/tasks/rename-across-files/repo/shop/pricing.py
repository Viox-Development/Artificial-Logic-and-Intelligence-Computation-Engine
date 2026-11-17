"""Price calculation."""

TAX_RATES = {"NL": 0.21, "DE": 0.19, "US": 0.0}


def calc(items, country="NL"):
    """Total price of [(unit_price, qty), ...] including the country's tax, rounded to cents."""
    net = sum(price * qty for price, qty in items)
    return round(net * (1 + TAX_RATES.get(country, 0.0)), 2)
