import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from . import __version__
from .api import APIError
from .config import load_config
from .pipeline import convert, default_work, synthesize_transcript


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="audiobook",
        description="Create a listener-friendly technical audiobook from PDF or EPUB.",
    )
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    conversion = commands.add_parser(
        "convert", help="Extract, narrate, synthesize, and package a book"
    )
    conversion.add_argument("source", type=Path)
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
        "synthesize", help="Produce M4B from an existing, optionally edited narration.json"
    )
    speech.add_argument("transcript", type=Path)
    for command in (conversion, speech):
        command.add_argument("-o", "--output", type=Path, required=True, help="Output .m4b file")
        command.add_argument("-c", "--config", type=Path, help="TOML configuration file")
        command.add_argument("--env-file", type=Path, help="Load credentials from this .env file")
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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if args.env_file and not args.env_file.is_file():
            raise ValueError(f"Environment file not found: {args.env_file}")
        env_file = args.env_file or ((args.config.parent if args.config else Path.cwd()) / ".env")
        load_dotenv(env_file, override=False)
        config_path = args.config or (
            Path("audiobook.toml") if Path("audiobook.toml").exists() else None
        )
        config = load_config(config_path)
        if args.command == "convert":
            result = convert(
                args.source,
                args.output,
                args.work_dir or default_work(args.output),
                config,
                args.until,
                args.force,
            )
        else:
            result = synthesize_transcript(args.transcript, args.output, config, args.force)
        print(result)
        return 0
    except KeyboardInterrupt:
        logging.error("Interrupted. Completed work is cached; rerun the same command to resume.")
        return 130
    except (ValueError, OSError, APIError, ValidationError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
