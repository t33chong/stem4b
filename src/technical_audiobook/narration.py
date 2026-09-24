import base64
import json
import logging
from pathlib import Path

from .api import LLMClient, TruncatedResponse
from .chunking import Chunk
from .config import Config
from .models import Book, Draft, Review, Segment, Transcript
from .prompts import NARRATION_POLICY, REVIEW_POLICY
from .storage import asset_path, atomic_text, digest, read_json, write_json

log = logging.getLogger(__name__)
NARRATION_VERSION = 1


def check_coverage(draft: Draft, chunk: Chunk):
    primary = {unit.id for unit in chunk.units}
    covered = [item.source_id for item in draft.coverage]
    if len(covered) != len(set(covered)) or set(covered) != primary:
        raise ValueError(
            f"Coverage must list exactly these source IDs once each: {sorted(primary)}"
        )
    cited = {sid for segment in draft.segments for sid in segment.source_ids}
    if cited - primary:
        raise ValueError(f"Segments cite non-primary source IDs: {sorted(cited - primary)}")
    for item in draft.coverage:
        if item.disposition == "omitted":
            if not item.reason or item.source_id in cited:
                raise ValueError("Omitted units need an explanation and cannot appear in narration")
        elif item.source_id not in cited:
            raise ValueError(f"{item.source_id} claims narration but no segment cites it")
    for index, segment in enumerate(draft.segments):
        if segment.continues_previous and index != 0:
            raise ValueError("Only the FIRST segment may continue the previous batch")


def append_segments(previous: list[Segment], incoming: list[Segment]) -> list[Segment]:
    result = [segment.model_copy(deep=True) for segment in previous]
    for segment in incoming:
        if segment.continues_previous:
            candidates = []
            for index in range(len(result) - 1, -1, -1):
                if result[index].kind == "heading":
                    break
                if result[index].kind == "paragraph":
                    candidates.append(index)
                    break
            if not candidates:
                raise ValueError("Continuation has no preceding prose paragraph in this section")
            preceding = result[candidates[0]]
            # A discretionary line-break hyphen should not become a spoken dash.
            if preceding.text.endswith(("-", "\u00ad")) and segment.text[:1].islower():
                preceding.text = preceding.text[:-1] + segment.text
            else:
                preceding.text = preceding.text.rstrip() + " " + segment.text.lstrip()
            preceding.source_ids = list(dict.fromkeys(preceding.source_ids + segment.source_ids))
        else:
            result.append(segment.model_copy(deep=True))
    return result


def source_content(chunk: Chunk, work: Path, config: Config, previous: list[Segment]) -> list[dict]:
    content = [
        {
            "type": "text",
            "text": "PRIMARY source units to narrate: " + ", ".join(u.id for u in chunk.units),
        }
    ]

    def add_unit(unit, role, context=False, tail=False):
        source_text = unit.text
        if context:
            limit = config.narration.context_chars
            source_text = (source_text[-limit:] if tail else source_text[:limit]) if limit else ""
        evidence = {
            "source_id": unit.id,
            "role": role,
            "location": unit.location,
            "outline_hint": unit.heading,
            "text": source_text,
        }
        content.append({"type": "text", "text": json.dumps(evidence, ensure_ascii=False)})
        for image in unit.images[:1] if context else unit.images:
            payload = base64.b64encode(asset_path(work, image.path).read_bytes()).decode()
            content.append({"type": "text", "text": f"{role} image for {unit.id}: {image.label}"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image.media_type};base64,{payload}",
                        "detail": "high",
                    },
                }
            )

    if chunk.before and config.narration.context_chars:
        add_unit(chunk.before, "CONTEXT ONLY, previous", context=True, tail=True)
    for unit in chunk.units:
        add_unit(unit, "PRIMARY")
    if chunk.after and config.narration.context_chars:
        add_unit(chunk.after, "CONTEXT ONLY, next", context=True)
    limit = config.narration.context_chars
    if previous and limit:
        content.append(
            {
                "type": "text",
                "text": "Previously narrated ending, DO NOT repeat:\n"
                + json.dumps([s.model_dump() for s in previous[-3:]], ensure_ascii=False)[-limit:],
            }
        )
    return content


class Narrator:
    def __init__(self, client: LLMClient, config: Config, work: Path):
        self.client, self.config, self.work = client, config, work
        self.policy = NARRATION_POLICY
        if config.narration.include_exercises:
            self.policy += (
                "\nExercise policy: Include and narrate exercises and their instructions.\n"
            )
        else:
            self.policy += (
                "\nExercise policy: Omit assigned homework exercises, but retain worked examples.\n"
            )
        self.policy += "\nPronunciation glossary:\n" + json.dumps(config.pronunciations)
        if config.narration.instructions_file:
            self.policy += "\nBook-specific instructions:\n" + Path(
                config.narration.instructions_file
            ).read_text(encoding="utf-8")

    def _cache_directory(self, chunk: Chunk, previous: list[Segment]) -> Path:
        narration_options = self.config.narration.model_dump(exclude={"max_revisions"})
        context = [u.model_dump() if u else None for u in [chunk.before, chunk.after]]
        inputs = [
            NARRATION_VERSION,
            self.policy,
            REVIEW_POLICY,
            self.config.llm.model_dump(),
            narration_options,
            [u.model_dump() for u in chunk.units],
            context,
            [s.model_dump() for s in previous[-3:]],
        ]

        def directory_for(options: dict) -> Path:
            key = digest([*inputs[:4], options, *inputs[5:]])
            return self.work / "narration" / f"{chunk.id}-{key[:20]}"

        directory = directory_for(narration_options)
        candidates = [directory]
        # Original checkpoints included max_revisions (then restricted to 0–5).
        # Reconstruct their exact keys, preserving every source/prompt/model/context
        # check. Never reuse an arbitrary cache just because its section ID matches.
        for limit in range(6):
            candidates.append(directory_for({**narration_options, "max_revisions": limit}))
        for candidate in candidates:
            if (candidate / "accepted.json").is_file():
                return candidate

        def progress(candidate: Path) -> tuple[int, bool]:
            revisions = [
                int(path.stem.removeprefix("draft-"))
                for path in candidate.glob("draft-*.json")
                if path.stem.removeprefix("draft-").isdigit()
            ]
            latest = max(revisions, default=-1)
            return latest, (candidate / f"review-{latest}.json").is_file()

        # Keep using legacy files in place, including unfinished draft/review history.
        # Raising the budget then resumes at the next revision instead of starting over.
        return max(candidates, key=progress)

    def chunk(self, chunk: Chunk, previous: list[Segment]) -> Draft:
        directory = self._cache_directory(chunk, previous)
        directory.mkdir(parents=True, exist_ok=True)
        final = directory / "accepted.json"
        if final.exists():
            draft = Draft.model_validate(read_json(final))
            check_coverage(draft, chunk)
            append_segments(previous, draft.segments)
            log.info("Reusing narration %s", chunk.id)
            return draft

        def validate(draft: Draft):
            check_coverage(draft, chunk)
            append_segments(previous, draft.segments)

        def validate_review(review: Review):
            valid_ids = {unit.id for unit in chunk.units}
            if any(set(finding.source_ids) - valid_ids for finding in review.findings):
                raise ValueError("Review findings must cite only primary source IDs")

        content = source_content(chunk, self.work, self.config, previous)
        feedback = None
        try:
            for revision in range(self.config.narration.max_revisions + 1):
                draft_file = directory / f"draft-{revision}.json"
                review_file = directory / f"review-{revision}.json"
                if draft_file.exists():
                    draft = Draft.model_validate(read_json(draft_file))
                    validate(draft)
                else:
                    log.info("Narrating section %s, draft %s", chunk.id, revision)
                    messages = [
                        {"role": "system", "content": self.policy},
                        {"role": "user", "content": content},
                    ]
                    if feedback:
                        messages.extend(
                            [
                                {"role": "assistant", "content": draft.model_dump_json()},
                                {
                                    "role": "user",
                                    "content": "Revise the complete narration to fix these source-grounded review findings:\n"
                                    + feedback.model_dump_json(),
                                },
                            ]
                        )
                    draft = self.client.generate(
                        messages, Draft, f"narrate:{chunk.id}:{revision}", validate
                    )
                    write_json(draft_file, draft)
                if not self.config.narration.review:
                    write_json(final, draft)
                    return draft
                cached_review = review_file.exists()
                if cached_review:
                    feedback = Review.model_validate(read_json(review_file))
                    validate_review(feedback)
                else:
                    feedback = self.client.generate(
                        [
                            {
                                "role": "system",
                                "content": REVIEW_POLICY + "\nNarration policy:\n" + self.policy,
                            },
                            {
                                "role": "user",
                                "content": content
                                + [
                                    {
                                        "type": "text",
                                        "text": "DRAFT TO REVIEW:\n" + draft.model_dump_json(),
                                    }
                                ],
                            },
                        ],
                        Review,
                        f"review:{chunk.id}:{revision}",
                        validate_review,
                    )
                    write_json(review_file, feedback)
                if feedback.approved:
                    draft.uncertainties.extend(f.description for f in feedback.findings)
                    write_json(final, draft)
                    return draft
                log.log(
                    logging.DEBUG if cached_review else logging.WARNING,
                    "Narration %s needs revision %s: %s",
                    chunk.id,
                    revision + 1,
                    "; ".join(f.description for f in feedback.findings),
                )
        except TruncatedResponse as exc:
            if len(chunk.units) < 2:
                raise ValueError(
                    f"{chunk.units[0].location}: model output is still truncated for one source unit. "
                    "Increase llm.max_output_tokens or use a model with a larger output budget."
                ) from exc
            log.warning("Splitting %s because the model reached its output limit", chunk.id)
            mid = len(chunk.units) // 2
            left = Chunk(chunk.id + "a", chunk.units[:mid], chunk.before, chunk.units[mid])
            right = Chunk(chunk.id + "b", chunk.units[mid:], chunk.units[mid - 1], chunk.after)
            first = self.chunk(left, previous)
            second = self.chunk(right, append_segments(previous, first.segments))
            # Keep continuation flags for appending against the preceding whole-book transcript.
            # Internal continuations are resolved here; an initial continuation remains external.
            first_segments = [s.model_copy(deep=True) for s in first.segments]
            second_segments = [s.model_copy(deep=True) for s in second.segments]
            # A left half containing only omitted content or figures can still be
            # followed by a continuation of prose from the PREVIOUS parent batch.
            if second_segments and second_segments[0].continues_previous:
                recent = []
                for segment in reversed(first_segments):
                    if segment.kind == "heading":
                        break
                    recent.append(segment.kind)
                if "paragraph" not in recent:
                    continuation = second_segments.pop(0)
                    first_segments.insert(0, continuation)
            external = bool(first_segments and first_segments[0].continues_previous)
            if external:
                first_segments[0].continues_previous = False
            combined = append_segments(first_segments, second_segments)
            if external:
                combined[0].continues_previous = True
            draft = Draft(
                segments=combined,
                coverage=first.coverage + second.coverage,
                uncertainties=first.uncertainties + second.uncertainties,
            )
            validate(draft)
            write_json(final, draft)
            return draft
        raise ValueError(
            f"Narration {chunk.id} failed source review after {self.config.narration.max_revisions} revisions. "
            f"Inspect {directory}; no incomplete audiobook will be packaged."
        )

    def narrate(self, book: Book, chunks: list[Chunk]) -> Transcript:
        segments: list[Segment] = []
        coverage, warnings = [], list(book.warnings)
        if not self.config.narration.review:
            warnings.append("Source review was disabled for this transcript.")
        for index, chunk in enumerate(chunks, 1):
            log.info(
                "Processing section %s/%s (%s–%s)",
                index,
                len(chunks),
                chunk.units[0].id,
                chunk.units[-1].id,
            )
            draft = self.chunk(chunk, segments)
            segments = append_segments(segments, draft.segments)
            coverage.extend(draft.coverage)
            warnings.extend(draft.uncertainties)
        all_ids = [item.source_id for item in coverage]
        if len(all_ids) != len(set(all_ids)) or set(all_ids) != {u.id for u in book.units}:
            raise ValueError("Whole-book coverage verification failed")
        if not segments:
            raise ValueError("The model omitted the entire selected source; nothing to narrate")
        transcript = Transcript(
            title=book.title,
            author=book.author,
            source_sha256=book.source_sha256,
            segments=segments,
            coverage=coverage,
            warnings=warnings,
            cover=book.cover,
        )
        write_json(self.work / "narration.json", transcript)
        export_text(transcript, self.work / "narration.txt")
        return transcript


def export_text(transcript: Transcript, target: Path):
    # Display heading text is not mixed into the actual spoken segment text.
    parts = [f"Title: {transcript.title}\nAuthor: {transcript.author}"]
    for segment in transcript.segments:
        if segment.kind == "heading":
            parts.append(f"{'#' * segment.heading_level} {segment.display_title}\n{segment.text}")
        else:
            parts.append(segment.text)
    atomic_text(target, "\n\n".join(parts) + "\n")
