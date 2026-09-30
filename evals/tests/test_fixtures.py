from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from evals.runner.__main__ import main
from evals.runner.fixtures import (
    FixtureError,
    FixtureIndex,
    line_counter,
    materialize,
    materialized,
    remove_tree,
)
from evals.tests.conftest import Fixture, git


def _case(source: Fixture, **changes: object) -> dict[str, object]:
    case: dict[str, object] = {
        "id": "shop-case-001",
        "category": "bug",
        "stack": ["java"],
        "mode": "pr",
        "fixture": source.name,
        "base": source.base,
        "head": source.head,
        "source": "synthetic:mutation/example",
        "expected": [
            {
                "lens": "correctness",
                "sev": "important",
                "loc": {"f": "src/OrderController.java", "l": [3, 4]},
            }
        ],
        "must_not": [],
        "adversarial": False,
    }
    case.update(changes)
    return case


def _write_case(directory: Path, case: dict[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{case['id']}.yaml"
    path.write_text(yaml.safe_dump(case), encoding="utf-8")
    return path


def test_materialize_checks_out_head_with_base_as_upstream(
    shop_fixture: Fixture, tmp_path: Path
) -> None:
    repo = materialize(shop_fixture.bundle, shop_fixture.base, shop_fixture.head, tmp_path / "r")
    assert git(repo, "rev-parse", "HEAD") == shop_fixture.head
    assert git(repo, "rev-parse", "@{upstream}") == shop_fixture.base
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-list", "--count", "@{upstream}..HEAD") == "1"


def test_materialize_accepts_abbreviated_commits(shop_fixture: Fixture, tmp_path: Path) -> None:
    repo = materialize(
        shop_fixture.bundle, shop_fixture.base[:7], shop_fixture.head[:7], tmp_path / "r"
    )
    assert git(repo, "rev-parse", "HEAD") == shop_fixture.head


def test_materialize_is_idempotent_and_restores_the_tree(
    shop_fixture: Fixture, tmp_path: Path
) -> None:
    dest = tmp_path / "r"
    materialize(shop_fixture.bundle, shop_fixture.base, shop_fixture.head, dest)
    (dest / "src" / "OrderController.java").write_text("changed\n", encoding="utf-8")
    (dest / "stray.txt").write_text("x", encoding="utf-8")
    materialize(shop_fixture.bundle, shop_fixture.base, shop_fixture.head, dest)
    assert git(dest, "status", "--porcelain") == ""
    assert git(dest, "rev-parse", "HEAD") == shop_fixture.head


def test_materialize_rejects_foreign_non_empty_directory(
    shop_fixture: Fixture, tmp_path: Path
) -> None:
    (tmp_path / "keep.txt").write_text("data", encoding="utf-8")
    with pytest.raises(FixtureError, match="not a materialization"):
        materialize(shop_fixture.bundle, shop_fixture.base, shop_fixture.head, tmp_path)
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "data"


def test_materialize_rejects_directory_of_other_commits(
    shop_fixture: Fixture, tmp_path: Path
) -> None:
    dest = tmp_path / "r"
    materialize(shop_fixture.bundle, shop_fixture.base, shop_fixture.head, dest)
    with pytest.raises(FixtureError, match="not a materialization"):
        materialize(shop_fixture.bundle, shop_fixture.head, shop_fixture.base, dest)


def test_materialize_reports_missing_bundle(tmp_path: Path) -> None:
    with pytest.raises(FixtureError, match="bundle not found"):
        materialize(tmp_path / "none.bundle", "a1b2c3d", "c3d4e5f", tmp_path / "r")


def test_materialize_reports_unknown_commit(shop_fixture: Fixture, tmp_path: Path) -> None:
    with pytest.raises(FixtureError, match="not found in the fixture"):
        materialize(shop_fixture.bundle, shop_fixture.base, "deadbee", tmp_path / "r")


def test_materialized_context_removes_the_directory(shop_fixture: Fixture) -> None:
    with materialized(shop_fixture.bundle, shop_fixture.base, shop_fixture.head) as repo:
        assert git(repo, "rev-parse", "HEAD") == shop_fixture.head
    assert not repo.exists()


def test_line_counter_counts_lines_at_ref(shop_fixture: Fixture, tmp_path: Path) -> None:
    repo = materialize(shop_fixture.bundle, shop_fixture.base, shop_fixture.head, tmp_path / "r")
    at_head = line_counter(repo, shop_fixture.head)
    at_base = line_counter(repo, shop_fixture.base)
    assert at_head("src/OrderController.java") == 5
    assert at_base("src/OrderController.java") == 3
    assert at_head("src/Missing.java") is None


def test_remove_tree_deletes_read_only_files(tmp_path: Path) -> None:
    target = tmp_path / "tree"
    target.mkdir()
    locked = target / "locked.txt"
    locked.write_text("x", encoding="utf-8")
    locked.chmod(0o444)
    remove_tree(target)
    assert not target.exists()


def test_fixture_index_counts_lines_at_case_head(shop_fixture: Fixture) -> None:
    with FixtureIndex(shop_fixture.fixtures_dir) as index:
        counter = index.line_counter_for(_case(shop_fixture))
        assert counter("src/OrderController.java") == 5


def test_fixture_index_rejects_unknown_fixture(shop_fixture: Fixture) -> None:
    with FixtureIndex(shop_fixture.fixtures_dir) as index:
        with pytest.raises(FixtureError, match="bundle not found"):
            index.line_counter_for(_case(shop_fixture, fixture="other"))


def test_fixture_index_rejects_unknown_commit(shop_fixture: Fixture) -> None:
    with FixtureIndex(shop_fixture.fixtures_dir) as index:
        with pytest.raises(FixtureError, match="not found in fixture 'shop'"):
            index.line_counter_for(_case(shop_fixture, head="deadbee"))


def test_case_lint_with_fixtures_accepts_valid_locations(
    shop_fixture: Fixture, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = tmp_path / "cases"
    _write_case(cases, _case(shop_fixture))
    code = main(["case-lint", str(cases), "--fixtures-dir", str(shop_fixture.fixtures_dir)])
    assert code == 0
    assert "0 issue(s)" in capsys.readouterr().out


def test_case_lint_with_fixtures_reports_location_beyond_file(
    shop_fixture: Fixture, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = tmp_path / "cases"
    bad = _case(shop_fixture)
    bad["expected"][0]["loc"]["l"] = [3, 40]
    _write_case(cases, bad)
    code = main(["case-lint", str(cases), "--fixtures-dir", str(shop_fixture.fixtures_dir)])
    assert code == 1
    assert "ends at 40" in capsys.readouterr().out


def test_case_lint_with_fixtures_reports_missing_bundle(
    shop_fixture: Fixture, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = tmp_path / "cases"
    _write_case(cases, _case(shop_fixture, fixture="absent"))
    code = main(["case-lint", str(cases), "--fixtures-dir", str(shop_fixture.fixtures_dir)])
    assert code == 1
    assert "bundle not found" in capsys.readouterr().out


def test_materialize_command_prints_the_repository_path(
    shop_fixture: Fixture, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = tmp_path / "cases"
    _write_case(cases / "java", _case(shop_fixture))
    dest = tmp_path / "out"
    code = main(
        [
            "materialize",
            "shop-case-001",
            "--dest",
            str(dest),
            "--cases-dir",
            str(cases),
            "--fixtures-dir",
            str(shop_fixture.fixtures_dir),
        ]
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == str(dest)
    assert git(dest, "rev-parse", "HEAD") == shop_fixture.head


def test_materialize_command_reports_unknown_case(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["materialize", "nope", "--cases-dir", str(tmp_path)]) == 2
    assert "case not found" in capsys.readouterr().err
