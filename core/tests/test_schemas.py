from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from review_ctx.schemas import SCHEMA_NAMES, load_schema, validation_errors

Mutation = Callable[[dict[str, Any]], None]

FINDING: dict[str, Any] = {
    "lens": "security",
    "sev": "important",
    "loc": {"f": "src/OrderController.java", "l": [48, 55]},
    "claim": "Endpoint returns any order without checking ownership",
    "trigger": "GET /orders/7 by user B returns the order of user A",
    "trace": [
        {
            "f": "src/OrderController.java",
            "l": 52,
            "q": "return repo.findById(id)",
            "why": "no ownership check",
        }
    ],
    "fix": "Filter by the authenticated tenant",
    "src": "semgrep:java.spring.missing-authz",
}

PACKET: dict[str, Any] = {
    "run": "a1f3",
    "tier": "M",
    "stack": ["java", "spring"],
    "files": [
        {"p": "src/OrderController.java", "st": "M", "+": 14, "-": 3, "tags": ["authz"]},
    ],
    "hunks": [
        {
            "id": "h1",
            "p": "src/OrderController.java",
            "scope": "getOrder(Long id)",
            "l": [40, 71],
            "code": "+ return repo.findById(id);",
        }
    ],
    "tools": ["OrderController.java:52 semgrep java.spring.missing-authz Endpoint sin authz"],
    "rules": ["REVIEW.md#tenant: queries must filter by tenant"],
    "jobs": [
        {"id": "j1", "lens": "security", "hunks": ["h1"], "why": "spring:authz"},
        {"id": "j2", "lens": "correctness", "hunks": ["h1"]},
    ],
}

CASE: dict[str, Any] = {
    "id": "spring-authz-007",
    "category": "bug",
    "stack": ["java", "spring"],
    "mode": "pr",
    "fixture": "shop",
    "base": "a1b2c3d",
    "head": "c3d4e5f",
    "source": "synthetic:mutation/remove-authz",
    "tier_expected": "S",
    "expected": [
        {
            "lens": "security",
            "sev": "important",
            "loc": {"f": "src/OrderController.java", "l": [48, 55]},
            "tol": 3,
            "must_cite": ["OrderController.java:52"],
            "claim_hint": "endpoint reads orders of other users",
        }
    ],
    "optional": [
        {"loc": {"f": "src/OrderService.java", "l": [20, 24]}, "claim_hint": "unused branch"}
    ],
    "must_not": [
        {"f": "src/OrderRepository.java", "reason": "parameterised JPA query"},
        {"pattern": "style|naming"},
    ],
    "adversarial": False,
    "notes": "authorization annotation removed",
}

EXAMPLES = {"finding": FINDING, "packet": PACKET, "case": CASE}


def _set(path: list[str | int], value: Any) -> Mutation:
    def apply(doc: dict[str, Any]) -> None:
        node: Any = doc
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return apply


def _drop(path: list[str | int]) -> Mutation:
    def apply(doc: dict[str, Any]) -> None:
        node: Any = doc
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]

    return apply


INVALID: dict[str, dict[str, Mutation]] = {
    "finding": {
        "missing trigger": _drop(["trigger"]),
        "self-declared confidence": _set(["confidence"], 0.9),
        "unknown severity": _set(["sev"], "critical"),
        "claim too long": _set(["claim"], "x" * 201),
        "empty trace": _set(["trace"], []),
        "line range with three items": _set(["loc", "l"], [1, 2, 3]),
        "citation too long": _set(["trace", 0, "q"], "x" * 161),
        "more than six trace items": _set(["trace"], [FINDING["trace"][0]] * 7),
        "line zero": _set(["trace", 0, "l"], 0),
    },
    "packet": {
        "unknown tier": _set(["tier"], "XXL"),
        "missing run": _drop(["run"]),
        "unknown top-level key": _set(["extra"], 1),
        "hunk without code": _drop(["hunks", 0, "code"]),
        "unknown file status": _set(["files", 0, "st"], "X"),
        "job without lens": _drop(["jobs", 0, "lens"]),
        "job without hunks": _set(["jobs", 0, "hunks"], []),
        "hunk id format": _set(["hunks", 0, "id"], "hunk-1"),
    },
    "case": {
        "unknown category": _set(["category"], "flaky"),
        "expected item without loc": _drop(["expected", 0, "loc"]),
        "base is not a hex sha": _set(["base"], "main"),
        "unknown top-level key": _set(["repo"], "shop"),
        "must_not with both forms": _set(["must_not", 0, "pattern"], "style"),
        "uppercase id": _set(["id"], "Spring-Authz"),
        "invalid source": _set(["source"], "manual"),
        "must_cite without line": _set(["expected", 0, "must_cite"], ["OrderController.java"]),
        "tolerance too large": _set(["expected", 0, "tol"], 50),
    },
}


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_is_valid_draft_2020_12(name: str) -> None:
    Draft202012Validator.check_schema(load_schema(name))


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_valid_example_has_no_errors(name: str) -> None:
    assert validation_errors(name, EXAMPLES[name]) == []


@pytest.mark.parametrize(
    ("name", "mutation"),
    [(name, mutation) for name, cases in INVALID.items() for mutation in cases.values()],
    ids=[f"{name}:{label}" for name, cases in INVALID.items() for label in cases],
)
def test_invalid_example_is_rejected(name: str, mutation: Mutation) -> None:
    document = copy.deepcopy(EXAMPLES[name])
    mutation(document)
    assert validation_errors(name, document) != []


def test_minimal_finding_without_optional_fields_is_valid() -> None:
    minimal = copy.deepcopy(FINDING)
    for key in ("fix", "src"):
        del minimal[key]
    del minimal["trace"][0]["why"]
    assert validation_errors("finding", minimal) == []


def test_negative_case_with_no_expected_findings_is_valid() -> None:
    negative = copy.deepcopy(CASE)
    negative["category"] = "negative"
    negative["expected"] = []
    assert validation_errors("case", negative) == []


def test_errors_report_the_location_of_the_violation() -> None:
    document = copy.deepcopy(FINDING)
    document["trace"][0]["l"] = 0
    assert validation_errors("finding", document)[0].startswith("trace/0/l:")


def test_unknown_schema_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown schema"):
        load_schema("ledger")
