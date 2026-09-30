from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from review_ctx.gitdiff import FileChange, Hunk
from review_ctx.packs import (
    UNIVERSAL,
    Hit,
    PackError,
    detect_packs,
    glob_match,
    lenses_dir,
    load_pack,
    load_packs,
    packs_dir,
    route,
)
from tests.conftest import run_git


def _write_pack(directory: Path, data: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "pack.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _change(path: str, *added: tuple[int, str]) -> FileChange:
    hunks = [Hunk(anchor=added[0][0], added=list(added))] if added else []
    return FileChange(path=path, status="M", additions=len(added), hunks=hunks)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/db/migration/**", "src/main/resources/db/migration/V1__init.sql", True),
        ("**/db/migration/**", "db/migration/V1.sql", True),
        ("**/db/migration/**", "src/db/other/V1.sql", False),
        ("**/*.sql", "a/b/c.sql", True),
        ("**/*.sql", "c.sql", True),
        ("*.sql", "a/c.sql", False),
        ("src/*.java", "src/A.java", True),
        ("src/*.java", "src/sub/A.java", False),
        ("src/**/A.java", "src/x/y/A.java", True),
        ("src/**/A.java", "src/A.java", True),
        ("a?c", "abc", True),
        ("a?c", "a/c", False),
        ("pom.xml", "pom.xml", True),
        ("pom.xml", "pomxxml", False),
    ],
)
def test_glob_match(pattern: str, path: str, expected: bool) -> None:
    assert glob_match(pattern, path) is expected


def test_pack_is_loaded_and_validated(tmp_path: Path) -> None:
    path = _write_pack(
        tmp_path / "spring",
        {
            "id": "spring",
            "detect": {"files": ["pom.xml"], "contains": ["spring-boot"]},
            "triggers": [
                {"added": "@PreAuthorize", "tags": ["authz"], "lenses": ["security"]},
                {"paths": ["**/db/migration/**"], "lenses": ["data-migration"]},
            ],
            "tools": [{"id": "semgrep", "cmd": "semgrep --json", "trust": "untrusted-ok"}],
        },
    )
    pack = load_pack(path)
    assert pack.id == "spring" and pack.root == path.parent
    assert [t.label for t in pack.triggers] == ["authz", "data-migration"]
    assert pack.tools[0].trust == "untrusted-ok"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({}, "'id' is required"),
        ({"id": "x", "detect": {"glob": []}}, "unknown detect keys"),
        ({"id": "x", "detect": {"files": "pom.xml"}}, "detect.files"),
        ({"id": "x", "triggers": [{"tags": ["a"]}]}, "needs 'added' or 'paths'"),
        ({"id": "x", "triggers": [{"added": "(", "tags": ["a"]}]}, "not a valid regex"),
        ({"id": "x", "triggers": [{"added": "a"}]}, "needs 'tags' or 'lenses'"),
        ({"id": "x", "triggers": [{"added": "a", "tags": ["t"], "extra": 1}]}, "unknown keys"),
        ({"id": "x", "tools": [{"id": "t", "cmd": "c", "trust": "maybe"}]}, "trust must be one of"),
        ({"id": "x", "tools": [{"id": "t"}]}, "needs 'id', 'cmd' and 'trust'"),
    ],
)
def test_malformed_packs_are_rejected(tmp_path: Path, data: dict[str, Any], message: str) -> None:
    with pytest.raises(PackError, match=message):
        load_pack(_write_pack(tmp_path / "p", data))


def test_unreadable_pack_is_reported(tmp_path: Path) -> None:
    with pytest.raises(PackError, match="cannot read pack"):
        load_pack(tmp_path / "missing" / "pack.yaml")


def test_shipped_packs_load_with_the_universal_pack_first() -> None:
    packs = load_packs()
    assert packs[0].id == UNIVERSAL
    assert packs[0].always == ("correctness",)


def test_directories_can_be_overridden_with_environment_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("REVIEW_SQUAD_PACKS", str(tmp_path / "p"))
    monkeypatch.setenv("REVIEW_SQUAD_LENSES", str(tmp_path / "l"))
    assert packs_dir() == tmp_path / "p" and lenses_dir() == tmp_path / "l"


@pytest.fixture
def stack_packs(tmp_path: Path):
    base = tmp_path / "packs"
    _write_pack(base / "_universal", {"id": UNIVERSAL, "always": ["correctness"]})
    _write_pack(base / "java", {"id": "java", "detect": {"extensions": [".java"]}})
    _write_pack(
        base / "spring",
        {"id": "spring", "detect": {"files": ["pom.xml"], "contains": ["spring-boot"]}},
    )
    _write_pack(
        base / "react",
        {"id": "react", "detect": {"files": ["package.json"], "contains": ['"react"']}},
    )
    _write_pack(base / "empty", {"id": "empty"})
    return load_packs(base)


def test_universal_pack_is_always_detected(repo: Path, stack_packs) -> None:
    ids = [p.id for p in detect_packs(repo, stack_packs, [])]
    assert ids == [UNIVERSAL]


def test_extension_detection_uses_the_changed_files(repo: Path, stack_packs) -> None:
    ids = {p.id for p in detect_packs(repo, stack_packs, ["src/OrderController.java"])}
    assert ids == {UNIVERSAL, "java"}
    assert "java" not in {p.id for p in detect_packs(repo, stack_packs, ["README.md"])}


def test_file_and_content_detection(repo: Path, stack_packs) -> None:
    (repo / "pom.xml").write_text("<artifactId>spring-boot-starter</artifactId>", "utf-8")
    (repo / "web").mkdir()
    (repo / "web" / "package.json").write_text('{"dependencies": {"lodash": "1"}}', "utf-8")
    run_git(repo, "add", "-A")
    ids = {p.id for p in detect_packs(repo, stack_packs, ["src/OrderController.java"])}
    assert ids == {UNIVERSAL, "java", "spring"}


def test_content_detection_finds_files_in_subdirectories(repo: Path, stack_packs) -> None:
    (repo / "web").mkdir()
    (repo / "web" / "package.json").write_text('{"dependencies": {"react": "18"}}', "utf-8")
    run_git(repo, "add", "-A")
    assert "react" in {p.id for p in detect_packs(repo, stack_packs, [])}


def test_pack_without_detection_rules_is_never_detected(repo: Path, stack_packs) -> None:
    assert "empty" not in {p.id for p in detect_packs(repo, stack_packs, ["src/A.java"])}


def _spring(tmp_path: Path):
    return load_pack(
        _write_pack(
            tmp_path / "spring",
            {
                "id": "spring",
                "triggers": [
                    {
                        "added": "@PreAuthorize|@Secured",
                        "tags": ["authz"],
                        "lenses": ["security", "correctness"],
                    },
                    {"paths": ["**/db/migration/**"], "lenses": ["data-migration"]},
                    {
                        "added": "@Transactional",
                        "paths": ["**/service/**"],
                        "tags": ["tx"],
                        "lenses": ["concurrency"],
                    },
                ],
            },
        )
    )


def test_added_line_trigger_records_tag_lens_and_reason(tmp_path: Path) -> None:
    files = [
        _change("src/OrderController.java", (50, "int a;"), (52, "@PreAuthorize(x)")),
        _change("src/Other.java", (3, "int b;")),
    ]
    routing = route([_spring(tmp_path)], files)
    assert routing.tags == {"src/OrderController.java": ["authz"], "src/Other.java": []}
    hit = Hit("src/OrderController.java", "spring:authz@src/OrderController.java:52")
    assert routing.lenses["security"] == [hit] and routing.lenses["correctness"] == [hit]


def test_path_trigger_needs_no_added_lines(tmp_path: Path) -> None:
    routing = route([_spring(tmp_path)], [_change("src/db/migration/V2__add.sql")])
    assert routing.lenses["data-migration"] == [
        Hit("src/db/migration/V2__add.sql", "spring:data-migration@src/db/migration/V2__add.sql")
    ]


def test_trigger_with_paths_and_regex_requires_both(tmp_path: Path) -> None:
    pack = _spring(tmp_path)
    inside = route([pack], [_change("app/service/A.java", (7, "@Transactional"))])
    outside = route([pack], [_change("app/web/A.java", (7, "@Transactional"))])
    assert "concurrency" in inside.lenses and "concurrency" not in outside.lenses
    assert inside.tags["app/service/A.java"] == ["tx"]


def test_always_lenses_apply_to_every_file(stack_packs) -> None:
    routing = route(stack_packs[:1], [_change("a.txt"), _change("b.txt")])
    assert [h.path for h in routing.lenses["correctness"]] == ["a.txt", "b.txt"]
    assert routing.lenses["correctness"][0].why == "_universal:always"


def test_untouched_lenses_stay_inactive(tmp_path: Path) -> None:
    routing = route([_spring(tmp_path)], [_change("src/A.java", (1, "int a;"))])
    assert routing.lenses == {}


def test_universal_security_triggers_fire_on_risky_lines() -> None:
    universal = next(p for p in load_packs() if p.id == UNIVERSAL)
    risky = [
        _change("a.java", (4, 'String password = "hunter22secret";')),
        _change("b.java", (9, "Runtime.getRuntime().exec(cmd);")),
        _change("c.ts", (2, "const h = md5(value);")),
    ]
    routing = route([universal], risky)
    assert {h.path for h in routing.lenses["security"]} == {"a.java", "b.java", "c.ts"}
    assert routing.tags["a.java"] == ["secret"] and routing.tags["b.java"] == ["command-exec"]


def test_universal_security_triggers_ignore_ordinary_lines() -> None:
    universal = next(p for p in load_packs() if p.id == UNIVERSAL)
    calm = [_change("a.java", (1, "int total = price * quantity;"), (2, 'log.info("token");'))]
    assert "security" not in route([universal], calm).lenses


def test_universal_migration_trigger_matches_sql_files() -> None:
    universal = next(p for p in load_packs() if p.id == UNIVERSAL)
    routing = route([universal], [_change("db/schema.sql", (1, "alter table t add c int;"))])
    assert routing.tags["db/schema.sql"] == ["migration"]
    assert "data-migration" in routing.lenses
