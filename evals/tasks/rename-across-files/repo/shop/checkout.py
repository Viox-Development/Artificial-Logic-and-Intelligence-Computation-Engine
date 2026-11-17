"""Checkout flow."""

from shop.pricing import calc


def checkout(cart, country):
    items = [(line["price"], line["qty"]) for line in cart]
    total = calc(items, country)
    return {"total": total, "lines": len(cart)}
