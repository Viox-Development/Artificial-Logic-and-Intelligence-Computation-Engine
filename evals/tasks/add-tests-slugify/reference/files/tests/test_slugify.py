from slugify import slugify


def test_lower():
    assert slugify("Hello World") == "hello-world"


def test_accents():
    assert slugify("Café Crème") == "cafe-creme"


def test_runs_and_edges():
    assert slugify("  --Hi!!  there?? ") == "hi-there"


def test_max_len():
    assert slugify("a" * 80) == "a" * 50
    assert slugify("abcd efgh", max_len=5) == "abcd"
