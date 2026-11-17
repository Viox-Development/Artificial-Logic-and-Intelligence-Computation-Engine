"""Split a list into pages."""


def paginate(items, page, per_page=10):
    """Return the items on a 1-based `page`. Pages past the end are empty lists."""
    if page < 1 or per_page < 1:
        raise ValueError("page and per_page must be positive")
    start = (page - 1) * per_page
    return items[start:start + per_page]


def page_count(items, per_page=10):
    """How many pages `items` fills. An empty list has 0 pages."""
    if per_page < 1:
        raise ValueError("per_page must be positive")
    return -(-len(items) // per_page)
