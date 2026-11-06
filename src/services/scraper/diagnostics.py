"""
What the diagnostics of the page reader may keep: the structure of responses, never their content.

The result goes to a file that is meant to be shown to whoever fixes twogis_site.py, so names of
keys pass only if they look like names of fields: a key may as well be a phone or an id.
"""

import re
from typing import Any
from urllib.parse import urlsplit

_MAX_DEPTH: int = 10
_MAX_KEYS: int = 80
_MAX_LIST_ITEMS: int = 20
_MASKED_KEY: str = "<ключ>"
_MASKED_SEGMENT: str = "<значение>"
_MORE_KEYS: str = "<другие ключи>"
_TOO_DEEP: str = "<глубже не смотрели>"

_FIELD_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]{0,63}", re.ASCII)
_MAX_DIGITS_IN_NAME: int = 4
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,31}|[0-9]+\.[0-9]+", re.ASCII)
_INLINE_STATE_RE = re.compile(r"(?:\bvar\s+|\bwindow\.)([A-Za-z_$][\w$]{0,40})\s*=\s*(?:JSON\.parse\(|\{)", re.ASCII)
_MAX_INLINE_NAMES: int = 10
_OUTLINE_MAX_DEPTH: int = 24
_OUTLINE_MAX_PATHS: int = 5000
# Names of keys worth finding in the state of a page: this is what the parser looks for.
_INTERESTING_KEYS: frozenset[str] = frozenset(
    {"contact_groups", "contacts", "phones", "phone", "website", "url", "email", "links", "name", "address", "rubrics"}
)


def _key_name(key: str) -> str:
    if _FIELD_NAME_RE.fullmatch(key) and sum(char.isdigit() for char in key) <= _MAX_DIGITS_IN_NAME:
        return key
    return _MASKED_KEY


def _merge(first: Any, second: Any) -> Any:
    """Records of a list differ in their fields, and the union of them is what is of interest."""
    if isinstance(first, dict) and isinstance(second, dict):
        merged = dict(first)
        for key, value in second.items():
            merged[key] = _merge(merged[key], value) if key in merged else value
        return merged
    if isinstance(first, list) and isinstance(second, list):
        return [_merge(first[0], second[0])] if first and second else first or second
    if first == second:
        return first
    return "|".join(sorted({_type_label(first), _type_label(second)}))


def _type_label(shape: Any) -> str:
    if isinstance(shape, dict):
        return "object"
    return "array" if isinstance(shape, list) else str(shape)


def json_shape(value: Any, depth: int = 0) -> Any:
    """The same nesting, with names of types in place of all values."""
    if isinstance(value, bool):
        return "bool"
    if value is None:
        return "null"
    if isinstance(value, str):
        return "str"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if depth >= _MAX_DEPTH:
        return _TOO_DEEP
    if isinstance(value, dict):
        shape: dict[str, Any] = {}
        for position, (key, item) in enumerate(value.items()):
            if position == _MAX_KEYS:
                shape[_MORE_KEYS] = len(value) - _MAX_KEYS
                break
            name, item_shape = _key_name(str(key)), json_shape(item, depth + 1)
            shape[name] = _merge(shape[name], item_shape) if name in shape else item_shape
        return shape
    if isinstance(value, list):
        shapes = [json_shape(item, depth + 1) for item in value[:_MAX_LIST_ITEMS]]
        if not shapes:
            return []
        merged = shapes[0]
        for item_shape in shapes[1:]:
            merged = _merge(merged, item_shape)
        return [merged]
    return type(value).__name__


def masked_url(url: str) -> str:
    """Host and path only: the query holds keys and search words, the path may hold ids."""
    parts = urlsplit(url)
    segments = [
        segment if _PATH_SEGMENT_RE.fullmatch(segment) else _MASKED_SEGMENT
        for segment in parts.path.split("/")
        if segment
    ]
    return f"{parts.scheme}://{parts.netloc}/{'/'.join(segments)}"


def inline_state_names(html: str) -> list[str]:
    """Names of variables with data in the text of a page: a page may bring its data without any request."""
    names = dict.fromkeys(match.group(1) for match in _INLINE_STATE_RE.finditer(html))
    return list(names)[:_MAX_INLINE_NAMES]


def state_outline(value: Any) -> dict[str, Any]:
    """
    A map of a big value: paths of keys, types of values, the longest array on a path. No values, no texts.

    Paths are the same for all items of an array and for keys that are not names of fields, so
    the map stays small. `key_hits` says where the keys of interest are, and how many there are.
    """
    outline: dict[str, dict[str, Any]] = {}
    hits: dict[str, int] = {}
    truncated = False
    # Not recursive: a page is free to nest as deep as it likes.
    stack: list[tuple[str, Any, int]] = [("$", value, 0)]
    while stack:
        path, item, depth = stack.pop()
        if path not in outline and len(outline) >= _OUTLINE_MAX_PATHS:
            truncated = True
            continue
        entry = outline.setdefault(path, {"types": set(), "count": 0})
        entry["types"].add(_type_label(item if isinstance(item, (dict, list)) else json_shape(item)))
        entry["count"] += 1
        if depth >= _OUTLINE_MAX_DEPTH:
            truncated = truncated or isinstance(item, (dict, list))
        elif isinstance(item, dict):
            for key, child in item.items():
                name = _key_name(str(key))
                if name.lower() in _INTERESTING_KEYS:
                    hits[f"{path}.{name}"] = hits.get(f"{path}.{name}", 0) + 1
                stack.append((f"{path}.{name}", child, depth + 1))
        elif isinstance(item, list):
            entry["max_length"] = max(entry.get("max_length", 0), len(item))
            stack.extend((f"{path}[]", child, depth + 1) for child in item)
    return {
        "paths": [{"path": path, **entry, "types": sorted(entry["types"])} for path, entry in sorted(outline.items())],
        "key_hits": [{"path": path, "count": count} for path, count in sorted(hits.items())],
        "truncated": truncated,
    }
