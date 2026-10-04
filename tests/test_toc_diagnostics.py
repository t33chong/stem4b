from copy import deepcopy

import pytest

from stem4b.models import Book, SourceUnit
from stem4b.storage import read_json
from stem4b.toc_diagnostics import (
    explain_report,
    failure_details,
    render_report,
    write_toc_report,
)


def old_report():
    return {
        "status": "needs_attention",
        "errors": [
            "Adjacent-page corrections conflict with source TOC order",
            "Exercises: no safely located heading at p00072",
            "Exercises: no safely located heading at p00132",
        ],
        "entries": [
            {
                "title": "Historical Notes",
                "source_id": "p00072",
                "resolved_source_id": "p00073",
                "action": "matched",
                "segment": 450,
            },
            {"title": "Exercises", "source_id": "p00072", "action": "unresolved"},
            {"title": "Exercises", "source_id": "p00132", "action": "unresolved"},
        ],
    }


def test_legacy_report_can_be_explained_without_source_or_narration_changes():
    original = old_report()
    untouched = deepcopy(original)
    report = explain_report(original)
    assert original == untouched
    assert report["errors"] == original["errors"]
    assert report["phase"] == "final_reconciliation"
    assert len(report["diagnostics"][0]["conflicts"]) == 1
    for issue, sid in zip(report["diagnostics"][1:], ["p00072", "p00132"], strict=True):
        assert len(issue["entries"]) == 1  # Repeated titles are not conflated.
        assert issue["entries"][0]["original_location"].startswith(sid)
    guide = render_report(report)
    for expected in [
        "physical PDF page 73",
        "Exercises",
        "not printed page labels",
        "same TOML file",
        "--until narrate",
        "toc-overrides.json",
        "accepted.json",
        "final heading standardization",
        "may require re-narration",
    ]:
        assert expected in guide


@pytest.mark.parametrize("verbose", [False, True])
def test_cli_logs_actionable_toc_errors_without_needing_verbose(
    workspace, monkeypatch, caplog, verbose
):
    from stem4b.cli import main
    from stem4b.config import Config

    def fail(*args):
        report = write_toc_report(old_report(), workspace)
        raise ValueError(
            "Source TOC reconciliation needs attention. " + failure_details(report, workspace)
        )

    monkeypatch.setattr("stem4b.cli.convert", fail)
    monkeypatch.setattr("stem4b.cli.load_config", lambda path: Config())
    monkeypatch.setattr("stem4b.cli.load_dotenv", lambda *args, **kwargs: None)
    args = ["convert", "unused.pdf", "-o", str(workspace / "unused.m4b")]
    assert main(args + (["--verbose"] if verbose else [])) == 1
    for expected in [
        "Historical Notes",
        "Exercises",
        "physical PDF page 73",
        "3 issue(s)",
        str(workspace / "toc-report.md"),
        "reconcile = false",
        "[navigation]",
        "No final narration was overwritten",
    ]:
        assert expected in caplog.text


def test_order_conflicts_follow_book_units_not_lexicographic_ids():
    book = Book(
        title="Book",
        source_sha256="hash",
        format="epub",
        units=[
            SourceUnit(id=sid, location=f"chapter.xhtml#{sid}", text="Body")
            for sid in ["z_first", "a_second"]
        ],
    )
    report = old_report()
    report["entries"] = [
        {"title": "Later", "source_id": "a_second"},
        {"title": "Unresolved", "source_id": None},
        {"title": "Earlier", "source_id": "z_first"},
    ]
    report["errors"] = report["errors"][:1]
    pair = explain_report(report, book)["diagnostics"][0]["conflicts"][0]
    assert pair["previous"]["title"] == "Later"
    assert pair["following"]["title"] == "Earlier"
    assert "chapter.xhtml#z_first" in pair["following"]["original_location"]


@pytest.mark.parametrize(
    "error,code,expected",
    [
        (
            "Logistic Regression: cannot safely retain numbering from accepted heading '4. Logistic "
            "Regression and Text Classification'; titles do not match exactly",
            "heading_label_conflict",
            "stale or abbreviated titles",
        ),
        (
            "Logistic Regression: conflicting source/narration numbers",
            "heading_label_conflict",
            "No numbers have been guessed",
        ),
        (
            "Logistic Regression: ambiguous headings at p00100",
            "ambiguous_heading",
            "More than one plausible heading",
        ),
        (
            "Logistic Regression: source nesting exceeds the supported six heading levels",
            "unsupported_depth",
            "audio.toc_depth",
        ),
        ("Logistic Regression: Broken fragment", "unresolved_destination", "source target"),
    ],
)
def test_issue_specific_checks_include_source_and_accepted_labels(error, code, expected):
    report = explain_report(
        {
            "status": "needs_attention",
            "errors": [error],
            "entries": [
                {
                    "title": "Logistic Regression",
                    "source_id": "p00100",
                    "target": "page:100",
                    "error": error,
                    "narration_heading": "4. Logistic Regression and Text Classification",
                    "narration_candidates": [
                        {
                            "title": "4. Logistic Regression and Text Classification",
                            "source_ids": ["p00100"],
                            "segment": 600,
                        }
                    ],
                }
            ],
        }
    )
    assert report["diagnostics"][0]["code"] == code
    guide = render_report(report)
    assert expected in guide
    assert "physical PDF page 100" in guide
    assert "Logistic Regression and Text Classification" in guide
    assert "narration segment 600" in guide
    assert r"&\#x27;" not in guide


def test_adjacent_candidates_are_readable_and_titles_cannot_break_markdown():
    title = "A | <script> [title](bad)"
    error = title + ": ambiguous source-verified adjacent-page headings at p00002"
    report = explain_report(
        {
            "status": "needs_attention",
            "errors": [error],
            "entries": [
                {
                    "title": title,
                    "source_id": "p00002",
                    "error": error,
                    "adjacent_candidates": [
                        {"narration_heading": title, "resolved_source_id": "p00003", "segment": 12},
                    ],
                }
            ],
        }
    )
    guide = render_report(report)
    assert "<script>" not in guide
    assert "[title](bad)" not in guide
    assert r"A \|" in guide
    assert "physical PDF page 3" in guide
    assert "narration segment 12" in guide


@pytest.mark.parametrize("status", ["aligned", "disabled", "no_source_toc"])
def test_fresh_report_replaces_stale_error_instructions(workspace, status):
    write_toc_report(old_report(), workspace)
    assert "What to do next" in (workspace / "toc-report.md").read_text()
    result = write_toc_report({"status": status, "errors": [], "entries": []}, workspace)
    assert read_json(workspace / "toc-report.json") == result
    assert result["diagnostics"] == []
    assert result["recovery_steps"] == []
    guide = (workspace / "toc-report.md").read_text()
    assert "What to do next" not in guide
    assert "Historical Notes" not in guide
    assert str(workspace / "toc-report.md") == result["instructions_file"]
