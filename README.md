# stem4b

**Turn STEM books and research papers into detailed, listener-friendly audiobooks.**
Give stem4b a **PDF or EPUB** and get a **chaptered M4B** with cover artwork, ready
for an audiobook player.

## What it does

- **Explains figures, equations, code, tables** and useful footnotes in spoken form.
- Retains substantive prose and worked examples; skips boilerplate and reference
  lists. Exercises are optional.
- Aligns navigation with the source table of contents and standardizes spoken
  chapter/section numbers. Offline guided repair helps resolve ambiguous entries.
- Uses separately-configured LLM and TTS providers with OpenAI-compatible APIs.
  Hosted services and local servers are supported; just configure `base_url`.
- Caches accepted narration and speech, with optional concurrent chapter narration.

[See sample output here.](https://github.com/t33chong/stem4b/releases/tag/samples)

## Why I built it

As a compulsive autodidact, I devour audiobooks so that I can learn while my hands
are full and my brain would otherwise be idle. The problem is that ordinary
text-to-speech omits too much from technical books and papers. Diagrams disappear,
equations are awkward to follow, code loses its structure, and tables become streams
of numbers without a clear point.

stem4b aims to make the material understandable without looking at the page. It uses
a vision-capable language model to adapt the source for listening, checks that
narration against the source, and then turns an editable script into speech. You can
review the text before synthesizing speech audio, pause a long conversion, and
resume completed work.

## Install

Use **Python 3.11+** and **FFmpeg** (`ffmpeg` and `ffprobe` on PATH), or follow the
[Docker guide](docs/docker.md) to avoid installing them on your host.

On macOS, install FFmpeg with `brew install ffmpeg`. On Debian/Ubuntu, use
`sudo apt update && sudo apt install ffmpeg`. On Windows, install an FFmpeg build
and add its `bin` directory to PATH, or use Docker Desktop.

From a downloaded or cloned copy of this repository:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
stem4b init
```

On Windows PowerShell, use `py -m venv .venv` and
`.venv\Scripts\Activate.ps1` instead of the first two commands. If activation is
unavailable, run `.venv\Scripts\python.exe -m pip install .` and use
`.venv\Scripts\stem4b.exe` for the commands below.

`init` creates `stem4b.toml` and `.env` in the current directory, never overwriting
existing files. Use `stem4b init DIRECTORY` for a separate working directory.
No model requests are made during setup.

## Configure

Edit the generated `stem4b.toml`: replace the LLM endpoint/model and TTS
endpoint/model/voice placeholders. Models are not downloaded or bundled.

| Component | Your provider must support |
| --- | --- |
| LLM | A `/chat/completions` endpoint with text and `image_url` inputs, returning JSON |
| TTS | An `/audio/speech` endpoint returning audio bytes |

Use API **base URLs** (often ending in `/v1`), not full operation URLs.
Choose actual model and voice identifiers offered by your providers. See
[configuration](docs/configuration.md) for compatibility options and settings.

Put credentials in `.env`:

```dotenv
LLM_API_KEY=your-llm-provider-key
TTS_API_KEY=your-tts-provider-key
```

Leave a key blank only for a server that requires no authentication. Even when
using one provider for both models, set both keys. They are deliberately independent.

```sh
stem4b doctor
```

This checks local settings and dependencies, not provider access or model quality.
Use `stem4b doctor --stage narrate` if you are only preparing text.

## Your first audiobook

Start with a short input or select a small PDF page range in `[extraction]`.
The recommended workflow is **narrate → review/edit → synthesize**:

```sh
stem4b convert textbook.pdf --until narrate
# Review and edit textbook.work/narration.txt in your text editor.
stem4b synthesize textbook.work/narration.txt -o textbook.m4b
```

The text file is the canonical speech script. `#` headings supply M4B navigation;
the line immediately below each heading supplies its spoken wording. Read the
[editing guide](docs/usage.md#inspect-review-edit-and-resume) before restructuring
headings. Synthesis makes no LLM requests and does not rewrite your edits.

For a one-command conversion:

```sh
stem4b convert textbook.epub
```

This writes `textbook.m4b` and keeps progress in `textbook.work/` beside the input.
Choose another output with `-o`, another workspace with `--work-dir`, or another
configuration with `-c`:

```sh
stem4b convert textbook.pdf -c stem4b.toml -o audio/textbook.m4b
stem4b convert paper.pdf --document-type paper --until narrate
stem4b synthesize paper.work/narration.txt -o paper.m4b
```

To stop, press Ctrl+C. Rerun the same command with the same source, settings and
workspace to resume; compatible accepted sections and audio clips are reused.
Keep the `.work` directory. Existing unrelated M4Bs are protected; use `--force`
only when you intend to replace one. It never authorizes overwriting text edits.

## Commands and help

| Command | Purpose |
| --- | --- |
| `stem4b init [DIRECTORY]` | Create configuration and credential templates |
| `stem4b doctor` | Check local setup without contacting providers |
| `stem4b convert INPUT` | Convert PDF/EPUB, optionally stopping at an intermediate stage |
| `stem4b synthesize SCRIPT -o OUTPUT.m4b` | Generate audio from reviewed text |
| `stem4b repair-toc WORKDIR` | Resolve TOC problems interactively and offline |
| `stem4b repair-cover INPUT -o OUTPUT.m4b` | Repair artwork without regenerating audio |

Use `stem4b --help` or `stem4b COMMAND --help`. `python -m stem4b` works too.

- [Workflow, text editing, papers and resuming](docs/usage.md)
- [Configuration and provider compatibility](docs/configuration.md)
- [Docker](docs/docker.md)
- [Troubleshooting and guided TOC repair](docs/troubleshooting.md)
- [Contributing and running tests](CONTRIBUTING.md)

## Cost, privacy and limitations

Extraction and repair commands are offline. Narration sends source text and images
to your configured LLM; synthesis sends spoken text to your configured TTS service.
Review, revisions and retries can add requests. Use `--until extract` to inspect a
plan before starting, but its request estimate is not a price quote.

Workspaces contain source content and generated text. Provider error bodies are
logged in full and may echo private data: inspect logs before sharing them. Use
only documents you have the right to process or distribute. DRM removal is not
supported. See [troubleshooting](docs/troubleshooting.md#reporting-a-problem) for
safe bug reports.

## License

stem4b's code is [MIT licensed](LICENSE). Dependencies have separate terms:
**PyMuPDF is AGPL/commercial licensed**, and FFmpeg licensing varies by build.
Read [third-party notices](THIRD_PARTY_NOTICES.md), particularly before distributing
an application or Docker image containing these components.
