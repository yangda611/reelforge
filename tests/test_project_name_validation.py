import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from novelvideo.api.deps import validate_project_name
from novelvideo.api.schemas import ProjectCreate


@pytest.mark.parametrize("name", ["a", "a" * 64, "project_01", "中文", "我的短剧"])
def test_schema_accepts_valid_project_name_lengths(name):
    assert ProjectCreate(name=name).name == name


def test_schema_rejects_65_character_project_name():
    with pytest.raises(ValidationError, match="64"):
        ProjectCreate(name="a" * 65)


def test_shared_validator_rejects_65_character_project_name():
    with pytest.raises(HTTPException) as exc:
        validate_project_name("a" * 65)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Project name must be at most 64 characters long"


def test_shared_validator_accepts_unicode_project_names():
    # P1-4: Chinese display names are allowed; no ASCII slug is required.
    for name in ["中文", "我的短剧", "短剧2026"]:
        validate_project_name(name)


@pytest.mark.parametrize("name", ["", "has space", "has-dash", "has/slash", "_hidden"])
def test_shared_validator_keeps_existing_format_rules(name):
    with pytest.raises(HTTPException):
        validate_project_name(name)
