# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

import re
from pathlib import Path


SECRET_PATTERNS = [
    re.compile(r"(?i)(password\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(passwd\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(pwd\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(secret\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(token\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(access[_-]?key\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(private[_-]?key\s*[=:]\s*)([^\s,;]+)"),
]

URI_CREDENTIAL_PATTERN = re.compile(r"(://[^\s:/?#]+:)([^@\s]+)(@)")


def read_context_file(path: Path, title: str, redact_secrets: bool = True) -> str:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Context file not found: {resolved}")
    if not resolved.is_file():
        raise IsADirectoryError(f"Context path is not a file: {resolved}")

    content = resolved.read_text(encoding="utf-8")
    if redact_secrets:
        content = redact_sensitive_values(content)

    return "\n".join(
        [
            f"# {title}",
            "",
            f"File: {resolved}",
            "",
            content,
        ]
    )


def redact_sensitive_values(content: str) -> str:
    redacted = content
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return URI_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]\3", redacted)
