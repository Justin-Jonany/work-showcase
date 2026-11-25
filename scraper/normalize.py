"""Messy scraped strings -> numbers. Every parser returns None (or a tuple of Nones) when it can't tell.

    python scraper/normalize.py --test
"""
import argparse
import re
from datetime import date

# Approximate USD per unit, mid-2024. Good enough for size bucketing; not for accounting.
FX_TO_USD = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "CAD": 0.73,
    "INR": 0.012,
    "JPY": 0.0067,
    "AUD": 0.66,
    "CHF": 1.12,
}

# Prefixed dollar forms must be checked before the bare "$" so C$/A$ aren't read as USD.
_CURRENCY_PATTERNS = [
    (r"\bUS\$|\bUSD\b", "USD"),
    (r"\bCA?\$|\bCAD\b", "CAD"),
    (r"\bAU?\$|\bAUD\b", "AUD"),
    (r"€|\bEUR\b|\beuros?\b", "EUR"),
    (r"£|\bGBP\b", "GBP"),
    (r"₹|\bINR\b|\brupees?\b", "INR"),
    (r"¥|\bJPY\b|\byen\b", "JPY"),
    (r"\bCHF\b", "CHF"),
    (r"\$", "USD"),
]

_MULT = {
    "k": 1e3, "thousand": 1e3,
    "m": 1e6, "mm": 1e6, "mn": 1e6, "million": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9,
    "t": 1e12, "tn": 1e12, "trillion": 1e12,
    # Indian sources quote revenue in lakh/crore.
    "lakh": 1e5, "crore": 1e7, "cr": 1e7,
}

# Longest alternatives first so "million" isn't consumed as "m"; the trailing \b stops "5 more" -> 5M.
_NUM_RE = re.compile(
    r"(?P<cur>[$€£¥₹])?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<mult>thousand|million|billion|trillion|crore|lakh|bn|mn|mm|tn|cr|k|m|b|t)?\b(?P<plus>\s*\+)?",
    re.IGNORECASE,
)
_RANGE_SEP_RE = re.compile(r"^\s*(?:-|–|—|to)\s*[$€£¥₹]?\s*$", re.IGNORECASE)
_UPPER_ONLY_RE = re.compile(r"\b(?:under|below|less than|up to|fewer than)\b|<", re.IGNORECASE)
_LOWER_ONLY_RE = re.compile(r"\b(?:over|above|more than|at least|greater than)\b|>", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)")
# "2010s" names a decade, not a founding year.
_FOUNDED_RE = re.compile(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d|'?s\b)")


def _tokens(s):
    out = []
    for m in _NUM_RE.finditer(s):
        raw = m.group("num").rstrip(",")
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        mult = (m.group("mult") or "").lower()
        out.append({
            "start": m.start(), "end": m.end(), "value": value,
            "mult": _MULT.get(mult), "cur": bool(m.group("cur")), "plus": bool(m.group("plus")),
            "yearish": not mult and not m.group("cur") and "," not in raw and "." not in raw
                       and 1900 <= value <= 2099,
        })
    return out


def _quantity(s, drop_years):
    """Find the first number or range in s -> (lo, hi) with multipliers applied, or None."""
    toks = _tokens(s)
    if drop_years or any(not t["yearish"] for t in toks):
        toks = [t for t in toks if not t["yearish"]]
    if not toks:
        return None
    first = toks[0]
    second = toks[1] if len(toks) > 1 else None
    if second and _RANGE_SEP_RE.match(s[first["end"]:second["start"]]):
        # "$10-50M": the low end inherits the high end's multiplier.
        lo = first["value"] * (first["mult"] or second["mult"] or 1)
        hi = second["value"] * (second["mult"] or 1)
        return (lo, hi) if lo <= hi else (hi, lo)
    v = first["value"] * (first["mult"] or 1)
    before = s[:first["start"]]
    if first["plus"] or _LOWER_ONLY_RE.search(before):
        return v, None
    if _UPPER_ONLY_RE.search(before):
        return 0.0, v
    return v, v


def _clean(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _find_year(s):
    m = _YEAR_RE.search(s)
    return int(m.group(1)) if m else None


def _currency(s):
    for pattern, code in _CURRENCY_PATTERNS:
        if re.search(pattern, s, re.IGNORECASE):
            return code
    return "USD"


def parse_money(raw):
    """-> (usd_min, usd_max, year); usd_max is None for open-ended ("$1B+")."""
    s = _clean(raw)
    if not s:
        return None, None, None
    q = _quantity(s, drop_years=True)
    if not q:
        return None, None, None
    rate = FX_TO_USD[_currency(s)]
    lo, hi = q
    return (lo * rate if lo is not None else None,
            hi * rate if hi is not None else None,
            _find_year(s))


def parse_employees(raw):
    """-> (min, max); max is None for open-ended ("10k+")."""
    s = _clean(raw)
    if not s:
        return None, None
    q = _quantity(s, drop_years=False)
    if not q:
        return None, None
    lo, hi = q
    return (int(round(lo)) if lo is not None else None,
            int(round(hi)) if hi is not None else None)


def parse_visits(raw):
    """-> monthly visits (int). Ranges collapse to their midpoint."""
    s = _clean(raw)
    if not s:
        return None
    q = _quantity(s, drop_years=True)
    if not q:
        return None
    lo, hi = q
    v = lo if hi is None else (lo + hi) / 2
    if re.search(r"\b(?:annual(?:ly)?|yearly|per year|a year|/\s*yr|/\s*year)\b", s, re.IGNORECASE):
        v /= 12
    return int(round(v))


def parse_year(raw):
    s = _clean(raw)
    if not s:
        return None
    for m in _FOUNDED_RE.finditer(s):
        y = int(m.group(1))
        if 1800 <= y <= date.today().year:
            return y
    return None


def _close(a, b):
    if a is None or b is None:
        return a is b
    return abs(a - b) <= 1e-6 * max(abs(a), abs(b), 1)


def _test():
    eur, gbp = FX_TO_USD["EUR"], FX_TO_USD["GBP"]
    money = {
        "$1.2B": (1.2e9, 1.2e9, None),
        "€40 million": (40e6 * eur, 40e6 * eur, None),
        "~$10-50M": (10e6, 50e6, None),
        "USD 3.4bn (FY2023)": (3.4e9, 3.4e9, 2023),
        "$5.1 billion net revenue (2024)": (5.1e9, 5.1e9, 2024),
        "£12m": (12e6 * gbp, 12e6 * gbp, None),
        "$500K": (5e5, 5e5, None),
        "Under $1M": (0.0, 1e6, None),
        "$1M-$5M": (1e6, 5e6, None),
        "~$50M ARR (2024 est.)": (50e6, 50e6, 2024),
        "$1B+": (1e9, None, None),
        "C$20 million": (20e6 * FX_TO_USD["CAD"], 20e6 * FX_TO_USD["CAD"], None),
        "₹500 crore": (5e9 * FX_TO_USD["INR"], 5e9 * FX_TO_USD["INR"], None),
        "undisclosed": (None, None, None),
        "": (None, None, None),
        None: (None, None, None),
    }
    for raw, want in money.items():
        got = parse_money(raw)
        assert all(_close(g, w) for g, w in zip(got, want)), (raw, got, want)

    employees = {
        "1,001-5,000 employees": (1001, 5000),
        "51–200": (51, 200),
        "10k+": (10000, None),
        "about 8,000": (8000, 8000),
        "~250": (250, 250),
        "5000+": (5000, None),
        "2000 employees (2023)": (2000, 2000),
        "8,000 employees as of 2023": (8000, 8000),
        "unknown": (None, None),
    }
    for raw, want in employees.items():
        assert parse_employees(raw) == want, (raw, parse_employees(raw), want)

    visits = {
        "2.3M visits/mo": 2_300_000,
        "45.6 million monthly visits": 45_600_000,
        "1.2B": 1_200_000_000,
        "350K": 350_000,
        "120M annual visits": 10_000_000,
        "24 million visits per year": 2_000_000,
        "12M monthly visits (Sept 2024)": 12_000_000,
        "n/a": None,
    }
    for raw, want in visits.items():
        assert parse_visits(raw) == want, (raw, parse_visits(raw), want)

    years = {
        "Founded in 2011": 2011,
        "est. 2011": 2011,
        "2011": 2011,
        2011: 2011,
        "1850": 1850,
        "1799": None,
        "2999": None,
        "founded 2999 or 2005": 2005,
        "early 2010s": None,
        "1990's": None,
        "unknown": None,
    }
    for raw, want in years.items():
        assert parse_year(raw) == want, (raw, parse_year(raw), want)
    print("ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="run the self-test over the messy cases")
    args = ap.parse_args()
    if args.test:
        _test()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
