"""Output formats for todo.py."""


def format_table(todos):
    rows = [f"{'ID':>3}  {'DONE':4}  TITLE"]
    for t in sorted(todos, key=lambda t: t["id"]):
        rows.append(f"{t['id']:>3}  {'x' if t['done'] else ' ':4}  {t['title']}")
    return "\n".join(rows)


def format_json(todos):
    import json

    return json.dumps(sorted(todos, key=lambda t: t["id"]), indent=2)
