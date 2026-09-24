# Technical Audiobook Generator

Convert a technical PDF or EPUB into a detailed, listener-friendly M4B audiobook. It
adapts equations, code, figures, tables and explanatory footnotes for someone who cannot
see the page, while preserving substantive prose and worked examples. Both the language
model and speech model use independently configured OpenAI-compatible endpoints.

This Python command-line program works with independently configured model providers.
FFmpeg handles final audio encoding; local GPU inference is optional.

## Install and configure

Requires Python 3.11+ and `ffmpeg` / `ffprobe` on PATH. For example, install FFmpeg with
`brew install ffmpeg` on macOS or `sudo apt install ffmpeg` on Debian/Ubuntu.

```sh
uv venv
uv pip install -e '.[dev]'
cp audiobook.example.toml audiobook.toml
cp .env.example .env
```

Alternatively, create a virtual environment with `python -m venv .venv`, activate it,
and run `pip install -e '.[dev]'`.

Edit `audiobook.toml` to select your models, endpoints and voice. Set `LLM_API_KEY` and
`TTS_API_KEY` in `.env` or your environment. For a local endpoint that needs no key, leave
its key unset. Keys are never shared between endpoints or stored in checkpoints.
`api_key_env` can name different environment variables for each provider.

The LLM must accept text plus `image_url` content in `/chat/completions` and return JSON.
The TTS endpoint must implement `/audio/speech` and return audio bytes. Base URLs are
API roots, usually including `/v1`, not individual operation URLs. The model and voice
names are passed through as configured; there is no hardcoded list of providers.

```sh
.venv/bin/audiobook convert textbook.pdf --config audiobook.toml -o textbook.m4b
.venv/bin/audiobook convert practitioner-book.epub --config audiobook.toml -o practitioner-book.m4b
```

Start with a representative short chapter containing equations, code and figures. For a
PDF, `extraction.start_page` / `end_page` select **one-based physical pages**, inclusive.
Omit them for the whole book. Different models still need different context/output limits
and narration tuning; automated review is helpful but cannot guarantee mathematical or
visual correctness.

## How it works

```text
PDF pages / EPUB spine + images
              ↓
source units → bounded narration → source-backed review → repair if needed
              ↓                        ↓
        editable transcript       coverage and review records
              ↓
      cached speech clips → uniform PCM → one AAC encode → chaptered M4B
```

PDFs are rendered to images alongside their text layer, including scanned pages and
vector diagrams. EPUBs use the package spine rather than archive order or filename
guesses; code, MathML, tables, images and linked footnotes retain their semantic context.
No PDF native-file API is needed. DRM-protected EPUBs and locked PDFs need an accessible
input copy. External EPUB images are not fetched; missing or unsupported visual assets
produce an actionable error instead of disappearing from the audiobook.

The model sees small primary batches and neighboring context. Only primary source IDs
may be narrated. It must account for every source unit, including explicit reasons for
omissions. The review pass compares the actual draft to the same source evidence and
requests bounded revisions. Truncated output triggers smaller batches; a single unit
that still exceeds limits stops with guidance. Oversized input units are never silently
truncated. Cross-batch paragraph continuations are joined before speech synthesis.

The built-in narration policy includes:

- Spoken math with scope and pauses, followed by explanations of displayed equations.
- Code explained in logical order, retaining syntax when the API or language is the point.
- Figure captions and descriptions grounded in visual evidence and nearby text.
- Tables explained through their structure, comparisons and meaningful values.
- Useful footnotes integrated at their references, plus coherent cross-page paragraphs.
- Original chapter titles for navigation, separate from their spoken pronunciation.

Homework, bibliographies, citations, indexes, running page furniture, printed contents
lists and publisher boilerplate are omitted by default. Worked examples stay. Set
`narration.include_exercises = true` to include homework. `narration.instructions_file`
adds book-specific directions. The pronunciation glossary guides the model and is also
applied to TTS text as exact, case-sensitive whole-term replacements.

## Inspect, review, edit, and resume

Offline extraction needs no credentials, model settings or FFmpeg:

```sh
.venv/bin/audiobook convert textbook.pdf -o textbook.m4b --until extract
```

It writes `textbook.work/plan.json`, including source sizes, images and the initial
request count. Review/repair, schema repair, adaptive splitting and network retries
can increase requests; the plan is not a price quote.

Generate and inspect narration before spending on TTS:

```sh
.venv/bin/audiobook convert textbook.pdf -c audiobook.toml -o textbook.m4b --until narrate
# Review textbook.work/narration.txt or edit the spoken text in narration.json.
.venv/bin/audiobook synthesize textbook.work/narration.json -c audiobook.toml -o textbook.m4b
```

`narration.json` is the editable source for `synthesize`; `narration.txt` is a readable
export, not a TTS input protocol. Keep the JSON structure intact. A `heading` segment's
`display_title` controls navigation; its `text` is spoken. The `synthesize` command does
not call the LLM or re-review your edits. Running `convert` again reconstructs the
transcript from its narration cache, so use `synthesize` to retain manual transcript edits.

The default workspace is the output path with `.m4b` replaced by `.work`. Override it
with `--work-dir`. Rerun the same command after interruption: validated narration and
speech clips are reused. Content and relevant configuration participate in cache keys;
changing voice regenerates speech without regenerating narration. An unchanged completed
M4B is reused. A different existing output requires `--force` or a new output path.
Only one conversion can use a workspace at a time.

If a section exhausts `narration.max_revisions`, raise that limit and rerun the same
command. The revision budget is not part of the narration cache identity: accepted
sections are reused, and the failed section continues from its saved draft/review history.
This also works with checkpoints created before this resume fix. Other source, model,
prompt and context changes still invalidate the affected narration. Logs distinguish
`Processing section` (checking a checkpoint) from `Reusing narration` and `Narrating section`
(making a new model request).

## Concurrent chapter narration

To narrate independent chapters concurrently, set this in your TOML configuration:

```toml
[narration]
workers = 4
```

The default is `1`. PDF top-level outline entries and EPUB top-level headings provide
candidate chapter starts. Before launching independent jobs, the LLM examines the source
on both sides of each candidate, including page images, to check that a new chapter starts
cleanly and no paragraph, equation, code listing, table or footnote continues across it.
A chapter beginning partway through a PDF page cannot be separated by this version.
Uncertain boundaries stay in the preceding sequential job. Books without suitable
chapter candidates stay sequential; no artificial page ranges are treated as chapters.

Each worker narrates one chapter job at a time. Its sections still receive the preceding
accepted narration and undergo the same review/revision checks. Newly independent
chapters start without preceding generated narration; neighboring **source** context and
the same narration policy and pronunciation glossary remain available. Completed jobs
are assembled in book order, even when later chapters finish first.

`plan.json` lists chapter candidates during offline extraction. Boundary checks happen
only during narration, add at most one initial logical request per uncached candidate
(API/schema retries can add calls), and are saved in `chapter-boundaries/`.
`chapter-plan.json` records verified boundaries, rejected candidates and the resulting
jobs. Boundary validation is a model judgment, not a guarantee; inspect its reasons and
review representative output when choosing a model. Inconclusive checks (including
truncated responses or evidence exceeding the input budget) are cached as rejected
boundaries too, so a restart does not silently change the chapter partition.

Worker count does not invalidate narration. When enabling workers in a partially narrated
book, chapters already started sequentially retain their original accepted context and
checkpoints. Switching back to one worker reuses cached boundary decisions and serializes
the same jobs. If a chapter fails, queued jobs are cancelled and active workers stop between
requests; completed sections in all chapters remain reusable. No final transcript or
audiobook is assembled from incomplete jobs.

Long chapters still run sequentially internally, so speedup depends on chapter sizes and
provider rate limits. Speculative lookahead within chapters is tracked separately in
[Upcoming features](UPCOMING_FEATURES.md).

## Workspace artifacts

Useful workspace files:

| File | Purpose |
| --- | --- |
| `source.json`, `source/assets/` | Source evidence and page/figure images |
| `plan.json` | Ordered source batches and initial request estimate |
| `chapter-boundaries/`, `chapter-plan.json` | Cached source checks and independent narration jobs |
| `narration/*/draft-*.json`, `review-*.json` | Inspectable draft/review history |
| `narration.json`, `narration.txt` | Final adaptation and human-readable export |
| `requests.jsonl` | Request purposes and provider-reported usage; no API keys |
| `audio/`, `audio.json` | Verified speech clips and ordered audio plan |
| `chapters.ffmeta`, `output.json` | Chapter timing and final artifact verification |

These files contain book content. The configured endpoints receive the relevant book
text/images or spoken text. The program does not send them to other services. Keep your
workspace if you want resume; normalized mono PCM uses roughly 173 MB per hour at 24 kHz,
in addition to page images, drafts and the M4B. No silent clips are substituted for failed
speech calls, and a failed source review stops the conversion before TTS.

## Provider compatibility and audio controls

- `llm.json_mode = "prompt"` omits `response_format` for servers without JSON mode;
  responses are still schema-validated. JSON mode is the default.
- Set `llm.token_parameter = "max_completion_tokens"` when required by your model.
  No temperature is sent unless configured. Increase `max_output_tokens` for verbose
  mathematical/code sections or models whose reasoning uses the same token budget.
- `llm.extra_body` and `tts.extra_body` pass provider-specific fields without overriding
  the core request. Neither function-calling support nor vendor-specific SDKs are required.
- `LLM_BASE_URL`, `LLM_MODEL`, `TTS_BASE_URL`, `TTS_MODEL`, and `TTS_VOICE` override TOML.
  Shell variables take precedence over `.env`. File references are relative to the TOML.
- `tts.max_chars` is enforced after pronunciation replacement, splitting at paragraphs,
  sentences or words. Choose a limit your endpoint supports. Speech requests run with
  bounded `tts.workers`; `narration.workers` independently controls chapter concurrency.
  Sections within each chapter stay sequential.
- WAV, MP3, FLAC, Opus and AAC responses are normalized to mono 16-bit PCM. Raw `pcm`
  responses are assumed little-endian signed 16-bit mono at `tts.pcm_sample_rate`.
- Set `tts.instructions` only when your model supports it. `tts.speed` is a request to
  the provider. `audio.bitrate`, heading/chapter pauses and table-of-contents depth
  control final assembly. Optional `book.title`, `book.author`, and `book.cover` override
  source metadata. EPUB covers and the first physical PDF page are used automatically.
- HTTP timeouts, rate limits and server errors have bounded retries. Authentication and
  unsupported-parameter failures stop immediately. Invalid audio is kept as `.invalid`
  for diagnosis; rerunning requests a replacement.

Clips are concatenated in source order as uniform PCM and encoded to AAC **once**. Chapter
times are computed from sample counts, including pauses, rather than rounded per-clip
timestamps. The completed M4B is checked with ffprobe for its audio stream, duration and
chapter boundaries, then atomically installed at the output path.

## Development

```sh
.venv/bin/pytest
.venv/bin/ruff check src tests
```

Tests use synthetic books and mocked compatible endpoints, with real FFmpeg integration
when available. No paid model calls are required. A real vision/TTS provider and listening
review of a representative chapter are still needed to evaluate narration quality.
