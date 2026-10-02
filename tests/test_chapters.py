import json
import threading

import pytest

from technical_audiobook.api import TruncatedResponse
from technical_audiobook.chapters import BoundaryDecision, ChapterPlanner, chapter_candidates
from technical_audiobook.chunking import plan_chunks
from technical_audiobook.config import Config, LLMConfig, NarrationConfig
from technical_audiobook.models import (
    Book,
    Coverage,
    Draft,
    Finding,
    Review,
    Segment,
    SourceToc,
    SourceUnit,
    TocEntry,
)
from technical_audiobook.narration import Narrator
from technical_audiobook.storage import read_json, write_json


def chapter_book():
    return Book(
        title="Independent chapters",
        source_sha256="source",
        format="pdf",
        units=[
            SourceUnit(
                id="one",
                location="page 1",
                text="Chapter 1. A complete thought.",
                heading="1. First",
                heading_level=1,
            ),
            SourceUnit(id="two", location="page 2", text="End of chapter one."),
            SourceUnit(
                id="three",
                location="page 3",
                text="Chapter 2. A new topic.",
                heading="2. Second",
                heading_level=1,
            ),
            SourceUnit(id="four", location="page 4", text="End of chapter two."),
        ],
    )


def chapter_config(workers=2, max_revisions=2):
    return Config(
        llm=LLMConfig(model="test"),
        narration=NarrationConfig(
            workers=workers,
            max_pdf_pages=1,
            max_revisions=max_revisions,
        ),
    )


class TestLLM:
    __test__ = False

    def __init__(self, independent=True):
        self.independent = independent
        self.calls = []
        self.messages = {}
        self.lock = threading.Lock()

    def generate(self, messages, schema, purpose, validate=None):
        with self.lock:
            self.calls.append(purpose)
            self.messages[purpose] = messages
        result = self.respond(messages, schema, purpose)
        if validate:
            validate(result)
        return result

    def respond(self, messages, schema, purpose):
        if schema is BoundaryDecision:
            return BoundaryDecision(
                source_id="three",
                independent=self.independent,
                title="2. Second" if self.independent else "",
                reason="A new chapter begins here."
                if self.independent
                else "The candidate starts with a continued derivation.",
            )
        if schema is Review:
            return Review(approved=True)
        primary = [
            json.loads(p["text"])
            for p in messages[1]["content"]
            if p["type"] == "text" and p["text"].startswith('{"source_id"')
        ]
        ids = [u["source_id"] for u in primary if u["role"] == "PRIMARY"]
        revision = purpose.rsplit(":", 1)[-1]
        return Draft(
            segments=[
                Segment(
                    kind="paragraph", text=f"Narration {ids[0]}, draft {revision}.", source_ids=ids
                )
            ],
            coverage=[Coverage(source_id=sid, disposition="narrated") for sid in ids],
        )


def test_boundary_candidates_and_cached_decisions(workspace):
    book, config = chapter_book(), chapter_config()
    chunks = plan_chunks(book, config.narration)
    assert [c.id for c in chapter_candidates(chunks)] == ["00003"]
    client = TestLLM()
    planner = ChapterPlanner(client, config, workspace)
    jobs = planner.plan(book, chunks)
    assert [[c.id for c in job.chunks] for job in jobs] == [["00001", "00002"], ["00003", "00004"]]
    assert client.calls == ["boundary:00003"]
    request = json.dumps(client.messages["boundary:00003"])
    assert "End of chapter one." in request and "Chapter 2. A new topic." in request
    config.narration.workers = 1
    assert planner.plan(book, chunks) == jobs
    assert client.calls == ["boundary:00003"]
    # A changed neighboring source invalidates the old decision.
    book.units[1].text = "Changed preceding source."
    assert len(planner.plan(book, chunks)) == 1  # One worker makes no new boundary requests.
    config.narration.workers = 2
    planner.plan(book, chunks)
    assert client.calls == ["boundary:00003", "boundary:00003"]


@pytest.mark.parametrize(
    "top_level, independent, workers", [(True, False, 2), (False, True, 2), (True, True, 1)]
)
def test_uncertain_missing_or_unrequested_boundaries_stay_sequential(
    workspace, top_level, independent, workers
):
    book, config = chapter_book(), chapter_config(workers=workers)
    if not top_level:
        book.units[2].heading_level = 2
    chunks = plan_chunks(book, config.narration)
    client = TestLLM(independent=independent)
    jobs = ChapterPlanner(client, config, workspace).plan(book, chunks)
    assert len(jobs) == 1 and jobs[0].chunks == chunks
    assert client.calls == (["boundary:00003"] if top_level and workers > 1 else [])


def test_wrong_boundary_id_is_rejected(workspace):
    class WrongID(TestLLM):
        def respond(self, messages, schema, purpose):
            return BoundaryDecision(
                source_id="not-the-candidate",
                independent=True,
                title="Other",
                reason="Wrong source.",
            )

    book, config = chapter_book(), chapter_config()
    with pytest.raises(ValueError, match="candidate source"):
        ChapterPlanner(WrongID(), config, workspace).plan(book, plan_chunks(book, config.narration))
    assert not list((workspace / "chapter-boundaries").glob("*.json"))


def add_toc(book):
    book.toc = SourceToc(
        kind="epub_ncx",
        entries=[
            TocEntry(title="First", level=1, target="one", source_id="one"),
            TocEntry(title="Subsection", level=2, target="two", source_id="two"),
            TocEntry(title="Second", level=1, target="three", source_id="three"),
        ],
    )


@pytest.mark.parametrize("kind", ["epub_ncx", "epub_nav", "pdf_outline"])
def test_candidates_prefer_toc_to_flat_heading_tags(kind):
    book, config = chapter_book(), chapter_config()
    for unit in book.units:
        unit.heading, unit.heading_level = unit.id, 1
    add_toc(book)
    book.toc.kind = kind
    chunks = plan_chunks(book, config.narration)
    assert len(chapter_candidates(chunks)) == 3
    assert [c.id for c in chapter_candidates(chunks, book)] == ["00003"]
    # The TOC does not need an accompanying h1 tag to nominate a boundary.
    book.units[2].heading_level = 2
    assert [c.id for c in chapter_candidates(chunks, book)] == ["00003"]
    book.toc.entries[2].selected = False
    assert chapter_candidates(chunks, book) == []


def test_candidates_do_not_split_chunks_or_promote_nested_toc_entries():
    book, config = chapter_book(), chapter_config()
    add_toc(book)
    # The top-level destination is inside the first chunk; retaining the whole
    # partition is safer than treating the unrelated h1 at unit three as a chapter.
    book.toc.entries[2].source_id = "two"
    config.narration.max_pdf_pages = 2
    chunks = plan_chunks(book, config.narration)
    assert len(chunks) == 2
    assert chapter_candidates(chunks, book) == []
    for entry in book.toc.entries:
        entry.source_id = None
    assert chapter_candidates(chunks, book) == chapter_candidates(chunks)


class BoundaryLLM(TestLLM):
    def respond(self, messages, schema, purpose):
        if schema is not BoundaryDecision:
            return super().respond(messages, schema, purpose)
        candidate = next(
            json.loads(part["text"])
            for part in messages[1]["content"]
            if part.get("type") == "text" and part["text"].startswith('{"role": "CANDIDATE')
        )
        return BoundaryDecision(
            source_id=candidate["source_id"],
            independent=True,
            title=candidate["heading_hint"],
            reason="Verified independent source start.",
        )


def test_boundary_checks_overlap_but_plan_stays_source_ordered(workspace):
    barrier, second_done = threading.Barrier(2), threading.Event()

    class ConcurrentChecks(BoundaryLLM):
        def respond(self, messages, schema, purpose):
            barrier.wait(timeout=5)
            if purpose == "boundary:00003":
                assert second_done.wait(timeout=5)
            else:
                second_done.set()
            return super().respond(messages, schema, purpose)

    book, config = chapter_book(), chapter_config()
    book.units[3].heading, book.units[3].heading_level = "Another major division", 1
    chunks = plan_chunks(book, config.narration)
    client = ConcurrentChecks()
    jobs = ChapterPlanner(client, config, workspace).plan(book, chunks)
    assert {"boundary:00003", "boundary:00004"} == set(client.calls)
    assert [job.id for job in jobs] == ["00001", "00003", "00004"]
    saved = read_json(workspace / "chapter-plan.json")
    assert [d["source_id"] for d in saved["boundaries"]] == ["three", "four"]


def test_interrupted_boundary_checks_reuse_finished_checks(workspace):
    barrier = threading.Barrier(2)

    class FailingChecks(BoundaryLLM):
        def respond(self, messages, schema, purpose):
            barrier.wait(timeout=5)
            if purpose == "boundary:00004":
                raise RuntimeError("Boundary provider failure")
            return super().respond(messages, schema, purpose)

    book, config = chapter_book(), chapter_config()
    book.units[3].heading, book.units[3].heading_level = "Another major division", 1
    chunks = plan_chunks(book, config.narration)
    with pytest.raises(RuntimeError, match="provider failure"):
        ChapterPlanner(FailingChecks(), config, workspace).plan(book, chunks)
    assert not (workspace / "chapter-plan.json").exists()
    assert len(list((workspace / "chapter-boundaries").glob("*.json"))) == 1
    client = BoundaryLLM()
    jobs = ChapterPlanner(client, config, workspace).plan(book, chunks)
    assert client.calls == ["boundary:00004"]
    assert [job.id for job in jobs] == ["00001", "00003", "00004"]


def test_toc_filter_preserves_legacy_partition_and_accepted_chapters(workspace):
    book, config = chapter_book(), chapter_config()
    book.units[1].heading, book.units[1].heading_level = "Subsection", 1
    chunks = plan_chunks(book, config.narration)
    # Reproduce the pre-filter partition and independent chapter contexts.
    jobs = ChapterPlanner(BoundaryLLM(), config, workspace).plan(book, chunks)
    narrator = Narrator(TestLLM(), config, workspace)
    for job in jobs:
        narrator._narrate_chapter(job, [], threading.Event())
    saved = {p: p.read_bytes() for p in (workspace / "narration").glob("*/accepted.json")}
    assert len(saved) == 4
    add_toc(book)
    assert [c.id for c in chapter_candidates(chunks, book)] == ["00003"]

    class NoRequests(TestLLM):
        def generate(self, *args, **kwargs):
            raise AssertionError("Compatible legacy jobs must keep their accepted context")

    for workers in (1, 3):
        config.narration.workers = workers
        assert ChapterPlanner(NoRequests(), config, workspace).plan(book, chunks) == jobs
        narrator = Narrator(NoRequests(), config, workspace)
        for job in jobs:
            narrator._narrate_chapter(job, [], threading.Event())
    assert all(p.read_bytes() == data for p, data in saved.items())

    # Matching section IDs alone are insufficient when the source partition differs.
    original = read_json(workspace / "chapter-plan.json")
    altered = read_json(workspace / "chapter-plan.json")
    altered["jobs"][0]["chunk_ids"] = ["other"]
    write_json(workspace / "chapter-plan.json", altered)
    filtered = ChapterPlanner(NoRequests(), config, workspace).plan(book, chunks)
    assert [job.id for job in filtered] == ["00001", "00003"]
    # Likewise never reuse an old boundary for a different model or evidence.
    write_json(workspace / "chapter-plan.json", original)
    config.llm.model = "different-model"
    client = BoundaryLLM()
    ChapterPlanner(client, config, workspace).plan(book, chunks)
    assert client.calls == ["boundary:00003"]


def test_truncated_boundary_check_stays_sequential_on_resume(workspace):
    class TruncatedBoundary(TestLLM):
        def respond(self, messages, schema, purpose):
            raise TruncatedResponse("Output limit")

    book, config = chapter_book(), chapter_config()
    chunks = plan_chunks(book, config.narration)
    client = TruncatedBoundary()
    planner = ChapterPlanner(client, config, workspace)
    jobs = planner.plan(book, chunks)
    assert len(jobs) == 1
    assert planner.plan(book, chunks) == jobs
    config.narration.workers = 1
    assert planner.plan(book, chunks) == jobs
    assert client.calls == ["boundary:00003"]
    decision = json.loads(next((workspace / "chapter-boundaries").glob("*.json")).read_text())
    assert not decision["independent"] and "truncated" in decision["reason"]


def test_boundary_evidence_is_not_truncated_to_fit_budget(workspace):
    book, config = chapter_book(), chapter_config()
    # Individually valid units whose combined boundary evidence exceeds the budget.
    book.units[1].text = "Previous chapter conclusion. " * 25
    book.units[2].text = "New chapter introduction. " * 25
    config.narration.max_source_chars = 1000
    chunks = plan_chunks(book, config.narration)
    client = TestLLM()
    planner = ChapterPlanner(client, config, workspace)
    jobs = planner.plan(book, chunks)
    assert len(jobs) == 1
    assert planner.plan(book, chunks) == jobs
    assert client.calls == []
    decision = json.loads(next((workspace / "chapter-boundaries").glob("*.json")).read_text())
    assert not decision["independent"] and "budget" in decision["reason"]
    # A deliberate input-budget change permits a new decision, using all the evidence.
    config.narration.max_source_chars = 2000
    assert len(planner.plan(book, chunks)) == 2
    assert client.calls == ["boundary:00003"]
    request = json.dumps(client.messages["boundary:00003"])
    assert book.units[1].text in request and book.units[2].text in request


def test_chapters_overlap_but_each_uses_accepted_context_and_output_stays_ordered(workspace):
    simultaneous = threading.Barrier(2)
    second_finished = threading.Event()

    class ParallelLLM(TestLLM):
        def respond(self, messages, schema, purpose):
            if purpose in {"narrate:00001:0", "narrate:00003:0"}:
                simultaneous.wait(timeout=5)  # Fails if the two chapters are run serially.
            if purpose == "narrate:00001:0":
                assert second_finished.wait(timeout=5)
            if purpose == "review:00001:0":
                return Review(
                    approved=False,
                    findings=[
                        Finding(
                            severity="error",
                            source_ids=["one"],
                            description="Explain the first definition.",
                        )
                    ],
                )
            if purpose == "review:00004:0":
                second_finished.set()
            if purpose == "narrate:00002:0":
                context = [
                    p["text"]
                    for p in messages[1]["content"]
                    if p["type"] == "text" and p["text"].startswith("Previously narrated")
                ]
                assert len(context) == 1
                assert "Narration one, draft 1." in context[0]
                assert "Narration one, draft 0." not in context[0]
                assert "Narration three" not in context[0]
            if purpose == "narrate:00003:0":
                assert not any(
                    p.get("text", "").startswith("Previously narrated")
                    for p in messages[1]["content"]
                )
            if purpose == "narrate:00004:0":
                assert "Narration three, draft 0." in json.dumps(messages)
            return super().respond(messages, schema, purpose)

    book, config = chapter_book(), chapter_config()
    chunks = plan_chunks(book, config.narration)
    client = ParallelLLM()
    result = Narrator(client, config, workspace).narrate(book, chunks)
    assert [s.source_ids for s in result.segments] == [["one"], ["two"], ["three"], ["four"]]
    assert client.calls.index("review:00004:0") < client.calls.index("review:00001:0")
    assert [c.source_id for c in result.coverage] == ["one", "two", "three", "four"]

    saved = {p: p.read_bytes() for p in (workspace / "narration").glob("*/accepted.json")}
    assert len(saved) == 4

    class NoRequests(TestLLM):
        def generate(self, *args, **kwargs):
            raise AssertionError("Changing worker count must not regenerate accepted chapters")

    # Returning to one worker also uses the verified chapter boundaries and caches.
    for workers in (1, 3):
        config.narration.workers = workers
        assert Narrator(NoRequests(), config, workspace).narrate(book, chunks) == result
    assert all(p.read_bytes() == data for p, data in saved.items())


def test_enabling_workers_preserves_sequential_prefix_and_partial_history(workspace):
    class FailFourth(TestLLM):
        def respond(self, messages, schema, purpose):
            if purpose == "review:00004:0":
                return Review(
                    approved=False,
                    findings=[
                        Finding(
                            severity="error",
                            source_ids=["four"],
                            description="Explain the last equation.",
                        )
                    ],
                )
            return super().respond(messages, schema, purpose)

    book, config = chapter_book(), chapter_config(workers=1, max_revisions=0)
    chunks = plan_chunks(book, config.narration)
    with pytest.raises(ValueError, match="Narration 00004 failed"):
        Narrator(FailFourth(), config, workspace).narrate(book, chunks)
    saved = {p: p.read_bytes() for p in (workspace / "narration").glob("*/accepted.json")}
    assert len(saved) == 3
    config.narration.workers = 2
    config.narration.max_revisions = 1
    client = TestLLM()
    result = Narrator(client, config, workspace).narrate(book, chunks)
    assert client.calls == ["boundary:00003", "narrate:00004:1", "review:00004:1"]
    assert len(result.coverage) == 4
    assert all(p.read_bytes() == data for p, data in saved.items())


def test_failed_chapter_preserves_other_completed_chapter_for_resume(workspace):
    other_done = threading.Event()

    class FailFirst(TestLLM):
        def respond(self, messages, schema, purpose):
            if purpose == "review:00001:0":
                assert other_done.wait(timeout=5)
                return Review(
                    approved=False,
                    findings=[
                        Finding(
                            severity="error",
                            source_ids=["one"],
                            description="Repair chapter one.",
                        )
                    ],
                )
            return super().respond(messages, schema, purpose)

    class NotifyingNarrator(Narrator):
        def _narrate_chapter(self, job, previous, stop):
            result = super()._narrate_chapter(job, previous, stop)
            if job.id == "00003":
                other_done.set()
            return result

    book, config = chapter_book(), chapter_config(max_revisions=0)
    chunks = plan_chunks(book, config.narration)
    with pytest.raises(ValueError, match="Narration 00001 failed"):
        NotifyingNarrator(FailFirst(), config, workspace).narrate(book, chunks)
    assert not (workspace / "narration.json").exists()
    saved = {p: p.read_bytes() for p in (workspace / "narration").glob("*/accepted.json")}
    assert len(saved) == 2
    config.narration.max_revisions = 1
    client = TestLLM()
    result = Narrator(client, config, workspace).narrate(book, chunks)
    assert client.calls == [
        "narrate:00001:1",
        "review:00001:1",
        "narrate:00002:0",
        "review:00002:0",
    ]
    assert len(result.coverage) == 4
    assert all(p.read_bytes() == data for p, data in saved.items())
