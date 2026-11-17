"""Checkout flow."""

from shop.pricing import calculate_total


def checkout(cart, country):
    items = [(line["price"], line["qty"]) for line in cart]
    total = calculate_total(items, country)
    return {"total": total, "lines": len(cart)}
