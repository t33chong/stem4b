"""Actionable TOC diagnostics, also renderable from an older saved JSON report."""

import html
import re
from itertools import pairwise
from pathlib import Path
from shlex import join as shell_join

from .models import Book
from .storage import atomic_text, write_json


def _location(source_id, book: Book | None) -> str:
    if not source_id:
        return "unresolved destination"
    if re.fullmatch(r"p\d+", source_id):
        return f"{source_id} (physical PDF page {int(source_id[1:])})"
    if book:
        unit = next((u for u in book.units if u.id == source_id), None)
        if unit:
            return f"{source_id} ({unit.location})"
    return source_id


def _entry(index: int, record: dict, book: Book | None) -> dict:
    effective = record.get("resolved_source_id") or record.get("source_id")
    return {
        "entry_number": record.get("source_toc_entry", index + 1),
        "title": record["title"],
        "target": record.get("target", ""),
        "original_location": _location(record.get("source_id"), book),
        "effective_location": _location(effective, book),
        "action": record.get("action", "unresolved"),
        "narration_heading": record.get("narration_heading"),
        "segment": record.get("segment"),
        "narration_candidates": record.get("narration_candidates", []),
        "adjacent_candidates": record.get("adjacent_candidates", []),
    }


def _order_conflicts(report: dict, book: Book | None, narration: bool) -> list[dict]:
    positions = {u.id: i for i, u in enumerate(book.units)} if book else {}
    ordered = []
    for index, record in enumerate(report.get("entries", [])):
        if record.get("action") in {
            "omitted_exercises",
            "omitted_reference_list",
            "omitted_user_navigation",
        }:
            continue
        if narration:
            position = record.get("segment")
            if record.get("action") not in {"matched", "inserted"}:
                continue
        else:
            sid = record.get("resolved_source_id") or record.get("source_id")
            position = positions.get(sid)
            if position is None and not book and sid and re.fullmatch(r"p\d+", sid):
                position = int(sid[1:])
        if position is not None:
            ordered.append((position, _entry(index, record, book)))
    return [
        {"previous": previous, "following": following}
        for (left, previous), (right, following) in pairwise(ordered)
        if left > right
    ]


def _explanation(error: str, phase: str) -> tuple[str, str, str]:
    if phase == "source_preflight":
        return (
            "unresolved_destination",
            "The source TOC links to content that could not be located in the extracted source.",
            "Check the bookmark page or EPUB file/fragment target below against the source. "
            "This is a source-navigation problem, not an LLM response failure.",
        )
    if "corrections conflict with source TOC order" in error:
        return (
            "source_order_conflict",
            "After proposed destination corrections, consecutive source TOC entries point "
            "backwards through the document. Reconciliation will not guess which entry to move.",
            "Compare each pair below with the PDF bookmarks and the headings on those physical "
            "pages. An unresolved following entry still uses its original bookmark destination; "
            "this does not prove that the accepted narration is out of order.",
        )
    if "not in source TOC order" in error:
        return (
            "narration_order_conflict",
            "Matched headings occur in a different order in the accepted narration and source TOC.",
            "Compare the pairs below with the source. Segment indices are zero-based positions "
            "in the assembled accepted narration, not page or section numbers.",
        )
    if "no safely located heading" in error:
        return (
            "missing_heading",
            "No accepted heading could be safely matched or inserted at this TOC destination.",
            "Compare the bookmark, printed heading and accepted headings at the location below. "
            "A missing heading does not necessarily mean missing narration. Exercises or references "
            "may have been intentionally omitted on a page containing retained content; do not add "
            "unwanted material just to satisfy the TOC check.",
        )
    if "saved correction" in error:
        return (
            "stale_user_correction",
            "A saved choice no longer identifies an available, unused accepted heading.",
            "Run repair-toc to choose the current heading or undo this entry's correction. "
            "Corrections never silently attach to changed narration.",
        )
    if "ambiguous" in error:
        return (
            "ambiguous_heading",
            "More than one plausible heading matches this TOC entry.",
            "Compare the candidate headings and their source locations below with the printed "
            "heading. Repeated titles must be distinguished by their location and numbering.",
        )
    if "number" in error:
        return (
            "heading_label_conflict",
            "The TOC title and accepted heading do not agree enough to preserve numbering safely.",
            "Compare both titles and numbers below with the printed chapter/section heading. "
            "PDF bookmarks can have stale or abbreviated titles; the accepted heading is not "
            "necessarily wrong. No numbers have been guessed or discarded.",
        )
    if "six heading levels" in error:
        return (
            "unsupported_depth",
            "The source TOC exceeds the six heading levels supported by the narration format.",
            "This needs a hierarchy-handling change or an explicit alignment bypass. Changing "
            "audio.toc_depth only limits M4B entries; it does not resolve this validation failure.",
        )
    if "Edited text" in error:
        return (
            "edited_text_conflict",
            "Your editable script's navigation differs from the reconciled generated narration.",
            "Keep a backup of your edits. Restore the generated headings, spoken heading text and "
            "opening structure, or deliberately synthesize your edited script without reconciliation.",
        )
    return (
        "unresolved_destination",
        "The source TOC entry could not be safely aligned with the extracted source and narration.",
        "Compare the source target and heading below. Keep the workspace and share this report "
        "with the relevant source pages if the source is correct.",
    )


def explain_report(report: dict, book: Book | None = None) -> dict:
    """Add explanations without changing matching decisions or mutating the input report."""
    result = dict(report)
    phase = report.get("phase", "final_reconciliation")
    result["phase"] = phase
    diagnostics = []
    for error in report.get("errors", []):
        code, meaning, check = _explanation(error, phase)
        entries = []
        for index, record in enumerate(report.get("entries", [])):
            # The fallback also supports reports written before per-entry errors existed.
            matches = record.get("error") == error
            if not record.get("error") and error.startswith(record["title"] + ":"):
                matches = " at " not in error or error.endswith(
                    " at " + (record.get("resolved_source_id") or record.get("source_id") or "")
                )
            if matches:
                entries.append(_entry(index, record, book))
        item = {
            "code": code,
            "error": error,
            "meaning": meaning,
            "check": check,
            "entries": entries,
        }
        if code in {"source_order_conflict", "narration_order_conflict"}:
            item["conflicts"] = _order_conflicts(report, book, code == "narration_order_conflict")
        diagnostics.append(item)
    result["diagnostics"] = diagnostics
    result["recovery_steps"] = []
    if not diagnostics:
        return result
    if report.get("status") == "edited_text_conflict":
        result["preserved_output"] = "Your edits were preserved."
        result["recovery_steps"] = [
            "Back up narration.txt, then restore its generated headings, spoken headings and "
            "opening structure while retaining your prose edits, and rerun the same convert command.",
            "If the changes are deliberate, use stem4b synthesize PATH/TO/narration.txt "
            "-c YOUR_CONFIG.toml -o YOUR_OUTPUT.m4b. This uses your text without source-TOC alignment.",
        ]
        return result
    if phase == "source_preflight":
        result["preserved_output"] = "No narration requests were made."
        resume = "Narration will then proceed normally and may make new paid model requests."
    else:
        result["preserved_output"] = (
            "No final narration was overwritten. Accepted section checkpoints remain available."
        )
        resume = (
            "Accepted sections are reused when the source, workspace and narration/model settings "
            "are unchanged. Existing narration.txt, if any, may be an older export; review the "
            "newly published text before synthesizing speech."
        )
    result["recovery_steps"] = [
        "Inspect the issues below. PDF page numbers are physical pages, not printed page labels. "
        "Keep the workspace and accepted.json checkpoints. toc-report.json is a diagnostic, not "
        "an override file; editing it will not fix reconciliation.",
        "To resolve individual entries while keeping alignment enabled, run the repair-toc command "
        "shown below. It presents source excerpts and accepted headings; choose a heading, correct "
        "a destination or omit only the navigation entry (never its speech). Choices are saved in "
        "toc-overrides.json and validated offline. No PDF/EPUB editing or provider calls are needed. "
        "Editing narration.txt will not repair this pass, which reads accepted checkpoints. "
        "Rerunning unchanged will repeat the failure.",
        "To explicitly proceed without alignment, set reconcile = false in the [navigation] "
        "table of the same TOML file supplied with -c (add the table only if absent). Rerun the "
        "same convert command with --until narrate first, keeping the same source and workspace. "
        + resume,
        "Bypassing reconciliation skips source-TOC matching, final heading standardization and "
        "the source-backed front-matter check. It does not repair bookmarks. Review the resulting "
        "narration.txt headings before TTS. Do not delete caches, force regeneration or change "
        "LLM settings to fix this deterministic navigation check. Replacing or editing the source "
        "file changes its identity and may require re-narration.",
    ]
    return result


def _md(value) -> str:
    return re.sub(
        r"([\\`*_{}\[\]()#+.!|>-])",
        r"\\\1",
        html.escape(str(value), quote=False).replace("\n", " "),
    )


def _describe(entry: dict) -> str:
    text = f"Report entry {entry['entry_number']}: {entry['title']} — {entry['original_location']}"
    if entry["effective_location"] != entry["original_location"]:
        text += f" → {entry['effective_location']}"
    text += f"; {entry['action']}"
    if entry["segment"] is not None:
        text += f"; narration segment {entry['segment']}"
    return text


def _candidate(candidate: dict) -> str:
    title = candidate.get("title") or candidate.get("narration_heading", "")
    ids = candidate.get("source_ids") or [candidate.get("resolved_source_id")]
    locations = ", ".join(_location(sid, None) for sid in ids)
    return f"{title} — {locations}; narration segment {candidate.get('segment')}"


def render_report(report: dict) -> str:
    """Render an explained report; no source files or model requests are needed."""
    lines = ["# Source TOC report", "", f"Status: {_md(report['status'])}", ""]
    if report.get("diagnostics"):
        lines += [
            report["preserved_output"],
            "",
            f"{len(report['diagnostics'])} issue(s) need attention. No automatic bypass was applied.",
            "",
            "## What to do next",
            "",
        ]
        lines += [f"{i}. {step}" for i, step in enumerate(report["recovery_steps"], 1)]
        if report.get("repair_command") and report["status"] != "edited_text_conflict":
            lines += [
                "",
                "Start the guided offline repair:",
                "",
                "```sh",
                report["repair_command"],
                "```",
            ]
        if report["status"] != "edited_text_conflict":
            lines += [
                "",
                "Optional bypass configuration (edit the existing table if present):",
                "",
                "```toml",
                "[navigation]",
                "reconcile = false",
                "```",
            ]
        for i, item in enumerate(report["diagnostics"], 1):
            lines += [
                "",
                f"## Issue {i}: {_md(item['code'])}",
                "",
                _md(item["error"]),
                "",
                item["meaning"],
                "",
                item["check"],
                "",
            ]
            if item.get("conflicts"):
                lines += ["Earlier TOC entry | Following TOC entry", "--- | ---"]
                for pair in item["conflicts"]:
                    lines.append(
                        f"{_md(_describe(pair['previous']))} | {_md(_describe(pair['following']))}"
                    )
                lines.append("")
            for entry in item["entries"]:
                lines += [
                    f"- {_md(_describe(entry))}",
                    f"  - Source target: {_md(entry['target'])}",
                ]
                if entry["narration_heading"]:
                    lines.append(f"  - Accepted heading: {_md(entry['narration_heading'])}")
                for candidate in entry["narration_candidates"]:
                    lines.append(f"  - Local accepted candidate: {_md(_candidate(candidate))}")
                for candidate in entry["adjacent_candidates"]:
                    lines.append(f"  - Adjacent-page candidate: {_md(_candidate(candidate))}")
    else:
        lines += ["No reconciliation errors were reported.", ""]
        if report["status"] == "disabled":
            lines += [
                "Source-TOC alignment was explicitly disabled; headings were not verified.",
                "",
            ]
    corrected = [
        e
        for e in report.get("entries", [])
        if e.get("label_matching") == "source_verified_printed_heading"
    ]
    omitted = [e for e in report.get("entries", []) if e.get("action") == "omitted_exercises"]
    manual = [e for e in report.get("entries", []) if e.get("user_correction")]
    if corrected or omitted or manual:
        lines += ["", "## Navigation corrections", ""]
        if omitted:
            lines += [
                f"- {len(omitted)} exercise-navigation entries excluded by policy; no speech removed."
            ]
        for entry in corrected:
            lines += [
                f"- Printed title verified: {_md(entry['title'])} → {_md(entry['resolved_title'])} "
                f"at {_md(entry.get('resolved_source_id') or entry['source_id'])}. "
                f"Evidence: {_md(entry['source_heading_evidence'])}"
            ]
        if manual:
            lines += [
                f"- {len(manual)} explicit user correction(s) applied; see user_correction in the JSON audit."
            ]
    if report.get("warnings"):
        lines += ["", "## Warnings", ""]
        lines += [f"- {_md(warning)}" for warning in report["warnings"]]
    lines += ["", "See toc-report.json for the complete machine-readable audit.", ""]
    return "\n".join(lines)


def write_toc_report(report: dict, work: Path, book: Book | None = None) -> dict:
    explained = explain_report(report, book)
    explained["instructions_file"] = str(work / "toc-report.md")
    explained["repair_command"] = shell_join(["stem4b", "repair-toc", str(work)])
    atomic_text(work / "toc-report.md", render_report(explained))
    write_json(work / "toc-report.json", explained)
    return explained


def failure_details(report: dict, work: Path) -> str:
    first = report["diagnostics"][0]
    detail = first["error"]
    if first.get("conflicts"):
        pair = first["conflicts"][0]
        detail += f" ({_describe(pair['previous'])}; followed by {_describe(pair['following'])})"
    next_step = (
        "Restore generated headings/opening structure, or explicitly synthesize your edited script."
        if report["status"] == "edited_text_conflict"
        else f"Resolve individual entries offline: {shell_join(['stem4b', 'repair-toc', str(work)])}. "
        "For an explicit bypass, set reconcile = false in your config's [navigation] table "
        "and rerun the same command with --until narrate; review the resulting headings before TTS."
    )
    return (
        f"{report['preserved_output']} {len(report['diagnostics'])} issue(s). First issue: {detail}\n"
        f"Read {work / 'toc-report.md'} for the affected headings, locations and recovery steps "
        f"(full audit: {work / 'toc-report.json'}).\n{next_step}"
    )
