"""Path extraction and comparison helpers for historical trace evidence."""

from __future__ import annotations

import json
import ntpath
import re
from typing import Any, Mapping, Optional, Sequence, Tuple


_PATH_KEY_NAMES = {'path', 'file', 'filename', 'filepath'}
_WINDOWS_PATH = re.compile(r'^[a-zA-Z]:[\\/]')


def find_path_argument(value: Any) -> Optional[Tuple[str, str]]:
    """Find a path argument, including camelCase keys and JSON arguments."""

    decoded = _decode_json_container(value)
    if decoded is not value:
        return find_path_argument(decoded)
    if isinstance(value, Mapping):
        for key, raw_path in value.items():
            if _normalized_key(key) not in _PATH_KEY_NAMES:
                continue
            path = optional_str(raw_path)
            if path:
                return str(key), path
        for child in value.values():
            found = find_path_argument(child)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            found = find_path_argument(child)
            if found is not None:
                return found
    return None


def extract_path(value: Any) -> Optional[str]:
    """Return only the first path value from a nested payload."""

    found = find_path_argument(value)
    return found[1] if found is not None else None


def contains_path(value: Any, path: str) -> bool:
    """Check nested values and JSON strings for the same path."""

    decoded = _decode_json_container(value)
    if decoded is not value:
        return contains_path(decoded, path)
    if isinstance(value, str):
        if path in value:
            return True
        if _WINDOWS_PATH.match(path):
            return path.casefold() in value.casefold()
        return False
    if isinstance(value, Mapping):
        return any(contains_path(item, path) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(contains_path(item, path) for item in value)
    return False


def paths_equal(left: Optional[str], right: Optional[str]) -> bool:
    """Compare Windows paths independently of host OS and path casing."""

    if not left or not right:
        return False
    if _WINDOWS_PATH.match(left) or _WINDOWS_PATH.match(right):
        return ntpath.normpath(left).casefold() == ntpath.normpath(right).casefold()
    return left == right


def optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decode_json_container(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped.startswith(('{', '[')):
        return value
    try:
        decoded = json.loads(stripped)
    except (TypeError, ValueError):
        return value
    return decoded if isinstance(decoded, (Mapping, list)) else value


def _normalized_key(value: Any) -> str:
    return str(value).replace('_', '').replace('-', '').casefold()
