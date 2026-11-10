"""Starter scenarios for different industries. Profiles hold realistic data per country/persona.
Starter rules are DISABLED examples: replace the selectors with ones captured from your own app."""

_LOCALES = {
    "US": {"_locale": "en_US", "_currency": "USD"},
    "India": {"_locale": "en_IN", "_currency": "INR"},
    "UK": {"_locale": "en_GB", "_currency": "GBP"},
    "Germany": {"_locale": "de_DE", "_currency": "EUR"},
}


def _p(name, **v):
    return {"name": name, "variables": {**_LOCALES[name], **v}}


TEMPLATES = {
    "education": {
        "name": "Education: school / LMS", "industry": "Education",
        "description": "Students, courses, completion rates and fees.",
        "target_host": "",
        "profiles": [
            _p("US", org="Lincoln High School", user="Sarah Mitchell", students="1482", fees="1250000", completion="0.87"),
            _p("India", org="Delhi Public School", user="Priya Sharma", students="2310", fees="18500000", completion="0.91"),
            _p("UK", org="Oakfield Academy", user="James Whitmore", students="965", fees="740000", completion="0.89"),
            _p("Germany", org="Gymnasium Rheinau", user="Lena Becker", students="1104", fees="910000", completion="0.93"),
        ],
        "rules": [
            {"selector": "[data-testid='institution-name']", "type": "text", "value": "{{org}}"},
            {"selector": "[data-testid='user-name']", "type": "text", "value": "{{user}}"},
            {"selector": "[data-testid='student-count']", "type": "text", "value": "{{students | number}}"},
            {"selector": "[data-testid='fees-collected']", "type": "text", "value": "{{fees | currency}}"},
            {"selector": "[data-testid='completion-rate']", "type": "text", "value": "{{completion | percent}}"},
            {"selector": "*/api/dashboard*", "type": "api",
             "value": "[{\"path\":\"students\",\"value\":\"{{students}}\"}]"},
        ],
    },
    "healthcare": {
        "name": "Healthcare: clinic / patient portal", "industry": "Healthcare",
        "description": "Synthetic patients and clinic KPIs only. Never use real patient data in a demo.",
        "target_host": "",
        "profiles": [
            _p("US", org="Riverside Medical Group", user="Dr. Amy Chen", patients="8420", revenue="4200000", wait="14"),
            _p("India", org="Sunrise Multispeciality Hospital", user="Dr. Rohan Mehta", patients="12650", revenue="96000000", wait="22"),
            _p("UK", org="Northgate Health Centre", user="Dr. Helen Price", patients="6110", revenue="2900000", wait="17"),
            _p("Germany", org="Klinikum Waldstadt", user="Dr. Jonas Keller", patients="9040", revenue="3800000", wait="12"),
        ],
        "rules": [
            {"selector": "[data-testid='clinic-name']", "type": "text", "value": "{{org}}"},
            {"selector": "[data-testid='clinician']", "type": "text", "value": "{{user}}"},
            {"selector": "[data-testid='active-patients']", "type": "text", "value": "{{patients | number}}"},
            {"selector": "[data-testid='monthly-revenue']", "type": "text", "value": "{{revenue | currency}}"},
            {"selector": "[data-testid='patient-name']", "type": "hide", "value": ""},
        ],
    },
    "social": {
        "name": "Social media: analytics dashboard", "industry": "Social media",
        "description": "Followers, reach and engagement for a brand account.",
        "target_host": "",
        "profiles": [
            _p("US", org="@northwind", user="Jordan Lee", followers="482000", reach="3200000", engagement="0.047"),
            _p("India", org="@chaiwala", user="Aarav Nair", followers="1250000", reach="9800000", engagement="0.062"),
            _p("UK", org="@thebrewhouse", user="Emily Clarke", followers="215000", reach="1400000", engagement="0.039"),
        ],
        "rules": [
            {"selector": "[data-testid='account-handle']", "type": "text", "value": "{{org}}"},
            {"selector": "[data-testid='followers']", "type": "text", "value": "{{followers | number}}"},
            {"selector": "[data-testid='reach']", "type": "text", "value": "{{reach | number}}"},
            {"selector": "[data-testid='engagement']", "type": "text", "value": "{{engagement | percent}}"},
        ],
    },
    "sales": {
        "name": "Sales: CRM / pipeline", "industry": "Sales",
        "description": "Pipeline, quota attainment and deal sizes for a sales team.",
        "target_host": "",
        "profiles": [
            _p("US", org="Acme Corp", user="Sarah Johnson", pipeline="5400000", deals="38", quota="0.92"),
            _p("India", org="Bharat Industries", user="Vikram Singh", pipeline="320000000", deals="54", quota="0.88"),
            _p("UK", org="Britannia Logistics", user="Oliver Hughes", pipeline="3100000", deals="29", quota="0.97"),
            _p("Germany", org="Nordwerk GmbH", user="Katrin Vogel", pipeline="4700000", deals="33", quota="1.04"),
        ],
        "rules": [
            {"selector": "[data-testid='account-name']", "type": "text", "value": "{{org}}"},
            {"selector": "[data-testid='rep-name']", "type": "text", "value": "{{user}}"},
            {"selector": "[data-testid='pipeline-total']", "type": "text", "value": "{{pipeline | currency}}"},
            {"selector": "[data-testid='open-deals']", "type": "text", "value": "{{deals}}"},
            {"selector": "[data-testid='quota']", "type": "text", "value": "{{quota | percent}}"},
        ],
    },
}


def template_list():
    return [{"key": k, "name": t["name"], "industry": t["industry"], "description": t["description"],
             "profiles": [p["name"] for p in t["profiles"]], "rules": len(t["rules"])}
            for k, t in TEMPLATES.items()]


def template_dump(key):
    t = TEMPLATES[key]
    return {
        "name": t["name"], "target_host": t["target_host"],
        "profiles": t["profiles"],
        "rules": [{**r, "disabled": True} for r in t["rules"]],
    }
