import pytest

from stem4b.api import TruncatedResponse
from stem4b.chunking import Chunk, plan_chunks
from stem4b.config import Config, LLMConfig, NarrationConfig
from stem4b.models import Book, Coverage, Draft, Finding, Review, Segment, SourceUnit
from stem4b.narration import (
    NARRATION_VERSION,
    REVIEW_POLICY,
    Narrator,
    append_segments,
    check_coverage,
)
from stem4b.prompts import NARRATION_POLICY, PARAGRAPH_ONLY_NARRATION_POLICY
from stem4b.storage import digest, write_json

pytestmark = pytest.mark.usefixtures("scripted_generation")


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


def test_code_continuation_joins_code_not_prose_and_preserves_evidence():
    previous = [
        paragraph("The implementation follows."),
        Segment(kind="code", text="The root can contain any", source_ids=["one"]),
        Segment(kind="figure", text="A tree diagram.", source_ids=["one"]),
    ]
    incoming = [
        Segment(
            kind="code",
            text="value. Set both bounds.",
            source_ids=["one", "two"],
            continues_previous=True,
        )
    ]
    joined = append_segments(previous, incoming)
    assert [s.kind for s in joined] == ["paragraph", "code", "figure"]
    assert joined[0].text == "The implementation follows."
    assert joined[1].text == "The root can contain any value. Set both bounds."
    assert joined[1].source_ids == ["one", "two"]
    assert not joined[1].continues_previous
    assert previous[1].text == "The root can contain any"
    assert incoming[0].continues_previous


@pytest.mark.parametrize("prior", ["none", "paragraph", "heading"])
def test_code_continuation_requires_code_in_the_same_section(prior):
    previous = []
    if prior == "paragraph":
        previous = [paragraph("Unrelated prose.")]
    elif prior == "heading":
        previous = [
            Segment(kind="code", text="Old listing.", source_ids=["one"]),
            Segment(
                kind="heading",
                text="New section.",
                display_title="New section",
                heading_level=2,
                source_ids=["one"],
            ),
        ]
    with pytest.raises(ValueError, match="no preceding code segment"):
        append_segments(
            previous,
            [
                Segment(
                    kind="code",
                    text="the rest.",
                    source_ids=["two"],
                    continues_previous=True,
                )
            ],
        )


def test_code_continuation_does_not_strip_a_minus_operator():
    previous = [Segment(kind="code", text="Return n -", source_ids=["one"])]
    incoming = [Segment(kind="code", text="one.", source_ids=["two"], continues_previous=True)]
    assert append_segments(previous, incoming)[0].text == "Return n - one."


@pytest.mark.parametrize("kind", ["heading", "figure", "table", "equation", "footnote"])
def test_other_segment_kinds_cannot_continue(kind):
    heading = {"display_title": "A heading", "heading_level": 1} if kind == "heading" else {}
    with pytest.raises(ValueError, match="Only paragraphs and code"):
        Segment(kind=kind, text="Text.", source_ids=["one"], continues_previous=True, **heading)


def test_only_first_segment_may_continue_code():
    chunk = Chunk("one", [SourceUnit(id="one", location="page 1", text="content")])
    draft = Draft(
        segments=[
            paragraph("Some prose."),
            Segment(
                kind="code",
                text="Continued code.",
                source_ids=["one"],
                continues_previous=True,
            ),
        ],
        coverage=[Coverage(source_id="one", disposition="narrated")],
    )
    with pytest.raises(ValueError, match="Only the FIRST"):
        check_coverage(draft, chunk)


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


@pytest.mark.parametrize("kind", ["paragraph", "code"])
def test_truncation_subdivides_and_preserves_boundary_continuity(workspace, kind):
    units = [
        SourceUnit(id="one", location="page 1", text="The mean is"),
        SourceUnit(id="two", location="page 2", text="the sum divided by the count."),
    ]
    book = Book(title="Test", source_sha256="abc", format="pdf", units=units)
    first = Draft(
        segments=[Segment(kind=kind, text="The mean is", source_ids=["one"])],
        coverage=[Coverage(source_id="one", disposition="narrated")],
    )
    second = Draft(
        segments=[
            Segment(
                kind=kind,
                text="the sum divided by the count.",
                source_ids=["two"],
                continues_previous=True,
            )
        ],
        coverage=[Coverage(source_id="two", disposition="narrated")],
    )
    config = Config(narration=NarrationConfig(review=False))
    client = ScriptedLLM([TruncatedResponse("too long"), first, second])
    result = Narrator(client, config, workspace).narrate(book, plan_chunks(book, config.narration))
    assert len(result.segments) == 1
    assert result.segments[0].text == "The mean is the sum divided by the count."
    assert result.segments[0].source_ids == ["one", "two"]
    assert result.segments[0].kind == kind
    assert len(client.calls) == 3


@pytest.mark.parametrize("kind", ["paragraph", "code"])
def test_split_with_omitted_left_half_continues_earlier_batch(workspace, kind):
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
        segments=[
            Segment(kind=kind, text="the remainder.", source_ids=["two"], continues_previous=True)
        ],
        coverage=[Coverage(source_id="two", disposition="narrated")],
    )
    client = ScriptedLLM([TruncatedResponse("too long"), omitted, continued])
    config = Config(narration=NarrationConfig(review=False))
    previous = [Segment(kind=kind, text="This is", source_ids=["previous"])]
    draft = Narrator(client, config, workspace).chunk(chunk, previous)
    assert append_segments(previous, draft.segments)[0].text == "This is the remainder."


@pytest.mark.parametrize("left_kind", ["figure", "paragraph"])
def test_split_continues_external_code_past_non_code_left_half(workspace, left_kind):
    chunk = Chunk(
        "two",
        [
            SourceUnit(id="one", location="page 1", text="Intervening material."),
            SourceUnit(id="two", location="page 2", text="the remainder."),
        ],
    )
    left = Draft(
        segments=[Segment(kind=left_kind, text="Intervening material.", source_ids=["one"])],
        coverage=[Coverage(source_id="one", disposition="narrated")],
    )
    right = Draft(
        segments=[
            Segment(kind="code", text="the remainder.", source_ids=["two"], continues_previous=True)
        ],
        coverage=[Coverage(source_id="two", disposition="narrated")],
    )
    client = ScriptedLLM([TruncatedResponse("too long"), left, right])
    previous = [Segment(kind="code", text="This is", source_ids=["previous"])]
    config = Config(narration=NarrationConfig(review=False))
    draft = Narrator(client, config, workspace).chunk(chunk, previous)
    assert draft.segments[0].kind == "code"
    assert draft.segments[0].continues_previous
    combined = append_segments(previous, draft.segments)
    assert combined[0].text == "This is the remainder."
    assert combined[1].text == "Intervening material."


@pytest.mark.parametrize("kind", ["paragraph", "code"])
def test_split_preserves_external_continuation_when_both_halves_continue(workspace, kind):
    chunk = Chunk(
        "two",
        [
            SourceUnit(id="one", location="page 1", text="the"),
            SourceUnit(id="two", location="page 2", text="value."),
        ],
    )
    drafts = [
        Draft(
            segments=[Segment(kind=kind, text=text, source_ids=[sid], continues_previous=True)],
            coverage=[Coverage(source_id=sid, disposition="narrated")],
        )
        for sid, text in (("one", "the"), ("two", "value."))
    ]
    client = ScriptedLLM([TruncatedResponse("too long"), *drafts])
    previous = [Segment(kind=kind, text="Return", source_ids=["previous"])]
    config = Config(narration=NarrationConfig(review=False))
    draft = Narrator(client, config, workspace).chunk(chunk, previous)
    assert draft.segments[0].text == "the value."
    assert draft.segments[0].source_ids == ["one", "two"]
    assert draft.segments[0].continues_previous
    joined = append_segments(previous, draft.segments)
    assert joined[0].text == "Return the value."
    assert joined[0].source_ids == ["previous", "one", "two"]
    assert not joined[0].continues_previous
    assert previous[0].text == "Return"


@pytest.mark.parametrize("document_type", ["book", "paper"])
def test_resume_paragraph_only_policy_cache_and_review_edited_code(workspace, document_type):
    config = Config(narration=NarrationConfig(max_pdf_pages=1, document_type=document_type))
    book = Book(
        title="Code",
        source_sha256="abc",
        format="pdf",
        units=[
            SourceUnit(id="one", location="page 1", text="The root can contain any"),
            SourceUnit(id="two", location="page 2", text="value."),
        ],
    )
    chunks = plan_chunks(book, config.narration)
    old = Narrator(ScriptedLLM([]), config, workspace)
    old.policy = PARAGRAPH_ONLY_NARRATION_POLICY + old.policy[len(NARRATION_POLICY) :]
    first = Draft(
        segments=[Segment(kind="code", text="The root can contain any", source_ids=["one"])],
        coverage=[Coverage(source_id="one", disposition="narrated")],
    )
    edited = Draft(
        segments=[Segment(kind="code", text="value.", source_ids=["two"], continues_previous=True)],
        coverage=[Coverage(source_id="two", disposition="narrated")],
    )
    accepted = old._cache_directory(chunks[0], []) / "accepted.json"
    draft_file = old._cache_directory(chunks[1], first.segments) / "draft-0.json"
    write_json(accepted, first)
    write_json(draft_file, edited)
    original_files = {p: p.read_bytes() for p in (accepted, draft_file)}

    class CodeReviewLLM(ScriptedLLM):
        def generate(self, messages, schema, purpose, validate=None):
            assert "Keep code explanations as kind=code" in messages[0]["content"]
            return super().generate(messages, schema, purpose, validate)

    client = CodeReviewLLM([Review(approved=True)])
    narrator = Narrator(client, config, workspace)
    assert narrator._cache_directory(chunks[0], []) == accepted.parent
    assert narrator._cache_directory(chunks[1], first.segments) == draft_file.parent
    transcript = narrator.narrate(book, chunks)
    assert client.calls == ["review:00002:0"]
    assert transcript.segments[0].text == "The root can contain any value."
    assert transcript.segments[0].source_ids == ["one", "two"]
    assert not transcript.segments[0].continues_previous
    assert all(path.read_bytes() == content for path, content in original_files.items())
    assert Narrator(ScriptedLLM([]), config, workspace).narrate(book, chunks) == transcript


def legacy_directory(narrator, chunk, previous):
    """Reproduce the original on-disk format, including its revision-limit bug."""
    key = digest(
        [
            NARRATION_VERSION,
            narrator.policy,
            REVIEW_POLICY,
            narrator.config.llm.cache_options(),
            narrator.config.narration.model_dump(exclude={"workers", "document_type"}),
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
@pytest.mark.parametrize("paragraph_only_policy", [False, True])
def test_legacy_cache_still_checks_source_settings_and_context(
    workspace, changed, paragraph_only_policy
):
    config = Config(llm=LLMConfig(model="original"))
    chunk = Chunk("00001", [SourceUnit(id="one", location="page 1", text="Definition.")])
    old = Draft(
        segments=[paragraph("Old narration.")],
        coverage=[Coverage(source_id="one", disposition="narrated")],
    )
    narrator = Narrator(ScriptedLLM([]), config, workspace)
    if paragraph_only_policy:
        narrator.policy = PARAGRAPH_ONLY_NARRATION_POLICY + narrator.policy[len(NARRATION_POLICY) :]
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
