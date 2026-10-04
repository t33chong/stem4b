"""Offline, guided navigation repair. Never edits the book or accepted checkpoints."""

import shutil
import sys
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

from filelock import FileLock, Timeout

from .config import Config
from .models import Book, Transcript
from .narration_text import load_script, save_narration
from .navigation import reconcile_toc, validate_published_navigation, validate_source_destinations
from .storage import digest, read_json
from .toc_diagnostics import write_toc_report
from .toc_overrides import TocCorrection, load_corrections, save_corrections


def _context(work):
    if not (work / "source.json").exists() or not (work / "toc-input.json").exists():
        raise ValueError(
            "No offline TOC input is available. Rerun your original convert command with "
            "--until narrate once to create it; keep the workspace so accepted sections can be reused."
        )
    book = Book.model_validate(read_json(work / "source.json"))
    saved = read_json(work / "toc-input.json")
    if (
        saved.get("version") != 1
        or saved.get("source_sha256") != book.source_sha256
        or saved.get("source_toc_sha256") != digest(book.toc)
    ):
        raise ValueError(
            "Offline TOC input is stale; rerun convert --until narrate for this source."
        )
    config = Config.model_validate(saved["config"])
    if not config.navigation.reconcile:
        raise ValueError(
            "Enable navigation.reconcile and rerun convert --until narrate before repair."
        )
    transcript = Transcript.model_validate(saved["transcript"]) if saved.get("transcript") else None
    if transcript and transcript.source_sha256 != book.source_sha256:
        raise ValueError(
            "Offline narration belongs to a different source; rerun convert --until narrate."
        )
    return book, config, transcript, set(saved.get("omitted_front_source_ids", []))


def _validate(work, book, config, transcript, omitted):
    try:
        # A final-pass snapshot already includes accepted narration. Do not replace
        # it with a preflight-only snapshot if an unresolved destination remains.
        if transcript:
            return reconcile_toc(transcript, book, config, work, omitted), None
        validate_source_destinations(book, config, work)
    except ValueError as exc:
        return None, str(exc)
    write_toc_report(
        {
            "source_sha256": book.source_sha256,
            "source_toc": book.toc.kind,
            "phase": "source_preflight",
            "status": "source_destinations_resolved",
            "errors": [],
            "entries": [],
            "warnings": [
                "Source destinations are resolved, but narration has not completed. "
                "Rerun the original convert command; this may make paid model requests."
            ],
        },
        work,
        book,
    )
    return None, None


def _problem_entries(report):
    result = set()
    for issue in report.get("diagnostics", []):
        result.update(e["entry_number"] for e in issue["entries"])
        for pair in issue.get("conflicts", []):
            result.update(e["entry_number"] for e in pair.values())
    return sorted(result)


def _preview(book, sid, work):
    unit = next((u for u in book.units if u.id == sid), None)
    if unit:
        print(f"\nSource {unit.id}: {unit.location}\n{unit.text[:1200] or '(no extracted text)'}")
        for asset in unit.images[:1]:
            print(f"Source image for inspection: {work / asset.path}")
    else:
        print("Source destination is unresolved.")


def _destination(book, ask, allowed=None):
    ids = {u.id for u in book.units}
    while True:
        value = ask(
            "Source unit ID / physical PDF page number; /text searches source; blank cancels: "
        ).strip()
        if not value:
            return None
        if value.startswith("/"):
            query = value[1:].casefold()
            matches = [
                u
                for u in book.units
                if (allowed is None or u.id in allowed)
                and query in (u.heading + " " + u.location + " " + u.text).casefold()
            ]
            for unit in matches[:20]:
                print(
                    f"  {unit.id} — {unit.location}: {unit.heading or ' '.join(unit.text.split())[:100]}"
                )
            print(
                f"{len(matches)} source match(es). Enter an ID above; refine /text if more than 20 match."
            )
            continue
        sid = f"p{int(value):05d}" if book.format == "pdf" and value.isdigit() else value
        if sid in ids and (allowed is None or sid in allowed):
            return sid
        print(
            "Choose an extracted source unit"
            + (f" cited by this heading: {', '.join(allowed)}" if allowed else ".")
        )


def _choose(entry_number, book, transcript, work, ask):
    entry = book.toc.entries[entry_number - 1]
    headings = [s for s in transcript.segments if s.kind == "heading"] if transcript else []
    positions = {u.id: i for i, u in enumerate(book.units)}
    origin = positions.get(entry.source_id, 0)
    headings.sort(
        key=lambda s: (
            min(abs(positions.get(sid, origin) - origin) for sid in s.source_ids),
            -SequenceMatcher(None, entry.title.casefold(), s.display_title.casefold()).ratio(),
        )
    )
    print(f"\nSource TOC entry {entry_number}: {entry.title} (level {entry.level})")
    _preview(book, entry.source_id, work)
    matches, offset = headings, 0
    while True:
        visible = matches[offset : offset + 8]
        print("\nAccepted headings (selecting one uses its title and number):")
        for i, heading in enumerate(visible, 1):
            print(f"  {i}. {heading.display_title} [{', '.join(heading.source_ids)}]")
        if not visible:
            print("  None available. Correct the destination or omit this navigation entry.")
        answer = ask(
            "Number = select; /text = search; n = next; d = destination; "
            "o = omit TOC entry (keep speech); u = undo this entry's correction; q = finish: "
        ).strip()
        if answer.casefold() in {"q", ""}:
            return "quit"
        if answer.casefold() == "u":
            return "undo"
        if answer.startswith("/"):
            matches = [s for s in headings if answer[1:].casefold() in s.display_title.casefold()]
            offset = 0
            continue
        if answer.casefold() == "n":
            offset = offset + 8 if offset + 8 < len(matches) else 0
            continue
        if answer.casefold() == "o":
            if ask("Remove only this TOC entry, preserving ALL speech? [y/N]: ").lower() == "y":
                return TocCorrection(entry=entry_number, action="omit")
            continue
        if answer.casefold() == "d":
            sid = _destination(book, ask)
            if sid:
                _preview(book, sid, work)
                if ask(f"Use {sid} as this bookmark's destination? [y/N]: ").lower() == "y":
                    return TocCorrection(entry=entry_number, action="destination", source_id=sid)
            continue
        if not answer.isdigit() or not 1 <= int(answer) <= len(visible):
            print("Choose one of the displayed options.")
            continue
        heading = visible[int(answer) - 1]
        sid = (
            heading.source_ids[0]
            if len(heading.source_ids) == 1
            else _destination(book, ask, heading.source_ids)
        )
        if not sid:
            continue
        _preview(book, sid, work)
        level = entry.level if entry.level <= 6 else heading.heading_level
        print(f"Proposed navigation: {heading.display_title}; source {sid}; level {level}.")
        print(f"Accepted spoken heading: {heading.text}")
        if (
            ask("Use this heading? Number pronunciation will be standardized. [y/N]: ").lower()
            == "y"
        ):
            return TocCorrection(
                entry=entry_number,
                action="match",
                source_id=sid,
                heading_id=digest(heading),
                level=level,
            )


def _publish(result, work, config, backup_edits):
    backup = None
    paths = [work / "narration.txt", work / "narration.json"]
    if backup_edits and any(path.exists() for path in paths):
        backup = Path(tempfile.mkdtemp(prefix="toc-repair-backup-", dir=work))
        # Copy both first, then move the editable text aside under the workspace lock.
        # Validation and preview generation have already succeeded at this point.
        for path in paths:
            if path.exists():
                shutil.copy2(path, backup / path.name)
        if paths[0].exists():
            paths[0].replace(backup / paths[0].name)
        print(f"Existing narration backed up to {backup}")
    try:
        save_narration(result, work)
        validate_published_navigation(result, load_script(paths[0]), config, work)
    except BaseException:
        if backup:
            for path in paths:
                if (backup / path.name).exists():
                    shutil.copy2(backup / path.name, path)
        raise


def repair_toc(
    work: Path, *, check=False, publish=False, reset=False, backup_edits=False, ask=None
) -> Path:
    work = work.resolve()
    if backup_edits and not publish:
        raise ValueError(
            "--backup-edits requires --publish; it explicitly replaces text edits with regenerated narration."
        )
    if not work.is_dir():
        raise ValueError(f"Workspace not found: {work}")
    if not check and not publish and ask is None and not sys.stdin.isatty():
        raise ValueError(
            "Guided TOC repair needs an interactive terminal. Use --check for offline validation or --publish to publish already-resolved narration."
        )
    ask = ask or input
    try:
        with FileLock(work / ".pipeline.lock", timeout=0):
            if reset:
                save_corrections(work, Book.model_validate(read_json(work / "source.json")), {})
                print(
                    "Cleared corrections; the previous file is backed up in toc-override-history/."
                )
            book, config, transcript, omitted = _context(work)
            choices = load_corrections(work, book)
            print(
                "Offline TOC repair: no provider calls; no PDF/EPUB or accepted checkpoint edits."
            )
            while True:
                result, error = _validate(work, book, config, transcript, omitted)
                if not error:
                    break
                if check or publish:
                    raise ValueError(error)
                report = read_json(work / "toc-report.json")
                problems = _problem_entries(report)
                print(f"\n{error}\n")
                if not problems:
                    raise ValueError(error)
                for number in problems:
                    print(f"  {number}. {book.toc.entries[number - 1].title}")
                value = ask(f"TOC entry to review [{problems[0]}], or q to finish: ").strip()
                if value.lower() == "q":
                    raise ValueError(
                        "Unresolved TOC issues remain. Saved choices are retained; run repair-toc again to continue."
                    )
                if not value:
                    number = problems[0]
                elif value.isdigit() and 1 <= int(value) <= len(book.toc.entries):
                    number = int(value)
                else:
                    print("Enter a source TOC entry number.")
                    continue
                choice = _choose(number, book, transcript, work, ask)
                if choice == "quit":
                    raise ValueError(
                        "Unresolved TOC issues remain. Saved choices are retained; run repair-toc again to continue."
                    )
                if choice == "undo":
                    choices.pop(number - 1, None)
                else:
                    choices[number - 1] = choice
                save_corrections(work, book, choices)
                print(
                    f"Saved {work / 'toc-overrides.json'}. Rechecking all navigation constraints."
                )
            if result is None:
                print(
                    "Destinations resolved. Rerun the original convert command to narrate; this may make paid requests."
                )
                return work / "toc-report.md"
            if reset:
                print("Automatic reconciliation passed after clearing local corrections.")
            print("TOC validation passed. See toc-report.md for the audit.")
            save_narration(result, work, stem="narration.toc-preview")
            print(
                f"Repaired preview: {work / 'narration.toc-preview.txt'} (your current narration.txt is unchanged)."
            )
            if publish or (
                not check
                and ask(
                    "Publish repaired narration.txt now? Manual edits remain protected. [y/N]: "
                ).lower()
                == "y"
            ):
                _publish(result, work, config, backup_edits)
                print(
                    "Published narration.txt from accepted narration. Review it before synthesizing speech."
                )
                return work / "narration.txt"
            print(
                "No final narration was changed. Use repair-toc --publish or rerun the original convert command to publish."
            )
            return work / "toc-report.md"
    except EOFError as exc:
        raise ValueError(
            "Input ended. Confirmed corrections are saved; run repair-toc again to continue."
        ) from exc
    except Timeout as exc:
        raise ValueError(f"Another process is using this workspace: {work}") from exc
