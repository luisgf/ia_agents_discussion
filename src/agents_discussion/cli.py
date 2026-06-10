import argparse
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel

from agents_discussion.config import get_settings
from agents_discussion.context_files import read_context_file
from agents_discussion.graph import run_debate
from agents_discussion.project_context import build_project_context


console = Console()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agents-discuss",
        description="Run a technical diagnosis debate with three AI agents.",
    )
    parser.add_argument(
        "topic",
        nargs="?",
        help="Technical issue, incident, performance problem, or code fix to diagnose.",
    )
    parser.add_argument(
        "--file",
        "-f",
        type=Path,
        help="Optional file with logs, stack traces, code snippets, metrics, or incident context.",
    )
    parser.add_argument(
        "--base-context",
        action="append",
        default=[],
        type=Path,
        help="Optional baseline context file with architecture, services, non-secret connection parameters, or constraints. Can be repeated.",
    )
    parser.add_argument(
        "--no-redact-context",
        action="store_true",
        help="Do not redact likely secrets from --file and --base-context before sending them to models.",
    )
    parser.add_argument(
        "--project",
        type=Path,
        help="Optional project directory to read source files from.",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Glob pattern to include from --project. Can be repeated. Defaults to common project files.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=20,
        help="Maximum project files to include. Default: 20.",
    )
    parser.add_argument(
        "--max-chars-per-file",
        type=int,
        default=12_000,
        help="Maximum characters per included file. Default: 12000.",
    )
    parser.add_argument(
        "--show-history",
        action="store_true",
        help="Print every agent turn after the final result.",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    if not args.topic and not args.file and not args.project and not args.base_context:
        console.print("[red]Provide a topic, a --file, a --base-context, a --project, or a combination.[/red]")
        raise SystemExit(2)

    topic = args.topic or "Diagnosticar el problema técnico descrito en el contexto."
    context_parts = []

    redact_context = not args.no_redact_context
    for base_context_file in args.base_context:
        try:
            context_parts.append(
                read_context_file(
                    base_context_file,
                    title="Base Technical Context",
                    redact_secrets=redact_context,
                )
            )
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(2) from exc

    if args.file:
        try:
            context_parts.append(
                read_context_file(
                    args.file,
                    title="Incident Context File",
                    redact_secrets=redact_context,
                )
            )
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(2) from exc

    if args.project:
        if args.max_files < 1:
            console.print("[red]--max-files must be greater than 0.[/red]")
            raise SystemExit(2)
        if args.max_chars_per_file < 1:
            console.print("[red]--max-chars-per-file must be greater than 0.[/red]")
            raise SystemExit(2)
        try:
            context_parts.append(
                build_project_context(
                    project_path=args.project,
                    include_patterns=args.include or None,
                    max_files=args.max_files,
                    max_chars_per_file=args.max_chars_per_file,
                )
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise SystemExit(2) from exc

    context = "\n\n".join(context_parts)

    try:
        settings = get_settings()
    except ValidationError as exc:
        console.print("[red]Invalid configuration. Check your .env file.[/red]")
        console.print(str(exc))
        raise SystemExit(2) from exc

    console.print(
        Panel.fit(
            "\n".join(
                [
                    f"Diagnostic model: {settings.diagnostic_model}",
                    f"Skeptic model: {settings.skeptic_model}",
                    f"Moderator model: {settings.moderator_model}",
                    f"Max rounds: {settings.max_rounds}",
                    f"Confidence threshold: {settings.confidence_threshold}",
                    f"Base context files: {len(args.base_context)}",
                    f"Context redaction: {'enabled' if redact_context else 'disabled'}",
                    f"Project context: {'enabled' if args.project else 'disabled'}",
                ]
            ),
            title="Agents Discussion",
        )
    )

    try:
        result = run_debate(topic=topic, context=context)
    except Exception as exc:
        console.print("[red]Debate execution failed.[/red]")
        console.print(str(exc))
        raise SystemExit(1) from exc

    console.print(Panel(result.get("final_result") or "No final result.", title="Final Result"))

    if args.show_history:
        for item in result.get("history", []):
            console.print(Panel(item.content, title=item.role))


if __name__ == "__main__":
    main()
