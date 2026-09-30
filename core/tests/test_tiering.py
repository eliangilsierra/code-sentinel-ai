from __future__ import annotations

import pytest
from review_ctx.gitdiff import FileChange
from review_ctx.packs import Hit, Routing
from review_ctx.schemas import validation_errors
from review_ctx.tiering import (
    MAX_CLUSTERS,
    PARAMS,
    TIERS,
    classify,
    make_clusters,
    plan_review,
    score,
)


def _file(path: str, lines: int, deletions: int = 0) -> FileChange:
    return FileChange(path=path, status="M", additions=lines, deletions=deletions)


def _routing(files: list[FileChange], lenses: dict[str, list[str]], tags=None) -> Routing:
    routing = Routing(tags={c.path: list((tags or {}).get(c.path, [])) for c in files})
    for lens, paths in lenses.items():
        routing.lenses[lens] = [Hit(p, f"pack:{lens}@{p}") for p in paths]
    return routing


@pytest.mark.parametrize(
    ("lines", "tier"),
    [(1, "XS"), (49, "XS"), (50, "S"), (199, "S"), (200, "M"), (799, "M"), (800, "L")]
    + [(1999, "L"), (2000, "XL"), (5000, "XL")],
)
def test_tier_follows_the_size_of_a_single_cluster(lines: int, tier: str) -> None:
    assert classify([_file("src/A.java", lines)], {}, 1) == tier


def test_removed_lines_count_towards_the_size() -> None:
    assert classify([_file("src/A.java", 25, deletions=25)], {}, 1) == "S"


def test_tagged_files_weigh_more_and_are_never_xs() -> None:
    files = [_file("src/A.java", 30)]
    tags = {"src/A.java": ["authz"]}
    assert score(files, tags, 1) == 45
    assert classify(files, tags, 1) == "S"
    assert classify([_file("src/A.java", 140)], tags, 1) == "M"


def test_extra_clusters_add_to_the_score() -> None:
    files = [_file("a/A.java", 40), _file("b/B.java", 5)]
    assert score(files, {}, 1) == 45
    assert score(files, {}, 2) == 55
    assert classify(files, {}, 2) == "S"


def test_files_of_a_directory_form_one_cluster() -> None:
    clusters = make_clusters(["src/a/One.java", "src/a/Two.java", "src/b/Three.java"])
    assert [(c.key, c.files) for c in clusters] == [
        ("src/a", ("src/a/One.java", "src/a/Two.java")),
        ("src/b", ("src/b/Three.java",)),
    ]
    assert [c.id for c in clusters] == ["c1", "c2"]


def test_clusters_are_coarsened_when_there_are_too_many() -> None:
    paths = [f"module{i}/src/A.java" for i in range(12)]
    clusters = make_clusters(paths)
    assert 1 <= len(clusters) <= MAX_CLUSTERS
    assert sorted(f for c in clusters for f in c.files) == sorted(paths)


def test_coarsening_stops_at_the_deepest_level_that_fits() -> None:
    paths = [f"m{i % 6}/x{i}/A.java" for i in range(12)]
    assert {c.key for c in make_clusters(paths)} == {f"m{i}" for i in range(6)}


def test_root_level_files_share_one_cluster() -> None:
    assert [c.key for c in make_clusters(["A.java", "B.java"])] == ["."]


def test_empty_change_has_no_jobs() -> None:
    plan = plan_review([], Routing())
    assert (plan.tier, plan.jobs, plan.clusters) == ("XS", [], [])


def test_xs_change_gets_one_combined_job() -> None:
    files = [_file("src/A.java", 10)]
    routing = _routing(files, {"correctness": ["src/A.java"], "security": ["src/A.java"]})
    (job,) = plan_review(files, routing).jobs
    assert (job.id, job.lens, job.files) == ("j1", "correctness+security", ("src/A.java",))


def test_xs_change_without_security_triggers_uses_correctness_only() -> None:
    files = [_file("src/A.java", 10)]
    (job,) = plan_review(files, _routing(files, {"correctness": ["src/A.java"]})).jobs
    assert job.lens == "correctness"


def test_small_and_medium_changes_get_one_job_per_lens() -> None:
    files = [_file("a/A.java", 120), _file("b/B.java", 120)]
    routing = _routing(files, {"correctness": ["a/A.java", "b/B.java"], "security": ["b/B.java"]})
    plan = plan_review(files, routing)
    assert plan.tier == "M"
    assert [(j.id, j.lens, j.files) for j in plan.jobs] == [
        ("j1", "correctness", ("a/A.java", "b/B.java")),
        ("j2", "security", ("b/B.java",)),
    ]


def test_lenses_beyond_the_job_limit_are_merged() -> None:
    files = [_file("a/A.java", 80)]
    routing = _routing(
        files,
        {"correctness": ["a/A.java"], "concurrency": ["a/A.java"], "security": ["a/A.java"]},
    )
    plan = plan_review(files, routing, frozenset({"correctness", "security", "concurrency"}))
    assert plan.tier == "S"
    assert [j.lens for j in plan.jobs] == ["correctness", "concurrency+security"]


def test_lenses_without_a_lens_file_are_ignored() -> None:
    files = [_file("db/V1.sql", 10)]
    routing = _routing(files, {"correctness": ["db/V1.sql"], "data-migration": ["db/V1.sql"]})
    (job,) = plan_review(files, routing).jobs
    assert job.lens == "correctness"


def test_large_changes_get_one_job_per_cluster_and_lens() -> None:
    files = [_file("a/A.java", 500), _file("b/B.java", 500), _file("c/C.java", 300)]
    routing = _routing(
        files,
        {
            "correctness": ["a/A.java", "b/B.java", "c/C.java"],
            "security": ["b/B.java"],
        },
    )
    plan = plan_review(files, routing)
    assert plan.tier == "L"
    assert [(j.lens, j.files, j.cluster) for j in plan.jobs] == [
        ("correctness", ("a/A.java",), "c1"),
        ("correctness", ("b/B.java",), "c2"),
        ("security", ("b/B.java",), "c2"),
        ("correctness", ("c/C.java",), "c3"),
    ]
    assert [j.id for j in plan.jobs] == ["j1", "j2", "j3", "j4"]


def test_extra_large_changes_review_only_tagged_clusters_and_warn() -> None:
    files = [_file("a/A.java", 1500), _file("b/B.java", 1500)]
    routing = _routing(
        files,
        {"correctness": ["a/A.java", "b/B.java"], "security": ["b/B.java"]},
        tags={"b/B.java": ["authz"]},
    )
    plan = plan_review(files, routing)
    assert plan.tier == "XL" and "split it" in plan.warnings[0]
    assert [(j.lens, j.files) for j in plan.jobs] == [
        ("correctness", ("b/B.java",)),
        ("security", ("b/B.java",)),
    ]


def test_reasons_of_a_job_are_deduplicated_and_ordered() -> None:
    files = [_file("a/A.java", 60)]
    routing = Routing(tags={"a/A.java": []})
    routing.lenses["correctness"] = [Hit("a/A.java", "u:always"), Hit("a/A.java", "u:always")]
    routing.lenses["security"] = [Hit("a/A.java", "spring:authz@a/A.java:5")]
    plan = plan_review(files, routing)
    assert [j.why for j in plan.jobs] == [("u:always",), ("spring:authz@a/A.java:5",)]


def test_lens_without_hits_creates_no_job() -> None:
    files = [_file("a/A.java", 60)]
    routing = Routing(tags={"a/A.java": []}, lenses={"correctness": []})
    assert plan_review(files, routing).jobs == []


def test_parameters_match_the_budget_table_and_grow_with_the_tier() -> None:
    assert [PARAMS[t].budget_usd for t in TIERS] == [0.03, 0.10, 0.25, 0.60, 1.00]
    assert [PARAMS[t].cap_usd for t in TIERS] == [0.10, 0.25, 0.50, 1.20, 2.00]
    assert [PARAMS[t].max_turns for t in TIERS] == [4, 6, 10, 12, 12]
    assert all(PARAMS[t].cap_usd >= 2 * PARAMS[t].budget_usd * 0.99 for t in TIERS[:3])


def test_packet_schema_accepts_combined_lenses_in_jobs() -> None:
    packet = {
        "run": "a1",
        "tier": "XS",
        "stack": [],
        "files": [],
        "hunks": [],
        "tools": [],
        "rules": [],
        "jobs": [{"id": "j1", "lens": "correctness+security", "hunks": ["h1"]}],
    }
    assert validation_errors("packet", packet) == []
    packet["jobs"][0]["lens"] = "Correctness+security"
    assert validation_errors("packet", packet) != []
    packet["jobs"][0]["lens"] = "correctness+"
    assert validation_errors("packet", packet) != []
