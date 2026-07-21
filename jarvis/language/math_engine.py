"""
language/math_engine.py
=========================

The "mathematical foundation" knowledge base: arithmetic evaluation, unit
conversion, and basic statistics over numbers found in retrieved document
chunks (e.g. spec tables). Built on `sympy`, a symbolic-math *library*
(deterministic algebra, not a trained model) — there is no LLM or neural
component anywhere in this file.

Design decisions
-----------------
- Arithmetic expressions are evaluated via `sympy.parsing.sympy_parser`
  with a restricted transformation set and a symbol whitelist of exactly
  zero free variables allowed for direct evaluation — this is NOT Python
  `eval()` on user input, which would be a code-execution vulnerability.
  Sympy's parser builds an expression tree; we only ever call `.evalf()`
  on it, never exec arbitrary code.
- The unit-conversion table is hand-authored and scoped to the domains
  the spec calls out (mechanical/electrical engineering): torque,
  length, mass, pressure, temperature. It's intentionally a plain dict of
  conversion factors, not a general-purpose units library dependency,
  keeping this Citrix-safe (zero extra installs beyond sympy).
- `extract_numeric_facts()` pulls `(value, unit)` pairs out of raw chunk
  text with a regex over the unit table's known unit tokens, so retrieved
  passages like "25 Nm" or "M8 bolts torque to 25 Nm" become structured
  data the reasoning/generation layers can compute over (e.g. "is 18 Nm
  within 10% of the 25 Nm spec?").
- `table_statistics()` operates on the pipe-delimited table text produced
  by `parser/tabular_parser.py` and `parser/chunker.py` — it re-parses
  that same rendering rather than needing a second, separate structured
  representation of table chunks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import sympy
from sympy.parsing.sympy_parser import parse_expr, standard_transformations

_SAFE_TRANSFORMATIONS = standard_transformations  # no implicit-multiplication-of-identifiers magic needed

# ------------------------------------------------------------------
# Unit conversion table: (unit, dimension) -> factor to the dimension's
# base unit. Conversion between two units of the same dimension is
# value_in_base = value * factor[from]; result = value_in_base / factor[to]
# (temperature handled specially, it's not a pure multiplicative scale).
# ------------------------------------------------------------------
_LENGTH_BASE = "mm"
_MASS_BASE = "kg"
_TORQUE_BASE = "Nm"
_PRESSURE_BASE = "bar"

_UNIT_TABLE = {
    # length (base: mm)
    "mm": ("length", 1.0), "cm": ("length", 10.0), "m": ("length", 1000.0),
    "in": ("length", 25.4), "ft": ("length", 304.8),
    # mass (base: kg)
    "kg": ("mass", 1.0), "g": ("mass", 0.001), "lb": ("mass", 0.453592), "oz": ("mass", 0.0283495),
    # torque (base: Nm)
    "nm": ("torque", 1.0), "n·m": ("torque", 1.0),
    "lb-ft": ("torque", 1.35582), "lbf-ft": ("torque", 1.35582), "ft-lb": ("torque", 1.35582),
    "lb-in": ("torque", 0.112985), "in-lb": ("torque", 0.112985),
    # pressure (base: bar)
    "bar": ("pressure", 1.0), "psi": ("pressure", 0.0689476), "kpa": ("pressure", 0.01), "mpa": ("pressure", 10.0),
}

_UNIT_TOKEN_PATTERN = "|".join(sorted((re.escape(u) for u in _UNIT_TABLE), key=len, reverse=True))
_NUMBER_WITH_UNIT_RE = re.compile(
    rf"(-?\d+(?:\.\d+)?)\s*({_UNIT_TOKEN_PATTERN})\b", re.IGNORECASE
)


class MathEngineError(Exception):
    pass


@dataclass
class NumericFact:
    value: float
    unit: Optional[str]
    raw_text: str


_OPERATOR_EXPR_RE = re.compile(r"\d\s*[\+\-\*/×÷]\s*\d")


_MATH_KEYWORDS_RE = re.compile(
    r"\b(average|mean|sum|total|minimum|maximum|convert|calculate|compute|percent|percentage)\b",
    re.IGNORECASE,
)


def looks_like_math_question(question: str) -> bool:
    q = question.lower()
    if _OPERATOR_EXPR_RE.search(q):
        return True
    # Keyword-triggered math (e.g. "average torque in the table") does NOT
    # require a digit in the question itself -- the numbers being averaged
    # live in the retrieved documents, not the question text. Word-boundary
    # matching (not plain substring) avoids false positives like "mean"
    # matching inside "meaning" ("what is the meaning of life").
    return bool(_MATH_KEYWORDS_RE.search(q)) or "difference between" in q


def evaluate_arithmetic(expression: str) -> str:
    """Safely evaluates a plain arithmetic expression (no variables) and
    returns a human-readable result. Raises MathEngineError on anything
    that isn't a closed-form numeric expression (e.g. contains a free
    variable), rather than guessing.
    """
    cleaned = expression.replace("×", "*").replace("÷", "/").strip()
    try:
        expr = parse_expr(cleaned, transformations=_SAFE_TRANSFORMATIONS, evaluate=True)
    except (SyntaxError, TypeError, sympy.SympifyError) as exc:
        raise MathEngineError(f"Couldn't parse '{expression}' as an arithmetic expression: {exc}")

    free_symbols = expr.free_symbols
    if free_symbols:
        raise MathEngineError(f"Expression contains unresolved terms: {', '.join(str(s) for s in free_symbols)}")

    result = expr.evalf()
    if result == int(result):
        return str(int(result))
    return str(round(float(result), 6))


def convert_units(value: float, from_unit: str, to_unit: str) -> float:
    from_key, to_key = from_unit.lower(), to_unit.lower()
    if from_key not in _UNIT_TABLE or to_key not in _UNIT_TABLE:
        raise MathEngineError(f"Unknown unit(s): '{from_unit}' -> '{to_unit}'")
    from_dim, from_factor = _UNIT_TABLE[from_key]
    to_dim, to_factor = _UNIT_TABLE[to_key]
    if from_dim != to_dim:
        raise MathEngineError(f"Cannot convert '{from_unit}' ({from_dim}) to '{to_unit}' ({to_dim}) — different dimensions")
    return value * from_factor / to_factor


def extract_numeric_facts(text: str) -> List[NumericFact]:
    facts = []
    for match in _NUMBER_WITH_UNIT_RE.finditer(text):
        value_str, unit = match.groups()
        try:
            facts.append(NumericFact(value=float(value_str), unit=unit.lower(), raw_text=match.group(0)))
        except ValueError:
            continue
    return facts


def table_statistics(table_text: str, column_index: Optional[int] = None) -> Optional[dict]:
    """Parses the pipe-delimited table rendering used throughout the
    parser layer and computes basic stats (mean/min/max/count) for a
    numeric column. If `column_index` isn't given, the first column that
    parses as fully numeric (excluding the header row) is used.
    """
    lines = [line for line in table_text.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    rows = [[cell.strip() for cell in line.split("|")] for line in lines]
    header, data_rows = rows[0], rows[1:]

    def col_is_numeric(idx: int) -> bool:
        values = [r[idx] for r in data_rows if idx < len(r)]
        if not values:
            return False
        for v in values:
            try:
                float(v)
            except ValueError:
                return False
        return True

    if column_index is None:
        candidates = [i for i in range(len(header)) if col_is_numeric(i)]
        if not candidates:
            return None
        column_index = candidates[0]
    elif not col_is_numeric(column_index):
        return None

    numbers = [float(r[column_index]) for r in data_rows if column_index < len(r)]
    if not numbers:
        return None

    return {
        "column_name": header[column_index] if column_index < len(header) else f"column {column_index}",
        "count": len(numbers), "mean": sum(numbers) / len(numbers),
        "min": min(numbers), "max": max(numbers), "sum": sum(numbers),
    }


def percent_difference(value: float, reference: float) -> float:
    if reference == 0:
        raise MathEngineError("Cannot compute percent difference against a zero reference value")
    return (value - reference) / reference * 100.0
