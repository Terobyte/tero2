"""Atomic TOML section writer for global config."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

try:
    import tomli_w as _tomli_w  # type: ignore[import]
    _HAS_TOMLI_W = True
except ImportError:
    _HAS_TOMLI_W = False

try:
    import tomllib as _tomllib
except ImportError:
    import tomli as _tomllib  # type: ignore[no-redef]

try:
    from tero2.errors import ConfigError as _ConfigError
except ImportError:
    _ConfigError = RuntimeError  # type: ignore[assignment,misc]


def _load_toml(path: Path) -> dict:
    try:
        return _tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except _tomllib.TOMLDecodeError as exc:
        raise _ConfigError(f"TOML syntax error in {path}: {exc}") from exc


def _serialize_toml(data: dict) -> str:
    if _HAS_TOMLI_W:
        return _tomli_w.dumps(data)
    return _simple_toml_dumps(data)


def _simple_toml_dumps(data: dict, prefix: str = "") -> str:
    """Fallback TOML writer for the subset of types used in tero2 config.

    CRITICAL: must pass the fully-qualified table name as `prefix` to
    recursive calls. Otherwise nested tables render as ``[b]`` instead of
    ``[a.b]`` — each ``write_global_config_section("roles.builder", …)`` call
    would corrupt the file. Install tomli-w to avoid this path entirely.
    """
    lines: list[str] = []
    tables: list[tuple[str, dict]] = []
    for k, v in data.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            tables.append((full_key, v))
        elif isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, str):
            escaped = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
            lines.append(f'{k} = "{escaped}"')
        elif isinstance(v, list):
            def _item(i):
                if isinstance(i, bool):
                    return "true" if i else "false"
                elif isinstance(i, int):
                    return str(i)
                else:
                    s = str(i).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
                    return f'"{s}"'
            items = ", ".join(_item(i) for i in v)
            lines.append(f"{k} = [{items}]")
        else:
            lines.append(f"{k} = {v}")
    result = "\n".join(lines)
    for tname, tdata in tables:
        result += f"\n\n[{tname}]\n" + _simple_toml_dumps(tdata, prefix=tname)
    return result


def write_global_config_section(config_path: Path, section: str, values: dict) -> None:
    """Atomically update one section in a TOML file, preserving all other sections."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = config_path.with_suffix(".lock")
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    tmp: Path | None = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        existing = _load_toml(config_path)
        parts = section.split(".")
        target = existing
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = values
        content = _serialize_toml(existing)
        tmp = config_path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(config_path)
        tmp = None  # successfully renamed — nothing to clean up
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
