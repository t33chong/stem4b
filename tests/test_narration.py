import pytest

from technical_audiobook.api import TruncatedResponse
from technical_audiobook.chunking import Chunk, plan_chunks
from technical_audiobook.config import Config, LLMConfig, NarrationConfig
from technical_audiobook.models import Book, Coverage, Draft, Finding, Review, Segment, SourceUnit
from technical_audiobook.narration import (
    NARRATION_VERSION,
    REVIEW_POLICY,
    Narrator,
    append_segments,
    check_coverage,
)
from technical_audiobook.storage import digest, write_json


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


def legacy_directory(narrator, chunk, previous):
    """Reproduce the original on-disk format, including its revision-limit bug."""
    key = digest(
        [
            NARRATION_VERSION,
            narrator.policy,
            REVIEW_POLICY,
            narrator.config.llm.model_dump(),
            narrator.config.narration.model_dump(),
            [unit.model_dump() for unit in chunk.units],
            [unit.model_dump() if unit else None for unit in (chunk.before, chunk.after)],
            [segment.model_dump() for segment in previous[-3:]],
        ]
    )
    return narrator.work / "narration" / f"{chunk.id}-{key[:20]}"


@pytest.mark.parametrize("legacy", [False, True], ids=["current-cache", "original-cache"])
def test_resume_after_revision_limit_reuses_accepted_sections_and_failed_drafts(
    workspace, caplog, legacy
):
    units = [
        SourceUnit(id=f"p{i:05}", location=f"page {i}", text=f"Source page {i}.")
        for i in range(1, 10)
    ]
    book = Book(title="Test", source_sha256="abc", format="pdf", units=units)
    config = Config(
        llm=LLMConfig(model="test"), narration=NarrationConfig(max_pdf_pages=1, max_revisions=2)
    )
    chunks = plan_chunks(book, config.narration)
    drafts = [
        Draft(
            segments=[paragraph(f"Narrated page {i}.", unit.id)],
            coverage=[Coverage(source_id=unit.id, disposition="narrated")],
        )
        for i, unit in enumerate(units, 1)
    ]
    rejected = Review(
        approved=False,
        findings=[
            Finding(
                severity="error", source_ids=[units[-1].id], description="Clarify the equation."
            )
        ],
    )

    if legacy:
        narrator = Narrator(ScriptedLLM([]), config, workspace)
        previous = []
        for chunk, draft in zip(chunks[:-1], drafts[:-1], strict=True):
            write_json(legacy_directory(narrator, chunk, previous) / "accepted.json", draft)
            previous = append_segments(previous, draft.segments)
        failed_directory = legacy_directory(narrator, chunks[-1], previous)
        for revision in range(3):
            write_json(failed_directory / f"draft-{revision}.json", drafts[-1])
            write_json(failed_directory / f"review-{revision}.json", rejected)
    else:
        responses = [
            response for draft in drafts[:-1] for response in (draft, Review(approved=True))
        ]
        responses.extend([drafts[-1], rejected] * 3)
        client = ScriptedLLM(responses)
        with pytest.raises(ValueError, match="Narration 00009 failed source review"):
            Narrator(client, config, workspace).narrate(book, chunks)
        assert len(client.calls) == 22

    accepted_before = {p: p.read_bytes() for p in (workspace / "narration").glob("*/accepted.json")}
    assert len(accepted_before) == 8
    config.narration.max_revisions = 5
    resumed = ScriptedLLM([drafts[-1], Review(approved=True)])
    caplog.clear()
    with caplog.at_level("INFO"):
        result = Narrator(resumed, config, workspace).narrate(book, chunks)
    assert resumed.calls == ["narrate:00009:3", "review:00009:3"]
    assert len(result.coverage) == 9
    assert [s.text for s in result.segments] == [d.segments[0].text for d in drafts]
    assert all(p.read_bytes() == content for p, content in accepted_before.items())
    assert sum("Reusing narration" in message for message in caplog.messages) == 8
    assert not any(message.startswith("Narrating section 00001") for message in caplog.messages)

    # Completed checkpoints remain reusable when the budget is subsequently reduced.
    config.narration.max_revisions = 0
    no_requests = ScriptedLLM([])
    assert Narrator(no_requests, config, workspace).narrate(book, chunks) == result
    assert not no_requests.calls


def test_legacy_accepted_checkpoint_takes_priority_over_new_unfinished_attempt(workspace):
    config = Config()
    chunk = Chunk("00001", [SourceUnit(id="one", location="page 1", text="Definition.")])
    accepted = Draft(
        segments=[paragraph("Accepted narration.")],
        coverage=[Coverage(source_id="one", disposition="narrated")],
    )
    narrator = Narrator(ScriptedLLM([]), config, workspace)
    current_directory = narrator._cache_directory(chunk, [])
    unfinished = accepted.model_copy(deep=True)
    unfinished.segments[0].text = "Unfinished rewrite."
    write_json(current_directory / "draft-0.json", unfinished)
    write_json(legacy_directory(narrator, chunk, []) / "accepted.json", accepted)
    config.narration.max_revisions = 5
    assert narrator.chunk(chunk, []) == accepted
    assert not narrator.client.calls


@pytest.mark.parametrize("changed", ["source", "model", "policy", "previous"])
def test_legacy_cache_still_checks_source_settings_and_context(workspace, changed):
    config = Config(llm=LLMConfig(model="original"))
    chunk = Chunk("00001", [SourceUnit(id="one", location="page 1", text="Definition.")])
    old = Draft(
        segments=[paragraph("Old narration.")],
        coverage=[Coverage(source_id="one", disposition="narrated")],
    )
    narrator = Narrator(ScriptedLLM([]), config, workspace)
    previous = [paragraph("Previous section.", "previous")]
    write_json(legacy_directory(narrator, chunk, previous) / "accepted.json", old)
    if changed == "source":
        chunk.units[0].text = "An updated definition."
    elif changed == "model":
        config.llm.model = "new-model"
    elif changed == "policy":
        config.narration.include_exercises = True
    else:
        previous[0].text = "A changed preceding section."
    new = old.model_copy(deep=True)
    new.segments[0].text = "Updated narration."
    client = ScriptedLLM([new, Review(approved=True)])
    assert Narrator(client, config, workspace).chunk(chunk, previous) == new
    assert client.calls == ["narrate:00001:0", "review:00001:0"]
