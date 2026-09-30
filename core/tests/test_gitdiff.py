from __future__ import annotations

from pathlib import Path

import pytest
from review_ctx.gitdiff import GitError, changes, default_base, git, parse_diff
from tests.conftest import run_git


def _numstat(root: Path, spec: str) -> dict[str, tuple[int, int]]:
    rows: dict[str, tuple[int, int]] = {}
    for line in git(root, "diff", "-M", "--numstat", spec).splitlines():
        added, deleted, path = line.split("\t", 2)
        if "=>" in path:
            path = path.split(" => ")[-1].rstrip("}")
        rows[path] = (0 if added == "-" else int(added), 0 if deleted == "-" else int(deleted))
    return rows


@pytest.fixture
def branch(repo: Path) -> Path:
    base = run_git(repo, "rev-parse", "HEAD")
    run_git(repo, "checkout", "--quiet", "-b", "feature")
    controller = repo / "src" / "OrderController.java"
    text = controller.read_text(encoding="utf-8")
    controller.write_text(
        text.replace(
            "        return repo.findById(id);\n",
            "        var order = repo.findById(id);\n"
            "        audit(order);\n"
            "        return order;\n",
        ).replace("        repo.deleteById(id);\n", ""),
        encoding="utf-8",
    )
    (repo / "src" / "Extra.java").write_text("class Extra {\n}\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "change")
    run_git(repo, "update-ref", "refs/remotes/origin/main", base)
    return repo


def test_files_statuses_and_counts_match_numstat(branch: Path) -> None:
    result = changes(branch, "main", "feature")
    expected = _numstat(branch, "main...feature")
    assert {c.path: (c.additions, c.deletions) for c in result} == expected
    assert {c.path: c.status for c in result} == {
        "src/Extra.java": "A",
        "src/OrderController.java": "M",
    }


def test_added_lines_carry_new_line_numbers(branch: Path) -> None:
    controller = next(c for c in changes(branch, "main", "feature") if c.status == "M")
    assert controller.added_lines() == [5, 6, 7]
    assert controller.hunks[0].added[1] == (6, "        audit(order);")


def test_removed_lines_are_recorded_with_their_anchor(branch: Path) -> None:
    controller = next(c for c in changes(branch, "main", "feature") if c.status == "M")
    deletion = controller.hunks[-1]
    assert deletion.removed == ["        repo.deleteById(id);"]
    assert deletion.added == []
    assert deletion.anchor == 10


def test_deleted_and_renamed_files(repo: Path) -> None:
    run_git(repo, "checkout", "--quiet", "-b", "feature")
    run_git(repo, "mv", "src/OrderController.java", "src/OrderApi.java")
    (repo / "src" / "Gone.java").write_text("class Gone {}\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "add")
    run_git(repo, "rm", "--quiet", "src/Gone.java")
    run_git(repo, "commit", "--quiet", "-m", "remove")
    result = {c.path: c for c in changes(repo, "main", "feature")}
    assert result["src/OrderApi.java"].status == "R"
    assert result["src/OrderApi.java"].old_path == "src/OrderController.java"
    assert (result["src/OrderApi.java"].additions, result["src/OrderApi.java"].deletions) == (0, 0)
    assert "src/Gone.java" not in result


def test_deleted_file_is_reported_as_deleted(repo: Path) -> None:
    run_git(repo, "rm", "--quiet", "src/OrderController.java")
    run_git(repo, "commit", "--quiet", "-m", "remove")
    result = changes(repo, "HEAD~1", "HEAD")
    assert [(c.path, c.status, c.deletions) for c in result] == [
        ("src/OrderController.java", "D", 10)
    ]


def test_local_mode_includes_unstaged_staged_and_untracked_changes(repo: Path) -> None:
    controller = repo / "src" / "OrderController.java"
    controller.write_text(
        controller.read_text(encoding="utf-8").replace("findById", "findOne"), encoding="utf-8"
    )
    (repo / "staged.txt").write_text("one\ntwo\n", encoding="utf-8")
    run_git(repo, "add", "staged.txt")
    (repo / "loose.txt").write_text("a\nb\nc\n", encoding="utf-8")
    result = {c.path: c for c in changes(repo)}
    assert result["src/OrderController.java"].status == "M"
    assert result["staged.txt"].additions == 2
    assert result["loose.txt"].additions == 3 and result["loose.txt"].status == "A"
    assert result["loose.txt"].added_lines() == [1, 2, 3]


def test_local_mode_measures_from_the_merge_base_with_upstream(branch: Path) -> None:
    (branch / "src" / "Extra.java").write_text("class Extra {\n  int x;\n}\n", encoding="utf-8")
    result = {c.path: c for c in changes(branch)}
    assert set(result) == {"src/Extra.java", "src/OrderController.java"}
    assert result["src/Extra.java"].additions == 3


def test_default_base_falls_back_to_head_without_any_branch(repo: Path) -> None:
    run_git(repo, "branch", "-m", "trunk")
    assert default_base(repo) == run_git(repo, "rev-parse", "HEAD")


def test_binary_files_are_flagged(repo: Path) -> None:
    (repo / "logo.bin").write_bytes(b"\x00\x01\x02binary")
    run_git(repo, "add", "logo.bin")
    run_git(repo, "commit", "--quiet", "-m", "binary")
    result = changes(repo, "HEAD~1", "HEAD")
    assert result[0].binary and result[0].hunks == []


def test_untracked_binary_file_is_flagged(repo: Path) -> None:
    (repo / "blob.bin").write_bytes(b"\x00\x00data")
    assert next(c for c in changes(repo) if c.path == "blob.bin").binary


def test_paths_with_spaces_and_unicode_are_kept(repo: Path) -> None:
    (repo / "docs").mkdir()
    (repo / "docs" / "guía de uso.md").write_text("hola\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "docs")
    assert changes(repo, "HEAD~1", "HEAD")[0].path == "docs/guía de uso.md"


def test_head_without_base_is_rejected(repo: Path) -> None:
    with pytest.raises(GitError, match="requires a base"):
        changes(repo, None, "HEAD")


def test_unknown_commit_is_reported(repo: Path) -> None:
    with pytest.raises(GitError, match="failed"):
        changes(repo, "nope", "HEAD")


def test_parse_diff_handles_a_pure_deletion_hunk() -> None:
    text = (
        "diff --git a/A.java b/A.java\n"
        "--- a/A.java\n"
        "+++ b/A.java\n"
        "@@ -3,2 +2,0 @@\n"
        "-first\n"
        "-second\n"
    )
    (change,) = parse_diff(text)
    assert change.deletions == 2 and change.additions == 0
    assert change.hunks[0].anchor == 3 and change.hunks[0].removed == ["first", "second"]


def test_parse_diff_of_empty_text_is_empty() -> None:
    assert parse_diff("") == []
