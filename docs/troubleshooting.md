# Troubleshooting

[Back to README](../README.md) · [Workflow](usage.md) · [Configuration](configuration.md) · [Docker](docker.md)

Start with `stem4b doctor` and `stem4b COMMAND --help`. Keep your `.work` directory:
most failures can be resumed without discarding completed narration or speech.

## Installation and configuration

- **Command not found:** activate the environment you installed into, or use
  `.venv/bin/stem4b` (Windows: `.venv\\Scripts\\stem4b.exe`). `python -m stem4b`
  also works with that environment's Python.
- **Missing model/voice:** replace the `your-...` placeholders in `stem4b.toml`.
  Check environment overrides too. `doctor` identifies local omissions but cannot
  validate provider-specific model identifiers.
- **Missing FFmpeg:** install both `ffmpeg` and `ffprobe` on PATH, or use Docker.
  They are not needed for `convert --until narrate`.
- **Invalid TOML:** inspect the named file and line. Use one `[llm]` or `[tts]`
  table per file; add settings within an existing table rather than duplicating it.
- **Wrong pages or policy:** check `extraction.start_page/end_page` and
  `narration.document_type`. Page numbers are physical PDF pages, not printed labels.

## Provider and quality failures

HTTP errors include the provider response body and request ID. A 401/403 usually
means credentials or access; a 404 may mean the API root or model name is wrong.
Read the actual response rather than retrying blindly. A compatibility error may
require `llm.json_mode = "prompt"` or a different token parameter. An endpoint
must implement the required chat-with-images or speech operation, not just a
similarly named route. See [configuration](configuration.md).

For 429/503 capacity failures, wait and resume, lower worker counts, or adjust
retry limits. [Flex retries](configuration.md#flex-retries-and-optional-standard-fallback)
never switch to a more expensive tier unless explicitly enabled. For connection
errors, check the server URL, network and timeout; Docker has its own networking.
Invalid speech is retained as `.invalid` for diagnosis, not inserted as silence.

If review exhausts `narration.max_revisions`, inspect the failed section's draft and
review files. Correct an underlying model/prompt problem, or raise the revision
budget and rerun. Raising only that budget preserves accepted section caches.
Simply allowing more revisions does not guarantee that a repeated quality problem
will resolve. Keep source review enabled and try a representative short input first.

## Outputs and manual edits

An existing different M4B needs a new output path or explicit `--force`. That flag
does not bypass narration-edit protections. To keep edited text, run
`stem4b synthesize BOOK.work/narration.txt -o BOOK.m4b`; add `--force` only if you
intend to replace an existing different M4B. If regeneration is needed, back up
edited text first. Do not delete the entire workspace to address a single issue.

## Table-of-contents errors

You do not need the author's source files, and you do not need to edit PDF bookmarks.
Start with `stem4b repair-toc BOOK.work`; the details below explain the guided workflow.

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
- A stale **unnumbered PDF bookmark title** can yield to the printed numbered heading
  when there is exactly one accepted heading and one retained TOC entry at that
  destination, and the actual extracted heading lines corroborate both title and
  number. Wrapped chapter openers are supported, including titles whose chapter number
  extracts at the end when a separate running header confirms it. Outline hints, prose
  mentions, ambiguous matches and conflicting explicit bookmark numbers are insufficient.
  The audit retains the original bookmark and records `resolved_title`,
  `source_heading_evidence` and numbering origin `printed_source_heading`.
- By default, PDF chapter labels omit the `CHAPTER` prefix (`2. Theory`); EPUB labels
  retain that prefix when supplied by the source (`CHAPTER 2. Theory`). Both are spoken
  as `Chapter two. Theory.` Set `navigation.chapter_prefix` to `keep` or `omit` to override.
- Body headings absent from the source TOC become ordinary spoken paragraphs. Their
  words and the explanations beneath them are retained, but they no longer create
  navigation entries. `audio.toc_depth` still limits which source levels appear in M4B.
- Already omitted content, such as excluded homework, does not acquire empty navigation
  entries. Missing headings are restored only at unambiguous source starts. Unresolved
  destinations, ambiguous placements or inconsistent ordering stop conversion with a
  `toc-report.md` recovery guide and a `toc-report.json` audit; no final narration is
  overwritten by that reconciliation.
- With `narration.include_exercises = false`, navigation branches titled “Exercises”,
  “Problems” or “Review Questions” are excluded **before** destination/order checks,
  including broken bookmarks and entries on pages with retained material. No accepted
  speech or source pages are deleted. Worked examples remain. Paper reference-list
  navigation is treated similarly; subsequent technical appendices remain eligible.
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

### Resolving TOC errors

Open `WORKDIR/toc-report.md` first. It explains every issue, identifies the affected
headings and source destinations, and gives recovery options. The JSON audit also
contains structured `diagnostics` and `recovery_steps`. For ordering conflicts the
report lists each backwards pair, including the original bookmark destination and
the proposed correction. IDs such as `p00073` mean **physical PDF page 73**, not the
page number printed on the page. Narration segment indices are zero-based positions
in the assembled accepted narration, not section numbers.

You do not need the textbook's authoring files, a PDF editor or a coding assistant.
The program first applies the source-backed automatic corrections described above.
For anything still ambiguous, run the exact command printed in the error, for example:

```sh
stem4b repair-toc books/MyBook.work
```

This is an **offline guided review**; it reads the saved source and accepted narration,
not your API credentials. It does not change the PDF/EPUB or re-narrate sections.

1. Select a reported TOC entry. The tool shows its source excerpt, location, a source
   image path when available, and nearby accepted headings.
2. Select the correct accepted heading (including its title and number), use `/text`
   to search all accepted headings, or `n` to view more candidates. Alternatively,
   `d` corrects a bookmark destination using a physical PDF page number or extracted
   EPUB source-unit ID (`/text` at the destination prompt searches the source and lists
   matching IDs and locations). For unwanted navigation, `o` omits **only that TOC entry**,
   never its speech or children. Each change requires explicit confirmation.
3. Choices are saved immediately in `toc-overrides.json`. The tool rechecks heading
   uniqueness, locations and order after each choice; a selection is not permission
   to produce inconsistent navigation. `u` undoes the selected entry's correction;
   `q` exits with confirmed choices retained for the next run.
4. Once validation passes, inspect `narration.toc-preview.txt`. The tool asks whether
   to publish the repaired `narration.txt`; existing manual edits remain protected.
   If publishing is blocked, compare your current script with the preview. Keep your
   edited script, or move it aside as a backup before publishing the regenerated
   version. Regeneration does not merge manual prose edits.

For scripts or repeated checks:

```sh
stem4b repair-toc books/MyBook.work --check
stem4b repair-toc books/MyBook.work --publish
```

If you deliberately want to replace edited text with the regenerated version:

```sh
stem4b repair-toc books/MyBook.work --publish --backup-edits
```

This first saves the existing `narration.txt` and its JSON baseline in a unique
`toc-repair-backup-*` directory inside the workspace. It does not merge manual edits.
The backup path is printed; normal `--publish` never opts into replacement implicitly.

`--check` refreshes the audit and repaired preview without changing final narration.
`--publish` validates and publishes without prompts, still refusing to overwrite manual
edits. Both commands make **no model or TTS requests**. They use the final-pass settings
saved by `convert`; no `-c` argument is needed. To change those settings, rerun `convert`
with your original config first. Workspaces from older versions need one rerun of
`convert --until narrate` to create `toc-input.json`; existing compatible section caches
are reused. If repair happens before narration (`source_preflight`), it fixes destinations
only; the tool then tells you to rerun `convert`, which may make paid narration requests.

Corrections are tied to the source file, original TOC and the selected accepted heading.
Changed inputs cannot silently reuse stale choices. `repair-toc WORKDIR --reset` backs
up the correction file in `toc-override-history/` and clears it; `--reset --check` works
without an interactive terminal. Prior correction versions are also kept on each edit.
Corrections are reused by subsequent `convert` runs without changing narration cache
identities. They affect final navigation only, not extraction hints or chapter partitioning.
`toc-report.json` is still an audit, not the correction file; editing final `narration.txt`
does not repair this pass, which works from accepted checkpoints.

As a broader bypass, you can deliberately disable alignment in the existing `[navigation]` table
  in the same configuration file used with `-c` (add it only if absent):

```toml
[navigation]
reconcile = false
```

Rerun the same `convert` command with `--until narrate` first. This skips source-TOC
matching, final heading standardization and source-backed front-matter review; it
does **not** fix bookmarks. Review the resulting `narration.txt` before synthesizing.

Keep your workspace and `accepted.json` checkpoints. After a final-pass failure, they
are reused when the source, workspace and narration/model settings are unchanged.
Changing only `navigation.reconcile` does not invalidate accepted narration. A failure
during `source_preflight` is different: narration has not started, so proceeding may
make new paid requests. Existing final narration, if any, may be an older export.
Do not delete caches, use `--force`, or change LLM settings to retry a TOC error.
Rerunning unchanged will reproduce it; editing/replacing the source file changes its
identity and may require re-narration.

## Repairing missing cover artwork

EPUB artwork is resolved from `cover-image` properties, legacy cover metadata (manifest
IDs or image paths), or explicitly declared guide cover pages, including SVG wrappers.
PDF artwork uses physical page 1 even when `extraction.start_page` selects a later page
for narration; the cover page is not added to the narrated content. `book.cover` still
overrides automatic selection. Missing-cover extraction caches are upgraded without
changing source units, section IDs, or accepted narration. Cover-only metadata changes
also preserve manual edits to `narration.txt`.

To fix an already generated audiobook without new model requests or re-encoding audio:

```bash
stem4b repair-cover books/MyBook.epub -c stem4b.toml -o books/MyBook.m4b
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

## Reporting a problem

Include `stem4b --version`, operating system, the command (with private paths
redacted), relevant configuration without keys, and the error. A minimal synthetic
PDF/EPUB is best; do not upload copyrighted books without permission. For TOC errors,
the relevant entries in `toc-report.md` are more useful than the entire workspace.

Provider error bodies are intentionally logged in full and may echo input or
credentials. Inspect/redact logs, `.env`, provider-specific settings, source images
and workspace files before sharing. `requests.jsonl` records usage, not error bodies.
