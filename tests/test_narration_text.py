import pytest

from stem4b.audio import speech_plan
from stem4b.config import AudioConfig, Config, TTSConfig
from stem4b.models import Asset, Segment, Transcript
from stem4b.narration_text import (
    FORMAT,
    legacy_text,
    load_script,
    parse_text,
    render_text,
    save_narration,
)
from stem4b.storage import write_json


@pytest.fixture
def baseline():
    return Transcript(
        title="Signals & Systems",
        author="Test Author",
        source_sha256="original-source",
        coverage=[],
        cover=Asset(path="source/cover.png", sha256="cover", label="Cover"),
        segments=[
            Segment(kind="paragraph", text="Preface. Welcome to the book.", source_ids=["p1"]),
            Segment(
                kind="heading",
                text="Chapter one. Signals.",
                display_title="1. Signals",
                heading_level=1,
                source_ids=["p1"],
            ),
            Segment(kind="paragraph", text="NumPy processes signals.", source_ids=["p1"]),
            Segment(kind="paragraph", text="A signal carries information.", source_ids=["p1"]),
            Segment(
                kind="heading",
                text="Section one point one. Voltage.",
                display_title="1.1. Voltage",
                heading_level=2,
                source_ids=["p2"],
            ),
            Segment(kind="equation", text="V equals I times R.", source_ids=["p2"]),
            Segment(kind="figure", text="Figure one depicts a circuit.", source_ids=["p2"]),
            Segment(kind="table", text="Table one lists its measurements.", source_ids=["p2"]),
            Segment(kind="code", text="Run the program twice.", source_ids=["p2"]),
            Segment(
                kind="footnote", text="Footnote. A useful detail. End footnote.", source_ids=["p2"]
            ),
        ],
    )


@pytest.mark.parametrize("max_chars", [32, 73, 2500])
@pytest.mark.parametrize("toc_depth", [1, 2, 6])
def test_roundtrip_preserves_exact_speech_chunks_keys_titles_and_pauses(
    baseline, max_chars, toc_depth
):
    config = Config(
        pronunciations={"NumPy": "numb pie"},
        tts=TTSConfig(max_chars=max_chars),
        audio=AudioConfig(toc_depth=toc_depth),
    )
    script = parse_text(render_text(baseline))
    assert speech_plan(script, config) == speech_plan(baseline, config)
    assert script.title == baseline.title and script.author == baseline.author
    assert not hasattr(script, "coverage")
    assert all(not hasattr(segment, "source_ids") for segment in script.segments)


def test_roundtrip_preserves_unusual_literal_text_and_multiline_headings(baseline):
    baseline.title = "Title\nwith a newline and \\backslash"
    baseline.author = "Author\rName"
    baseline.segments[0].text = "# Not a heading\n\\n is a literal escape.\n\n\nA blank line."
    baseline.segments[1].display_title = "1. Signals\nAnd systems \\ #1"
    baseline.segments[
        1
    ].text = "A spoken heading.\n\n\t\n# With a literal hash.\r\nAnd another line."
    baseline.segments[2].text = "First paragraph.\n\nSecond paragraph.\n\\ is a backslash."
    config = Config(tts=TTSConfig(max_chars=51))
    encoded = render_text(baseline)
    assert speech_plan(parse_text(encoded), config) == speech_plan(baseline, config)
    # Line-ending and BOM changes made by an editor don't change speech or escaping.
    assert speech_plan(parse_text("\ufeff" + encoded.replace("\n", "\r\n")), config) == speech_plan(
        baseline, config
    )


def test_legacy_export_and_edited_legacy_text(workspace, baseline):
    write_json(workspace / "narration.json", baseline)
    target = workspace / "narration.txt"
    target.write_text(legacy_text(baseline))
    script = load_script(target)
    assert speech_plan(script, Config()) == speech_plan(baseline, Config())
    assert script.cover == baseline.cover
    target.write_text(target.read_text().replace("NumPy processes signals.", "Edited speech."))
    script = load_script(target)
    speech = " ".join(p.text for c in speech_plan(script, Config()) for p in c.parts)
    assert "Edited speech." in speech and "NumPy" not in speech
    assert script.cover == baseline.cover


def test_legacy_unedited_ambiguous_export_uses_exact_baseline_match(workspace, baseline):
    baseline.segments[0].text = "# A literal line, not a heading"
    baseline.segments[1].text = "Spoken heading.\n\nContinued heading."
    write_json(workspace / "narration.json", baseline)
    target = workspace / "narration.txt"
    target.write_text(legacy_text(baseline))
    assert speech_plan(load_script(target), Config()) == speech_plan(baseline, Config())


def test_standalone_text_and_explicit_json_input(workspace, baseline):
    target = workspace / "custom.txt"
    target.write_text(render_text(baseline))
    assert load_script(target).cover is None
    assert speech_plan(load_script(target), Config()) == speech_plan(baseline, Config())
    write_json(workspace / "custom.json", baseline)
    target.write_text("A broken text file must not affect explicit JSON input.")
    assert load_script(workspace / "custom.json") == baseline
    with pytest.raises(ValueError, match=".txt or .json"):
        load_script(workspace / "custom.md")


@pytest.mark.parametrize(
    "text, message",
    [
        ("Only prose", "Title: and Author:"),
        ("Title: \nAuthor: Author\n\nProse", "must not be empty"),
        ("Title: Book\nAuthor: Author\nUnknown: metadata\n\nProse", "blank line"),
        ("Title: Book\nAuthor: Author\nFormat: unknown\n\nProse", "unsupported Format"),
        ("Title: Book\nAuthor: Author\n\n", "no speech"),
        ("Title: Book\nAuthor: Author\n\n# Heading\n\nProse", "needs spoken text"),
        ("Title: Book\nAuthor: Author\n\n####### Too deep\nSpeech", "1–6"),
        ("Title: Book\nAuthor: Author\n\n# Missing speech\n# Next heading\nSpeech", "blank line"),
        (f"Title: Book\nAuthor: Author\n{FORMAT}\n\nBad \\escape", "Invalid text escape"),
    ],
)
def test_invalid_scripts_fail_before_speech(text, message):
    with pytest.raises(ValueError, match=message):
        parse_text(text)


def test_preserves_manual_edits_with_unchanged_baseline(workspace, baseline):
    save_narration(baseline, workspace)
    text_path, json_path = workspace / "narration.txt", workspace / "narration.json"
    original_json = json_path.read_bytes()
    edited = text_path.read_text().replace("NumPy processes signals.", "My corrected explanation.")
    text_path.write_text(edited)
    save_narration(baseline, workspace)
    assert text_path.read_text() == edited
    assert json_path.read_bytes() == original_json
    assert any("My corrected explanation." in s.text for s in load_script(text_path).segments)


def test_changed_baseline_cannot_overwrite_manual_edits(workspace, baseline):
    save_narration(baseline, workspace)
    text_path, json_path = workspace / "narration.txt", workspace / "narration.json"
    text_path.write_text(
        text_path.read_text().replace("NumPy processes signals.", "My correction.")
    )
    original = {p: p.read_bytes() for p in [text_path, json_path]}
    baseline.segments[-1].text = "Newly generated footnote."
    with pytest.raises(
        ValueError, match="Neither narration.txt nor narration.json was overwritten"
    ):
        save_narration(baseline, workspace)
    assert all(p.read_bytes() == data for p, data in original.items())
    # Moving the edited file aside is the explicit way to request a fresh export.
    text_path.rename(workspace / "my-edited-narration.txt")
    save_narration(baseline, workspace)
    assert "Newly generated footnote." in text_path.read_text()
    assert "My correction." in (workspace / "my-edited-narration.txt").read_text()


@pytest.mark.parametrize("legacy", [False, True])
def test_unedited_exports_can_be_updated(workspace, baseline, legacy):
    save_narration(baseline, workspace)
    target = workspace / "narration.txt"
    if legacy:
        target.write_text(legacy_text(baseline))
    baseline.segments[-1].text = "Revised footnote."
    save_narration(baseline, workspace)
    assert target.read_text() == render_text(baseline)
    assert speech_plan(load_script(target), Config()) == speech_plan(baseline, Config())


def test_legacy_edited_export_is_not_migrated_or_overwritten(workspace, baseline):
    write_json(workspace / "narration.json", baseline)
    target = workspace / "narration.txt"
    edited = legacy_text(baseline).replace("NumPy processes signals.", "Edited speech.")
    target.write_text(edited)
    save_narration(baseline, workspace)
    assert target.read_text() == edited


def test_missing_baseline_and_interrupted_publish_are_safe(workspace, baseline):
    target = workspace / "narration.txt"
    target.write_text(render_text(baseline).replace("NumPy processes signals.", "Edited speech."))
    with pytest.raises(ValueError, match="baseline has changed or is missing"):
        save_narration(baseline, workspace)
    assert not (workspace / "narration.json").exists()
    assert "Edited speech." in target.read_text()
    # Recover a publish interrupted after the new text, but before the baseline JSON.
    target.write_text(render_text(baseline))
    save_narration(baseline, workspace)
    assert load_script(workspace / "narration.json") == baseline
