"""Atomic TOML section writer for global config."""

from __future__ import annotations

import errno
import fcntl
import os
import re
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
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as exc:
        # Structured file: raise domain error rather than silently dropping content.
        raise _ConfigError(f"Cannot decode {path} as UTF-8: {exc}") from exc
    try:
        return _tomllib.loads(text)
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
                if isinstance(i, int):
                    return str(i)
                # Bug L21: floats must round-trip as TOML floats, not as
                # quoted strings. ``repr`` preserves precision and handles
                # edge values sanely.
                if isinstance(i, float):
                    return repr(i)
                s = str(i).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
                return f'"{s}"'
            items = ", ".join(_item(i) for i in v)
            lines.append(f"{k} = [{items}]")
        elif v is None:
            continue  # TOML has no null type — skip None values
        else:
            lines.append(f"{k} = {v}")
    result = "\n".join(lines)
    for tname, tdata in tables:
        result += f"\n\n[{tname}]\n" + _simple_toml_dumps(tdata, prefix=tname)
    return result


def write_global_config_section(config_path: Path, section: str, values: dict) -> None:
    """Atomically update one section in a TOML file, preserving all other sections."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_.]*$', section):
        raise ValueError(f"invalid section name: {section!r}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = config_path.with_suffix(".lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
    except OSError as e:
        if e.errno == errno.EEXIST:
            lock_fd = os.open(str(lock_path), os.O_RDWR)
        else:
            raise
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
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        # Bug 115: do NOT unlink the lock file here. Removing a lock-file
        # dirent while a flock on its inode may still be held (by another
        # process that opened it before we released) breaks the mutual-
        # exclusion contract: a later process O_CREATs a fresh inode and
        # flocks that one, while the previous holder is still on the old
        # inode. The file is tiny, persistence is free, and the race is
        # real. Leave the dirent in place.
