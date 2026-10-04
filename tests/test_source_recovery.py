import json

import pytest

from stem4b.chunking import Chunk
from stem4b.config import Config, LLMConfig, NarrationConfig
from stem4b.models import Coverage, Draft, Finding, Review, Segment, SourceUnit
from stem4b.narration import Narrator, disputed_source_ids, source_content
from stem4b.storage import read_json, write_json

pytestmark = pytest.mark.usefixtures("scripted_generation")


class Responses:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.messages = []

    def generate(self, messages, schema, purpose, validate=None):
        self.calls.append(purpose)
        self.messages.append(messages)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, schema)
        if validate:
            validate(response)
        return response


def rejection(description):
    return Review(
        approved=False,
        findings=[Finding(severity="error", source_ids=["one"], description=description)],
    )


def recovery_case(workspace, max_revisions=20):
    config = Config(
        llm=LLMConfig(model="test"), narration=NarrationConfig(max_revisions=max_revisions)
    )
    chunk = Chunk(
        "00404",
        [
            SourceUnit(id="one", location="block 101", text="The gateway must stay lightweight."),
            SourceUnit(id="two", location="block 102", text="The engine handles complex tasks."),
        ],
        SourceUnit(id="before", location="block 100", text="Previous source context."),
        SourceUnit(id="after", location="block 103", text="Next source context."),
    )
    original = Draft(
        segments=[
            Segment(kind="paragraph", text=unit.text, source_ids=[unit.id]) for unit in chunk.units
        ],
        coverage=[Coverage(source_id=unit.id, disposition="narrated") for unit in chunk.units],
    )
    bad_review = rejection(
        "The primary source text for one is not included in the supplied material. "
        "The draft narrates a substantive paragraph, but its accuracy cannot be checked."
    )
    directory = Narrator(Responses(), config, workspace)._cache_directory(chunk, [])
    write_json(directory / "draft-0.json", original)
    write_json(directory / "review-0.json", bad_review)
    poisoned = original.model_copy(deep=True)
    poisoned.segments = poisoned.segments[1:]
    poisoned.coverage[0] = Coverage(
        source_id="one", disposition="omitted", reason="Primary source was not supplied."
    )
    for revision in range(1, 19):
        write_json(directory / f"draft-{revision}.json", poisoned)
        if revision < 18:
            write_json(directory / f"review-{revision}.json", bad_review)
    return config, chunk, directory, original, bad_review


def test_recovers_original_draft_without_replaying_poisoned_history(workspace):
    config, chunk, directory, original, _ = recovery_case(workspace)
    saved = {p: p.read_bytes() for p in directory.glob("*.json")}
    client = Responses(Review(approved=True))
    narrator = Narrator(client, config, workspace)
    result = narrator.chunk(chunk, [])
    assert result == original
    assert client.calls == ["review:00404:source-check:0"]
    parts = client.messages[0][1]["content"]
    # All original source/context is still supplied, and the disputed exact primary
    # text is copied after the original complete draft, not the poisoned last draft.
    assert parts[: len(source_content(chunk, workspace, config, []))] == source_content(
        chunk, workspace, config, []
    )
    draft_part = next(p["text"] for p in parts if p["text"].startswith("DRAFT TO REVIEW:"))
    assert Draft.model_validate_json(draft_part.split("\n", 1)[1]) == original
    assert json.loads(parts[-1]["text"])["text"] == chunk.units[0].text
    assert "Check ALL primary units" in parts[-2]["text"]
    assert all(p.read_bytes() == data for p, data in saved.items())
    assert Draft.model_validate(read_json(directory / "accepted.json")) == original
    assert Narrator(Responses(), config, workspace).chunk(chunk, []) == original


def test_real_findings_require_bounded_repairs_and_resume_in_separate_history(workspace):
    config, chunk, directory, original, _ = recovery_case(workspace, max_revisions=0)
    saved = {p: p.read_bytes() for p in directory.glob("*.json")}
    genuine = rejection("Clarify the latency requirement in the narration.")
    client = Responses(genuine)
    with pytest.raises(ValueError, match="failed source review after 0 revisions"):
        Narrator(client, config, workspace).chunk(chunk, [])
    assert not (directory / "accepted.json").exists()
    assert client.calls == ["review:00404:source-check:0"]

    config.narration.max_revisions = 1
    repaired = original.model_copy(deep=True)
    repaired.segments[0].text += " This keeps latency low."
    interrupted = Responses(repaired, RuntimeError("Interrupted review"))
    with pytest.raises(RuntimeError, match="Interrupted review"):
        Narrator(interrupted, config, workspace).chunk(chunk, [])
    assert interrupted.calls == [
        "narrate:00404:source-repair:1",
        "review:00404:source-repair:1",
    ]
    repair_messages = interrupted.messages[0]
    assert Draft.model_validate_json(repair_messages[2]["content"]) == original
    assert genuine.findings[0].description in repair_messages[3]["content"]
    assert "not supplied" not in repair_messages[3]["content"]
    assert not (directory / "accepted.json").exists()

    resumed = Responses(Review(approved=True))
    assert Narrator(resumed, config, workspace).chunk(chunk, []) == repaired
    assert resumed.calls == ["review:00404:source-repair:1"]
    assert all(p.read_bytes() == data for p, data in saved.items())
    assert len(list(directory.glob("source-recheck-*/draft-1.json"))) == 1


def test_repeated_missing_source_claim_stops_without_omission_or_endless_rechecks(workspace):
    config, chunk, directory, _, missing = recovery_case(workspace)
    client = Responses(missing)
    for attempt_client in (client, Responses()):
        with pytest.raises(ValueError, match="Stopping this non-progress loop"):
            Narrator(attempt_client, config, workspace).chunk(chunk, [])
    assert client.calls == ["review:00404:source-check:0"]
    assert not (directory / "accepted.json").exists()
    assert not list(directory.glob("source-recheck-*/draft-1.json"))


def test_new_missing_source_finding_is_rechecked_before_poisoning_the_next_draft(workspace):
    config, chunk, _, original, missing = recovery_case(workspace)
    client = Responses(original, missing, Review(approved=True))
    narrator = Narrator(client, config, workspace / "fresh")
    assert narrator.chunk(chunk, []) == original
    assert client.calls == ["narrate:00404:0", "review:00404:0", "review:00404:source-check:0"]


def test_missing_local_payload_is_not_claimed_to_be_present(workspace, monkeypatch):
    config, chunk, directory, _, _ = recovery_case(workspace)
    actual_content = source_content(chunk, workspace, config, [])
    broken = [p for p in actual_content if not p["text"].startswith('{"source_id": "one"')]
    monkeypatch.setattr("stem4b.narration.source_content", lambda *args: broken)
    with pytest.raises(ValueError, match="missing or changed in the local review payload"):
        Narrator(Responses(), config, workspace).chunk(chunk, [])
    assert not (directory / "accepted.json").exists()


def test_fresh_review_must_still_cite_only_primary_sources(workspace):
    config, chunk, directory, _, _ = recovery_case(workspace)
    bad = rejection("Source is not provided.")
    bad.findings[0].source_ids = ["after"]
    with pytest.raises(ValueError, match="only primary source IDs"):
        Narrator(Responses(bad), config, workspace).chunk(chunk, [])
    assert not (directory / "accepted.json").exists()


@pytest.mark.parametrize(
    "description,expected",
    [
        ("Primary source unit one was not supplied, so its content cannot be checked.", True),
        ("The source text is missing from the input.", True),
        ("The source text is not available.", True),
        ("The draft omits the latency requirement present in the source.", False),
        ("The source paragraph is not included in the draft.", False),
        ("Primary unit one is not present in the generated narration.", False),
        ("Missing code.", False),
        ("An explanation is not provided by the draft.", False),
    ],
)
def test_only_source_availability_errors_trigger_recheck(description, expected):
    review = rejection(description)
    assert disputed_source_ids(review) == ({"one"} if expected else set())
    review.findings[0].severity = "warning"
    assert not disputed_source_ids(review)
