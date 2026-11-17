from contacts import clean_name, format_customer, format_supplier


def test_customer():
    assert format_customer({"id": 7, "name": "  aDA   lovelace ", "email": "ada@x.org"}) == \
        "Customer #7: Ada Lovelace <ada@x.org>"
    assert format_customer({"id": 1, "name": None}) == "Customer #1: Unknown <->"
    assert format_customer({"id": 2, "name": "   "}) == "Customer #2: Unknown <->"


def test_supplier():
    assert format_supplier({"company": "acme\tCORP"}) == "Supplier Acme Corp (??)"
    assert format_supplier({"company": "", "country": "NL"}) == "Supplier Unknown (NL)"
    assert format_supplier({"company": 42, "country": "DE"}) == "Supplier 42 (DE)"


def test_helper():
    assert clean_name("  mcDONALD  o'brien") == "Mcdonald O'brien"
    assert clean_name(None) == "Unknown"
