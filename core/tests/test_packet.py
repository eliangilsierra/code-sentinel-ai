from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml
from review_ctx.cli import main
from review_ctx.gitdiff import changes
from review_ctx.packet import (
    DATA_BEGIN,
    DATA_END,
    MAX_HUNK_LINES,
    PrepareError,
    hunks_for_file,
    prepare,
)
from review_ctx.packs import load_packs
from review_ctx.schemas import validation_errors
from tests.conftest import run_git

SERVICE = """package shop;

public class OrderService {

    private int retries = 3;

    public Order find(long id) {
        Order order = repo.findById(id);
        if (order == null) {
            throw new NotFound(id);
        }
        return order;
    }

    public void remove(long id) {
        audit.log("remove");
        repo.deleteById(id);
        cache.evict(id);
        metrics.count("remove");
    }

    public int total(List<Order> orders) {
        int sum = 0;
        for (Order o : orders) {
            sum += o.price();
        }
        return sum;
    }
}
"""


@pytest.fixture
def lenses(tmp_path: Path) -> Path:
    directory = tmp_path / "lenses"
    directory.mkdir()
    (directory / "correctness.md").write_text("Find logic errors.\n", encoding="utf-8")
    (directory / "security.md").write_text("Find exploitable flaws.\n", encoding="utf-8")
    return directory


@pytest.fixture
def packs_dir(tmp_path: Path) -> Path:
    base = tmp_path / "packs"
    (base / "_universal").mkdir(parents=True)
    (base / "_universal" / "pack.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "_universal",
                "always": ["correctness"],
                "triggers": [
                    {"added": "getRuntime", "tags": ["command-exec"], "lenses": ["security"]}
                ],
            }
        ),
        encoding="utf-8",
    )
    (base / "java" / "lenses").mkdir(parents=True)
    (base / "java" / "pack.yaml").write_text(
        yaml.safe_dump({"id": "java", "detect": {"extensions": [".java"]}}), encoding="utf-8"
    )
    (base / "java" / "lenses" / "correctness.md").write_text(
        "Watch for Optional misuse.\n", "utf-8"
    )
    return base


@pytest.fixture
def service_repo(repo: Path) -> Path:
    (repo / "src" / "OrderService.java").write_text(SERVICE, encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "service")
    return repo


def _edit(repo: Path, *pairs: tuple[str, str]) -> None:
    path = repo / "src" / "OrderService.java"
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        assert old in text
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


def _file_change(repo: Path):
    (change,) = [c for c in changes(repo) if c.path == "src/OrderService.java"]
    return change


def test_changes_inside_a_method_produce_one_hunk_with_marked_lines(service_repo: Path) -> None:
    _edit(
        service_repo,
        ("        return order;\n", "        audit.log(order);\n        return order;\n"),
        (
            "            throw new NotFound(id);\n",
            "            throw new NotFound(String.valueOf(id));\n",
        ),
    )
    (hunk,) = hunks_for_file(service_repo, _file_change(service_repo))
    assert hunk.scope == "find(long id)" and (hunk.start, hunk.end) == (7, 14)
    lines = hunk.code.splitlines()
    assert lines[0] == "    7       public Order find(long id) {"
    assert "      -             throw new NotFound(id);" in lines
    assert "   10 +             throw new NotFound(String.valueOf(id));" in lines
    assert "   12 +         audit.log(order);" in lines


def test_removed_lines_appear_where_they_used_to_be(service_repo: Path) -> None:
    _edit(service_repo, ("        cache.evict(id);\n", ""))
    (hunk,) = hunks_for_file(service_repo, _file_change(service_repo))
    lines = hunk.code.splitlines()
    index = next(i for i, line in enumerate(lines) if "cache.evict(id);" in line)
    assert lines[index][:8].strip() == "-"
    assert 'metrics.count("remove");' in lines[index + 1]
    assert lines[index + 1][:8].strip().isdigit()


def test_changes_in_two_methods_produce_two_hunks(service_repo: Path) -> None:
    _edit(
        service_repo,
        ("        return order;", "        return order; // checked"),
        ("            sum += o.price();", "            sum += o.price() * 2;"),
    )
    hunks = hunks_for_file(service_repo, _file_change(service_repo))
    assert [h.scope for h in hunks] == ["find(long id)", "total(List<Order> orders)"]


def test_change_outside_any_function_gets_a_window_of_lines(service_repo: Path) -> None:
    _edit(service_repo, ("private int retries = 3;", "private int retries = 5;"))
    (hunk,) = hunks_for_file(service_repo, _file_change(service_repo))
    assert hunk.scope == "lines 1-25" and hunk.start == 1
    assert "    5 +     private int retries = 5;" in hunk.code.splitlines()


def test_oversized_scope_is_reduced_to_windows_around_the_changes(repo: Path) -> None:
    body = "\n".join(f"        step{i}();" for i in range(1, 400))
    (repo / "src" / "Big.java").write_text(
        f"class Big {{\n    void run() {{\n{body}\n    }}\n}}\n", encoding="utf-8"
    )
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", "big")
    path = repo / "src" / "Big.java"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("step10();", "step10(1);").replace("step300();", "step300(2);"), "utf-8"
    )
    (change,) = [c for c in changes(repo) if c.path == "src/Big.java"]
    (hunk,) = hunks_for_file(repo, change)
    assert hunk.scope == "run()"
    assert "      ..." in hunk.code.splitlines()
    assert (
        len(hunk.code.splitlines()) < 130 and "step10(1)" in hunk.code and "step300(2)" in hunk.code
    )


def test_new_large_file_is_truncated(repo: Path) -> None:
    (repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(1000)) + "\n", "utf-8")
    (change,) = [c for c in changes(repo) if c.path == "big.txt"]
    (hunk,) = hunks_for_file(repo, change)
    lines = hunk.code.splitlines()
    assert lines[-1] == "      ... truncated" and len(lines) == MAX_HUNK_LINES + 1


def test_deleted_or_missing_file_has_no_hunks(service_repo: Path) -> None:
    (service_repo / "src" / "OrderService.java").unlink()
    (change,) = [c for c in changes(service_repo) if c.path == "src/OrderService.java"]
    assert hunks_for_file(service_repo, change) == []


def test_prepare_writes_packet_jobs_and_summary(
    service_repo: Path, lenses: Path, packs_dir: Path
) -> None:
    _edit(service_repo, ("        return order;", "        return order; // checked"))
    result = prepare(
        service_repo, None, run="a1f3", packs=load_packs(packs_dir), lenses_path=lenses
    )
    packet = json.loads((result.directory / "packet.json").read_text(encoding="utf-8"))
    assert validation_errors("packet", packet) == []
    assert (packet["run"], packet["tier"], packet["stack"]) == ("a1f3", "XS", ["java"])
    assert [(j["id"], j["lens"]) for j in packet["jobs"]] == [("j1", "correctness")]
    assert packet["hunks"][0]["scope"] == "find(long id)"
    assert (result.directory / "jobs" / "j1.md").is_file()
    assert json.loads((result.directory / "excluded.json").read_text(encoding="utf-8")) == []


def test_job_file_puts_the_data_first_and_the_lens_last(
    service_repo: Path, lenses: Path, packs_dir: Path
) -> None:
    (service_repo / "REVIEW.md").write_text("- queries must filter by tenant\n", encoding="utf-8")
    run_git(service_repo, "add", "-A")
    run_git(service_repo, "commit", "--quiet", "-m", "rules")
    _edit(service_repo, ("        return order;", "        return order; // checked"))
    result = prepare(service_repo, None, run="b2", packs=load_packs(packs_dir), lenses_path=lenses)
    assert result.packet["rules"] == ["REVIEW.md#rules: queries must filter by tenant"]
    job = (result.directory / "jobs" / "j1.md").read_text(encoding="utf-8")
    order = [
        job.index("# Review job j1"),
        job.index("## Repository rules"),
        job.index(DATA_BEGIN),
        job.index("public Order find(long id)"),
        job.index(DATA_END),
        job.index("## Stack notes: java/correctness"),
        job.index("## Lens: correctness"),
    ]
    assert order == sorted(order)
    assert "Watch for Optional misuse." in job and "Find logic errors." in job


def test_security_lens_is_added_when_a_trigger_fires(
    service_repo: Path, lenses: Path, packs_dir: Path
) -> None:
    _edit(
        service_repo,
        (
            '        audit.log("remove");',
            '        Runtime.getRuntime().exec(id);\n        audit.log("remove");',
        ),
    )
    result = prepare(service_repo, None, run="c3", packs=load_packs(packs_dir), lenses_path=lenses)
    jobs = {job["id"]: job for job in result.packet["jobs"]}
    assert [(j["id"], j["lens"]) for j in jobs.values()] == [
        ("j1", "correctness"),
        ("j2", "security"),
    ]
    assert result.packet["tier"] == "S"
    assert "_universal:command-exec@" in jobs["j2"]["why"]
    assert result.packet["files"][0]["tags"] == ["command-exec"]
    assert "## Lens: security" in (result.directory / "jobs" / "j2.md").read_text(encoding="utf-8")


def test_range_mode_reads_rules_from_the_base_and_excludes_lockfiles(
    service_repo: Path, lenses: Path, packs_dir: Path
) -> None:
    (service_repo / "REVIEW.md").write_text("- base rule\n", encoding="utf-8")
    run_git(service_repo, "add", "-A")
    run_git(service_repo, "commit", "--quiet", "-m", "rules")
    base = run_git(service_repo, "rev-parse", "HEAD")
    (service_repo / "REVIEW.md").write_text("- rewritten rule\n", encoding="utf-8")
    (service_repo / "yarn.lock").write_text("lock\n", encoding="utf-8")
    _edit(service_repo, ("        return order;", "        return order; // checked"))
    run_git(service_repo, "add", "-A")
    run_git(service_repo, "commit", "--quiet", "-m", "change")
    result = prepare(
        service_repo, f"{base}...HEAD", run="d4", packs=load_packs(packs_dir), lenses_path=lenses
    )
    assert result.packet["rules"] == ["REVIEW.md#rules: base rule"]
    assert ("yarn.lock", "lockfile") in result.excluded
    assert "REVIEW.md" in [f["p"] for f in result.packet["files"]]


def test_summary_is_short_and_carries_a_machine_readable_plan(
    service_repo: Path, lenses: Path, packs_dir: Path
) -> None:
    _edit(service_repo, ("        return order;", "        return order; // checked"))
    result = prepare(service_repo, None, run="e5", packs=load_packs(packs_dir), lenses_path=lenses)
    assert len(result.summary) / 4 <= 200
    plan_line = next(line for line in result.summary.splitlines() if line.startswith("PLAN "))
    plan = json.loads(plan_line.removeprefix("PLAN "))
    assert plan == {
        "run": "e5",
        "tier": "XS",
        "max_turns": 4,
        "verify": "candidate",
        "jobs": [{"id": "j1", "lens": "correctness"}],
    }
    assert "stack java" in result.summary


def test_prepare_with_nothing_to_review(service_repo: Path, lenses: Path, packs_dir: Path) -> None:
    result = prepare(service_repo, None, run="f6", packs=load_packs(packs_dir), lenses_path=lenses)
    assert result.packet["jobs"] == [] and "nothing to review" in result.summary


def test_prepare_without_lens_files_plans_no_jobs(
    service_repo: Path, tmp_path: Path, packs_dir: Path
) -> None:
    _edit(service_repo, ("        return order;", "        return order; // checked"))
    empty = tmp_path / "no-lenses"
    empty.mkdir()
    result = prepare(service_repo, None, run="g7", packs=load_packs(packs_dir), lenses_path=empty)
    assert result.packet["jobs"] == []


def test_unknown_range_is_reported(service_repo: Path, lenses: Path, packs_dir: Path) -> None:
    with pytest.raises(PrepareError, match="failed"):
        prepare(service_repo, "nope...HEAD", packs=load_packs(packs_dir), lenses_path=lenses)


def test_generated_run_ids_are_unique_lowercase_hex(
    service_repo: Path, lenses: Path, packs_dir: Path
) -> None:
    first = prepare(service_repo, None, packs=load_packs(packs_dir), lenses_path=lenses)
    second = prepare(service_repo, None, packs=load_packs(packs_dir), lenses_path=lenses)
    assert first.run != second.run and len(first.run) == 6
    int(first.run, 16)


def test_cli_prepare_prints_the_summary(
    monkeypatch: pytest.MonkeyPatch,
    service_repo: Path,
    lenses: Path,
    packs_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("REVIEW_SQUAD_PACKS", str(packs_dir))
    monkeypatch.setenv("REVIEW_SQUAD_LENSES", str(lenses))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    _edit(service_repo, ("        return order;", "        return order; // checked"))
    assert main(["prepare", "--repo", str(service_repo), "--id", "h8"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("review-squad run h8: tier XS") and "PLAN {" in out


def test_cli_prepare_reports_bad_ranges(
    monkeypatch: pytest.MonkeyPatch,
    service_repo: Path,
    lenses: Path,
    packs_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("REVIEW_SQUAD_PACKS", str(packs_dir))
    monkeypatch.setenv("REVIEW_SQUAD_LENSES", str(lenses))
    assert main(["prepare", "nope...HEAD", "--repo", str(service_repo)]) == 2
    assert "error:" in capsys.readouterr().err
