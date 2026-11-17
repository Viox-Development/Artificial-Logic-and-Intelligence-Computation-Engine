"""A tiny shopping cart."""


class Cart:
    def __init__(self, items=None):
        self.items = list(items) if items is not None else []

    def add(self, name, qty=1):
        if qty < 1:
            raise ValueError("qty must be at least 1")
        self.items.append((name, qty))

    def total_qty(self):
        return sum(q for _, q in self.items)


def merge_tags(tags, extra=None):
    """Return a dict with every tag in `tags` set to True, plus the entries of `extra`."""
    out = dict(extra or {})
    out.update({t: True for t in tags})
    return out
