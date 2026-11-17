"""Format contact records for printing."""

import re


def format_customer(c):
    name = c.get("name")
    if name is None or not str(name).strip():
        name = "Unknown"
    else:
        name = re.sub(r"\s+", " ", str(name).strip())
        name = " ".join(part[:1].upper() + part[1:].lower() for part in name.split(" "))
    return f"Customer #{c['id']}: {name} <{c.get('email', '-')}>"


def format_supplier(s):
    name = s.get("company")
    if name is None or not str(name).strip():
        name = "Unknown"
    else:
        name = re.sub(r"\s+", " ", str(name).strip())
        name = " ".join(part[:1].upper() + part[1:].lower() for part in name.split(" "))
    country = s.get("country") or "??"
    return f"Supplier {name} ({country})"
