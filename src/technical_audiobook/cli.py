import argparse
import json
import logging
from pathlib import Path

from dotenv import load_dotenv
from openai import APIConnectionError, APIError, APIStatusError
from pydantic import ValidationError

from . import __version__
from .config import load_config
from .covers import repair_cover
from .pipeline import convert, default_work, synthesize_transcript
from .toc_repair import repair_toc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="audiobook",
        description="Create a listener-friendly audiobook from a technical book or research paper.",
    )
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    conversion = commands.add_parser(
        "convert", help="Extract, narrate, synthesize, and package a book"
    )
    conversion.add_argument("source", type=Path)
    conversion.add_argument(
        "--document-type",
        choices=["book", "paper"],
        help="Narration policy (overrides narration.document_type; default: book)",
    )
    conversion.add_argument(
        "--work-dir", type=Path, help="Persistent cache directory (default: OUTPUT.work)"
    )
    conversion.add_argument(
        "--until",
        choices=["extract", "narrate", "audio", "m4b"],
        default="m4b",
        help="Stop after a stage; extract is offline and needs no model settings",
    )
    speech = commands.add_parser(
        "synthesize",
        help="Produce M4B from editable narration.txt (or an explicit JSON transcript)",
    )
    speech.add_argument(
        "transcript", type=Path, help="Editable narration.txt, or an explicit JSON transcript"
    )
    cover = commands.add_parser(
        "repair-cover",
        help="Repair an existing M4B cover offline, preserving encoded audio and a backup",
    )
    cover.add_argument("source", type=Path, help="Original PDF or EPUB")
    cover.add_argument("--work-dir", type=Path, help="Original workspace (default: OUTPUT.work)")
    repair = commands.add_parser("repair-toc", help="Resolve TOC issues interactively, offline")
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
    repair.add_argument("--verbose", action="store_true")
    repair.add_argument(
        "--backup-edits",
        action="store_true",
        help="With --publish: back up current narration and replace it; manual text edits are not merged",
    )
    for command in (conversion, speech, cover):
        command.add_argument("-o", "--output", type=Path, required=True, help="Output .m4b file")
        command.add_argument("-c", "--config", type=Path, help="TOML configuration file")
        command.add_argument("--env-file", type=Path, help="Load credentials from this .env file")
        if command is not cover:
            command.add_argument(
                "--force", action="store_true", help="Replace an existing output if it differs"
            )
        command.add_argument("--verbose", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Do not expose request headers or embedded book images through HTTP debug logging.
    # Verbose output is for this application's diagnostics, not dependency payloads.
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("technical_audiobook").setLevel(
        logging.DEBUG if args.verbose else logging.INFO
    )
    logging.getLogger("openai").setLevel(logging.WARNING)
    try:
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
        config_path = args.config or (
            Path("audiobook.toml") if Path("audiobook.toml").exists() else None
        )
        config = load_config(config_path)
        if args.command == "convert":
            if args.document_type is not None:
                config.narration.document_type = args.document_type
            result = convert(
                args.source,
                args.output,
                args.work_dir or default_work(args.output),
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
    except (ValueError, OSError, ValidationError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
