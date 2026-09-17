"""Regression: same-named artifacts in different folders must not be dropped.

Job 4b5fed78 declared 13 deliverables whose report.md/report.json/report.xlsx
basenames recurred across 目录明细 / 目录校验报告 / 综合报告 folders. The
collector flattened names to basenames and silently skipped duplicates, so
only 9 of 13 files reached the user's workspace.
"""
from __future__ import annotations

from app.artifact_validation import (
    ARTIFACT_FILENAME_MAX,
    normalize_artifact_paths,
    unique_artifact_filenames,
)


JOB_4BFED78_PATHS = [
    "/workspace/output/执行状态.json",
    "/workspace/output/校验摘要.md",
    "/workspace/output/结果索引.md",
    "/workspace/output/输入与配置快照.json",
    "/workspace/output/目录明细/catalog.xlsx",
    "/workspace/output/目录明细/extraction.json",
    "/workspace/output/目录明细/report.md",
    "/workspace/output/目录校验报告/report.json",
    "/workspace/output/目录校验报告/report.md",
    "/workspace/output/目录校验报告/report.xlsx",
    "/workspace/output/综合报告/report.json",
    "/workspace/output/综合报告/report.md",
    "/workspace/output/综合报告/report.xlsx",
]


def test_all_13_job_paths_get_unique_names():
    names = unique_artifact_filenames(normalize_artifact_paths(JOB_4BFED78_PATHS))
    stored = list(names.values())
    assert len(stored) == 13
    assert len(set(stored)) == 13


def test_unique_basenames_unchanged_and_duplicates_prefixed():
    names = unique_artifact_filenames(normalize_artifact_paths(JOB_4BFED78_PATHS))
    assert names["/workspace/output/执行状态.json"] == "执行状态.json"
    assert names["/workspace/output/目录明细/catalog.xlsx"] == "catalog.xlsx"
    # First occurrence of a recurring basename keeps the plain name ...
    assert names["/workspace/output/目录明细/report.md"] == "report.md"
    # ... later occurrences are disambiguated with their parent folder.
    assert names["/workspace/output/目录校验报告/report.md"] == "目录校验报告_report.md"
    assert names["/workspace/output/综合报告/report.md"] == "综合报告_report.md"
    # report.json/xlsx first appear under 目录校验报告, so that copy keeps the
    # plain basename and 综合报告 is prefixed.
    assert names["/workspace/output/目录校验报告/report.json"] == "report.json"
    assert names["/workspace/output/综合报告/report.json"] == "综合报告_report.json"
    assert names["/workspace/output/目录校验报告/report.xlsx"] == "report.xlsx"
    assert names["/workspace/output/综合报告/report.xlsx"] == "综合报告_report.xlsx"


def test_exact_duplicate_path_collected_once_with_plain_name():
    paths = normalize_artifact_paths(
        ["/workspace/output/report.md", "/workspace/output/report.md"]
    )
    names = unique_artifact_filenames(paths)
    assert names == {"/workspace/output/report.md": "report.md"}


def test_same_named_subfolders_walk_up_until_unique():
    paths = normalize_artifact_paths(
        [
            "/workspace/output/a/x/report.md",
            "/workspace/output/b/x/report.md",
            "/workspace/output/c/x/report.md",
        ]
    )
    names = unique_artifact_filenames(paths)
    # First keeps the plain basename; later files add the shortest prefix that
    # resolves the collision (depth 1 collides, so depth 2 is used).
    assert names["/workspace/output/a/x/report.md"] == "report.md"
    assert names["/workspace/output/b/x/report.md"] == "x_report.md"
    assert names["/workspace/output/c/x/report.md"] == "c_x_report.md"


def test_unsafe_characters_sanitized():
    paths = normalize_artifact_paths(['/workspace/output/a:b/c|d?.md'])
    names = unique_artifact_filenames(paths)
    name = names["/workspace/output/a:b/c|d?.md"]
    for char in '/\\:|?<>':
        assert char not in name
    assert name.endswith(".md")


def test_long_filename_capped_but_extension_kept():
    long_stem = "a" * 220
    paths = normalize_artifact_paths([f"/workspace/output/{long_stem}.xlsx"])
    names = unique_artifact_filenames(paths)
    (name,) = names.values()
    assert len(name) == ARTIFACT_FILENAME_MAX
    assert name.endswith(".xlsx")


def test_every_path_looks_unique_even_with_many_repeats():
    paths = normalize_artifact_paths(
        [f"/workspace/output/set-{i}/report.md" for i in range(6)]
    )
    names = unique_artifact_filenames(paths)
    assert len(set(names.values())) == 6
