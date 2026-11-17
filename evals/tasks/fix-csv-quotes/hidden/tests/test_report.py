from report import total


def test_quoted_commas(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text('name,city,amount\n"A, B","C, D",1.5\nE,F,2\n', encoding="utf-8")
    assert total(p) == 3.5


def test_escaped_quotes(tmp_path):
    p = tmp_path / "b.csv"
    p.write_text('name,city,amount\n"He said ""hi"", ok",Rome,5\n', encoding="utf-8")
    assert total(p) == 5


def test_blank_trailing_line(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("name,city,amount\nX,Y,7\n\n", encoding="utf-8")
    assert total(p) == 7
