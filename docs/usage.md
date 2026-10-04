# Workflow and text editing

[Back to README](../README.md) · [Configuration](configuration.md) · [Troubleshooting](troubleshooting.md)

## Pipeline stages

| `convert --until` | Result | Requirements |
| --- | --- | --- |
| `extract` | Source evidence and `plan.json` | Offline; no models or FFmpeg |
| `narrate` | Reviewed, editable `narration.txt` | Vision-capable LLM |
| `audio` | Speech clips and `audio.json` | LLM, TTS, FFmpeg |
| `m4b` (default) | Complete chaptered M4B | LLM, TTS, FFmpeg |

`convert` defaults to an M4B beside the input with the same name; `-o` changes it.
`synthesize` uses the script's parent directory as its workspace and requires `-o`.
Quote paths containing spaces. All stages reuse compatible completed work.

## Inspect, review, edit, and resume

Offline extraction needs no credentials, model settings or FFmpeg:

```sh
stem4b convert textbook.pdf -o textbook.m4b --until extract
```

It writes `textbook.work/plan.json`, including source sizes, images and the initial
request count. Review/repair, schema repair, adaptive splitting and network retries
can increase requests; the plan is not a price quote.

Generate and inspect narration before spending on TTS:

```sh
stem4b convert textbook.pdf -c stem4b.toml -o textbook.m4b --until narrate
# Review and edit textbook.work/narration.txt in your text editor.
stem4b synthesize textbook.work/narration.txt -c stem4b.toml -o textbook.m4b
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
Source, model, prompt and context changes still invalidate the affected narration. Logs distinguish
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

## Research papers and technical reports

Use the same conversion pipeline with an explicit paper policy:

```sh
stem4b convert papers/paper.pdf -c stem4b.toml \
  --document-type paper -o papers/paper.m4b --until narrate
# Review papers/paper.work/narration.txt, then generate the audio:
stem4b synthesize papers/paper.work/narration.txt \
  -c stem4b.toml -o papers/paper.m4b
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
Compatible existing multi-job plans retain their partition to preserve accepted
narration contexts, even if a newer version would choose different candidates.

Worker count does not invalidate narration. When enabling workers in a partially narrated
book, chapters already started sequentially retain their original accepted context and
checkpoints. Switching back to one worker reuses cached boundary decisions and serializes
the same jobs. If a chapter fails, queued jobs are cancelled and active workers stop between
requests; completed sections in all chapters remain reusable. No final transcript or
audiobook is assembled from incomplete jobs.

Long chapters still run sequentially internally, so speedup depends on chapter sizes and
provider rate limits.

## Workspace artifacts

Useful workspace files:

| File | Purpose |
| --- | --- |
| `source.json`, `source/assets/` | Source evidence and page/figure images |
| `plan.json` | Ordered source batches and initial request estimate |
| `chapter-boundaries/`, `chapter-plan.json` | Cached source checks and independent narration jobs |
| `toc-report.md`, `toc-report.json`, `toc-front-matter/` | Readable TOC recovery guide, full audit and cached front-matter review |
| `toc-input.json`, `toc-overrides.json`, `toc-override-history/` | Offline TOC repair input, book-specific choices and previous choices |
| `narration.toc-preview.txt`, `narration.toc-preview.json` | Repaired preview from accepted narration; never silently replaces manual text edits |
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

## Upgrading an existing workspace

Existing configuration filenames work with `-c`. If no `stem4b.toml` exists,
`audiobook.toml` is discovered with a warning as a migration convenience. The command
and Python package are now `stem4b`; existing workspace formats, including
`Format: audiobook-text-v1`, intentionally remain compatible. Reinstall an updated
source checkout with `python -m pip install .`. If an old editable installation
exists, uninstall the old distribution first with
`python -m pip uninstall technical-audiobook-generator`, then install stem4b.
