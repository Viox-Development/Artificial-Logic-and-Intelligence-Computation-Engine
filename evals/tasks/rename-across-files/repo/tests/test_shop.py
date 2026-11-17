from shop.checkout import checkout
from shop.invoice import invoice_text
from shop.pricing import calc


def test_calc():
    assert calc([(10.0, 2)], "NL") == 24.2
    assert calc([(10.0, 2)], "US") == 20.0


def test_checkout():
    assert checkout([{"price": 5.0, "qty": 1}, {"price": 1.0, "qty": 3}], "DE") == {"total": 9.52, "lines": 2}


def test_invoice():
    assert invoice_text("Ada", [(100.0, 1)]) == "Invoice for Ada: 121.00 EUR"
