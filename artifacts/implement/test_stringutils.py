"""Unit tests for the pure-Python ``stringutils`` module.

Plan: PLAN-ID: AOP-0002230a

Run with either of::

    python3 -m unittest test_stringutils -v
    python3 -m unittest discover -v

Only the standard library ``unittest`` module and the module under test are
used. Every test method is independent: no shared mutable state and no
ordering assumptions.
"""

import unittest

from stringutils import slugify, camel_to_snake, truncate, is_palindrome


class TestSlugify(unittest.TestCase):
    """Tests for ``stringutils.slugify``."""

    def test_basic_sentence(self):
        """S1: punctuation and case are normalised."""
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_whitespace_runs_collapse(self):
        """S2: runs of spaces/tabs collapse into a single hyphen."""
        self.assertEqual(
            slugify("  Multiple   spaces\tand-tabs "),
            "multiple-spaces-and-tabs",
        )

    def test_already_slugified_is_preserved(self):
        """S3: single hyphens are preserved, not doubled."""
        self.assertEqual(slugify("Already-slugified"), "already-slugified")

    def test_only_non_alphanumerics(self):
        """S4: punctuation-only input becomes the empty string."""
        self.assertEqual(slugify("!!!"), "")

    def test_empty_string(self):
        """S5: empty input yields the empty string."""
        self.assertEqual(slugify(""), "")

    def test_digits_and_mixed_case(self):
        """S6: digits are preserved as alphanumerics."""
        self.assertEqual(slugify("123 ABC xyz"), "123-abc-xyz")

    def test_separator_run_collapses(self):
        """S7: runs of separators collapse to one hyphen."""
        self.assertEqual(slugify("a---b"), "a-b")

    def test_underscore_is_a_separator(self):
        """S8: underscore is non-alphanumeric for slugging purposes."""
        self.assertEqual(slugify("UPPER_case MIX"), "upper-case-mix")


class TestCamelToSnake(unittest.TestCase):
    """Tests for ``stringutils.camel_to_snake``."""

    def test_camel_case(self):
        """C1: camelCase -> camel_case."""
        self.assertEqual(camel_to_snake("camelCase"), "camel_case")

    def test_pascal_case(self):
        """C2: PascalCase -> pascal_case."""
        self.assertEqual(camel_to_snake("PascalCase"), "pascal_case")

    def test_lowercase_noop(self):
        """C3: all-lowercase input is unchanged."""
        self.assertEqual(camel_to_snake("simple"), "simple")

    def test_snake_case_noop(self):
        """C4: snake_case input is unchanged (underscores preserved)."""
        self.assertEqual(camel_to_snake("snake_case"), "snake_case")

    def test_acronym_then_word(self):
        """C5: HTTPResponse -> http_response."""
        self.assertEqual(camel_to_snake("HTTPResponse"), "http_response")

    def test_multiple_acronyms(self):
        """C6: XMLHttpRequest -> xml_http_request."""
        self.assertEqual(camel_to_snake("XMLHttpRequest"), "xml_http_request")

    def test_acronym_with_digit(self):
        """C7: getHTTP2Response -> get_http2_response."""
        self.assertEqual(camel_to_snake("getHTTP2Response"), "get_http2_response")

    def test_digit_to_upper_boundary(self):
        """C8: version2Update -> version2_update (digit -> upper boundary)."""
        self.assertEqual(camel_to_snake("version2Update"), "version2_update")

    def test_empty_string(self):
        """C9: empty input yields the empty string."""
        self.assertEqual(camel_to_snake(""), "")

    def test_single_uppercase_char(self):
        """C10: single character is simply lowercased."""
        self.assertEqual(camel_to_snake("A"), "a")

    def test_pure_acronym_run(self):
        """C11: a pure acronym run is lowercased without underscores."""
        self.assertEqual(camel_to_snake("ABC"), "abc")


class TestTruncate(unittest.TestCase):
    """Tests for ``stringutils.truncate``."""

    def test_truncation_appends_ellipsis(self):
        """T1: 'hello world' truncated to 8 chars keeps 5 chars + '...'."""
        self.assertEqual(truncate("hello world", 8), "hello...")

    def test_short_text_unchanged(self):
        """T2: text shorter than the limit is returned unchanged."""
        self.assertEqual(truncate("hi", 10), "hi")

    def test_exact_fit_gets_no_ellipsis(self):
        """T3: text of exactly max_len characters is unchanged, no ellipsis."""
        result = truncate("exact", 5)
        self.assertEqual(result, "exact")
        self.assertFalse(result.endswith("..."))

    def test_minimal_valid_truncation(self):
        """T4: max_len == 4 is the minimum valid truncation length."""
        self.assertEqual(truncate("abcdef", 4), "a...")

    def test_text_matching_limit_unchanged(self):
        """T5: len(text) == max_len returns text unchanged."""
        self.assertEqual(truncate("abcdef", 6), "abcdef")

    def test_empty_text_with_valid_limit(self):
        """T6: empty text with a valid limit is returned unchanged."""
        self.assertEqual(truncate("", 4), "")

    def test_limit_three_raises(self):
        """T7: max_len == 3 raises ValueError."""
        with self.assertRaises(ValueError):
            truncate("abcdef", 3)

    def test_limit_zero_raises(self):
        """T8: max_len == 0 raises ValueError."""
        with self.assertRaises(ValueError):
            truncate("abcdef", 0)

    def test_negative_limit_raises(self):
        """T9: negative max_len raises ValueError."""
        with self.assertRaises(ValueError):
            truncate("abcdef", -1)

    def test_invalid_limit_with_empty_text_raises(self):
        """T10: the limit is validated even when text is empty."""
        with self.assertRaises(ValueError):
            truncate("", 3)

    def test_truncation_result_invariants(self):
        """T11: truncated result has length max_len and ends with '...'."""
        result = truncate("hello world", 8)
        self.assertEqual(len(result), 8)
        self.assertTrue(result.endswith("..."))

    def test_custom_truncation_invariants(self):
        """T12: custom case keeps exact length and trailing ellipsis."""
        result = truncate("abcdefghij", 9)
        self.assertEqual(result, "abcdef...")
        self.assertEqual(len(result), 9)
        self.assertTrue(result.endswith("..."))


class TestIsPalindrome(unittest.TestCase):
    """Tests for ``stringutils.is_palindrome``."""

    def test_simple_palindrome(self):
        """P1: racecar is a palindrome."""
        self.assertTrue(is_palindrome("racecar"))

    def test_case_insensitive(self):
        """P2: RaceCar is a palindrome due to case folding."""
        self.assertTrue(is_palindrome("RaceCar"))

    def test_sentence_palindrome(self):
        """P3: punctuation and spacing are ignored."""
        self.assertTrue(is_palindrome("A man, a plan, a canal: Panama!"))

    def test_quoted_palindrome(self):
        """P4: apostrophes and spaces are ignored."""
        self.assertTrue(is_palindrome("No 'x' in Nixon"))

    def test_non_palindrome(self):
        """P5: hello is not a palindrome."""
        self.assertFalse(is_palindrome("hello"))

    def test_non_palindrome_with_spaces(self):
        """P6: race a car is not a palindrome once normalised."""
        self.assertFalse(is_palindrome("race a car"))

    def test_empty_string(self):
        """P7: empty input is a palindrome by convention."""
        self.assertTrue(is_palindrome(""))

    def test_no_alphanumerics(self):
        """P8: punctuation-only input normalises to '' -> True."""
        self.assertTrue(is_palindrome("!!! ,,,"))

    def test_numeric_palindrome(self):
        """P9: digits count as alphanumeric; 12321 is a palindrome."""
        self.assertTrue(is_palindrome("12321"))

    def test_numeric_non_palindrome(self):
        """P10: 12345 is not a palindrome."""
        self.assertFalse(is_palindrome("12345"))

    def test_even_length_non_palindrome(self):
        """P11: even-length non-palindrome 'ab' returns False."""
        self.assertFalse(is_palindrome("ab"))


if __name__ == "__main__":
    unittest.main()
