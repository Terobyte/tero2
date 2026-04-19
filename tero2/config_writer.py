"""Atomic TOML section writer for global config."""

from __future__ import annotations

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


def _load_toml(path: Path) -> dict:
    try:
        return _tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}


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
            escaped = v.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{k} = "{escaped}"')
        elif isinstance(v, list):
            items = ", ".join(f'"{i}"' for i in v)
            lines.append(f"{k} = [{items}]")
        else:
            lines.append(f"{k} = {v}")
    result = "\n".join(lines)
    for tname, tdata in tables:
        result += f"\n\n[{tname}]\n" + _simple_toml_dumps(tdata, prefix=tname)
    return result


def write_global_config_section(config_path: Path, section: str, values: dict) -> None:
    """Atomically update one section in a TOML file, preserving all other sections."""
    existing = _load_toml(config_path)
    # Navigate/create nested section path
    parts = section.split(".")
    target = existing
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = values

    content = _serialize_toml(existing)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.replace(config_path)
