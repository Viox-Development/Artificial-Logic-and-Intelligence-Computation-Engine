from cart import Cart, merge_tags


def test_carts_are_independent():
    a = Cart()
    a.add("apple", 2)
    b = Cart()
    assert b.items == []
    assert b.total_qty() == 0
    assert a.total_qty() == 2


def test_given_items_are_used():
    c = Cart([("pear", 3)])
    c.add("fig")
    assert c.total_qty() == 4


def test_merge_tags_independent():
    assert merge_tags(["a"]) == {"a": True}
    assert merge_tags(["b"]) == {"b": True}


def test_merge_tags_extra():
    base = {"x": False}
    assert merge_tags(["y"], base) == {"x": False, "y": True}
