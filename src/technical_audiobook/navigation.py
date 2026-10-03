"""Final source-TOC reconciliation; never changes accepted narration checkpoints."""

import logging
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from bs4 import BeautifulSoup

from .config import Config
from .models import Book, Segment, TocEntry, Transcript
from .storage import read_json, write_json

log = logging.getLogger(__name__)


def planned_toc(transcript, config: Config) -> list[str]:
    from .audio import speech_plan

    return [chapter.title for chapter in speech_plan(transcript, config)]


def validate_source_destinations(book: Book, config: Config, work: Path):
    if not config.navigation.reconcile:
        return
    ids = {u.id for u in book.units}
    unresolved = [e for e in book.toc.entries if e.selected and e.source_id not in ids]
    if unresolved:
        target = work / "toc-report.json"
        write_json(
            target,
            {
                "source_sha256": book.source_sha256,
                "source_toc": book.toc.kind,
                "status": "needs_attention",
                "phase": "source_preflight",
                "errors": [
                    f"{e.title}: {e.reason or 'unresolved source destination'}" for e in unresolved
                ],
                "entries": [e.model_dump() for e in unresolved],
            },
        )
        raise ValueError(
            f"Source TOC has unresolved destinations; inspect {target}. No narration requests were made. Disable navigation.reconcile explicitly to proceed without alignment."
        )


def number_words(number: str) -> str:
    if "." in number:
        return " point ".join(number_words(part) for part in number.split("."))
    if not number.isdigit():
        # Roman chapter/part labels, but a single appendix letter is handled by the caller.
        values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
        total = last = 0
        for letter in reversed(number.upper()):
            value = values[letter]
            total += -value if value < last else value
            last = max(last, value)
        return number_words(str(total))
    small = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
    if len(number) > 1 and number.startswith("0"):
        return " ".join(small[int(digit)] for digit in number)
    value = int(number)
    if value < 20:
        return small[value]
    if value < 100:
        tens = "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split()
        return tens[value // 10] + ("-" + small[value % 10] if value % 10 else "")
    for size, name in [
        (10**12, "trillion"),
        (10**9, "billion"),
        (10**6, "million"),
        (1000, "thousand"),
        (100, "hundred"),
    ]:
        if value >= size:
            return (
                number_words(str(value // size))
                + " "
                + name
                + (" " + number_words(str(value % size)) if value % size else "")
            )
    raise ValueError("Unsupported heading number")


def split_title(title: str) -> tuple[str, str, str]:
    match = re.match(
        r"^(?:(chapter|section|part|appendix)\s+)?((?>\d+(?:\.\d+)*)|[IVXLCDM]+|[A-Z])(?:\s*[.:,)\-–—]\s*|\s+)(.+)$",
        title.strip(),
        re.IGNORECASE,
    )
    if not match:
        return "", "", title.strip()
    prefix, number, body = match.groups(default="")
    if (
        not number.isdigit()
        and "." not in number
        and not prefix
        and not re.match(r"^[IVXLCDM]+[.:)]\s+", title)
    ):
        return "", "", title.strip()  # Don't mistake "A Circuit" or "I/O" for a number.
    if not number.isdigit() and "." not in number and prefix.lower() != "appendix":
        if not re.fullmatch(
            r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})", number.upper()
        ):
            return "", "", title.strip()
    return prefix, number, body


def title_key(title: str) -> str:
    value = unicodedata.normalize("NFKC", split_title(title)[2]).casefold()
    value = value.replace("&", "and")
    return "".join(character for character in value if character.isalnum())


def _matching_headings(title: str, headings: dict[int, Segment]) -> list[int]:
    candidates = [i for i, s in headings.items() if title_key(s.display_title) == title_key(title)]
    if not candidates:
        candidates = [
            i
            for i, s in headings.items()
            if SequenceMatcher(None, title_key(s.display_title), title_key(title)).ratio() >= 0.9
        ]
    if len(candidates) > 1:
        number = split_title(title)[1]
        numbered = [
            i for i in candidates if number and split_title(headings[i].display_title)[1] == number
        ]
        if len(numbered) == 1:
            candidates = numbered
    return candidates


def _same_heading(left: str, right: str) -> bool:
    # Nearby-page recovery is stricter than ordinary same-destination matching.
    # Numberless bookmarks may match numbered headings, but explicit numbers
    # in either the source or narration must not contradict one another.
    left_number, right_number = split_title(left)[1], split_title(right)[1]
    return (
        bool(title_key(left))
        and title_key(left) == title_key(right)
        and (
            not left_number or not right_number or left_number.casefold() == right_number.casefold()
        )
    )


def _pdf_heading_evidence(text: str, title: str) -> list[str]:
    """Find whole heading lines (up to three wrapped lines), never prose substrings.

    Do not use SourceUnit.heading: it comes from the very bookmarks being checked.
    Overlapping matches such as a separate number line plus a title line are one
    occurrence; two separate occurrences remain ambiguous.
    """
    lines = [
        line.strip() for line in unicodedata.normalize("NFKC", text).splitlines() if line.strip()
    ]
    matches = []
    for start in range(len(lines)):
        if start and re.fullmatch(
            r"(?:(?:chapter|section|part|appendix)\s+)?(?:\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z])[.:)]?",
            lines[start - 1],
            re.IGNORECASE,
        ):
            # Do not discard a separate printed number to make a conflicting
            # numbered heading appear to be an unnumbered exact title match.
            continue
        for end in range(start + 1, min(start + 3, len(lines)) + 1):
            label = " ".join(lines[start:end])
            if _same_heading(label, title):
                matches.append((start, end, label))
    return [
        label
        for start, end, label in matches
        if not any(a <= start and b >= end and (a, b) != (start, end) for a, b, _ in matches)
    ]


def _adjacent_pdf_destinations(
    entries: list[TocEntry], book: Book, headings: dict[int, Segment], covered: set[str]
) -> dict[int, dict]:
    """Resolve only corroborated, one-physical-page bookmark errors for this final pass."""
    if book.format != "pdf" or book.toc.kind != "pdf_outline":
        return {}
    units = {unit.id: unit for unit in book.units}
    resolutions = {}
    for entry_index, entry in enumerate(entries):
        unit = units.get(entry.source_id)
        if unit is None or not re.fullmatch(r"p\d{5,}", unit.id):
            continue
        local = {i: s for i, s in headings.items() if unit.id in s.source_ids}
        if _matching_headings(entry.title, local) or _pdf_heading_evidence(unit.text, entry.title):
            continue  # A local match or real source heading takes precedence.
        page = int(unit.id[1:])
        adjacent = {f"p{p:05d}" for p in (page - 1, page + 1) if p > 0}
        candidates = []
        for index, heading in headings.items():
            if len(heading.source_ids) != 1 or not _same_heading(
                entry.title, heading.display_title
            ):
                continue
            sid = heading.source_ids[0]
            if sid not in adjacent or sid not in covered or sid not in units:
                continue
            for evidence in _pdf_heading_evidence(units[sid].text, heading.display_title):
                if _same_heading(entry.title, evidence):
                    candidates.append(
                        {
                            "segment": index,
                            "resolved_source_id": sid,
                            "source_heading_evidence": evidence,
                            "narration_heading": heading.display_title,
                        }
                    )
        if len(candidates) == 1:
            resolutions[entry_index] = {
                "matching": "source_verified_adjacent_pdf_page",
                **candidates[0],
            }
        elif candidates:
            resolutions[entry_index] = {
                "error": f"{entry.title}: ambiguous source-verified adjacent-page headings at {entry.source_id}",
                "adjacent_candidates": candidates,
            }
    return resolutions


def sentence(text: str) -> str:
    text = text.strip().rstrip(".").rstrip()
    return text if text.endswith(("!", "?")) else text + "."


def standard_heading(
    entry: TocEntry, book: Book, config: Config, original: Segment | None = None
) -> Segment:
    prefix, number, body = split_title(entry.title)
    display = entry.title
    spoken_body = body.replace("(", ", ").replace(")", "").strip(" ,")
    if original:
        _, old_number, candidate = split_title(original.text)
        if not old_number and number:
            spoken_number = (
                number
                if prefix.lower() == "appendix" and number.isalpha()
                else number_words(number)
            )
            candidate = re.sub(
                r"^(?:(?:chapter|section|part|appendix)\s+)?"
                + re.escape(spoken_number).replace(r"\-", "[- ]")
                + r"[.:,]\s*",
                "",
                candidate,
                flags=re.IGNORECASE,
            )
        if SequenceMatcher(None, title_key(candidate), title_key(body)).ratio() >= 0.72:
            spoken_body = candidate
    if number:
        keep = config.navigation.chapter_prefix == "keep" or (
            config.navigation.chapter_prefix == "auto" and book.format == "epub"
        )
        display_prefix = prefix + " " if prefix and (prefix.lower() != "chapter" or keep) else ""
        display = f"{display_prefix}{number}. {body}"
        spoken_prefix = (
            prefix.capitalize()
            if prefix
            else ("Chapter" if entry.level == 1 and "." not in number else "Section")
        )
        spoken_number = (
            number if prefix.lower() == "appendix" and number.isalpha() else number_words(number)
        )
        spoken = f"{spoken_prefix} {spoken_number}. {sentence(spoken_body)}"
    else:
        spoken = sentence(spoken_body)
    return Segment(
        kind="heading",
        text=spoken,
        display_title=display,
        heading_level=entry.level,
        source_ids=original.source_ids if original else [entry.source_id],
    )


def reconcile_toc(
    transcript: Transcript,
    book: Book,
    config: Config,
    work: Path,
    omitted_front_ids: set[str] | None = None,
) -> Transcript:
    result = transcript.model_copy(deep=True)
    report = {
        "source_sha256": book.source_sha256,
        "source_toc": book.toc.kind,
        "status": "aligned",
        "entries": [],
        "demoted_headings": [],
        "errors": [],
        "warnings": list(book.toc.warnings),
        "excluded_source_entries": [e.model_dump() for e in book.toc.entries if not e.selected],
    }
    target = work / "toc-report.json"
    if not config.navigation.reconcile:
        report["status"] = "disabled"
        report["m4b_toc"] = planned_toc(result, config)
        write_json(target, report)
        return result
    if not book.toc.entries:
        report["status"] = "no_source_toc"
        report["warnings"].append(
            "No machine-readable source TOC; preserving generated headings, with standardized numbering."
        )
        result.segments = [
            standard_heading(
                TocEntry(
                    title=s.display_title,
                    level=s.heading_level,
                    target="generated",
                    source_id=s.source_ids[0],
                ),
                book,
                config,
                s,
            )
            if s.kind == "heading"
            else s
            for s in result.segments
        ]
        log.warning(report["warnings"][-1])
        report["m4b_toc"] = planned_toc(result, config)
        write_json(target, report)
        return result

    omitted_front_ids = omitted_front_ids or set()
    if omitted_front_ids:
        # Never cut a segment spanning both excluded front matter and useful content.
        mixed = [
            s
            for s in result.segments
            if set(s.source_ids) & omitted_front_ids and not set(s.source_ids) <= omitted_front_ids
        ]
        if mixed:
            report["warnings"].append(
                "Front matter shares a segment with retained content; omission was not applied."
            )
            omitted_front_ids = set()
        else:
            result.segments = [
                s for s in result.segments if not set(s.source_ids) <= omitted_front_ids
            ]
            for item in result.coverage:
                if item.source_id in omitted_front_ids:
                    item.disposition, item.reason = (
                        "omitted",
                        "Final source-backed review: noninstructional front matter",
                    )
    report["omitted_front_source_ids"] = sorted(omitted_front_ids)
    positions = {u.id: i for i, u in enumerate(book.units)}
    covered = {item.source_id for item in result.coverage if item.disposition == "narrated"}
    omitted = {
        item.source_id: item.reason for item in result.coverage if item.disposition == "omitted"
    }
    headings = {i: s for i, s in enumerate(result.segments) if s.kind == "heading"}
    source_entries = [entry for entry in book.toc.entries if entry.selected]
    entries = [entry.model_copy() for entry in source_entries]
    resolutions = _adjacent_pdf_destinations(entries, book, headings, covered)
    for index, resolution in resolutions.items():
        if "resolved_source_id" in resolution:
            entries[index].source_id = resolution["resolved_source_id"]
    # Corrections change final navigation scopes only. Source units, bookmarks,
    # chapter jobs and accepted narration/cache identities remain untouched.
    if resolutions:
        destinations = [positions[e.source_id] for e in entries if e.source_id in positions]
        if destinations != sorted(destinations):
            report["errors"].append("Adjacent-page corrections conflict with source TOC order")
    counts = Counter(entry.source_id for entry in entries)
    replacements, insertions = {}, {}
    matched_order = []
    for entry_index, entry in enumerate(entries):
        record = {**source_entries[entry_index].model_dump(), "action": "unresolved"}
        resolution = resolutions.get(entry_index, {})
        record.update(resolution)
        report["entries"].append(record)
        if "error" in resolution:
            report["errors"].append(resolution["error"])
            continue
        if entry.source_id not in positions:
            report["errors"].append(
                f"{entry.title}: {entry.reason or 'unresolved source destination'}"
            )
            continue
        start = positions[entry.source_id]
        if entry.source_id in omitted and re.fullmatch(
            r"(problems|exercises|review questions|references|bibliography|index|table of contents|contents|copyright(?: page)?)",
            split_title(entry.title)[2],
            re.IGNORECASE,
        ):
            record.update(action="omitted_source_content", reason=omitted[entry.source_id])
            continue
        end = next(
            (
                positions[e.source_id]
                for e in entries[entry_index + 1 :]
                if e.source_id in positions
                and e.level <= entry.level
                and positions[e.source_id] > start
            ),
            len(book.units),
        )
        scope = {u.id for u in book.units[start:end]}
        if not scope & covered:
            record["action"] = "omitted_source_content"
            continue
        if entry.level > 6:
            report["errors"].append(
                f"{entry.title}: source nesting exceeds the supported six heading levels"
            )
            continue
        if entry.source_id in omitted_front_ids:
            record["action"] = "omitted_front_matter"
            continue
        local = [
            i
            for i, s in headings.items()
            if entry.source_id in s.source_ids and i not in replacements
        ]
        candidates = _matching_headings(entry.title, {i: headings[i] for i in local})
        if not candidates and len(local) == 1 and counts[entry.source_id] == 1:
            candidates = local
            record["matching"] = "unique_source_destination"
        if len(candidates) == 1:
            index = candidates[0]
            replacements[index] = standard_heading(entry, book, config, headings[index])
            record.update(
                action="matched", segment=index, display_title=replacements[index].display_title
            )
            matched_order.append(index)
            continue
        # A standalone EPUB heading, or an explicit heading at a PDF page's start,
        # can be restored even if narration omitted the heading itself. Never guess
        # a position inside a narrated paragraph or a multi-heading PDF page.
        unit = book.units[start]
        plain = (
            BeautifulSoup(unit.text, "html.parser").get_text(" ", strip=True)
            if book.format == "epub"
            else unit.text
        )
        starts_here = title_key(unit.heading) == title_key(entry.title) and (
            book.format == "epub" or title_key(plain).startswith(title_key(entry.title))
        )
        following = [i for i, s in enumerate(result.segments) if set(s.source_ids) & scope]
        index = min(following) if following else None
        if (
            not candidates
            and counts[entry.source_id] == 1
            and starts_here
            and index is not None
            and all(positions[sid] >= start for sid in result.segments[index].source_ids)
        ):
            insertions.setdefault(index, []).append(standard_heading(entry, book, config))
            record.update(
                action="inserted", segment=index, display_title=insertions[index][-1].display_title
            )
            matched_order.append(index)
            for item in result.coverage:
                if item.source_id == entry.source_id:
                    item.disposition, item.reason = "narrated", ""
        else:
            report["errors"].append(
                f"{entry.title}: {'ambiguous headings' if candidates else 'no safely located heading'} at {entry.source_id}"
            )
    if matched_order != sorted(matched_order):
        report["errors"].append("Matched narration headings are not in source TOC order")
    if report["errors"]:
        report["status"] = "needs_attention"
        write_json(target, report)
        raise ValueError(
            f"Source TOC reconciliation needs attention; inspect {target}. No final narration was overwritten. First issue: {report['errors'][0]}"
        )
    segments = []
    for index, segment in enumerate(result.segments):
        segments.extend(insertions.get(index, []))
        if index in replacements:
            segments.append(replacements[index])
        elif segment.kind == "heading":
            report["demoted_headings"].append(
                {"display_title": segment.display_title, "source_ids": segment.source_ids}
            )
            segments.append(
                Segment(kind="paragraph", text=segment.text, source_ids=segment.source_ids)
            )
        else:
            segments.append(segment)
    result.segments = segments
    # Use the actual speech planner, including any retained opening material before
    # the first navigable heading and the configured TOC depth.
    report["m4b_toc"] = planned_toc(result, config)
    navigation_count = sum(
        s.kind == "heading" and s.heading_level <= config.audio.toc_depth for s in segments
    )
    if len(report["m4b_toc"]) > navigation_count:
        report["status"] = "aligned_with_opening_material"
        report["warnings"].append(
            "Retained narration before the first included TOC heading requires an opening M4B entry. "
            "It was not silently discarded or reassigned to a later source heading."
        )
    write_json(target, report)
    log.info(
        "Aligned %s navigation headings with %s; demoted %s body-only headings",
        len(replacements) + sum(map(len, insertions.values())),
        book.toc.kind,
        len(report["demoted_headings"]),
    )
    return result


def validate_published_navigation(transcript: Transcript, script, config: Config, work: Path):
    """Manual prose edits are allowed; conflicting navigation is never silently replaced."""
    if not config.navigation.reconcile:
        return

    def headings(segments):
        return [(s.heading_level, s.display_title, s.text) for s in segments if s.kind == "heading"]

    expected = planned_toc(transcript, config)
    actual = planned_toc(script, config)
    if headings(transcript.segments) != headings(script.segments) or expected != actual:
        path = work / "toc-report.json"
        report = read_json(path)
        report["status"] = "edited_text_conflict"
        report["edited_text_m4b_toc"] = actual
        report["errors"].append(
            "Edited text changes the reconciled heading structure, spoken headings or opening navigation entry."
        )
        write_json(path, report)
        raise ValueError(
            "Edited narration.txt conflicts with source-TOC reconciliation. Your edits were preserved. "
            "Restore the generated headings/opening structure, or use synthesize narration.txt "
            "to render your deliberate changes without reconciliation. See toc-report.json."
        )
