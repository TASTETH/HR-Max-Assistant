import re

import pytest
from dotenv import dotenv_values

from app.configure import create_env


def test_generated_credentials_match_the_urls_and_differ_between_installations(tmp_path):
    first = dotenv_values(create_env(tmp_path / "first.env"))
    second = dotenv_values(create_env(tmp_path / "second.env"))
    assert first["MAX_BOT_TOKEN"] == ""
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,256}", first["MAX_WEBHOOK_SECRET"])
    assert first["DATABASE_APP_PASSWORD"] in first["DATABASE_URL"]
    assert first["DATABASE_APP_PASSWORD"] in first["SQLALCHEMY_DATABASE_URL"]
    assert first["DATABASE_APP_PASSWORD"] != second["DATABASE_APP_PASSWORD"]
    assert first["MAX_WEBHOOK_SECRET"] != second["MAX_WEBHOOK_SECRET"]
    assert first["POSTGRES_PASSWORD"] != first["DATABASE_APP_PASSWORD"]
    assert "replace_with_random" not in first["DATABASE_ADMIN_URL"]


def test_existing_configuration_is_never_overwritten(tmp_path):
    target = tmp_path / ".env"
    target.write_text("KEEP_THIS=true\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_env(target)
    assert target.read_text(encoding="utf-8") == "KEEP_THIS=true\n"
