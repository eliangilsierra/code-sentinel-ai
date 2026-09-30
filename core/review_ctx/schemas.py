"""Loading and validation of the JSON Schemas that define code-sentinel data contracts."""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_NAMES = ("case", "finding", "packet")


@cache
def load_schema(name: str) -> dict[str, Any]:
    """Return the schema called ``name``; raises ``ValueError`` for an unknown name."""
    if name not in SCHEMA_NAMES:
        raise ValueError(f"unknown schema {name!r}; expected one of {', '.join(SCHEMA_NAMES)}")
    text = resources.files("review_ctx").joinpath("schema", f"{name}.json").read_text("utf-8")
    return json.loads(text)


@cache
def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name))


def validation_errors(name: str, instance: Any) -> list[str]:
    """Return one message per violation of schema ``name``, ordered by location; empty if valid."""
    errors = sorted(_validator(name).iter_errors(instance), key=lambda e: list(e.absolute_path))
    return [_format(error) for error in errors]


def _format(error: Any) -> str:
    location = "/".join(str(part) for part in error.absolute_path) or "<root>"
    return f"{location}: {error.message}"
