"""Sum the amount column of a CSV export."""

import sys


def total(path):
    s = 0.0
    with open(path, encoding="utf-8") as f:
        next(f)  # header
        for line in f:
            parts = line.strip().split(",")
            s += float(parts[2])
    return s


def main(argv):
    print(f"Total: {total(argv[1]):.2f}")


if __name__ == "__main__":
    main(sys.argv)
