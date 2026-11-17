import pytest

from pager import page_count, paginate


def test_first_page():
    assert paginate(list(range(25)), 1) == list(range(10))


def test_last_partial_page():
    assert paginate(list(range(25)), 3) == [20, 21, 22, 23, 24]


def test_past_the_end():
    assert paginate(list(range(5)), 2) == []


def test_page_count():
    assert page_count(list(range(25))) == 3
    assert page_count(list(range(20))) == 2
    assert page_count([]) == 0


def test_bad_page():
    with pytest.raises(ValueError):
        paginate([1, 2], 0)


def test_custom_size():
    assert paginate(list("abcdefg"), 2, per_page=3) == ["d", "e", "f"]
    assert page_count(list("abcdefg"), per_page=3) == 3
    assert page_count([1], per_page=1) == 1
