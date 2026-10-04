# Contributing

Bug reports, documentation improvements and focused fixes are welcome. Please
describe the user-visible problem and how to reproduce it. For bugs involving
source documents, prefer a small synthetic PDF/EPUB you can share rather than a
copyrighted book. Never include credentials, private configuration or a full work
directory in a public issue. See [reporting a problem](docs/troubleshooting.md#reporting-a-problem).

## Development setup

Install Python 3.11+ and FFmpeg, then from the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
ruff check src tests
ruff format --check src tests
```

Tests use synthetic documents and local mock API servers. They need loopback
network access but no provider credentials or paid model calls. FFmpeg integration
tests use the real binaries and are skipped when unavailable; install FFmpeg for
the complete suite. Listening to real model output is a separate quality evaluation,
not something mocked tests can establish.

## Making changes

Keep changes focused and add a regression test for fixes. Preserve source evidence,
coverage checks, deterministic ordering, atomic publication and manual-text-edit
protection. Workspace formats and cache keys are part of the user experience:
avoid invalidating expensive accepted narration just to rename an internal field.
Changes to prompts or source context need deliberate cache-version consideration.

The main stages live in `src/stem4b/`: extraction/chunking, narration/review,
navigation/repair, editable text, and audio packaging. `cli.py` handles command-line
orchestration; `setup.py` contains offline setup checks. Model requests use the
official SDK with independently configured endpoints.

When changing configuration or commands, update packaged templates, `--help`, tests
and the relevant guide. New source fixtures must be small, synthetic and redistributable.
Do not check in books, `.env`, provider logs, private TOMLs, workspaces or audio outputs.

## Packaging and Docker checks

```sh
python -m pip install build
python -m build
docker build -t stem4b:local .
docker run --rm stem4b:local --version
docker run --rm --network none stem4b:local doctor --stage extract
```

The source distribution and Docker context use allowlists to exclude private local
files. Check wheel contents and run `stem4b init` from an installed wheel when
changing package data. Both generated setup templates must work outside a source
checkout. CI runs lint, tests, packaging and Docker smoke checks; it does not publish
packages or images.

The project's code is MIT licensed. Dependencies retain their own terms; see
[third-party notices](THIRD_PARTY_NOTICES.md) before distributing builds.
