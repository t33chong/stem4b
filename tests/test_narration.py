import pytest

from technical_audiobook.api import TruncatedResponse
from technical_audiobook.chunking import Chunk, plan_chunks
from technical_audiobook.config import Config, LLMConfig, NarrationConfig
from technical_audiobook.models import Book, Coverage, Draft, Finding, Review, Segment, SourceUnit
from technical_audiobook.narration import Narrator, append_segments, check_coverage


def paragraph(text, source="one", **kwargs):
    return Segment(kind="paragraph", text=text, source_ids=[source], **kwargs)


def test_join_midword_and_move_figure_after_complete_paragraph():
    previous = [
        paragraph("The algorithm handles informa-"),
        Segment(kind="figure", text="Figure one depicts a signal.", source_ids=["one"]),
    ]
    joined = append_segments(
        previous, [paragraph("tion efficiently.", "two", continues_previous=True)]
    )
    assert joined[0].text == "The algorithm handles information efficiently."
    assert joined[0].source_ids == ["one", "two"]
    assert joined[1].kind == "figure"
    assert previous[0].text.endswith("-")  # No mutation of a cached draft.
    with pytest.raises(ValueError, match="no preceding"):
        append_segments([], [paragraph("the rest.", continues_previous=True)])


def test_exact_source_coverage():
    chunk = Chunk(
        "one",
        [
            SourceUnit(id="one", location="page 1", text="content"),
            SourceUnit(id="two", location="page 2", text="Bibliography"),
        ],
    )
    valid = Draft(
        segments=[paragraph("content")],
        coverage=[
            Coverage(source_id="one", disposition="narrated"),
            Coverage(source_id="two", disposition="omitted", reason="Bibliography"),
        ],
    )
    check_coverage(valid, chunk)
    broken = valid.model_copy(deep=True)
    broken.coverage.pop()
    with pytest.raises(ValueError, match="exactly"):
        check_coverage(broken, chunk)
    broken = valid.model_copy(deep=True)
    broken.segments[0].source_ids = ["context-only"]
    with pytest.raises(ValueError, match="non-primary"):
        check_coverage(broken, chunk)
    broken = valid.model_copy(deep=True)
    broken.coverage[1].reason = ""
    with pytest.raises(ValueError, match="explanation"):
        check_coverage(broken, chunk)


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def generate(self, messages, schema, purpose, validate=None):
        self.calls.append(purpose)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, schema)
        if validate:
            validate(response)
        return response


def test_review_repair_resume_and_voice_does_not_invalidate_narration(workspace):
    book = Book(
        title="Test",
        author="Author",
        source_sha256="abc",
        format="epub",
        units=[SourceUnit(id="one", location="chapter", text="A precise definition.")],
    )
    draft = Draft(
        segments=[paragraph("A precise definition.")],
        coverage=[Coverage(source_id="one", disposition="narrated")],
    )
    reject = Review(
        approved=False,
        findings=[Finding(severity="error", source_ids=["one"], description="Explain the term.")],
    )
    client = ScriptedLLM([draft, reject, draft, Review(approved=True)])
    config = Config(llm=LLMConfig(model="test"))
    chunks = plan_chunks(book, config.narration)
    transcript = Narrator(client, config, workspace).narrate(book, chunks)
    assert len(client.calls) == 4
    assert transcript.segments[0].text == "A precise definition."
    assert list((workspace / "narration").glob("*/review-1.json"))
    config.tts.voice = "another-voice"
    reused = Narrator(ScriptedLLM([]), config, workspace).narrate(book, chunks)
    assert reused == transcript


def test_rejected_review_does_not_write_final_transcript(workspace):
    book = Book(
        title="Test",
        source_sha256="abc",
        format="epub",
        units=[SourceUnit(id="one", location="chapter", text="Definition.")],
    )
    draft = Draft(
        segments=[paragraph("Definition.")],
        coverage=[Coverage(source_id="one", disposition="narrated")],
    )
    reject = Review(
        approved=False,
        findings=[Finding(severity="error", source_ids=["one"], description="Missing code.")],
    )
    config = Config(narration=NarrationConfig(max_revisions=0))
    client = ScriptedLLM([draft, reject])
    with pytest.raises(ValueError, match="failed source review"):
        Narrator(client, config, workspace).narrate(book, plan_chunks(book, config.narration))
    assert not (workspace / "narration.json").exists()


def test_truncation_subdivides_and_preserves_boundary_continuity(workspace):
    units = [
        SourceUnit(id="one", location="page 1", text="The mean is"),
        SourceUnit(id="two", location="page 2", text="the sum divided by the count."),
    ]
    book = Book(title="Test", source_sha256="abc", format="pdf", units=units)
    first = Draft(
        segments=[paragraph("The mean is")],
        coverage=[Coverage(source_id="one", disposition="narrated")],
    )
    second = Draft(
        segments=[paragraph("the sum divided by the count.", "two", continues_previous=True)],
        coverage=[Coverage(source_id="two", disposition="narrated")],
    )
    config = Config(narration=NarrationConfig(review=False))
    client = ScriptedLLM([TruncatedResponse("too long"), first, second])
    result = Narrator(client, config, workspace).narrate(book, plan_chunks(book, config.narration))
    assert len(result.segments) == 1
    assert result.segments[0].text == "The mean is the sum divided by the count."
    assert result.segments[0].source_ids == ["one", "two"]
    assert len(client.calls) == 3


def test_split_with_omitted_left_half_continues_earlier_batch(workspace):
    chunk = Chunk(
        "two",
        [
            SourceUnit(id="one", location="page 1", text="Only running header"),
            SourceUnit(id="two", location="page 2", text="the remainder."),
        ],
    )
    omitted = Draft(
        segments=[],
        coverage=[Coverage(source_id="one", disposition="omitted", reason="Running header")],
    )
    continued = Draft(
        segments=[paragraph("the remainder.", "two", continues_previous=True)],
        coverage=[Coverage(source_id="two", disposition="narrated")],
    )
    client = ScriptedLLM([TruncatedResponse("too long"), omitted, continued])
    config = Config(narration=NarrationConfig(review=False))
    previous = [paragraph("This is", "previous")]
    draft = Narrator(client, config, workspace).chunk(chunk, previous)
    assert append_segments(previous, draft.segments)[0].text == "This is the remainder."
