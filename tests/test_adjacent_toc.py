import pytest

from stem4b.chunking import plan_chunks
from stem4b.config import Config
from stem4b.models import (
    Book,
    Coverage,
    Draft,
    Segment,
    SourceToc,
    SourceUnit,
    TocEntry,
    Transcript,
)
from stem4b.narration import Narrator
from stem4b.narration_text import load_script
from stem4b.navigation import reconcile_toc
from stem4b.storage import read_json, write_json

pytestmark = pytest.mark.usefixtures("scripted_generation")

TITLE = "Hierarchical Structures"


def heading(sid, title="2.2.2. Hierarchical Structures"):
    return Segment(
        kind="heading", text=title + ".", display_title=title, heading_level=3, source_ids=[sid]
    )


def pdf_case(actual_page=3, source_heading="2.2.2  Hierarchical Structures"):
    sid = f"p{actual_page:05d}"
    book = Book(
        title="Test",
        source_sha256="source",
        format="pdf",
        units=[
            SourceUnit(
                id=f"p{page:05d}",
                location=f"PDF physical page {page}",
                text="Prior section exercises.",
            )
            for page in range(1, 5)
        ],
        toc=SourceToc(
            kind="pdf_outline",
            entries=[
                TocEntry(title=TITLE, level=3, target="page:2", source_id="p00002"),
            ],
        ),
    )
    # The erroneous bookmark also produced this misleading extraction hint.
    book.units[1].heading, book.units[1].heading_level = TITLE, 3
    book.units[actual_page - 1].text = (
        "Figure 2.5: List structure.\n\n" + source_heading + "\n\nUseful explanation."
    )
    transcript = Transcript(
        title=book.title,
        author=book.author,
        source_sha256=book.source_sha256,
        segments=[
            Segment(kind="figure", text="The figure shows nested pairs.", source_ids=[sid]),
            heading(sid),
            Segment(kind="paragraph", text="Lists can contain other lists.", source_ids=[sid]),
        ],
        coverage=[
            Coverage(
                source_id=u.id,
                disposition="narrated" if u.id == sid else "omitted",
                reason="" if u.id == sid else "Exercises",
            )
            for u in book.units
        ],
    )
    return book, transcript


@pytest.mark.parametrize("actual_page", [1, 3])
@pytest.mark.parametrize(
    "source_heading",
    [
        "2.2.2  Hierarchical Structures",
        "2.2.2  Hierarchical\n    Structures",
        "2.2.2\nHierarchical\nStructures",
    ],
)
def test_recovers_both_directions_with_source_evidence_without_moving_content(
    workspace,
    actual_page,
    source_heading,
):
    book, original = pdf_case(actual_page, source_heading)
    untouched_book, untouched_transcript = book.model_dump(), original.model_dump()
    result = reconcile_toc(original, book, Config(), workspace)
    assert book.model_dump() == untouched_book
    assert original.model_dump() == untouched_transcript
    assert result.coverage == original.coverage
    assert result.segments[0] == original.segments[0]  # Figure stays BEFORE the heading.
    assert result.segments[2] == original.segments[2]
    assert result.segments[1].source_ids == [f"p{actual_page:05d}"]
    assert result.segments[1].display_title == "2.2.2. Hierarchical Structures"
    assert result.segments[1].text == "Section two point two point two. Hierarchical Structures."
    report = read_json(workspace / "toc-report.json")
    assert not report["errors"]
    record = report["entries"][0]
    assert record["source_id"] == "p00002" and record["target"] == "page:2"
    assert record["resolved_source_id"] == f"p{actual_page:05d}"
    assert record["matching"] == "source_verified_adjacent_pdf_page"
    assert " ".join(record["source_heading_evidence"].split()) == "2.2.2 Hierarchical Structures"
    assert record["segment"] == 1 and record["action"] == "matched"


@pytest.mark.parametrize(
    "failure",
    [
        "prose_mention",
        "hint_only",
        "no_source_text",
        "no_accepted_heading",
        "fuzzy_title",
        "conflicting_source_number",
        "conflicting_separate_number",
        "conflicting_bookmark_number",
        "multiple_source_ids",
        "two_pages_away",
        "gap_in_selected_pages",
    ],
)
def test_unverified_or_out_of_bounds_recovery_fails_closed(workspace, failure):
    book, original = pdf_case(4 if failure in {"two_pages_away", "gap_in_selected_pages"} else 3)
    actual = book.units[3 if failure in {"two_pages_away", "gap_in_selected_pages"} else 2]
    if failure == "prose_mention":
        actual.text = "We discuss Hierarchical Structures in a later section."
    elif failure == "hint_only":
        actual.heading = TITLE
        actual.text = "Another topic without a printed heading."
    elif failure == "no_source_text":
        actual.text = ""
    elif failure == "no_accepted_heading":
        original.segments.pop(1)
    elif failure == "fuzzy_title":
        original.segments[1].display_title = "2.2.2. Hierarchical Structure"
    elif failure == "conflicting_source_number":
        actual.text = "2.2.3 Hierarchical Structures"
    elif failure == "conflicting_separate_number":
        actual.text = "2.2.3\nHierarchical\nStructures"
    elif failure == "conflicting_bookmark_number":
        book.toc.entries[0].title = "2.2.3 Hierarchical Structures"
    elif failure == "multiple_source_ids":
        original.segments[1].source_ids = ["p00003", "p00004"]
    elif failure == "gap_in_selected_pages":
        book.units.pop(2)  # List neighbors must not count as physical-page neighbors.
        original.coverage.pop(2)
    with pytest.raises(ValueError, match="no safely located heading"):
        reconcile_toc(original, book, Config(), workspace)
    assert not (workspace / "narration.json").exists()
    assert not (workspace / "narration.txt").exists()


@pytest.mark.parametrize(
    "duplicate", ["both_neighbors", "two_narration_headings", "two_source_headings"]
)
def test_ambiguous_nearby_matches_are_not_silently_chosen(workspace, duplicate):
    book, original = pdf_case()
    if duplicate == "both_neighbors":
        book.units[0].text = "2.2.2 Hierarchical Structures"
        original.segments.insert(0, heading("p00001"))
        original.coverage[0].disposition, original.coverage[0].reason = "narrated", ""
    elif duplicate == "two_narration_headings":
        original.segments.insert(1, heading("p00003"))
    else:
        book.units[2].text += "\n2.2.2 Hierarchical Structures"
    (workspace / "narration.txt").write_text("Keep existing user text.")
    with pytest.raises(ValueError, match="ambiguous source-verified adjacent-page headings"):
        reconcile_toc(original, book, Config(), workspace)
    record = read_json(workspace / "toc-report.json")["entries"][0]
    assert len(record["adjacent_candidates"]) == 2
    assert record["action"] == "unresolved"
    assert (workspace / "narration.txt").read_text() == "Keep existing user text."


def test_parent_and_child_bookmarks_on_same_page_are_resolved_independently(workspace):
    book, original = pdf_case()
    book.toc.entries.insert(
        0, TocEntry(title="Lists", level=2, target="page:2", source_id="p00002")
    )
    book.units[1].text = "2.2 Lists\nSome introductory text."
    book.units[1].heading = "Lists / " + TITLE
    original.segments.insert(0, heading("p00002", "2.2. Lists"))
    original.coverage[1].disposition, original.coverage[1].reason = "narrated", ""
    result = reconcile_toc(original, book, Config(), workspace)
    assert [(s.display_title, s.source_ids) for s in result.segments if s.kind == "heading"] == [
        ("2.2. Lists", ["p00002"]),
        ("2.2.2. Hierarchical Structures", ["p00003"]),
    ]
    records = read_json(workspace / "toc-report.json")["entries"]
    assert "resolved_source_id" not in records[0]
    assert records[1]["resolved_source_id"] == "p00003"


def test_existing_source_heading_prevents_relocating_to_a_duplicate_on_another_page(workspace):
    book, original = pdf_case()
    book.units[1].text = "2.2.2 Hierarchical Structures"
    # The normal restoration policy can restore the known page-start heading;
    # it must not reinterpret this valid bookmark as an off-by-one error.
    reconcile_toc(original, book, Config(), workspace)
    assert "resolved_source_id" not in read_json(workspace / "toc-report.json")["entries"][0]


def test_valid_local_narration_heading_takes_precedence(workspace):
    book, original = pdf_case()
    original.segments.insert(0, heading("p00002"))
    original.coverage[1].disposition, original.coverage[1].reason = "narrated", ""
    result = reconcile_toc(original, book, Config(), workspace)
    assert [s.source_ids for s in result.segments if s.kind == "heading"] == [["p00002"]]
    assert "resolved_source_id" not in read_json(workspace / "toc-report.json")["entries"][0]


def test_crossed_bookmark_corrections_fail_source_order_check(workspace):
    book, original = pdf_case()
    book.units[1].text = "2.2.3 A different topic"
    book.toc.entries.append(
        TocEntry(title="A different topic", level=3, target="page:3", source_id="p00003")
    )
    original.segments.insert(0, heading("p00002", "2.2.3 A different topic"))
    original.coverage[1].disposition, original.coverage[1].reason = "narrated", ""
    with pytest.raises(ValueError, match="corrections conflict with source TOC order"):
        reconcile_toc(original, book, Config(), workspace)
    issue = read_json(workspace / "toc-report.json")["diagnostics"][0]
    assert issue["code"] == "source_order_conflict"
    assert issue["conflicts"][0]["previous"]["effective_location"].startswith("p00003")
    assert issue["conflicts"][0]["following"]["effective_location"].startswith("p00002")


def test_recovered_heading_cannot_violate_narration_order(workspace):
    book, original = pdf_case()
    book.units[0].text = "2.2.1 Earlier topic"
    book.toc.entries.insert(
        0, TocEntry(title="Earlier topic", level=3, target="page:1", source_id="p00001")
    )
    original.segments.append(heading("p00001", "2.2.1 Earlier topic"))
    original.coverage[0].disposition, original.coverage[0].reason = "narrated", ""
    with pytest.raises(ValueError, match="not in source TOC order"):
        reconcile_toc(original, book, Config(), workspace)
    issue = read_json(workspace / "toc-report.json")["diagnostics"][0]
    assert issue["code"] == "narration_order_conflict"
    pair = issue["conflicts"][0]
    assert pair["previous"]["title"] == "Earlier topic"
    assert pair["previous"]["segment"] > pair["following"]["segment"]


def test_corrected_heading_followed_by_unresolved_exercises_has_actionable_report(workspace):
    book, original = pdf_case(source_heading="Historical Notes")
    book.toc.entries[0].title = "Historical Notes"
    original.segments[1] = heading("p00003", "Historical Notes")
    book.toc.entries.append(
        TocEntry(title="Exercises", level=3, target="page:2", source_id="p00002")
    )
    original.coverage[1].disposition, original.coverage[1].reason = "narrated", ""
    original.segments.insert(
        0, Segment(kind="paragraph", text="Useful prior material.", source_ids=["p00002"])
    )
    untouched = original.model_dump()
    published = workspace / "narration.txt"
    published.write_text("Previously published narration, with manual edits.")
    saved = published.read_bytes()
    config = Config()
    config.narration.include_exercises = True
    with pytest.raises(ValueError) as caught:
        reconcile_toc(original, book, config, workspace)
    message = str(caught.value)
    for expected in [
        "2 issue(s)",
        "Historical Notes",
        "Exercises",
        "physical PDF page 3",
        "reconcile = false",
        "[navigation]",
        str(workspace / "toc-report.md"),
        "No final narration was overwritten",
    ]:
        assert expected in message
    report = read_json(workspace / "toc-report.json")
    pair = report["diagnostics"][0]["conflicts"][0]
    assert pair["previous"]["original_location"].startswith("p00002")
    assert pair["previous"]["effective_location"].startswith("p00003")
    assert pair["following"]["action"] == "unresolved"
    assert report["entries"][1]["error"] == "Exercises: no safely located heading at p00002"
    assert report["diagnostics"][1]["entries"][0]["title"] == "Exercises"
    guide = (workspace / "toc-report.md").read_text()
    assert "intentionally omitted" in guide
    assert "Rerunning unchanged will repeat the failure" in guide
    assert "not an override file" in guide
    assert original.model_dump() == untouched
    assert published.read_bytes() == saved
    config.narration.include_exercises = False
    result = reconcile_toc(original, book, config, workspace)
    assert result.coverage == original.coverage
    assert [s.text for s in result.segments if s.kind != "heading"] == [
        s.text for s in original.segments if s.kind != "heading"
    ]
    assert read_json(workspace / "toc-report.json")["entries"][1]["action"] == "omitted_exercises"


def test_one_heading_cannot_satisfy_two_bookmarks(workspace):
    book, original = pdf_case()
    book.toc.entries.append(TocEntry(title=TITLE, level=3, target="page:3", source_id="p00003"))
    with pytest.raises(ValueError, match="no safely located heading"):
        reconcile_toc(original, book, Config(), workspace)


def test_adjacent_page_rule_does_not_apply_to_epubs(workspace):
    book, original = pdf_case()
    book.format, book.toc.kind = "epub", "epub_ncx"
    book.units[1].heading = ""
    with pytest.raises(ValueError, match="no safely located heading"):
        reconcile_toc(original, book, Config(), workspace)


def test_final_pass_exports_from_accepted_cache_without_requests_or_checkpoint_edits(workspace):
    class NoRequests:
        def generate(self, *args, **kwargs):
            raise AssertionError("Navigation recovery must not make model calls")

    book, original = pdf_case()
    config = Config()
    config.narration.max_pdf_pages = 20
    config.navigation.omit_front_matter = False
    narrator = Narrator(NoRequests(), config, workspace)
    chunks = plan_chunks(book, config.narration)
    assert len(chunks) == 1
    checkpoint = narrator._cache_directory(chunks[0], []) / "accepted.json"
    write_json(checkpoint, Draft(segments=original.segments, coverage=original.coverage))
    saved = checkpoint.read_bytes()
    result = narrator.narrate(book, chunks)
    assert narrator.narrate(book, chunks) == result
    assert checkpoint.read_bytes() == saved
    assert book.toc.entries[0].source_id == "p00002"
    assert (
        load_script(workspace / "narration.txt").segments[1].display_title
        == "2.2.2. Hierarchical Structures"
    )
