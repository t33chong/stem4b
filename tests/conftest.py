import io
import math
import struct
import wave
from pathlib import Path
from zipfile import ZipFile

import pymupdf
import pytest
from PIL import Image


def wave_bytes(rate=22050, duration=0.12):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(
            b"".join(
                struct.pack("<h", int(1000 * math.sin(i * 2 * math.pi * 440 / rate)))
                for i in range(round(rate * duration))
            )
        )
    return buffer.getvalue()


@pytest.fixture
def pdf_book(tmp_path):
    target = tmp_path / "technical.pdf"
    with pymupdf.open() as doc:
        for number, text in enumerate(
            [
                "1. Signals\nA signal carries information. Its amplitude is",
                "measured in volts. A code example follows.\nfor x in samples:\n    print(x)",
                "2. Information\nH(X) = - sum p(x) log p(x)\nFigure 2.1: A simple chart.",
            ]
        ):
            page = doc.new_page()
            page.insert_text((50, 50), text)
            if number == 2:
                page.draw_rect(pymupdf.Rect(50, 140, 160, 190), color=(0.1, 0.4, 0.9))
        doc.set_metadata({"title": "Signals & Information", "author": "Test Author"})
        doc.set_toc([[1, "1. Signals", 1], [1, "2. Information", 3]])
        doc.save(target)
    return target


@pytest.fixture
def epub_book(tmp_path):
    target = tmp_path / "technical.epub"
    pixels = io.BytesIO()
    Image.new("RGB", (30, 30), "blue").save(pixels, "PNG")
    container = """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
      <rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>"""
    package = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:title>Practical Signals</dc:title><dc:creator>Test Author</dc:creator>
      </metadata>
      <manifest>
        <item id="second" href="chapters/second.xhtml" media-type="application/xhtml+xml"/>
        <item id="first" href="chapters/first.xhtml" media-type="application/xhtml+xml"/>
        <item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/>
        <item id="cover" href="images/plot.png" media-type="image/png" properties="cover-image"/>
      </manifest>
      <spine><itemref idref="first"/><itemref idref="second"/><itemref idref="notes" linear="no"/></spine>
    </package>"""
    first = """<html xmlns:epub="http://www.idpf.org/2007/ops"><body><section>
      <h1>1. Signals</h1><p>A signal carries information.<a epub:type="noteref" href="../notes.xhtml#n1">1</a></p>
      <figure><img src="../images/plot.png" alt="Signal amplitude"/><figcaption>Figure 1.1: Amplitude.</figcaption></figure>
      <pre>for x in samples:\n    print(x)</pre>
      <table><tr><th>Signal</th><th>Volts</th></tr><tr><td>A</td><td>2</td></tr></table>
      <p>The mean is <math><mfrac><mi>x</mi><mn>2</mn></mfrac></math>.</p>
    </section></body></html>"""
    second = """<html><body><h1>2. Information</h1><p>Entropy measures uncertainty.</p>
      <svg xmlns="http://www.w3.org/2000/svg" width="120" height="80" viewBox="0 0 120 80">
      <rect width="120" height="80" fill="green"/></svg></body></html>"""
    notes = """<html xmlns:epub="http://www.idpf.org/2007/ops"><body>
      <aside epub:type="footnote" id="n1">This is an explanatory note.</aside></body></html>"""
    with ZipFile(target, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OPS/book.opf", package)
        archive.writestr("OPS/chapters/second.xhtml", second)
        archive.writestr("OPS/chapters/first.xhtml", first)
        archive.writestr("OPS/notes.xhtml", notes)
        archive.writestr("OPS/images/plot.png", pixels.getvalue())
    return target


@pytest.fixture
def workspace(tmp_path) -> Path:
    work = tmp_path / "Test's work with spaces"
    work.mkdir()
    return work
