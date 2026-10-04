from pathlib import Path

import pytest

from stem4b.chunking import plan_chunks
from stem4b.config import ExtractionConfig, NarrationConfig
from stem4b.extract import embedded_svg, epub_reference, extract_book, save_image
from stem4b.models import Book, SourceUnit


def test_pdf_renders_every_page_and_keeps_outline(pdf_book, workspace):
    book = extract_book(pdf_book, workspace, ExtractionConfig())
    assert book.title == "Signals & Information"
    assert [u.id for u in book.units] == ["p00001", "p00002", "p00003"]
    assert book.units[0].heading == "1. Signals"
    assert "H(X)" in book.units[2].text
    assert all((workspace / u.images[0].path).exists() for u in book.units)
    assert book.cover == book.units[0].images[0]
    chunks = plan_chunks(book, NarrationConfig())
    assert [[u.id for u in c.units] for c in chunks] == [["p00001", "p00002"], ["p00003"]]
    assert chunks[0].after.id == "p00003"
    assert chunks[1].before.id == "p00002"


def test_pdf_page_selection_and_scan(pdf_book, workspace, tmp_path):
    book = extract_book(pdf_book, workspace, ExtractionConfig(start_page=2, end_page=2))
    assert [u.id for u in book.units] == ["p00002"]
    assert book.cover and book.cover.label == "PDF physical page 1"
    assert book.cover != book.units[0].images[0]
    assert book.warnings
    with pytest.raises(ValueError, match="outside"):
        extract_book(pdf_book, workspace, ExtractionConfig(end_page=4))
    import pymupdf

    blank = tmp_path / "scan.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_image(page.rect, filename=str(workspace / book.units[0].images[0].path))
        doc.save(blank)
    scan = extract_book(blank, workspace, ExtractionConfig())
    assert scan.units[0].text == ""
    assert scan.units[0].images
    assert any("vision" in w for w in scan.warnings)


def test_epub_spine_images_technical_markup_and_cross_document_notes(epub_book, workspace):
    book = extract_book(epub_book, workspace, ExtractionConfig())
    assert book.title == "Practical Signals"
    assert [u.heading for u in book.units if u.heading] == ["1. Signals", "2. Information"]
    content = "\n".join(u.text for u in book.units)
    assert content.index("Signals") < content.index("Information")
    assert content.count("This is an explanatory note.") == 1
    assert "data-audiobook-footnote" in content
    assert "    print(x)" in content
    assert "<table>" in content
    assert "<mfrac>" in content
    assert sum(len(u.images) for u in book.units) == 2
    assert book.cover and (workspace / book.cover.path).exists()
    assert all(Path(workspace / image.path).is_file() for u in book.units for image in u.images)


def test_extraction_cache_recovers_missing_asset(pdf_book, workspace):
    config = ExtractionConfig()
    book = extract_book(pdf_book, workspace, config)
    asset = workspace / book.units[1].images[0].path
    asset.unlink()
    again = extract_book(pdf_book, workspace, config)
    assert asset.exists()
    assert again == book


def test_safe_epub_paths():
    assert epub_reference("OPS/text/ch1.xhtml", "../images/pic%201.png") == (
        "OPS/images/pic 1.png",
        "",
    )
    assert epub_reference("OPS/text/ch1.xhtml", "#note") == ("OPS/text/ch1.xhtml", "note")
    for unsafe in (
        "../../../outside.png",
        "/absolute.png",
        "https://example.com/image.png",
        "file:///etc/passwd",
    ):
        with pytest.raises(ValueError):
            epub_reference("OPS/ch1.xhtml", unsafe)


def test_chunk_limits_never_drop_units():
    units = [SourceUnit(id=str(i), location=str(i), text="a" * 600) for i in range(7)]
    book = Book(title="Test", source_sha256="abc", format="epub", units=units)
    chunks = plan_chunks(book, NarrationConfig(max_source_chars=1500))
    assert [u.id for c in chunks for u in c.units] == [u.id for u in units]
    assert all(sum(len(u.text) for u in c.units) <= 1500 for c in chunks)
    book.units[0].text = "a" * 1600
    with pytest.raises(ValueError, match="never silently truncated"):
        plan_chunks(book, NarrationConfig(max_source_chars=1500))


def test_svg_keeps_image_and_vector_overlay(workspace):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (50, 50), "blue").save(buffer, "PNG")
    svg = b"""<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50">
      <image href="../images/base.png" width="50" height="50"/>
      <rect x="50" y="0" width="50" height="50" fill="red"/>
    </svg>"""
    dependencies = []

    def read(resource):
        dependencies.append(resource)
        return buffer.getvalue()

    resolved = embedded_svg(svg, "OPS/text/diagram.svg", read)
    asset = save_image(resolved, "Diagram", workspace, 512, svg=True)
    assert dependencies == ["OPS/images/base.png"]
    with Image.open(workspace / asset.path) as image:
        assert image.getpixel((25, 25)) == (0, 0, 255)
        assert image.getpixel((150, 25)) == (255, 0, 0)
