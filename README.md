# Technical Audiobook Generator

Convert a technical book or research paper in PDF or EPUB into a detailed, listener-friendly
M4B audiobook. It adapts equations, code, figures, tables and explanatory footnotes for someone who cannot
see the page, while preserving substantive prose and worked examples. Both the language
model and speech model use independently configured OpenAI-compatible endpoints.
Both request paths use the official OpenAI Python SDK.

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

## Research papers and technical reports

Use the same conversion pipeline with an explicit paper policy:

```sh
.venv/bin/audiobook convert papers/2505.24832v3.pdf -c audiobook.toml \
  --document-type paper -o papers/2505.24832v3.m4b --until narrate
# Review papers/2505.24832v3.work/narration.txt, then generate the audio:
.venv/bin/audiobook synthesize papers/2505.24832v3.work/narration.txt \
  -c audiobook.toml -o papers/2505.24832v3.m4b
```

Omit `--until narrate` to produce the M4B in one command. Alternatively, set
`document_type = "paper"` in the `[narration]` section of your TOML; the CLI flag overrides
that setting. The default remains `book`, without guessing from filenames or directories.

Paper mode instructs both narration and source review to:

- Read the title, first author's name plus “et al.” for multiple named authors, and all
  distinct affiliated institutions shown in the source. A single author or collective/team
  author keeps their printed name; missing affiliations are not guessed. Omit the coauthor
  roll call, email addresses, affiliation markers and contribution footnotes.
- Keep the full abstract and substantive paper, including related work, methods, results,
  limitations, proofs and technical appendices. Figures, code, equations, tables and useful
  footnotes receive the same detailed treatment as in books; this is not a paper summary.
- Omit reference lists and their headings, but retain meaningful comparisons/attributions
  in the prose. References do not mark the end of the document: technical material after
  them stays, including on pages shared with bibliography entries.
- Use “Section one” rather than “Chapter one” for numbered top-level sections, and preserve
  appendix labels such as “Appendix A” and “Section A point one”.

All pages and page images still reach the model; no heuristic cuts off the document at a
References heading. Coverage records account for reference-only pages as intentional
omissions. The final TOC pass excludes reference-list entries even on partially narrated
pages, and paper title/credit blocks are not discarded as book front matter. Existing
column-layout, source-review, revision and resume safeguards still apply. Keep
`narration.review = true` (the default), and inspect the narration before synthesis;
author/affiliation selection and bibliography omission rely on the model's source reading.

Existing book caches stay compatible. Changing between book and paper policies requires
new narration/review, so choose the mode before starting and use it on every `convert`
resume. `synthesize` uses the already edited text and does not need the flag. If the PDF
lacks title/author metadata, `[book].title` and `[book].author` still override the M4B tags;
those metadata fields are not themselves spoken. Remove any book-specific page range from
your configuration when converting an entire paper.

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
# Review and edit textbook.work/narration.txt in your text editor.
.venv/bin/audiobook synthesize textbook.work/narration.txt -c audiobook.toml -o textbook.m4b
```

`narration.txt` is the canonical spoken-text source for conversion and text-file synthesis.
`--until narrate` returns its path. The text is parsed deterministically: no LLM rewrites
or re-reviews your edits. Pronunciation replacements, speech chunking, chapter navigation
and heading/chapter pauses still apply. An unedited export produces the same speech
requests and cache keys as the generated JSON transcript.

The text format is intentionally small, not general-purpose Markdown:

```text
Title: Signals & Information
Author: Test Author
Format: audiobook-text-v1

# 1. Signals
Chapter one. Signals.

A signal carries information. Edit the spoken prose here.

## 1.1. Amplitude
Section one point one. Amplitude.

Amplitude is measured in volts.
```

- The first three lines are metadata and are not spoken. Keep a blank line after them.
- One to six `#` characters mark a navigation heading and its depth. The display title
  after `#` is not spoken; the following nonblank block is its spoken wording. Keep that
  wording immediately below the heading, then a blank line before ordinary prose.
- All other body text is spoken. Blank lines separate paragraphs; there are no comments,
  hidden instructions, or general Markdown formatting to be stripped automatically.
- In the versioned format, write a literal backslash as `\\` and a literal leading `#`
  as `\#`. Exports escape these automatically. A line containing only `\` preserves a
  blank line *inside* a spoken heading. Metadata can use `\n` for embedded newlines.
  Leave these escapes intact unless you intend to change the corresponding text.

Keep `narration.json` beside `narration.txt`: it remains the generated baseline with source
coverage, review warnings and cover metadata. Edits to the text are not attributed to
source units or represented as having passed the original review. A standalone `.txt`
script also works; an optional same-stem `.json` supplies its cover, or use `book.cover`.
Existing exports without the `Format` line remain readable (their backslashes are literal).
Unedited old exports are upgraded on the next conversion; edited ones are preserved.

Rerunning `convert` preserves and uses edited text when the generated baseline is unchanged.
If the baseline changes while the text contains edits, conversion stops without overwriting
either final narration file. Use `synthesize narration.txt` to keep the edited script, or
move the edited file aside and rerun `convert` to export the new generated narration.
Accepted narration checkpoints remain cached. `--force` permits replacing the M4B, **not**
overwriting text edits.

For backward compatibility, explicitly passing a `.json` to `synthesize` still uses that
JSON's spoken text and ignores any sibling `.txt` edits. Prefer the `.txt` command above
for normal editing. Unchanged speech chunks are reused; an edit can also change neighboring
chunks if it changes where speech is split.

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

If a review incorrectly claims primary source text was not supplied, the application
checks that text against the outgoing payload and requests a fresh **full-section**
review with the disputed text repeated beside the draft. This also repairs saved loops:
the earliest affected draft is rechecked before replaying later revisions that may have
omitted material in response to the erroneous finding. Approval still requires a passing
review. Genuine new findings get a separate bounded revision history under
`narration/<section-cache>/source-recheck-*/`; the original history and accepted sections
are retained. A repeated missing-source claim after rechecking stops with a diagnostic
instead of consuming the revision budget. No source material is automatically omitted.

### Repairing missing cover artwork

EPUB artwork is resolved from `cover-image` properties, legacy cover metadata (manifest
IDs or image paths), or explicitly declared guide cover pages, including SVG wrappers.
PDF artwork uses physical page 1 even when `extraction.start_page` selects a later page
for narration; the cover page is not added to the narrated content. `book.cover` still
overrides automatic selection. Missing-cover extraction caches are upgraded without
changing source units, section IDs, or accepted narration. Cover-only metadata changes
also preserve manual edits to `narration.txt`.

To fix an already generated audiobook without new model requests or re-encoding audio:

```bash
audiobook repair-cover books/MyBook.epub -c audiobook.toml -o books/MyBook.m4b
```

Use the original PDF/EPUB and conversion workspace (`--work-dir` if needed). This offline
command checks that the source and M4B match the workspace, reads only the cover, and
copies the encoded audio unchanged. It verifies audio-packet hashes, duration, chapter
titles/timings, metadata and attached artwork before replacing the output. Originals are
kept beside the M4B as `MyBook.before-cover.m4b` and `MyBook.before-cover.output.json`.
Existing backups are never overwritten. Repair also updates cover metadata in
`source.json` and `narration.json`, leaving speech text, source units and speech clips
alone. Use the original configuration to retain the final-packaging cache signature;
if settings differ, only that signature is invalidated. Repeating an unchanged repair
reuses the verified output.

## Final table-of-contents reconciliation

At the end of `convert` (including `--until narrate`), a final pass aligns navigation
headings with the source's PDF bookmarks or EPUB navigation/NCX TOC. It reads the full
hierarchy and destinations separately from extraction batches, including multiple PDF
headings on the same page and EPUB subsections. It does not invalidate accepted section
checkpoints or ask the LLM to rewrite chapters.

- Display titles and nesting follow the source TOC. Numbered labels get a period after
  the number: `2.10.1. Earth Ground`. Spoken headings consistently use number words:
  `Section two point ten point one. Earth Ground.` Previously reviewed verbalizations
  of the title itself are retained when they correspond to the source title.
- Unnumbered TOC labels do not erase chapter/section numbers already present in a
  matching accepted heading. An exact normalized title match is required to retain
  those numbers; they are never inferred from the entry's position in the TOC.
  Conflicting explicit TOC/narration numbers, or an unsafe number transfer between
  different titles, stop the pass with a diagnostic. `toc-report.json` records each
  matched heading's resolved number and whether it came from the TOC or accepted
  narration. Rerunning `convert --until narrate` repairs older unedited exports from
  the accepted cache without re-narrating the book.
- By default, PDF chapter labels omit the `CHAPTER` prefix (`2. Theory`); EPUB labels
  retain that prefix when supplied by the source (`CHAPTER 2. Theory`). Both are spoken
  as `Chapter two. Theory.` Set `navigation.chapter_prefix` to `keep` or `omit` to override.
- Body headings absent from the source TOC become ordinary spoken paragraphs. Their
  words and the explanations beneath them are retained, but they no longer create
  navigation entries. `audio.toc_depth` still limits which source levels appear in M4B.
- Already omitted content, such as excluded homework, does not acquire empty navigation
  entries. Missing headings are restored only at unambiguous source starts. Unresolved
  destinations, ambiguous placements or inconsistent ordering stop conversion with a
  `toc-report.json` diagnostic; no final narration is overwritten by that reconciliation.
- If a PDF bookmark is one physical page early or late, reconciliation can match an
  existing accepted heading on the immediately adjacent page. This requires an exact
  title match in both the narration and actual extracted heading lines (up to three
  wrapped lines), with compatible numbers. Bookmark-derived heading hints and prose
  mentions do not count as evidence. Ambiguous matches, missing evidence and conflicting
  TOC order still stop the final pass. The report preserves the original destination
  alongside `resolved_source_id` and `source_heading_evidence`. These corrections affect
  only final navigation: source units, chapter partitions and accepted narration remain
  unchanged, and no new model calls are needed for this fallback.
- Without a machine-readable TOC, generated headings remain in place with standardized
  numbering and an explicit warning. This version does not infer a TOC from printed
  contents-page images. Set `navigation.reconcile = false` to disable the pass.

An optional front-matter check considers only the prefix before the first substantive
TOC entry. It may remove covers, copyright, author biographies, credits, promotional
material and printed contents lists **only when the complete source evidence is approved
as noninstructional**. A book-title entry alone is not grounds to delete its content.
Forewords, prefaces, introductions and learning guidance are retained. Ambiguous,
truncated or over-budget evidence is retained too, as are segments crossing into useful
content. This bounded check uses the configured LLM, may add one initial logical request
(plus API/schema retries), and caches its decision in `toc-front-matter/`. Set
`navigation.omit_front_matter = false` to skip it. Model decisions still merit inspection.

`toc-report.json` records matching, restored/demoted headings, omissions and the planned
M4B navigation. If useful or unverified speech is retained before the first included TOC
heading, the report explicitly flags the additional opening M4B entry rather than silently
discarding that speech. Navigation metadata is refreshed for existing extraction caches without
changing source-unit IDs. Rerun your existing conversion with `--until narrate` to apply
the pass to cached narration before spending on speech.

Manual text edits remain protected. If reconciliation changes the generated baseline
while `narration.txt` contains edits, conversion stops for you to reconcile those versions.
If only your edited script's headings or opening navigation differ, conversion also reports
the conflict without overwriting your edits. Explicit `synthesize narration.txt` continues
to honor your script as written, without re-running TOC alignment or source review.

## Concurrent chapter narration

To narrate independent chapters concurrently, set this in your TOML configuration:

```toml
[narration]
workers = 4
```

The default is `1`. Resolved top-level entries in the PDF outline or EPUB navigation/NCX
table of contents provide candidate chapter starts. Top-level extracted headings are a
fallback when no top-level TOC destinations can be resolved; this avoids mistaking hundreds
of EPUB subsection `h1` tags for chapters. Only destinations at existing chunk starts are
eligible, so selecting candidates never changes source chunks or section IDs.
Before launching independent jobs, the LLM examines the source
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
Independent boundary checks run concurrently, bounded by `narration.workers`, with the
same complete neighboring evidence as sequential checks. They do not use generated
narration. Their results are assembled in source order; interrupted planning reuses
finished checks on restart. One worker makes no new boundary requests.
`chapter-plan.json` records verified boundaries, rejected candidates and the resulting
jobs. Boundary validation is a model judgment, not a guarantee; inspect its reasons and
review representative output when choosing a model. Inconclusive checks (including
truncated responses or evidence exceeding the input budget) are cached as rejected
boundaries too, so a restart does not silently change the chapter partition.
Compatible existing multi-job plans retain their partition, including splits accepted
before TOC-based candidate filtering was added, to preserve accepted narration contexts.
Consequently, a resumed legacy run may have more jobs than a fresh conversion of the book.

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
| `toc-report.json`, `toc-front-matter/` | Final source-TOC audit and cached front-matter review |
| `narration/*/draft-*.json`, `review-*.json` | Inspectable draft/review history |
| `narration.txt` | Editable canonical speech script, with navigation headings |
| `narration.json` | Generated narration baseline, source coverage and cover metadata |
| `requests.jsonl` | Request purposes and provider-reported usage; no API keys |
| `audio/`, `audio.json` | Verified speech clips and ordered audio plan |
| `chapters.ffmeta`, `output.json` | Chapter timing and final artifact verification |

These files contain book content. The configured endpoints receive the relevant book
text/images or spoken text. The program does not send them to other services. Keep your
workspace if you want resume; normalized mono PCM uses roughly 173 MB per hour at 24 kHz,
in addition to page images, drafts and the M4B. No silent clips are substituted for failed
speech calls, and a failed source review stops the conversion before TTS.

## Provider compatibility and audio controls

- Chat Completions and speech use ordinary `OpenAI` client instances with separate
  base URLs and credentials; there are no application client wrappers or custom HTTP
  transports. OpenAI-hosted models are not required. Request lifecycle, network retries
  and backoff are managed by the [official SDK](https://developers.openai.com/api/reference/python).
- `llm.json_mode = "prompt"` omits `response_format` for servers without JSON mode;
  responses are still schema-validated. JSON mode is the default.
- Set `llm.token_parameter = "max_completion_tokens"` when required by your model.
  No temperature is sent unless configured. Increase `max_output_tokens` for verbose
  mathematical/code sections or models whose reasoning uses the same token budget.
- `llm.extra_body` and `tts.extra_body` pass provider-specific fields without overriding
  the core request, using the SDK's `extra_body` option. Function calling is not required.
- Optional `llm.service_tier = "flex"` selects Flex processing on supported models/endpoints;
  omit it to send no tier parameter. Other provider-supported tier strings pass through.
  This applies to every LLM call (boundaries, narration, reviews, repairs and front-matter
  checks), not to `/audio/speech`. It can also be set through `LLM_SERVICE_TIER`.
  [OpenAI's Flex guide](https://developers.openai.com/api/docs/guides/flex-processing)
  recommends allowing for slower responses and occasional unavailable capacity. Consider
  a longer `llm.timeout_seconds`, such as `900`, when configuring a new conversion.
  Retries keep the requested tier; the program never silently switches to a more expensive
  tier. `requests.jsonl` records the requested and actual tier when available.
  Changing only `llm.service_tier` preserves narration, boundary and front-matter caches;
  the SDK migration also preserves existing speech caches. Other pre-existing LLM settings,
  including timeout/retry settings, still participate in legacy cache identities.
  An existing `llm.extra_body.service_tier` is still supported, but do not set both forms.
- `LLM_BASE_URL`, `LLM_MODEL`, `LLM_SERVICE_TIER`, `TTS_BASE_URL`, `TTS_MODEL`, and `TTS_VOICE` override TOML.
  Shell variables take precedence over `.env`. File references are relative to the TOML.
- `tts.max_chars` is enforced after pronunciation replacement, splitting at paragraphs,
  sentences or words. Choose a limit your endpoint supports. Speech requests run with
  bounded `tts.workers`; `narration.workers` independently controls chapter concurrency.
  Sections within each chapter stay sequential.
- WAV, MP3, FLAC, Opus and AAC responses are normalized to mono 16-bit PCM. Raw `pcm`
  responses are assumed little-endian signed 16-bit mono at `tts.pcm_sample_rate`.
- Set `tts.instructions` only when your model supports it. `audio.bitrate`,
  heading/chapter pauses and table-of-contents depth control final assembly. Optional
  `book.title`, `book.author`, and `book.cover` override source metadata. EPUB covers and
  the first physical PDF page are used automatically, independently of narrated page selection.
- `llm.retries` / `tts.retries` configure the SDK's `max_retries`, and each endpoint's
  `timeout_seconds` configures its SDK timeout. There is no additional application-level
  network retry loop. Authentication and unsupported-parameter failures stop immediately.
  Speech downloads are received one bounded clip at a time before being atomically saved,
  so the SDK can also retry interrupted body reads. Memory use scales with clip size and
  `tts.workers`, not book length. Partial downloads never replace complete clips.
  Invalid audio is kept as `.invalid` for diagnosis; rerunning requests a replacement.
- Provider HTTP failures log the full response body, HTTP status, endpoint path and request
  ID (when supplied) to stderr, without needing `--verbose`. This is the final response
  after any SDK retries; connection failures also log their underlying cause. Text/JSON
  returned by a speech provider in place of audio is included in the error too. Request
  headers and request payloads are not logged, and successful audio is never dumped to the
  console. Error bodies are not redacted, so review logs before sharing them if a provider
  echoes input or credentials. `requests.jsonl` remains a usage log, not an error log.

Clips are concatenated in source order as uniform PCM and encoded to AAC **once**. Chapter
times are computed from sample counts, including pauses, rather than rounded per-clip
timestamps. The completed M4B is checked with ffprobe for its audio stream, duration,
chapter boundaries and requested cover artwork, then atomically installed at the output path.

## Development

```sh
.venv/bin/pytest
.venv/bin/ruff check src tests
```

Tests use synthetic books and mocked compatible endpoints, with real FFmpeg integration
when available. No paid model calls are required. A real vision/TTS provider and listening
review of a representative chapter are still needed to evaluate narration quality.
