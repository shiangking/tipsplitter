"""PLAN-ID: AOP-0002230a

Pure-Python string utilities module.

This module provides four dependency-free, deterministic, side-effect-free
string helpers:

- ``slugify(text)``          -- convert arbitrary text into a URL-safe slug.
- ``camel_to_snake(text)``   -- convert camelCase/PascalCase to snake_case.
- ``truncate(text, max_len)``-- truncate text with an ``...`` ellipsis marker.
- ``is_palindrome(text)``    -- case-insensitive, alphanumeric-only palindrome check.

Only the Python standard library is used (``re``). No I/O, no printing, no
global mutable state, and no third-party imports.
"""

import re

__all__ = ["slugify", "camel_to_snake", "truncate", "is_palindrome"]

# Every run of one or more characters that are NOT ASCII lowercase letters or
# digits is collapsed into a single hyphen by ``slugify``.
_SLUG_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")

# Pass 1 of the camelCase transform: split an acronym run from the following
# capitalised word, e.g. ``HTTPResponse`` -> ``HTTP_Response``.
_ACRONYM_BOUNDARY_RE = re.compile(r"(.)([A-Z][a-z]+)")

# Pass 2 of the camelCase transform: split a lowercase letter or digit from a
# following uppercase letter, e.g. ``camelCase`` -> ``camel_Case`` and
# ``version2Update`` -> ``version2_Update``.
_LOWER_UPPER_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")

# Number of characters needed by the ellipsis marker used by ``truncate``.
_ELLIPSIS = "..."


def slugify(text):
    """Convert arbitrary text into a URL-safe slug.

    The input is lowercased, every run of one or more non-ASCII-alphanumeric
    characters is replaced by exactly one hyphen, and leading/trailing hyphens
    are stripped.

    >>> slugify("Hello, World!")
    'hello-world'
    >>> slugify("!!!")
    ''
    """
    return _SLUG_SEPARATOR_RE.sub("-", text.lower()).strip("-")


def camel_to_snake(text):
    """Convert camelCase / PascalCase identifiers to snake_case.

    Boundaries are inserted between an acronym run and the following
    capitalised word (``HTTPResponse`` -> ``http_response``) and between a
    lowercase letter or digit and a following uppercase letter
    (``camelCase`` -> ``camel_case``, ``version2Update`` -> ``version2_update``).
    The result is lowercased; already lower/snake_case input is returned
    unchanged apart from lowercasing.

    >>> camel_to_snake("XMLHttpRequest")
    'xml_http_request'
    """
    # Pass order matters: acronym boundaries must be resolved before the
    # lowercase/digit -> uppercase boundaries.
    partial = _ACRONYM_BOUNDARY_RE.sub(r"\1_\2", text)
    partial = _LOWER_UPPER_BOUNDARY_RE.sub(r"\1_\2", partial)
    return partial.lower()


def truncate(text, max_len):
    """Truncate ``text`` to at most ``max_len`` characters.

    When truncation occurs the result is ``text[:max_len - 3] + '...'`` so that
    the returned string is exactly ``max_len`` characters long and ends with
    ``'...'``. When ``len(text) <= max_len`` the text is returned unchanged.

    A ``max_len`` smaller than 4 can never fit any content plus the three
    character ellipsis and therefore raises ``ValueError`` regardless of the
    text content (including empty text).

    >>> truncate("hello world", 8)
    'hello...'
    """
    if max_len < 4:
        raise ValueError("max_len must be at least 4 to fit the '...' ellipsis")
    if len(text) <= max_len:
        return text
    return text[: max_len - len(_ELLIPSIS)] + _ELLIPSIS


def is_palindrome(text):
    """Return ``True`` iff ``text`` is a case-insensitive palindrome.

    All non-alphanumeric characters are ignored and the remainder is
    lowercased before the comparison. Empty input (or input containing no
    alphanumeric characters) yields ``""`` which is a palindrome by
    convention, so ``True`` is returned.

    >>> is_palindrome("A man, a plan, a canal: Panama!")
    True
    """
    normalized = "".join(ch for ch in text.lower() if ch.isalnum())
    return normalized == normalized[::-1]
