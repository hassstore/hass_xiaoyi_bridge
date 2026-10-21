"""Validate the integration's JSON files and translation parity.

Runs in CI (see ``.github/workflows/tests.yml``) and locally:

    python tools/check_translations.py

Checks
------
1. Every ``.json`` file in the component parses.
2. ``strings.json`` and ``translations/en.json`` are byte-identical in content
   (HA treats ``strings.json`` as the English source of truth).
3. Every other translation has exactly the same key set as ``strings.json``.
4. Placeholders (``{name}``) match one-to-one in every language, so a missing
   argument cannot raise ``KeyError`` at runtime.

Exit code 0 means everything is consistent.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

COMPONENT = Path("custom_components/hass_xiaoyi_bridge")
SOURCE = COMPONENT / "strings.json"
TRANSLATIONS = COMPONENT / "translations"

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested translation dict into ``a.b.c`` keys."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten(value, path))
        else:
            flat[path] = value
    return flat


def placeholders(value: Any) -> set[str]:
    """Return the placeholder names used in a string value."""
    return set(PLACEHOLDER.findall(str(value)))


def main() -> int:
    """Validate JSON files and translation parity."""
    errors: list[str] = []

    json_files = sorted(COMPONENT.rglob("*.json"))
    parsed: dict[Path, dict[str, Any]] = {}
    for path in json_files:
        try:
            parsed[path] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            errors.append(f"{path}: invalid JSON ({err})")

    if errors:
        return report(errors)
    if SOURCE not in parsed:
        return report([f"{SOURCE} is missing"])

    source_flat = flatten(parsed[SOURCE])

    english = TRANSLATIONS / "en.json"
    if english in parsed and parsed[english] != parsed[SOURCE]:
        errors.append("translations/en.json differs from strings.json")

    for path, data in parsed.items():
        if path.parent != TRANSLATIONS or path == english:
            continue
        flat = flatten(data)
        missing = sorted(set(source_flat) - set(flat))
        extra = sorted(set(flat) - set(source_flat))
        if missing:
            errors.append(f"{path.name}: missing keys {missing[:5]}")
        if extra:
            errors.append(f"{path.name}: unknown keys {extra[:5]}")
        for key in sorted(set(source_flat) & set(flat)):
            expected = placeholders(source_flat[key])
            actual = placeholders(flat[key])
            if expected != actual:
                errors.append(
                    f"{path.name}:{key}: placeholders {sorted(actual)} "
                    f"!= {sorted(expected)}"
                )

    return report(errors)


def report(errors: list[str]) -> int:
    """Print the result and return the process exit code."""
    if errors:
        print("Translation check failed:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("JSON + translation parity OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
