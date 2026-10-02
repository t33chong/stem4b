from zipfile import ZipFile

import pytest

from technical_audiobook.config import ExtractionConfig
from technical_audiobook.extract import extract_book
from technical_audiobook.models import Book, SourceUnit
from technical_audiobook.source_toc import read_source_toc
from technical_audiobook.storage import read_json, write_json


def epub_navigation(tmp_path, nav=None, ncx=None):
    target = tmp_path / "toc.epub"
    with ZipFile(target, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
        )
        manifest = (
            '<item id="chapter" href="text/chapter.xhtml" media-type="application/xhtml+xml"/>'
        )
        if nav:
            manifest += '<item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>'
            archive.writestr("OPS/nav.xhtml", nav)
        if ncx:
            manifest += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
            archive.writestr("OPS/toc.ncx", ncx)
        archive.writestr(
            "OPS/book.opf",
            f'<package><manifest>{manifest}</manifest><spine toc="ncx"><itemref idref="chapter"/></spine></package>',
        )
        archive.writestr(
            "OPS/text/chapter.xhtml",
            '<html><body><section id="chapter"><h1 id="start">Chapter 1. Signals</h1><p>Substantive prose.</p><section id="wrapped"><h2>1.1. Voltage</h2><p>More prose.</p></section><a id="empty"></a><h2>1.2. Current</h2></section></body></html>',
        )
    units = [
        SourceUnit(
            id="one",
            location="OPS/text/chapter.xhtml, block 1",
            text='<h1 id="start">Chapter 1. Signals</h1>',
            heading="Chapter 1. Signals",
            heading_level=1,
        ),
        SourceUnit(id="two", location="OPS/text/chapter.xhtml, block 2", text="Substantive prose."),
        SourceUnit(
            id="three",
            location="OPS/text/chapter.xhtml, block 3",
            text="<h2>1.1. Voltage</h2>",
            heading="1.1. Voltage",
            heading_level=2,
        ),
        SourceUnit(id="four", location="OPS/text/chapter.xhtml, block 4", text="More prose."),
        SourceUnit(
            id="five",
            location="OPS/text/chapter.xhtml, block 5",
            text="<h2>1.2. Current</h2>",
            heading="1.2. Current",
            heading_level=2,
        ),
    ]
    return target, Book(title="Signals", source_sha256="source", format="epub", units=units)


def test_epub3_nav_hierarchy_fragments_and_precedence(tmp_path):
    nav = """<html xmlns:epub="http://www.idpf.org/2007/ops"><body>
      <nav epub:type="page-list"><ol><li><a href="missing.xhtml">Wrong nav</a></li></ol></nav>
      <nav epub:type="toc"><ol><li><a href="text/chapter.xhtml#chapter">1. Signals</a><ol>
      <li><a href="text/chapter.xhtml#wrapped">1.1. Voltage</a></li>
      <li><a href="text/chapter.xhtml#empty">1.2. Current</a></li>
      </ol></li></ol></nav></body></html>"""
    source, book = epub_navigation(
        tmp_path,
        nav,
        '<ncx><navMap><navPoint><navLabel><text>Wrong NCX</text></navLabel><content src="missing.xhtml"/></navPoint></navMap></ncx>',
    )
    toc = read_source_toc(source, book)
    assert toc.kind == "epub_nav"
    assert [(e.level, e.source_id) for e in toc.entries] == [(1, "one"), (2, "three"), (2, "five")]
    assert toc.source_title == "Signals"


def test_epub2_fragmentless_subentries_resolve_to_distinct_body_headings(tmp_path):
    ncx = """<ncx><navMap><navPoint><navLabel><text>1. Signals</text></navLabel><content src="text/chapter.xhtml"/>
      <navPoint><navLabel><text>1.1. Voltage</text></navLabel><content src="text/chapter.xhtml"/></navPoint>
      <navPoint><navLabel><text>1.2. Current</text></navLabel><content src="text/chapter.xhtml"/></navPoint>
      </navPoint></navMap></ncx>"""
    source, book = epub_navigation(tmp_path, ncx=ncx)
    toc = read_source_toc(source, book)
    assert toc.kind == "epub_ncx"
    assert [e.source_id for e in toc.entries] == ["one", "three", "five"]
    assert [e.level for e in toc.entries] == [1, 2, 2]


def test_missing_fragment_is_not_silently_moved_to_document_start(tmp_path):
    ncx = """<ncx><navMap><navPoint><navLabel><text>1. Signals</text></navLabel><content src="text/chapter.xhtml#missing"/></navPoint>
      <navPoint><navLabel><text>Excluded supplement</text></navLabel><content src="supplement.xhtml"/></navPoint>
      </navMap></ncx>"""
    source, book = epub_navigation(tmp_path, ncx=ncx)
    toc = read_source_toc(source, book)
    assert toc.entries[0].source_id is None and toc.entries[0].selected
    assert "uniquely located" in toc.entries[0].reason
    assert not toc.entries[1].selected


def test_malformed_navigation_fails_explicitly(tmp_path):
    source, book = epub_navigation(tmp_path, ncx="<ncx><broken>")
    with pytest.raises(ValueError, match="Cannot read the source table"):
        read_source_toc(source, book)


def test_old_extraction_cache_gains_toc_without_changing_units(pdf_book, workspace, monkeypatch):
    book = extract_book(pdf_book, workspace, ExtractionConfig())
    assert [(e.title, e.source_id) for e in book.toc.entries] == [
        ("1. Signals", "p00001"),
        ("2. Information", "p00003"),
    ]
    cache = next((workspace / "source").glob("*.json"))
    write_json(cache, book.model_dump(exclude={"toc"}))

    def no_extraction(*args):
        raise AssertionError("Navigation metadata must not re-extract source units")

    monkeypatch.setattr("technical_audiobook.extract.extract_pdf", no_extraction)
    again = extract_book(pdf_book, workspace, ExtractionConfig())
    assert again.units == book.units and again.toc == book.toc
    assert read_json(workspace / "source.json")["toc"]["kind"] == "pdf_outline"


def test_pdf_outline_preserves_multiple_entries_on_one_page_and_selection(pdf_book, workspace):
    import pymupdf

    with pymupdf.open(pdf_book) as document:
        document.set_toc([[1, "1. Signals", 1], [2, "1.1. Amplitude", 1], [1, "2. Information", 3]])
        document.saveIncr()
    book = extract_book(pdf_book, workspace, ExtractionConfig(end_page=2))
    assert [(e.level, e.source_id) for e in book.toc.entries[:2]] == [(1, "p00001"), (2, "p00001")]
    assert not book.toc.entries[2].selected
    assert book.units[0].heading == "1. Signals / 1.1. Amplitude"
