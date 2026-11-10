"""Country-aware formatting for scenario values.

A rule value can contain filter expressions such as

    {{revenue | currency}}    {{growth | percent:1}}    {{signup | date:long}}

The profile decides how they look through two reserved variables:

    _locale    e.g. en_US, en_GB, en_IN, de_DE   (default en_US)
    _currency  e.g. USD, GBP, INR, EUR           (default USD)

The extension only understands plain {{name}} placeholders, so expand_filters()
rewrites every filter expression into a plain placeholder and adds the formatted
text to each profile as an extra variable. Switching profiles inside the popup
then keeps working with no network call.
"""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from babel import Locale, UnknownLocaleError
from babel.dates import format_date
from babel.numbers import format_currency, format_decimal, format_percent

DEFAULT_LOCALE = "en_US"
DEFAULT_CURRENCY = "USD"
DATE_STYLES = ("short", "medium", "long", "full")

# {{ revenue | currency }}  /  {{ growth | percent:1 }}
FILTER_EXPR = re.compile(r"\{\{\s*([\w.-]+)\s*\|\s*([A-Za-z_]+)\s*(?::\s*(\w+))?\s*\}\}")
# plain {{ name }}
PLAIN_EXPR = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")


# ---------- parsing helpers ----------
def get_locale(variables: dict) -> Locale:
    raw = str(variables.get("_locale") or DEFAULT_LOCALE).strip().replace("-", "_")
    try:
        return Locale.parse(raw)
    except (ValueError, UnknownLocaleError):
        return Locale.parse(DEFAULT_LOCALE)


def get_currency(variables: dict) -> str:
    raw = str(variables.get("_currency") or DEFAULT_CURRENCY).strip().upper()
    return raw if re.fullmatch(r"[A-Z]{3}", raw) else DEFAULT_CURRENCY


def parse_number(raw) -> Decimal | None:
    """Accepts '1980500', '1,980,500', '₹12,40,000', '12.5%'. Dot is the decimal mark."""
    cleaned = re.sub(r"[^\d.\-]", "", str(raw))
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_date(raw) -> date | None:
    text = str(raw).strip()
    for candidate in (text, text[:10]):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            pass
    return None


def _with_decimals(pattern: str, decimals: int | None) -> str:
    """Swap the fraction part of a CLDR pattern, keeping the locale's own grouping."""
    if decimals is None:
        return pattern
    return re.sub(r"\.[0#]+", ("." + "0" * decimals) if decimals else "", pattern)


def _arg_int(arg) -> int | None:
    return int(arg) if arg is not None and str(arg).isdigit() and int(arg) <= 6 else None


# ---------- the filters ----------
def _number(raw, variables, arg):
    num = parse_number(raw)
    if num is None:
        return None
    loc = get_locale(variables)
    pattern = _with_decimals(loc.decimal_formats[None].pattern, _arg_int(arg))
    return format_decimal(num, format=pattern, locale=loc)


def _currency(raw, variables, arg):
    num = parse_number(raw)
    if num is None:
        return None
    loc = get_locale(variables)
    decimals = _arg_int(arg)
    if decimals is None:                       # whole amounts show no cents
        decimals = 0 if num == num.to_integral_value() else 2
    pattern = _with_decimals(loc.currency_formats["standard"].pattern, decimals)
    return format_currency(num, get_currency(variables), format=pattern, locale=loc, currency_digits=False)


def _percent(raw, variables, arg):
    """12.5 means 12.5%, the way people write it."""
    num = parse_number(raw)
    if num is None:
        return None
    loc = get_locale(variables)
    decimals = _arg_int(arg)
    base = re.sub(r"\.[0#]+", "", loc.percent_formats[None].pattern)
    frac = ("." + "0" * decimals) if decimals else ("" if decimals == 0 else ".##")
    pattern = re.sub(r"0(?!.*0)", "0" + frac, base)
    return format_percent(num / 100, format=pattern, locale=loc)


def _date(raw, variables, arg):
    d = parse_date(raw)
    if d is None:
        return None
    style = arg if arg in DATE_STYLES else "medium"
    return format_date(d, format=style, locale=get_locale(variables))


FILTERS = {"number": _number, "currency": _currency, "percent": _percent, "date": _date}


def apply_filter(name: str, raw, variables: dict, arg=None) -> str | None:
    """Formatted text, or None when the filter is unknown or the value can't be read."""
    fn = FILTERS.get(name)
    return fn(raw, variables, arg) if fn else None


# ---------- bundle integration ----------
def _key(var: str, name: str, arg) -> str:
    return f"{var}__{name}" + (f"_{arg}" if arg else "")


def expand_filters(rules: list[dict], profiles: dict[str, dict]):
    """Rewrite filter expressions to plain placeholders and compute them per profile.

    rules     [{"value": "{{revenue | currency}}", ...}]
    profiles  {"India": {"revenue": "1240000", "_locale": "en_IN"}}
    returns   (rules, profiles, origins) where origins maps the generated placeholder
              back to the text the author wrote, for friendly error display.
    """
    needed: dict[str, tuple[str, str, str | None]] = {}
    origins: dict[str, str] = {}

    def rewrite(match: re.Match) -> str:
        var, name, arg = match.group(1), match.group(2), match.group(3)
        if name not in FILTERS:
            return match.group(0)              # unknown filter: leave visible so it gets noticed
        key = _key(var, name, arg)
        needed[key] = (var, name, arg)
        origins[key] = match.group(0)
        return "{{" + key + "}}"

    new_rules = [{**r, "value": FILTER_EXPR.sub(rewrite, r.get("value") or "")} for r in rules]

    new_profiles = {}
    for pname, variables in profiles.items():
        merged = dict(variables)
        for key, (var, name, arg) in needed.items():
            if var in variables:
                text = apply_filter(name, variables[var], variables, arg)
                merged[key] = text if text is not None else str(variables[var])   # unreadable value: show it as typed
        new_profiles[pname] = merged
    return new_rules, new_profiles, origins


def resolve_text(value: str, variables: dict, origins: dict | None = None) -> str:
    """Fill {{placeholders}} the same way the extension does. Unknown ones stay visible."""
    def fill(m: re.Match) -> str:
        name = m.group(1)
        if name in variables:
            return str(variables[name])
        return (origins or {}).get(name, m.group(0))

    return PLAIN_EXPR.sub(fill, value)
