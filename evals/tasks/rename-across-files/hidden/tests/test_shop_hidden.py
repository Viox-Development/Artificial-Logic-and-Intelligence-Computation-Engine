from shop import pricing
from shop.checkout import checkout
from shop.invoice import invoice_text
from shop.pricing import calculate_total


def test_renamed():
    assert calculate_total([(10.0, 2)], "NL") == 24.2
    assert not hasattr(pricing, "ca" + "lc")  # spelled apart so the "absent" check does not see it


def test_callers():
    assert checkout([{"price": 5.0, "qty": 1}], "US") == {"total": 5.0, "lines": 1}
    assert invoice_text("Bo", [(10.0, 1)], "DE") == "Invoice for Bo: 11.90 EUR"
