"""Filename disambiguation for flat workspace namespaces."""
from app.workspace_service import deduplicate_filenames


def test_unique_names_unchanged():
    assert deduplicate_filenames(["a.txt", "b.txt", "c"]) == ["a.txt", "b.txt", "c"]


def test_duplicates_get_numbered_suffix_before_extension():
    out = deduplicate_filenames(["report.pdf", "report.pdf", "report.pdf"])
    assert out == ["report.pdf", "report (2).pdf", "report (3).pdf"]


def test_collision_match_is_case_insensitive():
    out = deduplicate_filenames(["Data.xlsx", "data.XLSX"])
    assert out == ["Data.xlsx", "data (2).XLSX"]


def test_files_without_extension_and_dotfiles():
    out = deduplicate_filenames(["README", "README", ".bashrc", ".bashrc"])
    assert out == ["README", "README (2)", ".bashrc", ".bashrc (2)"]


def test_numbered_name_takes_next_free_slot():
    out = deduplicate_filenames(["x.txt", "x (2).txt", "x.txt"])
    assert out == ["x.txt", "x (2).txt", "x (3).txt"]


def test_multi_part_extension_only_last_part_split():
    out = deduplicate_filenames(["a.tar.gz", "a.tar.gz"])
    assert out == ["a.tar.gz", "a.tar (2).gz"]


def test_renamed_names_never_exceed_180_chars():
    # Input at the 180-char limit enforced by safe_workspace_filename.
    name = "n" * (180 - 4) + ".txt"
    out = deduplicate_filenames([name, name])
    assert len(out[0]) == 180
    assert len(out[1]) <= 180
    assert out[1].endswith(").txt")
