import pytest
from filelock import FileLock

from technical_audiobook.cli import main
from technical_audiobook.config import Config
from technical_audiobook.models import (
    Book,
    Coverage,
    Segment,
    SourceToc,
    SourceUnit,
    TocEntry,
    Transcript,
)
from technical_audiobook.narration_text import save_narration
from technical_audiobook.navigation import reconcile_toc, validate_source_destinations
from technical_audiobook.storage import digest, read_json, write_json
from technical_audiobook.toc_overrides import TocCorrection, load_corrections, save_corrections
from technical_audiobook.toc_repair import _destination, repair_toc


def case(work, text="Source explanation.", bookmark="Old label", accepted="4. New Title"):
    book = Book(
        title="Book",
        source_sha256="hash",
        format="pdf",
        units=[SourceUnit(id="p00100", location="PDF physical page 100", text=text)],
        toc=SourceToc(
            kind="pdf_outline",
            entries=[
                TocEntry(title=bookmark, target="page:100", source_id="p00100", level=1),
            ],
        ),
    )
    transcript = Transcript(
        title=book.title,
        author=book.author,
        source_sha256=book.source_sha256,
        segments=[
            Segment(
                kind="heading",
                text=accepted,
                display_title=accepted,
                heading_level=1,
                source_ids=["p00100"],
            ),
            Segment(
                kind="paragraph", text="All substantive explanation stays.", source_ids=["p00100"]
            ),
        ],
        coverage=[Coverage(source_id="p00100", disposition="narrated")],
    )
    write_json(work / "source.json", book)
    return book, transcript, Config()


@pytest.mark.parametrize(
    "text",
    [
        "4. New Title\nExplanation.",
        "CHAPTER 4\nNew Title\nExplanation.",
        "CHAPTER\n4\nNew Title\nExplanation.",
        "92 CHAPTER 4 • OLD LABEL\n\nCHAPTER\nNew\nTitle4\nExplanation.",
    ],
)
def test_printed_number_and_title_can_correct_stale_unnumbered_bookmark(workspace, text):
    book, original, config = case(workspace, text)
    before = book.model_dump(), original.model_dump()
    result = reconcile_toc(original, book, config, workspace)
    assert result.segments[0].display_title == "4. New Title"
    assert result.segments[0].text == "Chapter four. New Title."
    assert result.segments[1:] == original.segments[1:]
    assert result.coverage == original.coverage
    assert (book.model_dump(), original.model_dump()) == before
    record = read_json(workspace / "toc-report.json")["entries"][0]
    assert record["title"] == "Old label"
    assert record["resolved_title"] == "4. New Title"
    assert record["numbering"]["source"] == "printed_source_heading"
    assert record["source_heading_evidence"]


def test_printed_chapter_kind_survives_nesting_under_a_part(workspace):
    book, original, config = case(workspace, "92 CHAPTER 4 • OLD LABEL\nCHAPTER\nNew\nTitle4")
    book.toc.entries[0].level = 2
    original.segments[0].heading_level = 2
    result = reconcile_toc(original, book, config, workspace)
    assert result.segments[0].display_title == "4. New Title"
    assert result.segments[0].heading_level == 2
    assert result.segments[0].text == "Chapter four. New Title."


@pytest.mark.parametrize(
    "text",
    [
        "New Title\nExplanation.",
        "5. New Title\nExplanation.",
        "This paragraph mentions 4. New Title but is not a heading.",
        "CHAPTER\nNew\nTitle4\nExplanation.",  # Ambiguous trailing number without corroboration.
        "92 CHAPTER 5 • OLD LABEL\nCHAPTER\nNew\nTitle4\nExplanation.",
        "4. New Title\nExplanation.\n4. New Title\nAnother heading.",
    ],
)
def test_uncorroborated_or_conflicting_printed_titles_still_fail(workspace, text):
    book, transcript, config = case(workspace, text)
    with pytest.raises(ValueError, match="cannot safely retain numbering"):
        reconcile_toc(transcript, book, config, workspace)
    assert not (workspace / "narration.txt").exists()


@pytest.mark.parametrize(
    "change",
    ["numbered_bookmark", "multiple_headings", "multiple_bookmarks", "epub", "multiple_pages"],
)
def test_printed_title_repair_does_not_guess_ambiguous_relationships(workspace, change):
    book, transcript, config = case(workspace, "4. New Title")
    if change == "numbered_bookmark":
        book.toc.entries[0].title = "3. Old label"
    elif change == "multiple_headings":
        transcript.segments.append(
            transcript.segments[0].model_copy(update={"display_title": "5. Another heading"})
        )
    elif change == "multiple_bookmarks":
        book.toc.entries.append(book.toc.entries[0].model_copy(update={"title": "Another label"}))
    elif change == "epub":
        book.format, book.toc.kind = "epub", "epub_nav"
    else:
        book.units.append(SourceUnit(id="p00101", location="page 101", text="More."))
        transcript.segments[0].source_ids.append("p00101")
    with pytest.raises(ValueError):
        reconcile_toc(transcript, book, config, workspace)


def test_exercise_subtree_is_excluded_even_with_broken_destinations_but_examples_survive(workspace):
    book, original, config = case(workspace, bookmark="4. New Title")
    book.toc.entries.extend(
        [
            TocEntry(title="4.9. Exercises", level=2, target="broken"),
            TocEntry(title="Problem 1", level=3, target="broken"),
            TocEntry(title="Worked examples", level=2, target="page:100", source_id="p00100"),
        ]
    )
    original.segments.append(
        original.segments[0].model_copy(
            update={"display_title": "Worked examples", "text": "Worked examples."}
        )
    )
    validate_source_destinations(book, config, workspace)
    result = reconcile_toc(original, book, config, workspace)
    assert [s.display_title for s in result.segments if s.kind == "heading"] == [
        "4. New Title",
        "Worked examples",
    ]
    assert result.segments[1] == original.segments[1]
    assert [e["action"] for e in read_json(workspace / "toc-report.json")["entries"]][1:3] == [
        "omitted_exercises"
    ] * 2
    config.narration.include_exercises = True
    with pytest.raises(ValueError, match="unresolved destinations"):
        validate_source_destinations(book, config, workspace)


def failed_case(work):
    book, original, config = case(work)
    with pytest.raises(ValueError):
        reconcile_toc(original, book, config, work)
    return book, original, config


def answers(*values):
    values = iter(values)
    return lambda prompt: next(values)


def test_guided_heading_selection_publishes_offline_and_convert_reuses_correction(
    workspace, monkeypatch
):
    book, original, config = failed_case(workspace)

    def no_api(*args, **kwargs):
        raise AssertionError("Offline repair must not make provider calls")

    monkeypatch.setattr("technical_audiobook.api.generate_json", no_api)
    source = (workspace / "source.json").read_bytes()
    result = repair_toc(workspace, ask=answers("", "1", "y", "y"))
    assert result == workspace / "narration.txt"
    assert "Chapter four. New Title." in result.read_text()
    choice = load_corrections(workspace, book)[0]
    assert choice.heading_id == digest(original.segments[0])
    record = read_json(workspace / "toc-report.json")["entries"][0]
    assert record["matching"] == "user_selected_heading"
    assert record["numbering"]["source"] == "user_correction"
    assert reconcile_toc(original, book, config, workspace).segments[1:] == original.segments[1:]
    assert (workspace / "source.json").read_bytes() == source


def test_check_does_not_publish_and_publish_protects_manual_edits(workspace):
    book, original, config = failed_case(workspace)
    save_corrections(
        workspace,
        book,
        {
            0: TocCorrection(
                entry=1, action="match", source_id="p00100", heading_id=digest(original.segments[0])
            )
        },
    )
    save_narration(original, workspace)
    path = workspace / "narration.txt"
    path.write_text(path.read_text().replace("All substantive", "My edited"))
    saved = path.read_bytes()
    assert repair_toc(workspace, check=True) == workspace / "toc-report.md"
    assert path.read_bytes() == saved
    assert (workspace / "narration.toc-preview.txt").exists()
    assert "My edited" not in (workspace / "narration.toc-preview.txt").read_text()
    with pytest.raises(ValueError, match="contains edits"):
        repair_toc(workspace, publish=True)
    assert path.read_bytes() == saved


def test_omitting_only_navigation_keeps_all_spoken_content(workspace):
    book, original, config = failed_case(workspace)
    repair_toc(workspace, ask=answers("", "o", "y", "y"))
    result = Transcript.model_validate(read_json(workspace / "narration.json"))
    assert [s.text for s in result.segments] == [s.text for s in original.segments]
    assert result.coverage == original.coverage
    assert all(s.kind == "paragraph" for s in result.segments)


def test_preflight_destination_can_be_repaired_without_narration_or_keys(workspace):
    book, original, config = case(workspace)
    book.toc.entries[0].source_id = None
    write_json(workspace / "source.json", book)
    with pytest.raises(ValueError):
        validate_source_destinations(book, config, workspace)
    repair_toc(workspace, ask=answers("", "d", "100", "y"))
    assert read_json(workspace / "toc-report.json")["status"] == "source_destinations_resolved"
    assert load_corrections(workspace, book)[0].source_id == "p00100"
    assert not (workspace / "narration.txt").exists()
    validate_source_destinations(book, config, workspace)


@pytest.mark.parametrize("kind", ["source", "toc", "heading"])
def test_stale_corrections_cannot_silently_attach_to_other_content(workspace, kind):
    book, original, config = failed_case(workspace)
    save_corrections(
        workspace,
        book,
        {
            0: TocCorrection(
                entry=1, action="match", source_id="p00100", heading_id=digest(original.segments[0])
            )
        },
    )
    if kind == "source":
        book.source_sha256 = "other"
    elif kind == "toc":
        book.toc.entries[0].title = "Other"
    else:
        original.segments[0].text = "Changed accepted heading."
    with pytest.raises(ValueError, match="different source|saved correction"):
        reconcile_toc(original, book, config, workspace)
    assert not (workspace / "narration.txt").exists()


def test_reset_preserves_even_malformed_override_file(workspace):
    failed_case(workspace)
    path = workspace / "toc-overrides.json"
    path.write_text("{not valid json")
    with pytest.raises(ValueError, match="cannot safely retain numbering"):
        repair_toc(workspace, check=True, reset=True)
    assert read_json(path)["entries"] == []
    assert [p.read_text() for p in (workspace / "toc-override-history").glob("*.json")] == [
        "{not valid json"
    ]


def test_workspace_lock_and_noninteractive_cli_are_clear(workspace, monkeypatch, caplog):
    failed_case(workspace)
    with FileLock(workspace / ".pipeline.lock"):
        with pytest.raises(ValueError, match="Another process"):
            repair_toc(workspace, check=True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["repair-toc", str(workspace)]) == 1
    assert "interactive terminal" in caplog.text
    monkeypatch.setattr(
        "technical_audiobook.cli.load_config", lambda *a: pytest.fail("Provider config not needed")
    )
    assert main(["repair-toc", str(workspace), "--check"]) == 1
    assert "cannot safely retain numbering" in caplog.text


@pytest.mark.parametrize("fail_publication", [False, True])
def test_explicit_backup_replacement_and_rollback(workspace, monkeypatch, fail_publication):
    book, original, config = failed_case(workspace)
    save_corrections(
        workspace,
        book,
        {
            0: TocCorrection(
                entry=1, action="match", source_id="p00100", heading_id=digest(original.segments[0])
            ),
        },
    )
    save_narration(original, workspace)
    text, baseline = workspace / "narration.txt", workspace / "narration.json"
    text.write_text(text.read_text().replace("All substantive", "My edited"))
    before = text.read_bytes(), baseline.read_bytes()
    if fail_publication:

        def fail(*args):
            raise ValueError("Simulated publication failure")

        monkeypatch.setattr("technical_audiobook.toc_repair.validate_published_navigation", fail)
        with pytest.raises(ValueError, match="Simulated publication failure"):
            repair_toc(workspace, publish=True, backup_edits=True)
        assert (text.read_bytes(), baseline.read_bytes()) == before
    else:
        repair_toc(workspace, publish=True, backup_edits=True)
        assert "My edited" not in text.read_text()
        assert "Chapter four. New Title." in text.read_text()
    backups = list(workspace.glob("toc-repair-backup-*"))
    assert len(backups) == 1
    assert (
        (backups[0] / text.name).read_bytes(),
        (backups[0] / baseline.name).read_bytes(),
    ) == before


def test_cannot_request_backup_replacement_implicitly(workspace):
    with pytest.raises(ValueError, match="requires --publish"):
        repair_toc(workspace, check=True, backup_edits=True)
    assert not list(workspace.glob("toc-repair-backup-*"))


def test_user_selected_headings_still_must_be_unique_and_ordered(workspace):
    book, original, config = failed_case(workspace)
    book.units.append(SourceUnit(id="p00101", location="page 101", text="Another topic."))
    book.toc.entries.append(
        TocEntry(title="Other old label", target="page:101", source_id="p00101", level=1)
    )
    other = original.segments[0].model_copy(
        update={"source_ids": ["p00101"], "display_title": "5. Other topic", "text": "Other topic."}
    )
    original.segments.append(other)
    original.coverage.append(Coverage(source_id="p00101", disposition="narrated"))
    first = TocCorrection(entry=1, action="match", source_id="p00101", heading_id=digest(other))
    second = TocCorrection(
        entry=2, action="match", source_id="p00100", heading_id=digest(original.segments[0])
    )
    save_corrections(workspace, book, {0: first, 1: second})
    with pytest.raises(ValueError, match="User corrections conflict with source TOC order"):
        reconcile_toc(original, book, config, workspace)
    assert not (workspace / "narration.txt").exists()
    second = first.model_copy(update={"entry": 2})
    save_corrections(workspace, book, {0: first, 1: second})
    with pytest.raises(ValueError, match="reuses another TOC"):
        reconcile_toc(original, book, config, workspace)


def test_confirmed_choices_survive_quit_and_source_entry_numbers_are_stable(workspace):
    book, original, config = case(workspace)
    book.toc.entries.insert(
        0, TocEntry(title="Not selected", level=1, target="page:99", selected=False)
    )
    book.toc.entries.append(TocEntry(title="Another missing heading", level=1, target="broken"))
    write_json(workspace / "source.json", book)
    with pytest.raises(ValueError):
        reconcile_toc(original, book, config, workspace)
    with pytest.raises(ValueError, match="Unresolved TOC issues remain"):
        repair_toc(workspace, ask=answers("2", "/New Title", "1", "y", "q"))
    assert load_corrections(workspace, book)[1].entry == 2
    assert not (workspace / "narration.txt").exists()
    assert (
        read_json(workspace / "toc-report.json")["diagnostics"][0]["entries"][0]["entry_number"]
        == 3
    )


def test_snapshot_has_no_provider_configuration(workspace):
    book, original, config = case(workspace)
    config.llm.base_url = "https://private-provider.invalid/v1"
    config.llm.model = "private-model"
    config.tts.model = "private-speech-model"
    with pytest.raises(ValueError):
        reconcile_toc(original, book, config, workspace)
    snapshot = (workspace / "toc-input.json").read_text()
    assert "private-provider" not in snapshot
    assert "private-model" not in snapshot
    assert "private-speech-model" not in snapshot


def test_control_d_is_actionable_and_does_not_publish(workspace):
    failed_case(workspace)

    def eof(prompt):
        raise EOFError

    with pytest.raises(ValueError, match="Input ended"):
        repair_toc(workspace, ask=eof)
    assert not (workspace / "narration.txt").exists()


def test_epub_destination_search_makes_source_ids_discoverable(workspace, capsys):
    book, original, config = case(workspace)
    book.format = "epub"
    book.units[0].id = "e000015"
    book.units[0].location = "chap04.xhtml, block 15"
    book.units[0].text = "Searchable technical explanation."
    sid = _destination(book, answers("/technical", "e000015"))
    assert sid == "e000015"
    output = capsys.readouterr().out
    assert "e000015" in output and "chap04.xhtml, block 15" in output
