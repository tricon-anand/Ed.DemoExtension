import os
from app import dotenv


def test_parse_handles_comments_quotes_and_placeholders():
    got = dotenv.parse('''
# a comment
GROQ_API_KEY=gsk_abc123
AGENT_MODEL = openai/gpt-oss-120b   # inline comment
QUOTED="has # a hash"
export APP_ENV='production'
EMPTY=
PLACEHOLDER=<paste here>
not a line
''')
    assert got == {"GROQ_API_KEY": "gsk_abc123", "AGENT_MODEL": "openai/gpt-oss-120b",
                   "QUOTED": "has # a hash", "APP_ENV": "production"}


def test_load_never_overrides_the_terminal(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_bytes("﻿TEST_DOTENV_A=from-file\nTEST_DOTENV_B=from-file\n".encode("utf-8"))   # Windows editors add a BOM
    monkeypatch.delenv("APP_SKIP_DOTENV", raising=False)
    monkeypatch.setenv("TEST_DOTENV_B", "from-terminal")
    monkeypatch.delenv("TEST_DOTENV_A", raising=False)
    assert dotenv.load(f) == ["TEST_DOTENV_A"]
    assert os.environ["TEST_DOTENV_A"] == "from-file" and os.environ["TEST_DOTENV_B"] == "from-terminal"
    monkeypatch.delenv("TEST_DOTENV_A")


def test_missing_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_SKIP_DOTENV", raising=False)
    assert dotenv.load(tmp_path / "nope.env") == []


def test_terminal_value_that_hides_the_file_is_reported(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("TEST_DOTENV_C=new-key\nTEST_DOTENV_D=same\n")
    monkeypatch.delenv("APP_SKIP_DOTENV", raising=False)
    monkeypatch.setenv("TEST_DOTENV_C", "old-key")
    monkeypatch.setenv("TEST_DOTENV_D", "same")
    dotenv.SHADOWED.clear()
    dotenv.load(f)
    assert dotenv.SHADOWED == ["TEST_DOTENV_C"]          # same value is not a conflict
    dotenv.SHADOWED.clear()


def test_agent_status_shows_key_hint_without_leaking_the_key(client, org_a, monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_abcdefghijklmnop1234")
    s = client.get("/agent/status", headers=org_a).json()
    assert s["key_hint"] == "...1234" and "abcdefgh" not in str(s) and s["provider"] == "groq"
