import json

import pytest

from technical_audiobook.api import TruncatedResponse
from technical_audiobook.audio import speech_plan
from technical_audiobook.chunking import plan_chunks
from technical_audiobook.config import Config
from technical_audiobook.front_matter import FrontMatterDecision, review_front_matter
from technical_audiobook.models import (
    Book,
    Coverage,
    Draft,
    Segment,
    SourceToc,
    SourceUnit,
    TocEntry,
    Transcript,
)
from technical_audiobook.narration import Narrator
from technical_audiobook.narration_text import load_script, save_narration
from technical_audiobook.navigation import (
    number_words,
    reconcile_toc,
    standard_heading,
    validate_published_navigation,
)
from technical_audiobook.storage import read_json, write_json


def heading(title, sid, level=1, spoken=None):
    return Segment(
        kind="heading",
        display_title=title,
        heading_level=level,
        text=spoken or title,
        source_ids=[sid],
    )


def paragraph(text, sid):
    return Segment(kind="paragraph", text=text, source_ids=[sid])


def source_book(titles, format="pdf"):
    units = [
        SourceUnit(id=sid, location=sid, text=title, heading=title, heading_level=level)
        for title, sid, level in titles
    ]
    return Book(
        title="Test book",
        source_sha256="source",
        format=format,
        units=units,
        toc=SourceToc(
            kind="pdf_outline" if format == "pdf" else "epub_ncx",
            entries=[
                TocEntry(title=title, source_id=sid, level=level, target=sid)
                for title, sid, level in titles
            ],
        ),
    )


def transcript(book, segments):
    cited = {sid for s in segments for sid in s.source_ids}
    return Transcript(
        title=book.title,
        author=book.author,
        source_sha256=book.source_sha256,
        segments=segments,
        coverage=[
            Coverage(
                source_id=u.id,
                disposition="narrated" if u.id in cited else "omitted",
                reason="" if u.id in cited else "Omitted by accepted source review",
            )
            for u in book.units
        ],
    )


@pytest.mark.parametrize(
    "number,words",
    [
        ("2.10.12", "two point ten point twelve"),
        ("21", "twenty-one"),
        ("104", "one hundred four"),
        ("XIV", "fourteen"),
        ("2000", "two thousand"),
    ],
)
def test_spoken_numbers(number, words):
    assert number_words(number) == words


@pytest.mark.parametrize(
    "spoken",
    [
        "Currents in Perspective",
        "2.2.1. Currents in Perspective.",
        "Section 2.2.1, Currents in Perspective.",
        "Two point two point one. Currents in Perspective.",
        "Section two point two point one. Currents in Perspective..",
    ],
)
def test_standardized_heading_is_idempotent_and_removes_old_number_forms(spoken):
    book = source_book([("2.2.1 Currents in Perspective", "one", 3)])
    entry = book.toc.entries[0]
    normalized = standard_heading(
        entry, book, Config(), heading("2.2.1 Currents in Perspective", "one", 2, spoken)
    )
    assert normalized.display_title == "2.2.1. Currents in Perspective"
    assert normalized.text == "Section two point two point one. Currents in Perspective."
    assert normalized.heading_level == 3
    assert standard_heading(entry, book, Config(), normalized) == normalized


@pytest.mark.parametrize("format,display", [("pdf", "2. Theory"), ("epub", "CHAPTER 2. Theory")])
def test_chapter_number_style(format, display):
    book = source_book([("CHAPTER 2: Theory", "one", 1)], format)
    result = standard_heading(book.toc.entries[0], book, Config())
    assert result.display_title == display
    assert result.text == "Chapter two. Theory."
    config = Config()
    config.navigation.chapter_prefix = "omit"
    assert standard_heading(book.toc.entries[0], book, config).display_title == "2. Theory"


def test_pdf_multiple_headings_on_same_page_preserve_order_and_fix_depth(workspace):
    book = source_book([("CHAPTER 2 Theory", "one", 1), ("2.3 Voltage", "two", 2)])
    book.toc.entries.insert(
        1,
        TocEntry(title="2.2.1 Currents in Perspective", source_id="one", level=3, target="page:1"),
    )
    original = transcript(
        book,
        [
            heading("CHAPTER 2. Theory", "one"),
            paragraph("Original explanation.", "one"),
            heading("2.2.1 Currents in Perspective", "one", 2),
            paragraph("Preserve the worked example.", "one"),
            heading("2.3. Voltage", "two", 2),
            heading("A useful body-only heading", "two", 3),
            paragraph("Do not discard this explanation.", "two"),
        ],
    )
    untouched = original.model_copy(deep=True)
    result = reconcile_toc(original, book, Config(), workspace)
    assert [(s.heading_level, s.display_title) for s in result.segments if s.kind == "heading"] == [
        (1, "2. Theory"),
        (3, "2.2.1. Currents in Perspective"),
        (2, "2.3. Voltage"),
    ]
    assert result.segments[-2].kind == "paragraph"
    assert result.segments[-2].text == "A useful body-only heading"
    assert [
        s
        for s in result.segments
        if s.text
        in {
            "Original explanation.",
            "Preserve the worked example.",
            "Do not discard this explanation.",
        }
    ] == [s for s in original.segments if s.kind == "paragraph"]
    assert original == untouched
    report = read_json(workspace / "toc-report.json")
    assert report["m4b_toc"] == [c.title for c in speech_plan(result, Config())]


def test_chapter_only_epub_toc_does_not_promote_body_subheadings(workspace):
    book = source_book([("FOREWORD", "one", 1), ("CHAPTER 1: Scaling", "two", 1)], "epub")
    original = transcript(
        book,
        [
            heading("FOREWORD", "one", spoken="Foreword."),
            paragraph("Useful learning guidance.", "one"),
            heading("CHAPTER 1. Scaling", "two"),
            heading("Single server setup", "two", 2),
            paragraph("Substantive server explanation.", "two"),
        ],
    )
    result = reconcile_toc(original, book, Config(), workspace)
    assert [c.title for c in speech_plan(result, Config())] == ["FOREWORD", "CHAPTER 1. Scaling"]
    assert (
        result.segments[-2].text == "Single server setup"
        and result.segments[-2].kind == "paragraph"
    )


def test_restore_unambiguously_located_missing_heading_but_not_midpage_guess(workspace):
    book = source_book([("1. Signals", "one", 1), ("2. Information", "two", 1)], "epub")
    original = transcript(
        book, [heading("1. Signals", "one"), paragraph("Information measures uncertainty.", "two")]
    )
    result = reconcile_toc(original, book, Config(), workspace)
    assert [s.display_title for s in result.segments if s.kind == "heading"] == [
        "1. Signals",
        "2. Information",
    ]
    book.format = "pdf"
    book.units[1].text = "End of prior discussion.\n2. Information\nA new topic."
    with pytest.raises(ValueError, match="no safely located heading"):
        reconcile_toc(original, book, Config(), workspace)
    assert not (workspace / "narration.json").exists()


def test_unresolved_source_destination_and_reordered_headings_fail_closed(workspace):
    book = source_book([("1. Signals", "one", 1), ("2. Information", "two", 1)])
    original = transcript(book, [heading("2. Information", "two"), heading("1. Signals", "one")])
    with pytest.raises(ValueError, match="source TOC order"):
        reconcile_toc(original, book, Config(), workspace)
    book.toc.entries[0].source_id = None
    book.toc.entries[0].reason = "Broken fragment"
    with pytest.raises(ValueError, match="Broken fragment"):
        reconcile_toc(original, book, Config(), workspace)


def test_omitted_exercises_do_not_reappear_but_neighboring_table_survives(workspace):
    book = source_book([("4.2.11 Problems", "one", 3), ("4.3 Transistors", "three", 2)])
    book.units.insert(
        1, SourceUnit(id="two", location="page two", text="Useful diode summary table.")
    )
    original = transcript(
        book,
        [paragraph("Useful diode summary table.", "two"), heading("4.3 Transistors", "three", 2)],
    )
    result = reconcile_toc(original, book, Config(), workspace)
    assert result.segments[0].text == "Useful diode summary table."
    assert [s.display_title for s in result.segments if s.kind == "heading"] == ["4.3. Transistors"]
    assert (
        read_json(workspace / "toc-report.json")["entries"][0]["action"] == "omitted_source_content"
    )


def front_book():
    book = source_book(
        [("Test book", "one", 1), ("FOREWORD", "three", 1), ("CHAPTER 1: Scaling", "four", 1)],
        "epub",
    )
    book.units[0].text = "Test book. Copyright. Publisher credits."
    book.units.insert(
        1,
        SourceUnit(
            id="two",
            location="promo.xhtml, block 1",
            text="Join the mailing list. Subscribe to the community.",
        ),
    )
    original = transcript(
        book,
        [
            paragraph("Copyright and credits.", "one"),
            heading("Join the mailing list", "two", 2),
            paragraph("Subscribe to the community.", "two"),
            heading("FOREWORD", "three", spoken="Foreword."),
            paragraph("Important learning advice.", "three"),
            heading("CHAPTER 1. Scaling", "four"),
            paragraph("Technical substance.", "four"),
        ],
    )
    return book, original


class FrontClient:
    def __init__(self, omit=True, truncated=False):
        self.omit, self.truncated, self.calls = omit, truncated, []

    def generate(self, messages, schema, purpose, validate):
        self.calls.append(purpose)
        if self.truncated:
            raise TruncatedResponse("Limit")
        ids = [
            json.loads(p["text"])["source_id"]
            for p in messages[1]["content"]
            if p["type"] == "text"
        ]
        assert ids == [
            "one",
            "two",
        ]  # Foreword/teaching material never enters this deletion proposal.
        decision = FrontMatterDecision(
            source_ids=ids,
            omit=self.omit,
            reason="Only copyright and promotional material."
            if self.omit
            else "Potentially useful guidance; retain.",
        )
        validate(decision)
        return decision


def test_front_matter_source_check_is_cached_and_omission_updates_coverage(workspace):
    book, original = front_book()
    config, client = Config(), FrontClient()
    omitted = review_front_matter(client, original, book, config, workspace)
    assert omitted == {"one", "two"}
    assert review_front_matter(client, original, book, config, workspace) == omitted
    assert client.calls == ["toc:front-matter"]
    result = reconcile_toc(original, book, config, workspace, omitted)
    assert result.segments[0].display_title == "FOREWORD"
    assert [c.title for c in speech_plan(result, config)] == ["FOREWORD", "CHAPTER 1. Scaling"]
    assert all(c.disposition == "omitted" for c in result.coverage[:2])
    assert any(s.text == "Important learning advice." for s in result.segments)
    assert original.coverage[0].disposition == "narrated"


@pytest.mark.parametrize("mode", ["reject", "truncated", "budget", "disabled"])
def test_unverified_front_matter_is_retained(workspace, mode):
    book, original = front_book()
    config, client = Config(), FrontClient(omit=False, truncated=mode == "truncated")
    if mode == "budget":
        book.units[0].text = "large " * 4000
    if mode == "disabled":
        config.navigation.omit_front_matter = False
    assert review_front_matter(client, original, book, config, workspace) == set()
    assert review_front_matter(client, original, book, config, workspace) == set()
    assert len(client.calls) == (0 if mode in {"budget", "disabled"} else 1)
    result = reconcile_toc(original, book, config, workspace)
    assert any(s.text == "Subscribe to the community." for s in result.segments)


def test_front_matter_spanning_useful_content_is_never_cut(workspace):
    book, original = front_book()
    original.segments[2].source_ids = ["two", "three"]
    result = reconcile_toc(original, book, Config(), workspace, {"one", "two"})
    assert any(s.text == "Subscribe to the community." for s in result.segments)
    assert read_json(workspace / "toc-report.json")["omitted_front_source_ids"] == []


def test_no_source_toc_preserves_subheadings_and_navigation_config_does_not_change_caches(
    workspace,
):
    book = source_book([("2. Theory", "one", 1)])
    original = transcript(book, [heading("2. Theory", "one"), heading("2.10 Grounds", "one", 2)])
    book.toc = SourceToc()
    config = Config()
    narrator = Narrator(None, config, workspace)
    chunk = plan_chunks(book, config.narration)[0]
    before = narrator._cache_directory(chunk, [])
    result = reconcile_toc(original, book, config, workspace)
    assert len([s for s in result.segments if s.kind == "heading"]) == 2
    assert result.segments[-1].text == "Section two point ten. Grounds."
    assert read_json(workspace / "toc-report.json")["status"] == "no_source_toc"
    config.navigation.chapter_prefix = "keep"
    config.navigation.omit_front_matter = False
    config.navigation.reconcile = False
    assert narrator._cache_directory(chunk, []) == before
    assert reconcile_toc(original, book, config, workspace) == original


def test_manual_edits_are_protected_and_published_navigation_is_checked(workspace):
    book = source_book([("1. Signals", "one", 1)])
    original = transcript(book, [heading("1. Signals", "one"), paragraph("Original prose.", "one")])
    result = reconcile_toc(original, book, Config(), workspace)
    save_narration(result, workspace)
    path = workspace / "narration.txt"
    path.write_text(path.read_text().replace("Original prose.", "Edited prose."))
    validate_published_navigation(result, load_script(path), Config(), workspace)
    path.write_text(path.read_text().replace("# 1. Signals", "# My different heading"))
    saved = path.read_bytes()
    with pytest.raises(ValueError, match="Your edits were preserved"):
        validate_published_navigation(result, load_script(path), Config(), workspace)
    assert path.read_bytes() == saved
    assert read_json(workspace / "toc-report.json")["status"] == "edited_text_conflict"


def test_final_pass_reuses_accepted_drafts_and_navigation_settings_do_not_renarrate(workspace):
    class NoRequests:
        def generate(self, *args, **kwargs):
            raise AssertionError("The final pass must reuse accepted narration")

    book = source_book([("CHAPTER 1 Signals", "one", 1)])
    config = Config()
    narrator = Narrator(NoRequests(), config, workspace)
    chunks = plan_chunks(book, config.narration)
    accepted = narrator._cache_directory(chunks[0], []) / "accepted.json"
    write_json(
        accepted,
        Draft(
            segments=[
                heading("CHAPTER 1. Signals", "one", spoken="Chapter 1. Signals."),
                paragraph("Complete accepted explanation.", "one"),
            ],
            coverage=[Coverage(source_id="one", disposition="narrated")],
        ),
    )
    original = accepted.read_bytes()
    result = narrator.narrate(book, chunks)
    assert result.segments[0].display_title == "1. Signals"
    assert result.segments[0].text == "Chapter one. Signals."
    config.navigation.chapter_prefix = "keep"
    result = narrator.narrate(book, chunks)
    assert result.segments[0].display_title == "CHAPTER 1. Signals"
    assert accepted.read_bytes() == original


def test_unresolved_destinations_fail_before_any_narration_requests(workspace):
    class NoRequests:
        def generate(self, *args, **kwargs):
            raise AssertionError("Source-destination errors should precede paid work")

    book = source_book([("1. Signals", "one", 1)])
    book.toc.entries[0].source_id = None
    config = Config()
    with pytest.raises(ValueError, match="No narration requests were made"):
        Narrator(NoRequests(), config, workspace).narrate(book, plan_chunks(book, config.narration))
    assert read_json(workspace / "toc-report.json")["phase"] == "source_preflight"
    assert not (workspace / "narration.json").exists()


def test_same_page_repeated_titles_are_disambiguated_by_number(workspace):
    book = source_book([("2.1 Overview", "one", 2)])
    book.toc.entries.append(
        TocEntry(title="2.2 Overview", level=2, source_id="one", target="page:1")
    )
    original = transcript(
        book,
        [
            heading("2.1. Overview", "one", 2),
            paragraph("First topic.", "one"),
            heading("2.2. Overview", "one", 2),
            paragraph("Second topic.", "one"),
        ],
    )
    result = reconcile_toc(original, book, Config(), workspace)
    assert [s.text for s in result.segments if s.kind == "heading"] == [
        "Section two point one. Overview.",
        "Section two point two. Overview.",
    ]


def test_front_matter_decision_must_cover_exact_source(workspace):
    class WrongSource:
        def generate(self, messages, schema, purpose, validate):
            result = FrontMatterDecision(source_ids=["one"], omit=True, reason="Incomplete review")
            validate(result)
            return result

    book, original = front_book()
    with pytest.raises(ValueError, match="exactly the supplied source IDs"):
        review_front_matter(WrongSource(), original, book, Config(), workspace)
    assert not list((workspace / "toc-front-matter").glob("*.json"))
