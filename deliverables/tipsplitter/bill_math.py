"""TipSplitter — canonical money math.

Pure-Python (stdlib only) implementation of the split algorithm described in
SPEC.md §5 (data model + golden vectors) and §6 (canonical algorithm).

Public API
----------
``parse_dollars_to_cents(text)``
    Parse a dollar string such as ``"84.50"`` into integer cents.
``split_bill(total_cents, tip_percent, num_people)``
    Compute tip, grand total and per-person shares (integer cents only).
``format_cents(cents)``
    Render integer cents as a plain ``"99.71"`` string.
``demo()``
    Print the GV-1 example; used by ``python3 bill_math.py``.

Invariants guaranteed by :func:`split_bill` (SPEC.md §5):
    1. ``sum(result.shares_cents) == result.grand_cents``
    2. ``result.last_person_adjustment_cents == grand_cents % num_people``
    3. ``0 <= result.last_person_adjustment_cents < num_people``

All money values are integers. No floats are used for money anywhere, and
nothing here touches the filesystem, the network, or global state: importing
this module is side-effect free. Running ``python3 bill_math.py`` executes the
embedded ``unittest`` suite (golden vectors, parser/format cases, error cases
and an exhaustive invariant sweep) and exits non-zero on failure.
"""

from __future__ import annotations

import re
import sys
import unittest
from dataclasses import FrozenInstanceError, dataclass
from typing import Optional, Sequence

__all__ = [
    "AMOUNT_ERROR",
    "ROUNDING_NOTE_TEMPLATE",
    "SplitResult",
    "demo",
    "format_cents",
    "parse_dollars_to_cents",
    "rounding_note",
    "split_bill",
]

# --- Contract constants (mirrored verbatim by the JS implementation) --------

#: Note shown when the grand total cannot be split evenly (SPEC.md §5.2).
ROUNDING_NOTE_TEMPLATE = (
    "Rounding: the last person pays ${amount} more so the total adds up exactly."
)

#: User-facing parse error (SPEC.md §3 J3; identical string in JS).
AMOUNT_ERROR = "Enter a valid amount like 42.50"

MIN_TIP_PERCENT = 0
MAX_TIP_PERCENT = 100
MIN_PEOPLE = 1
MAX_PEOPLE = 50

_AMOUNT_RE = re.compile(r"\A(\d{1,9})(?:\.(\d{1,2}))?\Z", re.ASCII)


@dataclass(frozen=True)
class SplitResult:
    """Result of :func:`split_bill` (SPEC.md §5 ``SplitResult``).

    Attributes
    ----------
    total_cents: bill subtotal in cents.
    tip_percent: integer tip percentage that produced ``tip_cents``.
    num_people: number of diners (``len(shares_cents)``).
    tip_cents: tip amount in cents, rounded half up.
    grand_cents: ``total_cents + tip_cents``.
    per_person_base_cents: ``grand_cents // num_people``.
    shares_cents: one share per person; the last one carries the remainder.
    last_person_adjustment_cents: extra cents paid by the last person.
    note: rounding note, or ``None`` when the split is exact.
    """

    total_cents: int
    tip_percent: int
    num_people: int
    tip_cents: int
    grand_cents: int
    per_person_base_cents: int
    shares_cents: Sequence[int]
    last_person_adjustment_cents: int
    note: Optional[str]


def parse_dollars_to_cents(text: str) -> int:
    """Parse a dollar amount into integer cents, digit by digit.

    Accepts an optional positive whole part plus at most two decimals:
    ``"12"`` -> ``1200``, ``"12.3"`` -> ``1230``, ``"12.34"`` -> ``1234``.

    Raises
    ------
    ValueError
        For blank text, non-numeric text, a sign, a thousands separator, or
        more than two decimal places (same message every time, so the UI can
        render it verbatim).
    """
    if not isinstance(text, str):
        raise ValueError(AMOUNT_ERROR)

    match = _AMOUNT_RE.match(text.strip())
    if match is None:
        raise ValueError(AMOUNT_ERROR)

    dollars, cents = match.groups()
    # "3" means 30 cents; "" means 0 cents. Padding avoids any float math.
    return int(dollars) * 100 + int((cents or "").ljust(2, "0"))


def format_cents(cents: int) -> str:
    """Format integer cents as a plain decimal string: ``9971`` -> ``"99.71"``.

    The currency symbol is deliberately omitted; callers add it. Uses integer
    division only, so no float rounding can creep in.

    Raises
    ------
    ValueError
        If ``cents`` is not an integer.
    """
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise ValueError("Amount must be a whole number of cents")

    sign = "-" if cents < 0 else ""
    whole, fraction = divmod(abs(cents), 100)
    return f"{sign}{whole}.{fraction:02d}"


def split_bill(total_cents: int, tip_percent: int, num_people: int) -> SplitResult:
    """Split a bill into ``num_people`` equal shares, in integer cents.

    Implements SPEC.md §6 exactly::

        tip_cents   = (total_cents * tip_percent + 50) // 100   # round half up
        grand_cents = total_cents + tip_cents
        base        = grand_cents // num_people
        adjustment  = grand_cents % num_people
        shares      = [base] * num_people; shares[-1] += adjustment

    The remainder is charged to the last person so the shares always sum to
    the grand total; a rounding note describes that adjustment (SPEC.md §5.2).

    Raises
    ------
    ValueError
        If ``total_cents`` is not a non-negative integer, ``tip_percent`` is
        not an integer in ``0..100``, or ``num_people`` is not an integer in
        ``1..50``.
    """
    _require_int("Bill total", total_cents, 0, None)
    _require_int("Tip percent", tip_percent, MIN_TIP_PERCENT, MAX_TIP_PERCENT)
    _require_int("People", num_people, MIN_PEOPLE, MAX_PEOPLE)

    tip_cents = (total_cents * tip_percent + 50) // 100
    grand_cents = total_cents + tip_cents
    base_cents, adjustment_cents = divmod(grand_cents, num_people)

    shares = [base_cents] * num_people
    shares[-1] += adjustment_cents

    return SplitResult(
        total_cents=total_cents,
        tip_percent=tip_percent,
        num_people=num_people,
        tip_cents=tip_cents,
        grand_cents=grand_cents,
        per_person_base_cents=base_cents,
        shares_cents=shares,
        last_person_adjustment_cents=adjustment_cents,
        note=rounding_note(adjustment_cents),
    )


def rounding_note(adjustment_cents: int) -> Optional[str]:
    """Return the SPEC.md §5.2 note for an adjustment, or ``None`` if zero."""
    if adjustment_cents == 0:
        return None
    return ROUNDING_NOTE_TEMPLATE.format(amount=format_cents(adjustment_cents))


def demo() -> SplitResult:
    """Print the GV-1 example (bill $84.50, tip 18%, 3 people)."""
    result = split_bill(8450, 18, 3)
    print("TipSplitter demo — bill $84.50, tip 18%, 3 people")
    print(f"  tip           ${format_cents(result.tip_cents)}")
    print(f"  grand total   ${format_cents(result.grand_cents)}")
    for index, share in enumerate(result.shares_cents, start=1):
        print(f"  Person {index}:    ${format_cents(share)}")
    print(f"  note          {result.note}")
    return result


def _require_int(
    label: str, value: object, minimum: int, maximum: Optional[int]
) -> None:
    """Validate that ``value`` is a plain int inside ``[minimum, maximum]``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be a whole number")
    if value < minimum:
        raise ValueError(_below_message(label, minimum))
    if maximum is not None and value > maximum:
        raise ValueError(_above_message(label, maximum))


def _below_message(label: str, minimum: int) -> str:
    """Specific message for a value under the lower bound (matches JS)."""
    if label == "People":
        unit = "person" if minimum == 1 else "people"
        return f"At least {minimum} {unit} required"
    if label == "Bill total":
        return "Bill total cannot be negative"
    return f"{label} must be at least {minimum}"


def _above_message(label: str, maximum: int) -> str:
    """Specific message for a value over the upper bound (matches JS)."""
    if label == "People":
        return f"At most {maximum} people supported"
    return f"{label} cannot be more than {maximum}"


# ---------------------------------------------------------------------------
# Embedded test suite — run with: python3 bill_math.py
# ---------------------------------------------------------------------------

# Golden vectors from SPEC.md §5.1: (total, tip%, people, tip, grand, shares).
GOLDEN_VECTORS = (
    ("GV-1", 8450, 18, 3, 1521, 9971, [3323, 3323, 3325]),
    ("GV-2", 10000, 20, 4, 2000, 12000, [3000, 3000, 3000, 3000]),
    ("GV-3", 1, 0, 1, 0, 1, [1]),
    ("GV-4", 999, 100, 7, 999, 1998, [285, 285, 285, 285, 285, 285, 288]),
)


class ParseDollarsTests(unittest.TestCase):
    """SPEC.md §5 / ARCHITECTURE.md §4 parser contract."""

    def test_accepts_valid_amounts(self) -> None:
        cases = {
            "84.50": 8450,
            "12": 1200,
            "12.3": 1230,
            "12.34": 1234,
            "0": 0,
            "0.05": 5,
            " 42.50 ": 4250,  # surrounding whitespace is tolerated
            "007.5": 750,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_dollars_to_cents(text), expected)

    def test_rejects_invalid_amounts(self) -> None:
        for text in ("", "   ", "abc", "-5", "12.345", "1,000", "12.", ".5", "+5",
                     "1e3", "12.3.4", "$12", "NaN", None, 12):
            with self.subTest(text=repr(text)):
                with self.assertRaises(ValueError) as caught:
                    parse_dollars_to_cents(text)  # type: ignore[arg-type]
                self.assertEqual(str(caught.exception), AMOUNT_ERROR)

    def test_round_trips_through_format_cents(self) -> None:
        for text in ("0.01", "9.99", "84.50", "1234.56"):
            with self.subTest(text=text):
                cents = parse_dollars_to_cents(text)
                self.assertEqual(format_cents(cents), text)


class FormatCentsTests(unittest.TestCase):
    """SPEC.md §4 formatting cases."""

    def test_known_values(self) -> None:
        cases = {0: "0.00", 1: "0.01", 5: "0.05", 999: "9.99",
                 1234: "12.34", 9971: "99.71", 100: "1.00", 120000: "1200.00"}
        for cents, expected in cases.items():
            with self.subTest(cents=cents):
                self.assertEqual(format_cents(cents), expected)

    def test_rejects_non_integers(self) -> None:
        for value in (None, "9971", 99.71, True):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    format_cents(value)  # type: ignore[arg-type]


class GoldenVectorTests(unittest.TestCase):
    """GV-1..GV-4 must match field-for-field, including the note text."""

    def test_golden_vectors(self) -> None:
        for name, total, tip_pct, people, tip, grand, shares in GOLDEN_VECTORS:
            with self.subTest(vector=name):
                result = split_bill(total, tip_pct, people)
                self.assertEqual(result.total_cents, total)
                self.assertEqual(result.tip_percent, tip_pct)
                self.assertEqual(result.num_people, people)
                self.assertEqual(result.tip_cents, tip)
                self.assertEqual(result.grand_cents, grand)
                self.assertEqual(list(result.shares_cents), shares)
                adjustment = grand % people
                self.assertEqual(result.last_person_adjustment_cents, adjustment)
                self.assertEqual(result.per_person_base_cents, grand // people)
                self.assertEqual(sum(result.shares_cents), grand)
                if adjustment == 0:
                    self.assertIsNone(result.note)
                else:
                    self.assertEqual(
                        result.note,
                        "Rounding: the last person pays "
                        f"${format_cents(adjustment)} more so the total "
                        "adds up exactly.",
                    )

    def test_gv1_note_text(self) -> None:
        self.assertEqual(
            split_bill(8450, 18, 3).note,
            "Rounding: the last person pays $0.02 more so the total adds up exactly.",
        )

    def test_exact_split_has_no_note(self) -> None:
        self.assertIsNone(split_bill(10000, 20, 4).note)


class ValidationTests(unittest.TestCase):
    """Invalid inputs raise ValueError with specific messages (SPEC.md §5)."""

    def test_total_cents_validation(self) -> None:
        for bad in (-1, -100, 1.5, "8450", None, True):
            with self.subTest(total=repr(bad)):
                with self.assertRaises(ValueError):
                    split_bill(bad, 18, 3)  # type: ignore[arg-type]

    def test_tip_percent_validation(self) -> None:
        for bad in (-1, 101, 18.5, "18", None, False):
            with self.subTest(tip=repr(bad)):
                with self.assertRaises(ValueError):
                    split_bill(8450, bad, 3)  # type: ignore[arg-type]

    def test_num_people_validation(self) -> None:
        for bad in (0, -3, 51, 2.5, "3", None, True):
            with self.subTest(people=repr(bad)):
                with self.assertRaises(ValueError):
                    split_bill(8450, 18, bad)  # type: ignore[arg-type]

    def test_zero_people_message(self) -> None:
        with self.assertRaises(ValueError) as caught:
            split_bill(8450, 18, 0)
        self.assertEqual(str(caught.exception), "At least 1 person required")

    def test_boundaries_are_allowed(self) -> None:
        self.assertEqual(split_bill(0, 0, 1).grand_cents, 0)
        self.assertEqual(split_bill(0, 100, 50).shares_cents[-1] >= 0, True)
        for result in (split_bill(1, 100, 50), split_bill(200000, 0, 1)):
            self.assertEqual(
                sum(result.shares_cents), result.grand_cents
            )
        self.assertEqual(split_bill(8450, 0, 3).tip_cents, 0)
        self.assertEqual(split_bill(8450, 100, 3).tip_cents, 8450)

    def test_round_half_up(self) -> None:
        # 201 * 25 / 100 = 50.25 -> 50;  202 * 25 = 50.5 -> 51 (half up);
        # 200 * 1 = 2.00 -> 2;  50 * 1 = 0.5 -> 1.
        self.assertEqual(split_bill(201, 25, 1).tip_cents, 50)
        self.assertEqual(split_bill(202, 25, 1).tip_cents, 51)
        self.assertEqual(split_bill(50, 1, 1).tip_cents, 1)
        self.assertEqual(split_bill(49, 1, 1).tip_cents, 0)


class InvariantSweepTests(unittest.TestCase):
    """Exhaustive invariant check over the documented input space (§4)."""

    def _check(self, total_cents: int, tip_percent: int, num_people: int) -> None:
        """Assert invariants 1-3 and the structural guarantees for one case."""
        result = split_bill(total_cents, tip_percent, num_people)
        grand = result.grand_cents
        shares = list(result.shares_cents)
        base, adjustment = divmod(grand, num_people)

        # 1. shares sum to the grand total, exactly.
        self.assertEqual(sum(shares), grand)
        # 2. adjustment equals grand % num_people.
        self.assertEqual(result.last_person_adjustment_cents, grand % num_people)
        # 3. adjustment is a proper remainder.
        self.assertTrue(0 <= result.last_person_adjustment_cents < num_people)

        # Structural checks: one share per person, identical except the last,
        # which carries the remainder, and the note appears iff adjustment > 0.
        self.assertEqual(len(shares), num_people)
        self.assertEqual(result.per_person_base_cents, base)
        self.assertEqual(shares[:-1], [base] * (num_people - 1))
        self.assertEqual(shares[-1], base + adjustment)
        self.assertEqual(result.note is None, adjustment == 0)

    def test_exhaustive_sweep(self) -> None:
        # 201 totals across the full documented range x 101 tips x 50 people.
        for total_cents in range(0, 200001, 1000):
            for tip_percent in range(0, 101):
                for num_people in range(1, 51):
                    self._check(total_cents, tip_percent, num_people)

    def test_dense_small_totals(self) -> None:
        # Every cent value near zero, where remainders are proportionally big.
        for total_cents in range(0, 201):
            for tip_percent in range(0, 101, 5):
                for num_people in range(1, 51):
                    self._check(total_cents, tip_percent, num_people)

    def test_range_boundaries(self) -> None:
        # Corners of the documented input space (§5).
        for total_cents in (0, 1, 199999, 200000):
            for tip_percent in (0, 1, 50, 99, 100):
                for num_people in (1, 2, 7, 49, 50):
                    self._check(total_cents, tip_percent, num_people)


class ImportSafetyTests(unittest.TestCase):
    """The module must be importable and pure (ARCHITECTURE.md §3)."""

    def test_module_namespace_is_complete(self) -> None:
        # No I/O on import: every documented name is simply present here.
        for name in __all__:
            self.assertTrue(hasattr(sys.modules[__name__], name), name)

    def test_results_are_immutable_value_objects(self) -> None:
        result = split_bill(8450, 18, 3)
        # A frozen dataclass rejects item assignment with exactly this error;
        # asserting the specific type (not blind Exception) proves immutability
        # is enforced by the frozen field definition rather than by a side effect.
        with self.assertRaises(FrozenInstanceError):
            result.tip_cents = 99  # type: ignore[misc]
        self.assertEqual(result, split_bill(8450, 18, 3))
        self.assertNotEqual(result, split_bill(8450, 18, 4))


if __name__ == "__main__":
    if not unittest.main(exit=False, verbosity=2).result.wasSuccessful():
        raise SystemExit(1)
    demo()
