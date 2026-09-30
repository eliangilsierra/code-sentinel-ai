"""Effort tier of a change and the plan of review jobs derived from it."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from review_ctx.gitdiff import FileChange
from review_ctx.packs import Hit, Routing

TIERS = ("XS", "S", "M", "L", "XL")
SCORE_LIMITS = (("XS", 50), ("S", 200), ("M", 800), ("L", 2000))
TAG_WEIGHT = 1.5
CLUSTER_WEIGHT = 10
MAX_CLUSTERS = 8
DEFAULT_LENSES = frozenset({"correctness", "security"})
CORRECTNESS = "correctness"


@dataclass(frozen=True)
class TierParams:
    """Limits applied to the jobs of one tier."""

    max_turns: int
    verify: str
    budget_usd: float
    cap_usd: float
    max_jobs: int | None
    extra_reads: int
    per_cluster: bool


PARAMS: dict[str, TierParams] = {
    "XS": TierParams(4, "candidate", 0.03, 0.10, 1, 2, False),
    "S": TierParams(6, "important", 0.10, 0.25, 2, 4, False),
    "M": TierParams(10, "important+security", 0.25, 0.50, 4, 8, False),
    "L": TierParams(12, "all", 0.60, 1.20, None, 12, True),
    "XL": TierParams(12, "all", 1.00, 2.00, None, 12, True),
}


@dataclass(frozen=True)
class Cluster:
    id: str
    key: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class JobSpec:
    """A unit of review work: one or more lenses over a set of files."""

    id: str
    lens: str
    files: tuple[str, ...]
    why: tuple[str, ...]
    cluster: str | None = None


@dataclass
class Plan:
    tier: str
    params: TierParams
    score: float
    clusters: list[Cluster]
    jobs: list[JobSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def changed_lines(change: FileChange) -> int:
    """Lines added plus removed in ``change``."""
    return change.additions + change.deletions


def score(files: list[FileChange], tags: dict[str, list[str]], clusters: int) -> float:
    """Weighted size: changed lines (heavier for tagged files) plus a cost per extra cluster."""
    total = sum(changed_lines(c) * (TAG_WEIGHT if tags.get(c.path) else 1.0) for c in files)
    return total + CLUSTER_WEIGHT * max(0, clusters - 1)


def classify(files: list[FileChange], tags: dict[str, list[str]], clusters: int) -> str:
    """Tier of a change; tagged changes are never XS."""
    value = score(files, tags, clusters)
    has_tags = any(tags.get(c.path) for c in files)
    for tier, limit in SCORE_LIMITS:
        if value < limit:
            return "S" if tier == "XS" and has_tags else tier
    return "XL"


def make_clusters(paths: list[str]) -> list[Cluster]:
    """Group paths by directory, coarsening the grouping until at most ``MAX_CLUSTERS`` remain."""
    directories = {p: PurePosixPath(p).parent.parts for p in paths}
    depth = max((len(parts) for parts in directories.values()), default=0)
    while True:
        groups: dict[str, list[str]] = {}
        for path, parts in directories.items():
            groups.setdefault("/".join(parts[:depth]) or ".", []).append(path)
        if len(groups) <= MAX_CLUSTERS or depth == 0:
            break
        depth -= 1
    return [
        Cluster(f"c{index}", key, tuple(sorted(members)))
        for index, (key, members) in enumerate(sorted(groups.items()), 1)
    ]


def plan_review(
    files: list[FileChange],
    routing: Routing,
    available_lenses: frozenset[str] = DEFAULT_LENSES,
) -> Plan:
    """Choose the tier and the jobs for ``files`` given the lenses activated by ``routing``."""
    clusters = make_clusters([c.path for c in files])
    tier = classify(files, routing.tags, len(clusters))
    plan = Plan(tier, PARAMS[tier], score(files, routing.tags, len(clusters)), clusters)
    if not files:
        return plan
    active = {
        lens: hits for lens, hits in routing.lenses.items() if lens in available_lenses and hits
    }
    tagged = {path for path, tags in routing.tags.items() if tags}
    if tier == "XL":
        plan.warnings.append("the change is too large; split it into smaller pull requests")
        active = {
            lens: [hit for hit in hits if hit.path in tagged or lens != CORRECTNESS]
            for lens, hits in active.items()
        }
        active = {lens: hits for lens, hits in active.items() if hits}
    if not active:
        return plan
    if tier == "XS":
        plan.jobs = [_combined("j1", active, None)]
    elif plan.params.per_cluster:
        plan.jobs = _per_cluster_jobs(active, clusters)
    else:
        plan.jobs = _per_lens_jobs(active, plan.params.max_jobs)
    return plan


def _files_of(hits: list[Hit]) -> tuple[str, ...]:
    return tuple(sorted({hit.path for hit in hits}))


def _whys(hits: list[Hit]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(hit.why for hit in hits))


def _combined(job_id: str, active: dict[str, list[Hit]], cluster: str | None) -> JobSpec:
    lenses = sorted(active, key=lambda lens: (lens != CORRECTNESS, lens))
    hits = [hit for lens in lenses for hit in active[lens]]
    return JobSpec(job_id, "+".join(lenses), _files_of(hits), _whys(hits), cluster)


def _per_lens_jobs(active: dict[str, list[Hit]], limit: int | None) -> list[JobSpec]:
    lenses = sorted(active, key=lambda lens: (lens != CORRECTNESS, lens))
    if limit is not None and len(lenses) > limit:
        kept, merged = lenses[: limit - 1], lenses[limit - 1 :]
        groups: list[list[str]] = [[lens] for lens in kept] + [merged]
    else:
        groups = [[lens] for lens in lenses]
    jobs = []
    for index, group in enumerate(groups, 1):
        hits = [hit for lens in group for hit in active[lens]]
        jobs.append(JobSpec(f"j{index}", "+".join(group), _files_of(hits), _whys(hits)))
    return jobs


def _per_cluster_jobs(active: dict[str, list[Hit]], clusters: list[Cluster]) -> list[JobSpec]:
    jobs: list[JobSpec] = []
    lenses = sorted(active, key=lambda lens: (lens != CORRECTNESS, lens))
    for cluster in clusters:
        members = set(cluster.files)
        for lens in lenses:
            hits = [hit for hit in active[lens] if hit.path in members]
            if hits:
                jobs.append(
                    JobSpec(f"j{len(jobs) + 1}", lens, _files_of(hits), _whys(hits), cluster.id)
                )
    return jobs
