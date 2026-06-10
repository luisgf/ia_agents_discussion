from pathlib import Path


DEFAULT_INCLUDE_PATTERNS = [
    "README*",
    "pyproject.toml",
    "requirements*.txt",
    "package.json",
    "tsconfig.json",
    "go.mod",
    "Cargo.toml",
    "Dockerfile",
    "docker-compose*.yml",
    "src/**/*",
    "tests/**/*",
]

EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "target",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".turbo",
}


def build_project_context(
    project_path: Path,
    include_patterns: list[str] | None = None,
    max_files: int = 20,
    max_chars_per_file: int = 12_000,
) -> str:
    root = project_path.expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Project path does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Project path is not a directory: {root}")

    patterns = include_patterns or DEFAULT_INCLUDE_PATTERNS
    files = _collect_files(root, patterns, max_files)

    lines = [
        "# Project Context",
        "",
        f"Project root: {root}",
        "",
        "## Included files",
        "",
    ]
    if files:
        lines.extend(f"- {_relative_path(root, path)}" for path in files)
    else:
        lines.append("- No files matched the provided include patterns.")

    for path in files:
        relative = _relative_path(root, path)
        content, truncated = _read_text_file(path, max_chars_per_file)
        if content is None:
            continue

        lines.extend(
            [
                "",
                f"## File: {relative}",
                "",
                "```text",
                content,
            ]
        )
        if truncated:
            lines.append("\n[File truncated due to max-chars-per-file limit]")
        lines.append("```")

    return "\n".join(lines)


def _collect_files(root: Path, patterns: list[str], max_files: int) -> list[Path]:
    collected: dict[Path, None] = {}

    for pattern in patterns:
        for path in root.glob(pattern):
            if len(collected) >= max_files:
                break
            if not path.is_file():
                continue
            if _is_excluded(root, path):
                continue
            collected[path.resolve()] = None
        if len(collected) >= max_files:
            break

    return sorted(collected.keys(), key=lambda item: item.relative_to(root).as_posix())[:max_files]


def _is_excluded(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(part in EXCLUDED_DIRS for part in relative.parts)


def _read_text_file(path: Path, max_chars: int) -> tuple[str | None, bool]:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None, False
    except OSError:
        return None, False

    if len(content) <= max_chars:
        return content, False
    return content[:max_chars], True


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()
