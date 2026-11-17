"""Sum the amount column of a CSV export."""

import csv
import sys


def total(path):
    s = 0.0
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("amount"):
                s += float(row["amount"])
    return s


def main(argv):
    print(f"Total: {total(argv[1]):.2f}")


if __name__ == "__main__":
    main(sys.argv)
