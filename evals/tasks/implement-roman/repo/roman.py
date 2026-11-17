"""Roman numerals."""


def to_roman(n: int) -> str:
    """Convert an integer from 1 to 3999 to an upper-case Roman numeral using subtractive notation
    (4 -> "IV", 1994 -> "MCMXCIV"). Raise ValueError for anything outside 1..3999 and TypeError for non-int
    input (bool counts as non-int)."""
    raise NotImplementedError


def from_roman(s: str) -> int:
    """Convert a Roman numeral to an integer. Lower case is accepted and surrounding whitespace ignored.
    Raise ValueError for an empty string, characters other than I V X L C D M, or a numeral that is not in
    canonical form (for example "IIII", "IC", "VX"): the rule is that to_roman(from_roman(s)) == s.upper()."""
    raise NotImplementedError
