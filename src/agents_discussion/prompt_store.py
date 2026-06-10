"""Versioned, admin-editable prompt templates.

Built-in templates ship with the package under prompt_templates/ as
<name>.<lang>.yaml files. Admins can add new templates or override built-in
ones by dropping files with the same naming scheme into PROMPTS_DIR
(default: ~/.local/share/agents-discussion/prompts). Custom files take
precedence over built-ins with the same name+language.

Each YAML file must define:
  name, language, version, description,
  diagnostic_system, skeptic_system, moderator_system
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from agents_discussion.config import get_settings

BUILTIN_DIR = Path(__file__).parent / "prompt_templates"

_REQUIRED_KEYS = (
    "name",
    "language",
    "diagnostic_system",
    "skeptic_system",
    "moderator_system",
)


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    language: str
    version: int
    description: str
    diagnostic_system: str
    skeptic_system: str
    moderator_system: str
    source: str  # "builtin" | "custom"


def _load_file(path: Path, source: str) -> PromptTemplate | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if any(not data.get(k) for k in _REQUIRED_KEYS):
            return None
        return PromptTemplate(
            name=str(data["name"]),
            language=str(data["language"]),
            version=int(data.get("version", 1)),
            description=str(data.get("description", "")),
            diagnostic_system=str(data["diagnostic_system"]).strip(),
            skeptic_system=str(data["skeptic_system"]).strip(),
            moderator_system=str(data["moderator_system"]).strip(),
            source=source,
        )
    except Exception:  # noqa: BLE001 — a broken custom file must not kill the app
        return None


def _load_all() -> dict[tuple[str, str], PromptTemplate]:
    """Map (name, language) → template. Custom files override built-ins."""
    templates: dict[tuple[str, str], PromptTemplate] = {}
    for source, directory in (("builtin", BUILTIN_DIR), ("custom", _custom_dir())):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            tpl = _load_file(path, source)
            if tpl is not None:
                templates[(tpl.name, tpl.language)] = tpl
    return templates


def _custom_dir() -> Path:
    try:
        return Path(get_settings().prompts_dir)
    except Exception:  # noqa: BLE001
        return Path.home() / ".local" / "share" / "agents-discussion" / "prompts"


def list_templates() -> list[dict]:
    """Template metadata for the UI, sorted with 'default' first."""
    items = [
        {
            "name": tpl.name,
            "language": tpl.language,
            "version": tpl.version,
            "description": tpl.description,
            "source": tpl.source,
        }
        for tpl in _load_all().values()
    ]
    return sorted(items, key=lambda t: (t["name"] != "default", t["name"], t["language"]))


def get_template(name: str = "", language: str = "") -> PromptTemplate:
    """Resolve a template with graceful fallbacks:
    (name, lang) → (name, es) → (default, lang) → (default, es)."""
    name = name or "default"
    language = language or "es"
    templates = _load_all()
    for key in ((name, language), (name, "es"), ("default", language), ("default", "es")):
        if key in templates:
            return templates[key]
    raise FileNotFoundError(
        f"No prompt template found for '{name}' ({language}) and no default available."
    )
