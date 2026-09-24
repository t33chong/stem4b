"""Local ingestion: preserve visual evidence and the publisher's reading order."""

import base64
import hashlib
import io
import mimetypes
import posixpath
import re
import xml.etree.ElementTree as XML
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

import pymupdf
from bs4 import BeautifulSoup, NavigableString, Tag
from defusedxml import ElementTree as ET
from PIL import Image, ImageOps

from .config import ExtractionConfig
from .models import Asset, Book, SourceUnit
from .storage import asset_path, atomic_bytes, digest, file_digest, read_json, write_json

EXTRACT_VERSION = 2


def embedded_svg(data: bytes, document: str, read: Callable[[str], bytes]) -> bytes:
    """Make SVG image dependencies self-contained without discarding other graphics."""
    root = ET.fromstring(data)
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "image":
            continue
        for attr in ("href", "{http://www.w3.org/1999/xlink}href"):
            href = element.attrib.get(attr)
            if not href or href.startswith(("data:", "#")):
                continue
            resource, _ = epub_reference(document, href)
            payload = read(resource)
            if resource.lower().endswith(".svg"):
                raise ValueError("Nested external SVG images require flattening before conversion")
            mime = mimetypes.guess_type(resource)[0] or "application/octet-stream"
            element.set(attr, f"data:{mime};base64,{base64.b64encode(payload).decode()}")
    XML.register_namespace("", "http://www.w3.org/2000/svg")
    XML.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    return XML.tostring(root, encoding="utf-8")


def inline_svg_bytes(tag: Tag) -> bytes:
    # HTML parsers lowercase attribute names, but SVG uses case-sensitive names.
    case_sensitive = (
        "viewBox",
        "preserveAspectRatio",
        "gradientUnits",
        "gradientTransform",
        "spreadMethod",
        "patternUnits",
        "patternContentUnits",
        "patternTransform",
        "markerWidth",
        "markerHeight",
        "markerUnits",
        "refX",
        "refY",
        "textLength",
        "lengthAdjust",
        "clipPathUnits",
    )
    for element in [tag, *tag.find_all(True)]:
        for name in case_sensitive:
            if name.lower() in element.attrs:
                element.attrs[name] = element.attrs.pop(name.lower())
        for name in ("linearGradient", "radialGradient", "clipPath", "textPath"):
            if element.name == name.lower():
                element.name = name
    tag["xmlns"] = "http://www.w3.org/2000/svg"
    tag["xmlns:xlink"] = "http://www.w3.org/1999/xlink"
    return str(tag).encode("utf-8")


def save_image(data: bytes, label: str, work: Path, maximum: int, svg: bool = False) -> Asset:
    if svg:
        with pymupdf.open(stream=data, filetype="svg") as document:
            page = document[0]
            scale = min(2, maximum / max(page.rect.width, page.rect.height))
            data = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
    with Image.open(io.BytesIO(data)) as original:
        image = ImageOps.exif_transpose(original).convert("RGBA")
        image.thumbnail((maximum, maximum))
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        buffer = io.BytesIO()
        background.save(buffer, format="PNG")
    payload = buffer.getvalue()
    checksum = hashlib.sha256(payload).hexdigest()
    relative = f"source/assets/{checksum}.png"
    target = work / relative
    if not target.exists() or file_digest(target) != checksum:
        atomic_bytes(target, payload)
    return Asset(path=relative, sha256=checksum, label=label)


def extract_pdf(source: Path, work: Path, config: ExtractionConfig) -> Book:
    units = []
    with pymupdf.open(source) as document:
        if document.needs_pass:
            raise ValueError("This PDF is password protected; supply an unlocked copy")
        start = (config.start_page or 1) - 1
        end = config.end_page or len(document)
        if start >= len(document) or end > len(document):
            raise ValueError(f"Page selection is outside this {len(document)}-page PDF")
        outline: dict[int, list[tuple[int, str]]] = {}
        for level, title, page in document.get_toc():
            if page > 0:
                outline.setdefault(page - 1, []).append((level, title))
        for number in range(start, end):
            page = document[number]
            scale = min(
                config.pdf_dpi / 72,
                config.image_max_dimension / max(page.rect.width, page.rect.height),
            )
            image = save_image(
                page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png"),
                f"PDF physical page {number + 1}",
                work,
                config.image_max_dimension,
            )
            headings = outline.get(number, [])
            unit = SourceUnit(
                id=f"p{number + 1:05d}",
                location=f"PDF physical page {number + 1}",
                text=page.get_text("text", sort=True).strip(),
                images=[image],
                heading=" / ".join(title for _, title in headings),
                heading_level=min((level for level, _ in headings), default=0),
            )
            units.append(unit)
        metadata = document.metadata or {}
        warnings = []
        if config.start_page or config.end_page:
            warnings.append(f"Only physical PDF pages {start + 1}–{end} were selected.")
        if any(not unit.text for unit in units):
            warnings.append(
                "Some pages have no extractable text; their images require a vision model."
            )
        return Book(
            source_sha256=file_digest(source),
            title=metadata.get("title") or source.stem,
            author=metadata.get("author") or "Unknown author",
            format="pdf",
            units=units,
            cover=units[0].images[0] if start == 0 and units else None,
            warnings=warnings,
        )


def epub_reference(document: str, reference: str) -> tuple[str, str]:
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc:
        raise ValueError(f"External EPUB resource is not embedded: {reference}")
    resource = unquote(parsed.path)
    resolved = (
        posixpath.normpath(posixpath.join(posixpath.dirname(document), resource))
        if resource
        else document
    )
    if resolved.startswith(("../", "/")) or "\\" in resolved:
        raise ValueError(f"Unsafe EPUB resource path: {reference}")
    return resolved, unquote(parsed.fragment)


def epub_semantics(tag: Tag) -> set[str]:
    values = f"{tag.get('epub:type', '')} {tag.get('role', '')}"
    return set(values.split())


def _blocks(node: Tag):
    """Descend wrappers without flattening tables, code, lists, math, or figures."""
    atomic = {"p", "pre", "table", "figure", "ul", "ol", "dl", "blockquote", "math", "svg", "img"}
    for child in node.children:
        if isinstance(child, NavigableString):
            if child.strip():
                yield str(child).strip()
        elif isinstance(child, Tag):
            if child.name in atomic or re.fullmatch("h[1-6]", child.name or ""):
                yield child
            else:
                yield from _blocks(child)


def extract_epub(source: Path, work: Path, config: ExtractionConfig) -> Book:
    if config.start_page or config.end_page:
        raise ValueError("Physical page selection applies to PDFs, not reflowable EPUBs")
    warnings: list[str] = []
    with ZipFile(source) as archive:
        # Read ZIP members directly; never extract publisher-controlled paths to disk.
        def read(name: str) -> bytes:
            entry = archive.getinfo(name)
            if entry.file_size > 100_000_000:
                raise ValueError(f"EPUB member is unexpectedly large: {name}")
            return archive.read(name)

        container = ET.fromstring(read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile")
        if rootfile is None:
            raise ValueError("EPUB container has no package document")
        package, _ = epub_reference("", rootfile.attrib["full-path"])
        root = ET.fromstring(read(package))
        manifest = {}
        for item in root.findall(".//{*}manifest/{*}item"):
            resource, _ = epub_reference(package, item.attrib["href"])
            manifest[item.attrib["id"]] = (resource, item.attrib)
        spine = root.find("{*}spine")
        if spine is None:
            raise ValueError("EPUB package has no reading-order spine")
        documents = []
        for item in spine:
            if item.attrib.get("linear", "yes") == "no" and not config.include_nonlinear_epub:
                warnings.append(f"Nonlinear spine item excluded: {item.attrib.get('idref')}")
                continue
            resource, attrs = manifest[item.attrib["idref"]]
            if "nav" in attrs.get("properties", "").split():
                continue
            if attrs.get("media-type") not in {"application/xhtml+xml", "text/html"}:
                raise ValueError(f"Unsupported EPUB spine content type: {attrs.get('media-type')}")
            documents.append(resource)
        if not documents:
            raise ValueError("EPUB contains no readable spine documents")

        soups: dict[str, BeautifulSoup] = {}

        def soup_for(resource: str) -> BeautifulSoup:
            if resource not in soups:
                soups[resource] = BeautifulSoup(read(resource), "html.parser")
            return soups[resource]

        # Resolve notes even if their document is outside the linear spine. Inline first,
        # then remove the originals, so note placement never depends on ZIP member order.
        referenced_notes: set[tuple[str, str]] = set()
        for resource in documents:
            soup = soup_for(resource)
            for link in soup.find_all("a", href=True):
                try:
                    target_file, fragment = epub_reference(resource, link["href"])
                except ValueError:
                    continue
                if not fragment:
                    continue
                explicit_note = bool(epub_semantics(link) & {"noteref", "doc-noteref"})
                try:
                    target = soup_for(target_file).find(id=fragment)
                except (KeyError, ValueError):
                    if explicit_note:
                        warnings.append(f"Unresolved footnote: {resource} → {link['href']}")
                    continue
                if target is None:
                    if explicit_note:
                        warnings.append(f"Unresolved footnote: {resource} → {link['href']}")
                    continue
                note = target
                if not explicit_note and not epub_semantics(note) & {
                    "footnote",
                    "endnote",
                    "doc-footnote",
                    "doc-endnote",
                }:
                    continue
                replacement = soup.new_tag("span")
                replacement["data-audiobook-footnote"] = "true"
                replacement.string = f"[Footnote: {note.get_text(' ', strip=True)} End footnote.]"
                link.replace_with(replacement)
                referenced_notes.add((target_file, fragment))
        for resource, fragment in referenced_notes:
            original = soup_for(resource).find(id=fragment)
            if original:
                original.decompose()

        units = []
        for resource in documents:
            soup = soup_for(resource)
            for tag in soup.find_all(["script", "style", "head", "nav"]):
                if tag.name == "style" and tag.find_parent("svg"):
                    continue
                tag.decompose()
            for tag in soup.find_all(True):
                if tag.name in {"svg", "math"} or tag.find_parent(["svg", "math"]):
                    continue
                if tag.attrs is not None:
                    # Retain semantic markup and note content but shed verbose styling.
                    for attr in list(tag.attrs):
                        if attr not in {
                            "id",
                            "src",
                            "href",
                            "xlink:href",
                            "alt",
                            "title",
                            "epub:type",
                            "role",
                            "colspan",
                            "rowspan",
                            "display",
                            "data-audiobook-footnote",
                        }:
                            del tag[attr]
            for block_number, block in enumerate(_blocks(soup.body or soup), 1):
                images: list[Asset] = []
                heading = ""
                level = 0
                if isinstance(block, Tag):
                    heading = (
                        block.get_text(" ", strip=True)
                        if re.fullmatch("h[1-6]", block.name)
                        else ""
                    )
                    level = int(block.name[1]) if heading else 0
                    visuals = (
                        [block] if block.name in {"img", "svg"} else block.find_all(["img", "svg"])
                    )
                    for visual in visuals:
                        if visual.parent is None:
                            continue
                        # The surrounding SVG is rendered as a whole, not each embedded image.
                        if visual.find_parent("svg"):
                            continue
                        label = (
                            visual.get("alt")
                            or visual.get("title")
                            or f"{resource}, visual {len(images) + 1}"
                        )
                        try:
                            if visual.name == "svg":
                                image_data = embedded_svg(inline_svg_bytes(visual), resource, read)
                                is_svg = True
                            else:
                                src = visual.get("src", "")
                                if src.startswith("data:image/") and ";base64," in src:
                                    header, encoded = src.split(",", 1)
                                    image_data = base64.b64decode(encoded, validate=True)
                                    is_svg = "svg+xml" in header
                                else:
                                    image_file, _ = epub_reference(resource, src)
                                    image_data = read(image_file)
                                    is_svg = image_file.lower().endswith(".svg")
                                    if is_svg:
                                        image_data = embedded_svg(image_data, image_file, read)
                            asset = save_image(
                                image_data, label, work, config.image_max_dimension, is_svg
                            )
                        except Exception as exc:
                            raise ValueError(
                                f"Cannot load EPUB visual in {resource}: {label}: {exc}"
                            ) from exc
                        images.append(asset)
                        marker = soup.new_tag("span")
                        marker.string = f"[Attached visual {len(images)}: {label}]"
                        if visual is block:
                            block = marker
                        else:
                            visual.replace_with(marker)
                    content = str(block)
                    if not block.get_text(strip=True) and not images:
                        continue
                else:
                    content = block
                units.append(
                    SourceUnit(
                        id=f"e{len(units) + 1:06d}",
                        location=f"{resource}, block {block_number}",
                        text=content,
                        images=images,
                        heading=heading,
                        heading_level=level,
                    )
                )

        cover = None
        cover_id = next(
            (
                item.attrib.get("content")
                for item in root.findall(".//{*}meta")
                if item.attrib.get("name") == "cover"
            ),
            None,
        )
        for item_id, (resource, attrs) in manifest.items():
            if "cover-image" in attrs.get("properties", "").split() or item_id == cover_id:
                cover_bytes = read(resource)
                if resource.lower().endswith(".svg"):
                    cover_bytes = embedded_svg(cover_bytes, resource, read)
                cover = save_image(
                    cover_bytes,
                    "Book cover",
                    work,
                    config.image_max_dimension,
                    resource.lower().endswith(".svg"),
                )
                break
        title = root.find(".//{*}metadata/{*}title")
        authors = root.findall(".//{*}metadata/{*}creator")
        return Book(
            source_sha256=file_digest(source),
            format="epub",
            units=units,
            cover=cover,
            title=(title.text if title is not None else None) or source.stem,
            author=", ".join(a.text for a in authors if a.text) or "Unknown author",
            warnings=warnings,
        )


def extract_book(source: Path, work: Path, config: ExtractionConfig) -> Book:
    if source.suffix.lower() not in {".pdf", ".epub"}:
        raise ValueError("Input must be a PDF or EPUB file")
    key = digest([EXTRACT_VERSION, file_digest(source), config.model_dump()])
    cached = work / "source" / f"{key}.json"
    if cached.exists():
        book = Book.model_validate(read_json(cached))
        assets = [image for unit in book.units for image in unit.images]
        if book.cover:
            assets.append(book.cover)
        if all(
            asset_path(work, a.path).is_file() and file_digest(asset_path(work, a.path)) == a.sha256
            for a in assets
        ):
            write_json(work / "source.json", book)
            return book
    extractor = extract_pdf if source.suffix.lower() == ".pdf" else extract_epub
    book = extractor(source, work, config)
    if not book.units:
        raise ValueError("No source content was extracted")
    write_json(cached, book)
    write_json(work / "source.json", book)
    return book
