from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from evals.runner.__main__ import main
from evals.runner.lint import collect_case_files, lint_case, lint_files

BUG_CASE: dict[str, Any] = {
    "id": "spring-authz-007",
    "category": "bug",
    "stack": ["java", "spring"],
    "mode": "pr",
    "fixture": "shop",
    "base": "a1b2c3d",
    "head": "c3d4e5f",
    "source": "synthetic:mutation/remove-authz",
    "expected": [
        {
            "lens": "security",
            "sev": "important",
            "loc": {"f": "src/OrderController.java", "l": [48, 55]},
            "must_cite": ["src/OrderController.java:52"],
        }
    ],
    "optional": [{"loc": {"f": "src/OrderService.java", "l": [20, 24]}, "claim_hint": "unused"}],
    "must_not": [{"f": "src/OrderRepository.java", "reason": "parameterised query"}],
    "adversarial": False,
}

FILE_LINES = {
    "src/OrderController.java": 80,
    "src/OrderService.java": 30,
    "src/OrderRepository.java": 10,
}


def _case(**changes: Any) -> dict[str, Any]:
    case = copy.deepcopy(BUG_CASE)
    case.update(changes)
    return case


def _write(directory: Path, case: dict[str, Any], name: str | None = None) -> Path:
    path = directory / f"{name or case['id']}.yaml"
    path.write_text(yaml.safe_dump(case), encoding="utf-8")
    return path


def test_valid_bug_case_has_no_issues() -> None:
    assert lint_case(BUG_CASE) == []


def test_schema_violations_are_reported() -> None:
    assert lint_case(_case(category="flaky")) != []


def test_negative_case_must_not_expect_findings() -> None:
    issues = lint_case(_case(category="negative"))
    assert any("negative" in issue and "empty" in issue for issue in issues)


def test_negative_case_without_expected_is_valid() -> None:
    assert lint_case(_case(category="negative", expected=[])) == []


def test_bug_case_requires_expected_findings() -> None:
    assert any("bug" in issue for issue in lint_case(_case(expected=[])))


def test_pre_existing_case_requires_pre_existing_severity() -> None:
    issues = lint_case(_case(category="pre_existing"))
    assert any("pre_existing" in issue for issue in issues)


def test_pre_existing_case_with_matching_severity_is_valid() -> None:
    case = _case(category="pre_existing")
    case["expected"][0]["sev"] = "pre_existing"
    assert lint_case(case) == []


@pytest.mark.parametrize(
    ("category", "adversarial"),
    [("adversarial", False), ("bug", True)],
)
def test_adversarial_flag_must_follow_category(category: str, adversarial: bool) -> None:
    issues = lint_case(_case(category=category, adversarial=adversarial))
    assert any("adversarial" in issue for issue in issues)


def test_base_and_head_must_differ() -> None:
    assert any("differ" in issue for issue in lint_case(_case(head="a1b2c3d")))


def test_reversed_line_range_is_reported() -> None:
    case = _case()
    case["expected"][0]["loc"]["l"] = [55, 48]
    assert any("greater than its end" in issue for issue in lint_case(case))


def test_locations_are_checked_against_head_files() -> None:
    assert lint_case(BUG_CASE, FILE_LINES.get) == []


def test_missing_file_at_head_is_reported() -> None:
    lines = {k: v for k, v in FILE_LINES.items() if k != "src/OrderController.java"}
    issues = lint_case(BUG_CASE, lines.get)
    assert any("expected[0].loc.f" in issue for issue in issues)
    assert any("must_cite" in issue for issue in issues)


def test_range_beyond_end_of_file_is_reported() -> None:
    lines = {**FILE_LINES, "src/OrderController.java": 50}
    issues = lint_case(BUG_CASE, lines.get)
    assert any("ends at 55" in issue for issue in issues)


def test_must_cite_line_beyond_end_of_file_is_reported() -> None:
    case = _case()
    case["expected"][0]["must_cite"] = ["src/OrderController.java:90"]
    assert any("beyond line 80" in issue for issue in lint_case(case, FILE_LINES.get))


def test_must_not_file_must_exist_at_head() -> None:
    lines = {k: v for k, v in FILE_LINES.items() if k != "src/OrderRepository.java"}
    assert any("must_not[0].f" in issue for issue in lint_case(BUG_CASE, lines.get))


def test_optional_location_is_checked_against_head_files() -> None:
    lines = {**FILE_LINES, "src/OrderService.java": 21}
    assert any("optional[0].loc.l" in issue for issue in lint_case(BUG_CASE, lines.get))


def test_collect_expands_directories_recursively(tmp_path: Path) -> None:
    (tmp_path / "java").mkdir()
    first = _write(tmp_path / "java", BUG_CASE)
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    assert collect_case_files([tmp_path]) == [first]


def test_duplicate_ids_are_reported(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _write(tmp_path / "a", BUG_CASE)
    _write(tmp_path / "b", BUG_CASE)
    issues = lint_files(collect_case_files([tmp_path]))
    assert any("duplicate id" in issue.message for issue in issues)


def test_id_must_match_file_name(tmp_path: Path) -> None:
    path = _write(tmp_path, BUG_CASE, name="other-name")
    issues = lint_files([path])
    assert [issue.message for issue in issues] == ["id 'spring-authz-007' must match the file name"]


def test_invalid_yaml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("id: [unclosed", encoding="utf-8")
    assert "cannot read case" in lint_files([path])[0].message


def test_non_mapping_document_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    assert lint_files([path])[0].message == "case must be a mapping"


def test_line_counter_factory_receives_each_valid_case(tmp_path: Path) -> None:
    path = _write(tmp_path, BUG_CASE)
    seen: list[str] = []

    def factory(case: dict[str, Any]):
        seen.append(case["id"])
        return FILE_LINES.get

    assert lint_files([path], factory) == []
    assert seen == ["spring-authz-007"]


def test_cli_exits_zero_for_valid_cases(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, BUG_CASE)
    assert main(["case-lint", str(tmp_path)]) == 0
    assert "1 case(s) checked, 0 issue(s)" in capsys.readouterr().out


def test_cli_exits_one_and_names_the_case(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path, _case(category="negative"))
    assert main(["case-lint", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "spring-authz-007.yaml" in output
    assert "1 issue(s)" in output


def test_cli_exits_two_for_missing_path(tmp_path: Path) -> None:
    assert main(["case-lint", str(tmp_path / "missing")]) == 2
