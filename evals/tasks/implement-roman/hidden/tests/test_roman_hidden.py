import pytest

from roman import from_roman, to_roman

CASES = {1: "I", 4: "IV", 9: "IX", 14: "XIV", 40: "XL", 90: "XC", 400: "CD", 1994: "MCMXCIV", 2024: "MMXXIV",
         3999: "MMMCMXCIX"}


@pytest.mark.parametrize("n,s", CASES.items())
def test_to_roman(n, s):
    assert to_roman(n) == s


@pytest.mark.parametrize("n,s", CASES.items())
def test_from_roman(n, s):
    assert from_roman(s) == n
    assert from_roman(f"  {s.lower()} ") == n


def test_round_trip():
    for n in range(1, 4000):
        assert from_roman(to_roman(n)) == n


@pytest.mark.parametrize("bad", [0, -1, 4000])
def test_to_roman_range(bad):
    with pytest.raises(ValueError):
        to_roman(bad)


@pytest.mark.parametrize("bad", [1.0, "5", True])
def test_to_roman_type(bad):
    with pytest.raises(TypeError):
        to_roman(bad)


@pytest.mark.parametrize("bad", ["", "   ", "ABC", "IIII", "IC", "VX", "MMMM", "IIV"])
def test_from_roman_invalid(bad):
    with pytest.raises(ValueError):
        from_roman(bad)
