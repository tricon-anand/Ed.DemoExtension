from app.formatting import apply_filter, expand_filters, resolve_text


def fmt(name, raw, locale="en_US", currency="USD", arg=None):
    return apply_filter(name, raw, {"_locale": locale, "_currency": currency}, arg)


def test_currency_uses_each_countrys_grouping():
    assert fmt("currency", "1240000", "en_US", "USD") == "$1,240,000"
    assert fmt("currency", "1240000", "en_GB", "GBP") == "£1,240,000"
    assert fmt("currency", "1240000", "en_IN", "INR") == "₹12,40,000"
    assert fmt("currency", "1240000", "de_DE", "EUR") == "1.240.000\u00a0€"


def test_currency_cents_only_when_needed():
    assert fmt("currency", "1999.5") == "$1,999.50"
    assert fmt("currency", "1999.5", arg="0") == "$2,000"
    assert fmt("currency", "1999", arg="2") == "$1,999.00"


def test_number_percent_date():
    assert fmt("number", "1,234.5678", "en_IN", arg="2") == "1,234.57"
    assert fmt("percent", "12.5") == "12.5%"
    assert fmt("percent", "12.5", "de_DE") == "12,5 %"
    assert fmt("date", "2026-11-02", "en_US") == "Nov 2, 2026"
    assert fmt("date", "2026-11-02", "de_DE") == "02.11.2026"
    assert fmt("date", "2026-11-02", "en_GB", arg="long") == "2 November 2026"


def test_input_may_already_contain_symbols_and_commas():
    assert fmt("currency", "₹12,40,000", "en_IN", "INR") == "₹12,40,000"


def test_bad_input_and_bad_settings_do_not_crash():
    assert fmt("currency", "n/a") is None
    assert fmt("date", "not a date") is None
    assert fmt("nosuchfilter", "1") is None
    assert fmt("number", "1234567", locale="xx_YY") == "1,234,567"      # unknown locale falls back to en_US
    assert fmt("currency", "5", currency="dollars") == "$5"             # unknown currency falls back to USD


def test_expand_filters_rewrites_rules_and_adds_per_profile_values():
    rules = [{"id": "r1", "value": "{{currency_symbol}}x {{ revenue | currency }} and {{growth|percent:1}}"}]
    profiles = {
        "UK": {"revenue": "1240000", "growth": "7", "_locale": "en_GB", "_currency": "GBP", "currency_symbol": "!"},
        "India": {"revenue": "1240000", "growth": "7", "_locale": "en_IN", "_currency": "INR", "currency_symbol": "!"},
    }
    new_rules, new_profiles, origins = expand_filters(rules, profiles)
    assert new_rules[0]["value"] == "{{currency_symbol}}x {{revenue__currency}} and {{growth__percent_1}}"
    assert new_profiles["UK"]["revenue__currency"] == "£1,240,000"
    assert new_profiles["India"]["revenue__currency"] == "₹12,40,000"
    assert new_profiles["India"]["growth__percent_1"] == "7.0%"
    assert origins["revenue__currency"] == "{{ revenue | currency }}"
    assert rules[0]["value"].startswith("{{currency_symbol}}x {{ revenue")     # inputs are not mutated
    assert "revenue__currency" not in profiles["UK"]


def test_unknown_filter_is_left_visible():
    new_rules, _, _ = expand_filters([{"value": "{{a | shout}}"}], {"p": {"a": "1"}})
    assert new_rules[0]["value"] == "{{a | shout}}"


def test_missing_variable_stays_a_placeholder_and_reads_as_the_authors_text():
    rules, profiles, origins = expand_filters([{"value": "{{revenue | currency}}"}], {"p": {"company": "X"}})
    assert "revenue__currency" not in profiles["p"]
    assert resolve_text(rules[0]["value"], profiles["p"], origins) == "{{revenue | currency}}"


def test_unreadable_value_is_shown_as_typed():
    rules, profiles, _ = expand_filters([{"value": "{{revenue | currency}}"}], {"p": {"revenue": "n/a"}})
    assert profiles["p"]["revenue__currency"] == "n/a"


def test_resolve_text_matches_the_extension():
    assert resolve_text("Hi {{ user }}, {{missing}}", {"user": "Ann"}) == "Hi Ann, {{missing}}"
