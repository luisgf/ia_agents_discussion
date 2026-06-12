from pathlib import Path

from agents_discussion.tools import _resolve_ssh_key
from agents_discussion.web import _append_ssh_defaults


def _make_key(tmp_path: Path, name: str = "clave") -> Path:
    key = tmp_path / name
    key.write_text("---KEY---")
    return key


# ── _resolve_ssh_key ────────────────────────────────────────────────────


def test_existing_requested_path_used(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SSH_KEY_PATH", raising=False)
    key = _make_key(tmp_path)
    filename, note = _resolve_ssh_key(str(key))
    assert filename == str(key)
    assert note == ""


def test_missing_requested_falls_back_to_env_default(tmp_path, monkeypatch) -> None:
    default = _make_key(tmp_path, "default_key")
    monkeypatch.setenv("SSH_KEY_PATH", str(default))
    filename, note = _resolve_ssh_key("/home/user/.ssh/id_ed25519")
    assert filename == str(default)
    assert "no existe" in note
    assert "/home/user/.ssh/id_ed25519" in note


def test_missing_requested_and_missing_default_fails_clearly(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SSH_KEY_PATH", str(tmp_path / "tampoco_existe"))
    filename, note = _resolve_ssh_key(str(tmp_path / "no_existe"))
    assert filename is None
    assert note.startswith("SSH key file not found")
    assert "not found either" in note


def test_missing_requested_no_default_fails_clearly(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("SSH_KEY_PATH", raising=False)
    filename, note = _resolve_ssh_key(str(tmp_path / "no_existe"))
    assert filename is None
    assert note.startswith("SSH key file not found")


def test_empty_request_uses_env_default(tmp_path, monkeypatch) -> None:
    default = _make_key(tmp_path)
    monkeypatch.setenv("SSH_KEY_PATH", str(default))
    filename, note = _resolve_ssh_key("")
    assert filename == str(default)
    assert note == ""


def test_empty_request_no_default_autodiscover(monkeypatch) -> None:
    monkeypatch.delenv("SSH_KEY_PATH", raising=False)
    assert _resolve_ssh_key("") == (None, "")


def test_expanduser(tmp_path, monkeypatch) -> None:
    key = _make_key(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    filename, note = _resolve_ssh_key("~/clave")
    assert filename == str(key)
    assert note == ""


# ── _append_ssh_defaults ────────────────────────────────────────────────


def test_context_injects_existing_key(tmp_path) -> None:
    key = _make_key(tmp_path)
    out = _append_ssh_defaults("ctx", "h", "u", 22, str(key))
    assert f"Default SSH key path: {key}" in out


def test_context_omits_missing_key(tmp_path) -> None:
    out = _append_ssh_defaults("ctx", "h", "u", 22, str(tmp_path / "no_existe"))
    assert "Default SSH key path" not in out
    assert "Default SSH host: h" in out
