"""Roman numerals."""

VALUES = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
          (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
DIGITS = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def to_roman(n: int) -> str:
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError("n must be an int")
    if not 1 <= n <= 3999:
        raise ValueError("n must be in 1..3999")
    out = []
    for value, sym in VALUES:
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)


def from_roman(s: str) -> int:
    t = s.strip().upper()
    if not t or any(c not in DIGITS for c in t):
        raise ValueError(f"not a Roman numeral: {s!r}")
    total = 0
    for i, c in enumerate(t):
        v = DIGITS[c]
        total += -v if i + 1 < len(t) and DIGITS[t[i + 1]] > v else v
    if not 1 <= total <= 3999 or to_roman(total) != t:
        raise ValueError(f"not a canonical Roman numeral: {s!r}")
    return total
