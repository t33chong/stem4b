"""Read publisher navigation independently of source batches and narration caches."""

import posixpath
import re
import unicodedata
import xml.etree.ElementTree as XML
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile

import pymupdf
from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET

from .models import Book, SourceToc, TocEntry


def reference(document: str, href: str) -> tuple[str, str]:
    parsed = urlsplit(href)
    resource = unquote(parsed.path)
    path = (
        posixpath.normpath(posixpath.join(posixpath.dirname(document), resource))
        if resource
        else document
    )
    if parsed.scheme or parsed.netloc or path.startswith(("../", "/")) or "\\" in path:
        raise ValueError("Nonlocal or unsafe TOC target")
    return path, unquote(parsed.fragment)


def read_source_toc(source: Path, book: Book) -> SourceToc:
    try:
        toc = _read_source_toc(source, book)
        toc.source_title = book.title
        return toc
    except (XML.ParseError, KeyError, BadZipFile) as exc:
        raise ValueError(
            f"Cannot read the source table of contents in {source.name}: {exc}"
        ) from exc


def _read_source_toc(source: Path, book: Book) -> SourceToc:
    ids = {u.id for u in book.units}
    if book.format == "pdf":
        with pymupdf.open(source) as document:
            entries = []
            for level, title, page in document.get_toc():
                sid = f"p{page:05d}"
                entries.append(
                    TocEntry(
                        title=title,
                        level=level,
                        target=f"page:{page}",
                        source_id=sid if sid in ids else None,
                        selected=sid in ids,
                        reason=""
                        if sid in ids
                        else "Outside the selected pages or not a local page target",
                    )
                )
        return SourceToc(kind="pdf_outline" if entries else "none", entries=entries)

    documents = {}
    for unit in book.units:
        resource = unit.location.rsplit(", block ", 1)[0]
        documents.setdefault(resource, []).append(unit)
    with ZipFile(source) as archive:

        def read(path):
            if archive.getinfo(path).file_size > 100_000_000:
                raise ValueError(f"EPUB navigation resource is unexpectedly large: {path}")
            return archive.read(path)

        container = ET.fromstring(read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile")
        if rootfile is None:
            raise ValueError("EPUB container has no package document")
        package, _ = reference("", rootfile.attrib["full-path"])
        root = ET.fromstring(read(package))
        manifest = root.findall(".//{*}manifest/{*}item")
        raw = []
        kind = "none"
        for item in manifest:
            if "nav" not in item.attrib.get("properties", "").split():
                continue
            path, _ = reference(package, item.attrib["href"])
            nav_root = ET.fromstring(read(path))
            nav = next(
                (
                    node
                    for node in nav_root.iter()
                    if node.tag.rsplit("}", 1)[-1] == "nav"
                    and (
                        "toc" in node.attrib.get("{http://www.idpf.org/2007/ops}type", "").split()
                        or "doc-toc" in node.attrib.get("role", "").split()
                    )
                ),
                None,
            )
            if nav is None:
                continue

            def walk_nav(ol, level=1, nav_path=path):
                if ol is None:
                    return
                for li in ol.findall("{*}li"):
                    label = li.find("{*}a")
                    if label is None:
                        label = li.find("{*}span")
                    if label is not None:
                        raw.append(
                            (
                                level,
                                " ".join("".join(label.itertext()).split()),
                                nav_path,
                                label.attrib.get("href", ""),
                            )
                        )
                    walk_nav(li.find("{*}ol"), level + 1)

            walk_nav(nav.find("{*}ol"))
            if raw:
                kind = "epub_nav"
                break
        if not raw:
            spine = root.find("{*}spine")
            ncx_id = spine.attrib.get("toc") if spine is not None else None
            ncx = next((item for item in manifest if item.attrib.get("id") == ncx_id), None)
            if ncx is None:
                ncx = next(
                    (
                        item
                        for item in manifest
                        if item.attrib.get("media-type") == "application/x-dtbncx+xml"
                    ),
                    None,
                )
            if ncx is not None:
                path, _ = reference(package, ncx.attrib["href"])
                nav_map = ET.fromstring(read(path)).find(".//{*}navMap")

                def walk_ncx(parent, level=1):
                    if parent is None:
                        return
                    for point in parent.findall("{*}navPoint"):
                        label, content = point.find("{*}navLabel/{*}text"), point.find("{*}content")
                        if label is not None and content is not None:
                            raw.append(
                                (
                                    level,
                                    " ".join("".join(label.itertext()).split()),
                                    path,
                                    content.attrib.get("src", ""),
                                )
                            )
                        walk_ncx(point, level + 1)

                walk_ncx(nav_map)
                if raw:
                    kind = "epub_ncx"

        anchors = {}
        soups = {}

        def label_key(value):
            value = unicodedata.normalize("NFKC", value).casefold()
            value = re.sub(r"^(chapter|part|section|appendix)\s+", "", value)
            return "".join(c for c in value if c.isalnum())

        def locate(resource, fragment, title):
            units = documents[resource]
            if not fragment:
                # Some publishers point EVERY subentry at its chapter file without
                # fragments. Resolve distinct labels against actual body headings.
                matches = [
                    u.id for u in units if u.heading and label_key(u.heading) == label_key(title)
                ]
                if matches:
                    return matches[0] if len(matches) == 1 else None
                return units[0].id
            if resource not in anchors:
                mapping = {}
                for unit in units:
                    soup = BeautifulSoup(unit.text, "html.parser")
                    for tag in soup.find_all(True):
                        for anchor in (tag.get("id"), tag.get("name") if tag.name == "a" else None):
                            if anchor:
                                mapping.setdefault(anchor, set()).add(unit.id)
                anchors[resource] = mapping
            found = anchors[resource].get(fragment, set())
            if len(found) == 1:
                return next(iter(found))
            if resource not in soups:
                soups[resource] = BeautifulSoup(read(resource), "html.parser")
            soup = soups[resource]
            target = soup.find(id=fragment) or soup.find("a", attrs={"name": fragment})
            if target is None:
                return None
            # Wrapper IDs disappear when extraction flattens sections. Locate their
            # first heading in the same document, never fall back to the document start.
            heading = (
                target
                if target.name in {f"h{i}" for i in range(1, 7)}
                else target.find([f"h{i}" for i in range(1, 7)])
            )
            if heading is None and not target.get_text(strip=True):
                heading = target.find_next([f"h{i}" for i in range(1, 7)])
            if heading is not None:
                matches = [u.id for u in units if u.heading == heading.get_text(" ", strip=True)]
                if len(matches) == 1:
                    return matches[0]
            if target.name == "body":
                return units[0].id
            return None

        entries = []
        for level, title, document, href in raw:
            entry = TocEntry(title=title, level=level, target=f"{document} → {href}")
            try:
                if not href:
                    raise ValueError("Navigation label has no destination")
                resource, fragment = reference(document, href)
                if resource not in documents:
                    entry.selected = False
                    entry.reason = "Destination is not in the selected reading-order source"
                else:
                    entry.source_id = locate(resource, fragment, title)
                    if entry.source_id is None:
                        entry.reason = "Destination fragment could not be uniquely located"
            except ValueError as exc:
                entry.reason = str(exc)
            entries.append(entry)
        return SourceToc(kind=kind, entries=entries)
