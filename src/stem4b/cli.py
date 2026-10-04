import argparse
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, APIError, APIStatusError
from pydantic import ValidationError

from . import __version__
from .config import load_config
from .covers import repair_cover
from .pipeline import convert, default_work, synthesize_transcript
from .setup import diagnose, initialize
from .toc_repair import repair_toc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="stem4b",
        description="Turn STEM books and research papers (PDF/EPUB) into narrated M4B audiobooks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Start here:\n"
            "  stem4b init                         Create stem4b.toml and .env; edit them first\n"
            "  stem4b doctor                       Check configuration and FFmpeg offline\n"
            "  stem4b convert book.pdf --until narrate\n"
            "                                      Review/edit book.work/narration.txt\n"
            "  stem4b synthesize book.work/narration.txt -o book.m4b\n\n"
            "Use 'stem4b COMMAND --help' for command options.\n"
            "Rerun an interrupted conversion with the same options to reuse completed work."
        ),
    )
    result.add_argument("--version", action="version", version=f"stem4b {__version__}")
    commands = result.add_subparsers(dest="command", metavar="COMMAND", required=True)
    setup = commands.add_parser(
        "init",
        help="Create configuration and credential templates without overwriting files",
        description="Create stem4b.toml and .env. Edit the model, endpoint and voice settings before converting.",
    )
    setup.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path("."),
        help="Destination directory (default: current directory)",
    )
    doctor = commands.add_parser(
        "doctor",
        help="Check configuration and dependencies offline (no API calls)",
        description="Check local setup without sending requests, spending credits or displaying API keys.",
    )
    doctor.add_argument(
        "--stage",
        choices=["extract", "narrate", "m4b"],
        default="m4b",
        help="Check only what this stage needs (default: m4b)",
    )
    conversion = commands.add_parser(
        "convert",
        help="Convert a PDF/EPUB to M4B, or stop to review the narration",
        description="Extract, narrate with source-backed review, synthesize speech, and package M4B.",
        epilog="Example: stem4b convert book.pdf -c stem4b.toml --until narrate. Rerun unchanged to resume.",
    )
    conversion.add_argument("source", type=Path, help="Input PDF or EPUB file")
    conversion.add_argument(
        "--document-type",
        choices=["book", "paper"],
        help="Override narration.document_type (default in configuration: book)",
    )
    conversion.add_argument(
        "--work-dir", type=Path, help="Persistent cache directory (default: OUTPUT.work)"
    )
    conversion.add_argument(
        "--until",
        choices=["extract", "narrate", "audio", "m4b"],
        default="m4b",
        help="Stop after a stage (default: m4b); extract is offline, narrate produces editable text",
    )
    speech = commands.add_parser(
        "synthesize",
        help="Produce M4B from editable narration.txt (or an explicit JSON transcript)",
        description="Use edited narration text directly, without any LLM requests. Reuse unchanged speech clips.",
        epilog="Example: stem4b synthesize book.work/narration.txt -c stem4b.toml -o book.m4b",
    )
    speech.add_argument(
        "transcript", type=Path, help="Editable narration.txt, or an explicit JSON transcript"
    )
    cover = commands.add_parser(
        "repair-cover",
        help="Repair an existing M4B cover offline, preserving encoded audio and a backup",
        description="Extract source artwork or use book.cover, then repair an existing M4B without regenerating speech.",
    )
    cover.add_argument("source", type=Path, help="Original PDF or EPUB")
    cover.add_argument("--work-dir", type=Path, help="Original workspace (default: OUTPUT.work)")
    repair = commands.add_parser(
        "repair-toc",
        help="Resolve table-of-contents issues interactively, offline",
        description="Match problematic TOC entries to source locations or accepted headings. Never edit the source book.",
        epilog="Start with: stem4b repair-toc book.work. Uses the saved conversion settings; no -c or API keys needed.",
    )
    repair.add_argument("work_dir", type=Path, help="Existing book .work directory")
    mode = repair.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate offline without prompts or publishing narration",
    )
    mode.add_argument(
        "--publish",
        action="store_true",
        help="Validate and publish resolved narration, protecting manual edits",
    )
    repair.add_argument(
        "--reset", action="store_true", help="Back up and clear saved TOC corrections first"
    )
    repair.add_argument("--verbose", action="store_true", help="Show detailed diagnostics")
    repair.add_argument(
        "--backup-edits",
        action="store_true",
        help="With --publish: back up current narration and replace it; manual text edits are not merged",
    )
    for command in (conversion, speech, cover):
        command.add_argument(
            "-o",
            "--output",
            type=Path,
            required=command is not conversion,
            help="Output .m4b file"
            + (" (default: beside the input, same stem)" if command is conversion else ""),
        )
        if command is not cover:
            command.add_argument(
                "--force",
                action="store_true",
                help="Replace a different existing M4B; never overwrite manual text edits",
            )
    for command in (conversion, speech, cover, doctor):
        command.add_argument(
            "-c", "--config", type=Path, help="TOML settings (default: ./stem4b.toml if present)"
        )
        command.add_argument(
            "--env-file",
            type=Path,
            help="Credentials file (default: .env beside config, or in current directory)",
        )
        command.add_argument("--verbose", action="store_true", help="Show detailed diagnostics")
    return result


def main(argv: list[str] | None = None) -> int:
    command_parser = parser()
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments:
        command_parser.print_help()
        return 0
    args = command_parser.parse_args(arguments)
    verbose = getattr(args, "verbose", False)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Do not expose request headers or embedded book images through HTTP debug logging.
    # Verbose output is for this application's diagnostics, not dependency payloads.
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("stem4b").setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("openai").setLevel(logging.WARNING)
    try:
        if args.command == "init":
            for created in initialize(args.directory):
                print(f"Created {created}")
            print("Edit stem4b.toml and .env, then run 'stem4b doctor' from that directory.")
            return 0
        if args.command == "repair-toc":
            print(
                repair_toc(
                    args.work_dir,
                    check=args.check,
                    publish=args.publish,
                    reset=args.reset,
                    backup_edits=args.backup_edits,
                )
            )
            return 0
        if args.env_file and not args.env_file.is_file():
            raise ValueError(f"Environment file not found: {args.env_file}")
        env_file = args.env_file or ((args.config.parent if args.config else Path.cwd()) / ".env")
        load_dotenv(env_file, override=False)
        config_path = args.config
        if config_path is None:
            if Path("stem4b.toml").exists():
                config_path = Path("stem4b.toml")
            elif Path("audiobook.toml").exists():
                config_path = Path("audiobook.toml")
                logging.warning(
                    "Using legacy audiobook.toml; rename it to stem4b.toml when convenient."
                )
        config = load_config(config_path)
        if args.command == "doctor":
            if config_path is None:
                print(
                    "No stem4b.toml found; using defaults and environment overrides. 'stem4b init' creates examples."
                )
            messages, ready = diagnose(config, args.stage)
            print("\n".join(messages))
            return 0 if ready else 1
        if args.command == "convert":
            output = args.output or args.source.with_suffix(".m4b")
            if args.document_type is not None:
                config.narration.document_type = args.document_type
            result = convert(
                args.source,
                output,
                args.work_dir or default_work(output),
                config,
                args.until,
                args.force,
            )
        elif args.command == "repair-cover":
            result = repair_cover(
                args.source, args.output, args.work_dir or default_work(args.output), config
            )
        else:
            result = synthesize_transcript(args.transcript, args.output, config, args.force)
        print(result)
        return 0
    except KeyboardInterrupt:
        logging.error("Interrupted. Completed work is cached; rerun the same command to resume.")
        return 130
    except APIStatusError as exc:
        logging.error(
            "Provider returned HTTP %s for %s %s (request ID: %s).\nProvider response body:\n%s",
            exc.status_code,
            exc.request.method,
            exc.request.url.path,
            exc.request_id or "unavailable",
            exc.response.text or "<empty body>",
        )
        return 1
    except APIConnectionError as exc:
        logging.error(
            "Provider connection failed after the configured retries (%s): %s. Rerun to resume.",
            type(exc).__name__,
            exc,
        )
        if exc.__cause__ is not None:
            logging.error(
                "Underlying connection error (%s): %s",
                type(exc.__cause__).__name__,
                exc.__cause__,
            )
        return 1
    except APIError as exc:
        logging.error("Provider request failed (%s): %s", type(exc).__name__, exc)
        if exc.body is not None:
            body = (
                exc.body if isinstance(exc.body, str) else json.dumps(exc.body, ensure_ascii=False)
            )
            logging.error("Provider response body:\n%s", body)
        return 1
    except ValidationError as exc:
        # Field paths are useful to users; the full input dump may contain private settings.
        details = "\n".join(
            f"  {'.'.join(str(part) for part in error['loc']) or 'configuration'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        logging.error("Invalid settings or data:\n%s", details)
        return 1
    except (ValueError, OSError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
