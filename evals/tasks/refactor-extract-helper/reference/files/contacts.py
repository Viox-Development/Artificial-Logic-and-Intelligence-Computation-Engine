"""Format contact records for printing."""

import re


def clean_name(name):
    if name is None or not str(name).strip():
        return "Unknown"
    name = re.sub(r"\s+", " ", str(name).strip())
    return " ".join(part[:1].upper() + part[1:].lower() for part in name.split(" "))


def format_customer(c):
    return f"Customer #{c['id']}: {clean_name(c.get('name'))} <{c.get('email', '-')}>"


def format_supplier(s):
    country = s.get("country") or "??"
    return f"Supplier {clean_name(s.get('company'))} ({country})"
