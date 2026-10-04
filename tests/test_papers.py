import json
import shutil

import pymupdf
import pytest
from conftest import Reply, wave_bytes

from stem4b.chunking import Chunk
from stem4b.cli import main
from stem4b.config import Config, LLMConfig, NarrationConfig, TTSConfig, load_config
from stem4b.front_matter import review_front_matter
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
from stem4b.narration import NARRATION_VERSION, Narrator, check_coverage
from stem4b.narration_text import load_script
from stem4b.navigation import (
    reconcile_toc,
    standard_heading,
    validate_source_destinations,
)
from stem4b.pipeline import convert
from stem4b.prompts import NARRATION_POLICY, PAPER_POLICY, REVIEW_POLICY
from stem4b.storage import digest, read_json


def heading(title, sid, level=1, spoken=None):
    return Segment(
        kind="heading",
        display_title=title,
        text=spoken or title,
        heading_level=level,
        source_ids=[sid],
    )


def paragraph(text, sid, kind="paragraph"):
    return Segment(kind=kind, text=text, source_ids=[sid])


def paper_config():
    return Config(narration=NarrationConfig(document_type="paper"))


def paper_book():
    return Book(
        title="A Paper",
        format="pdf",
        source_sha256="source",
        units=[SourceUnit(id="p00001", location="page 1", text="1 Introduction")],
        toc=SourceToc(
            kind="pdf_outline",
            entries=[
                TocEntry(title="1 Introduction", source_id="p00001", level=1, target="page:1")
            ],
        ),
    )


def test_paper_config_and_cli_override(pdf_book, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / "paper.toml"
    config_file.write_text('[narration]\ndocument_type = "paper"\n')
    assert load_config(config_file).narration.document_type == "paper"
    assert Config().narration.document_type == "book"
    with pytest.raises(ValueError):
        NarrationConfig(document_type="automatic")
    for flag, expected in [(None, "paper"), ("book", "book"), ("paper", "paper")]:
        output = tmp_path / f"cli-{flag}.m4b"
        args = [
            "convert",
            str(pdf_book),
            "-c",
            str(config_file),
            "-o",
            str(output),
            "--until",
            "extract",
        ]
        if flag:
            args += ["--document-type", flag]
        assert main(args) == 0
        assert read_json(output.with_suffix(".work") / "plan.json")["document_type"] == expected


def test_book_cache_identity_unchanged_and_paper_cache_separate(workspace):
    chunk = Chunk("00001", paper_book().units)
    config = Config()
    narrator = Narrator(None, config, workspace)
    old_options = config.narration.model_dump(exclude={"document_type", "max_revisions", "workers"})
    assert config.narration.cache_options() == old_options
    assert PAPER_POLICY not in narrator.policy
    key = digest(
        [
            NARRATION_VERSION,
            narrator.policy,
            REVIEW_POLICY,
            config.llm.cache_options(),
            old_options,
            [u.model_dump() for u in chunk.units],
            [None, None],
            [],
        ]
    )
    old_directory = workspace / "narration" / f"00001-{key[:20]}"
    assert narrator._cache_directory(chunk, []) == old_directory
    paper = Narrator(None, paper_config(), workspace)
    assert NARRATION_POLICY in paper.policy and PAPER_POLICY in paper.policy
    assert paper._cache_directory(chunk, []) != old_directory


@pytest.mark.parametrize("toc_numbered", [True, False])
@pytest.mark.parametrize(
    "number,spoken",
    [
        ("1", "one"),
        ("2.1", "two point one"),
        ("A.1", "A point one"),
        ("I.2", "I point two"),
        ("B.2.3", "B point two point three"),
    ],
)
def test_paper_heading_pronunciation(number, spoken, toc_numbered):
    book = paper_book()
    title = f"{number}. Methods"
    entry = book.toc.entries[0].model_copy(update={"title": title if toc_numbered else "Methods"})
    result = standard_heading(entry, book, paper_config(), heading(title, "p00001"))
    assert result.display_title == title
    assert result.text == f"Section {spoken}. Methods."
    assert standard_heading(entry, book, paper_config(), result) == result


def test_paper_title_and_credit_are_not_discarded_as_book_front_matter(workspace):
    book = paper_book()
    book.units.insert(
        0, SourceUnit(id="title", location="cover", text="A Paper. Alex et al. Institute.")
    )
    transcript = Transcript(
        title=book.title,
        author=book.author,
        source_sha256=book.source_sha256,
        segments=[paragraph("A Paper. Alex et al. Institute.", "title")],
        coverage=[Coverage(source_id="title", disposition="narrated")],
    )
    # No provider exists: omission must not even be proposed for paper attribution.
    assert review_front_matter(None, transcript, book, paper_config(), workspace) == set()


@pytest.mark.parametrize("label", ["References", "8. References", "Bibliography", "Works Cited"])
def test_reference_heading_cannot_pass_paper_coverage_validation(label):
    book = paper_book()
    chunk = Chunk("00001", book.units)
    draft = Draft(
        segments=[heading(label, "p00001")],
        coverage=[Coverage(source_id="p00001", disposition="narrated")],
    )
    with pytest.raises(ValueError, match="omit reference-list"):
        check_coverage(draft, chunk, document_type="paper")
    # A technical section about references is not the reference list.
    draft.segments[0] = heading("Cross References in Code", "p00001")
    check_coverage(draft, chunk, document_type="paper")


def test_broken_reference_bookmark_is_skipped_but_other_broken_bookmarks_fail(workspace):
    book = paper_book()
    book.toc.entries.append(TocEntry(title="References", level=1, target="broken"))
    config = paper_config()
    validate_source_destinations(book, config, workspace)
    transcript = Transcript(
        title=book.title,
        author=book.author,
        source_sha256=book.source_sha256,
        segments=[heading("1 Introduction", "p00001")],
        coverage=[Coverage(source_id="p00001", disposition="narrated")],
    )
    reconcile_toc(transcript, book, config, workspace)
    assert (
        read_json(workspace / "toc-report.json")["entries"][-1]["action"]
        == "omitted_reference_list"
    )
    with pytest.raises(ValueError, match="unresolved destinations"):
        validate_source_destinations(book, Config(), workspace)
    book.toc.entries[-1].title = "Related Work"
    with pytest.raises(ValueError, match="unresolved destinations"):
        validate_source_destinations(book, config, workspace)


def test_article_a_in_a_paper_heading_is_not_invented_appendix(workspace):
    book = paper_book()
    book.toc.entries[0].title = "A New Method"
    transcript = Transcript(
        title=book.title,
        author=book.author,
        source_sha256=book.source_sha256,
        segments=[heading("A New Method", "p00001")],
        coverage=[Coverage(source_id="p00001", disposition="narrated")],
    )
    result = reconcile_toc(transcript, book, paper_config(), workspace)
    assert result.segments[0].text == "A New Method."
    assert result.segments[0].display_title == "A New Method"


@pytest.fixture
def pdf_paper(tmp_path):
    path = tmp_path / "paper.pdf"
    with pymupdf.open() as doc:
        for text in [
            "Research on Signals\nAlex First, Blair Second, Casey Third\n"
            "First Institute; Second University\nAbstract\nA complete abstract.\n"
            "1 Introduction\nA detailed introduction.\nFigure 1: Signal amplitude.\nx = 2",
            "2 Results\nImportant results and limitations.\nReferences\n"
            "[1] Bibliography Entry One. Journal, 2020.",
            "[2] Bibliography Entry Two. Journal, 2021.\n[3] Bibliography Entry Three. Journal, 2022.",
            "[4] Bibliography Entry Four. Journal, 2023.\nA Additional Details\n"
            "A proof that must be kept.\nA.1 Code\nfor value in values:\n    print(value)",
        ]:
            page = doc.new_page()
            page.insert_text((50, 50), text)
        doc[0].draw_rect(pymupdf.Rect(50, 240, 200, 280))
        doc.set_metadata(
            {"title": "Research on Signals", "author": "Alex First; Blair Second; Casey Third"}
        )
        doc.set_toc(
            [
                [1, "1 Introduction", 1],
                [1, "2 Results", 2],
                [1, "References", 2],
                [1, "A Additional Details", 4],
                [2, "A.1 Code", 4],
            ]
        )
        doc.save(path)
    return path


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg not installed")
def test_paper_to_narration_and_m4b_with_review_omissions_and_resume(
    pdf_paper,
    workspace,
    tmp_path,
    sdk_server,
):
    segments = {
        "p00001": [
            paragraph("Research on Signals.", "p00001"),
            paragraph("Alex First et al. First Institute and Second University.", "p00001"),
            heading("Abstract", "p00001"),
            paragraph("A complete abstract.", "p00001"),
            heading("1. Introduction", "p00001"),
            paragraph("A detailed introduction.", "p00001"),
            paragraph("Figure one. The rectangle represents signal amplitude.", "p00001", "figure"),
            paragraph("X equals two. This sets the signal's magnitude.", "p00001", "equation"),
        ],
        "p00002": [
            heading("2. Results", "p00002"),
            paragraph("Important results and limitations.", "p00002"),
        ],
        "p00003": [],
        "p00004": [
            heading("Appendix A. Additional Details", "p00004"),
            paragraph("A proof that must be kept.", "p00004"),
            heading("A.1. Code", "p00004", 2),
            paragraph(
                "For each value in values, print the value inside the loop.", "p00004", "code"
            ),
        ],
    }
    calls = {"narrate": 0, "review": 0, "speech": 0}
    speech_inputs = []

    def handle(request):
        payload = json.loads(request.content)
        if request.path.endswith("speech"):
            calls["speech"] += 1
            speech_inputs.append(payload["input"])
            return Reply(wave_bytes())
        policy = payload["messages"][0]["content"]
        assert NARRATION_POLICY in policy and PAPER_POLICY in policy
        assert any(part["type"] == "image_url" for part in payload["messages"][1]["content"])
        if policy.startswith(REVIEW_POLICY):
            calls["review"] += 1
            result = {"approved": True}
        else:
            calls["narrate"] += 1
            evidence = [
                json.loads(part["text"])
                for part in payload["messages"][1]["content"]
                if part["type"] == "text" and part["text"].startswith("{")
            ]
            primary = [unit for unit in evidence if unit.get("role") == "PRIMARY"]
            assert len(primary) == 1
            sid = primary[0]["source_id"]
            result = Draft(
                segments=segments[sid],
                coverage=[
                    Coverage(
                        source_id=sid,
                        disposition="narrated" if segments[sid] else "omitted",
                        reason="" if segments[sid] else "Reference list",
                    )
                ],
            ).model_dump()
        return Reply(
            {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}
        )

    base_url = sdk_server(handle)
    config = Config(
        narration=NarrationConfig(document_type="paper", max_pdf_pages=1),
        llm=LLMConfig(model="test-vision", base_url=base_url),
        tts=TTSConfig(model="test-tts", base_url=base_url),
    )
    output = tmp_path / "paper.m4b"
    text = convert(pdf_paper, output, workspace, config, until="narrate")
    assert calls == {"narrate": 4, "review": 4, "speech": 0}
    script = load_script(text)
    spoken = "\n".join(s.text for s in script.segments)
    for keep in [
        "Alex First et al.",
        "First Institute",
        "Second University",
        "A complete abstract",
        "A proof that must be kept",
        "Section one.",
        "Section A point one.",
    ]:
        assert keep in spoken
    for omit in ["Blair Second", "Casey Third", "Bibliography Entry", "References", "Chapter"]:
        assert omit not in spoken
    transcript = read_json(workspace / "narration.json")
    assert len(transcript["coverage"]) == 4
    assert [c["source_id"] for c in transcript["coverage"] if c["disposition"] == "omitted"] == [
        "p00003"
    ]
    report = read_json(workspace / "toc-report.json")
    assert report["entries"][2]["action"] == "omitted_reference_list"
    assert "A.1. Code" in report["m4b_toc"]
    assert "References" not in report["m4b_toc"]
    assert (
        read_json(workspace / "source.json")["toc"]["entries"][-2]["title"]
        == "A Additional Details"
    )
    assert not output.exists()
    convert(pdf_paper, output, workspace, config)
    assert output.stat().st_size > 0
    assert calls["narrate"] == calls["review"] == 4
    assert "Alex First et al." in "\n".join(speech_inputs)
    assert not any("Bibliography Entry" in text or "Blair Second" in text for text in speech_inputs)
    before = calls.copy()
    convert(pdf_paper, output, workspace, config)
    assert calls == before
