"""Invoices."""

from shop import pricing


def invoice_text(customer, items, country="NL"):
    amount = pricing.calc(items, country)
    return f"Invoice for {customer}: {amount:.2f} EUR"
