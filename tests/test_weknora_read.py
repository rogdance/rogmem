"""Office and spreadsheet text must survive import, including table cells."""

from __future__ import annotations

from pathlib import Path

from rogmem.sections import read_sections


def _text(path: Path) -> str:
    return "\n".join(body for _, body in read_sections(path))


def test_docx_table_is_kept(tmp_path: Path):
    from docx import Document

    document = Document()
    document.add_paragraph("合同开头")
    table = document.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "甲方"
    table.rows[0].cells[1].text = "周生生"
    table.rows[1].cells[0].text = "金额"
    table.rows[1].cells[1].text = "1000"
    path = tmp_path / "contract.docx"
    document.save(path)
    text = _text(path)
    assert "合同开头" in text
    assert "周生生" in text
    assert "1000" in text


def test_sheet_cells_remain_when_the_rich_reader_fails(tmp_path: Path, monkeypatch):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet["A1"] = "捷安特"
    path = tmp_path / "names.xlsx"
    book.save(path)

    def broken(_path):
        raise RuntimeError("富阅读器没起来")

    monkeypatch.setattr("rogmem.weknora_read.markdown_for", broken)
    assert "捷安特" in _text(path)


def test_plain_table_text_remains_when_the_rich_reader_fails(tmp_path: Path, monkeypatch):
    path = tmp_path / "cities.csv"
    path.write_text("城市,台北\n", encoding="utf-8")

    def broken(_path):
        raise RuntimeError("富阅读器没起来")

    monkeypatch.setattr("rogmem.weknora_read.markdown_for", broken)
    assert "台北" in _text(path)


def test_a_reader_failure_keeps_its_reason(tmp_path: Path, monkeypatch):
    path = tmp_path / "deck.ppt"
    path.write_bytes(b"not a presentation")

    def broken(_path):
        raise ValueError("这份文稿要先换成可读的样子")

    monkeypatch.setattr("rogmem.weknora_read.markdown_for", broken)
    try:
        read_sections(path)
    except ValueError as exc:
        assert "这份文稿要先换成可读的样子" in str(exc)
        assert str(exc) != "没有抽出正文"
    else:
        raise AssertionError("应该把读失败交出来")


def _text_pdf(words: str) -> bytes:
    content = f"BT /F1 24 Tf 72 120 Td ({words}) Tj ET\n".encode()
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        b"4 0 obj\n<< /Length %d >>\nstream\n" % len(content) + content + b"endstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    blob = b"%PDF-1.4\n"
    offsets = [0]
    for obj in objects:
        offsets.append(len(blob))
        blob += obj
    xref_at = len(blob)
    xref = b"xref\n0 6\n0000000000 65535 f \n"
    xref += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets[1:])
    xref += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return blob + xref


def _picture_pdf(path: Path, words: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (960, 320), "white")
    ImageDraw.Draw(image).text((48, 120), words, fill="black", font=ImageFont.load_default(size=52))
    image.save(path, "PDF")


def test_a_picture_page_contributes_the_words_in_the_picture(tmp_path: Path):
    path = tmp_path / "deck.pdf"
    _picture_pdf(path, "NORTHWIND HARBOR")
    text = _text(path)
    assert "NORTHWIND" in text
    assert "HARBOR" in text
    assert "images/" not in text
    assert "_page_" not in text


def test_a_text_page_stays_text(tmp_path: Path, monkeypatch):
    path = tmp_path / "note.pdf"
    path.write_bytes(_text_pdf("Harbor ledger stays"))

    def unused(_data: bytes) -> str:
        raise AssertionError("a text layer does not need the picture reader")

    monkeypatch.setattr("rogmem.weknora_read._words_in_image", unused)
    assert "Harbor ledger stays" in _text(path)


def test_an_unread_picture_is_not_kept_as_its_filename(tmp_path: Path, monkeypatch):
    path = tmp_path / "deck.pdf"
    _picture_pdf(path, "NORTHWIND HARBOR")
    monkeypatch.setattr("rogmem.weknora_read._words_in_image", lambda _data: "")
    try:
        read_sections(path)
    except ValueError as exc:
        assert "没有抽出正文" in str(exc)
        assert "_page_" not in str(exc)
    else:
        raise AssertionError("没有读出的图片不应当变成正文")


def test_a_picture_whose_name_has_a_space_still_yields_its_words(tmp_path: Path):
    path = tmp_path / "deck page.pdf"
    _picture_pdf(path, "NORTHWIND HARBOR")
    text = _text(path)
    assert "NORTHWIND" in text
    assert "HARBOR" in text
    assert "_page_" not in text
    assert "images/" not in text


def test_prose_stays_when_the_picture_beside_it_cannot_be_read(monkeypatch):
    from rogmem.weknora_read import _absorb_images

    monkeypatch.setattr("rogmem.weknora_read._words_in_image", lambda _data: "")
    text = _absorb_images(
        "开篇一句还在\n\n![deck _page_1.jpg](images/deck _page_1.jpg)",
        {"images/deck _page_1.jpg": "aaaa"},
    )
    assert text == "开篇一句还在"


def test_a_broken_reader_keeps_prose_and_drops_the_filename(monkeypatch):
    from rogmem.weknora_read import _absorb_images

    def broken():
        raise RuntimeError("识图组件没起来")

    monkeypatch.setattr("rogmem.weknora_read._reader", broken)
    text = _absorb_images(
        "正文还在\n\n![deck _page_1.jpg](images/deck _page_1.jpg)",
        {"images/deck _page_1.jpg": "aaaa"},
    )
    assert "正文还在" in text
    assert "_page_" not in text


def test_words_from_a_spaced_picture_join_the_prose(monkeypatch):
    from rogmem.weknora_read import _absorb_images

    monkeypatch.setattr("rogmem.weknora_read._words_in_image", lambda _data: "北港账册")
    text = _absorb_images(
        "开篇一句\n\n![deck _page_1.jpg](images/deck _page_1.jpg)",
        {"images/deck _page_1.jpg": "aaaa"},
    )
    assert "开篇一句" in text
    assert "北港账册" in text
    assert "_page_" not in text


def test_only_a_picture_link_is_too_thin_to_be_words():
    from rogmem.engine import _too_thin

    assert _too_thin("![deck _page_1.jpg](images/deck _page_1.jpg)")
    assert not _too_thin("![deck _page_1.jpg](images/deck _page_1.jpg)\n\n北港还有一句正文")


def test_xlsx_rows_are_kept(tmp_path: Path):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "名录"
    sheet["A1"] = "名称"
    sheet["B1"] = "城市"
    sheet["A2"] = "捷安特"
    sheet["B2"] = "台湾"
    path = tmp_path / "names.xlsx"
    book.save(path)
    text = _text(path)
    assert "捷安特" in text
    assert "台湾" in text
