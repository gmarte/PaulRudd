"""Configuration loading (trusted, from the base commit) and diff preparation."""

import pytest

import config as config_module
from config import is_trusted_path, load_config
from conftest import pr_file
from diff_processor import (
    MAX_PART_CHARS,
    FileChange,
    PathFilter,
    annotate_patch,
    fetch_file_changes,
    new_line_range,
    skip_reason,
    split_in_two,
    split_patch,
    walkthrough_diff,
)

# ── Path filters (B4) ────────────────────────────────────────────────────────


@pytest.mark.parametrize("path, excluded", [
    ("package-lock.json", True),          # root files used to slip past every default
    ("yarn.lock", True),
    ("web/package-lock.json", True),
    ("go.sum", True),
    ("dist/app.js", True),
    ("pkg/build/out.js", True),
    ("app.min.js", True),
    ("src/build.py", False),
    ("app/views.py", False),
    ("src/payments.lock/index.js", False),  # a directory named like a lockfile hides nothing
    ("lib/app.min.js/index.js", False),
])
def test_default_exclusions_match_at_any_depth(path, excluded):
    assert PathFilter.from_config({"excluded_paths": [], "exclude_defaults": True}).excluded(path) is excluded


def test_user_patterns_extend_the_defaults_and_are_anchored():
    path_filter = PathFilter.from_config({"excluded_paths": ["docs/**"], "exclude_defaults": True})
    assert path_filter.excluded("docs/guide.md")
    assert not path_filter.excluded("api/docs/views.py")  # used to match
    assert path_filter.excluded("poetry.lock")             # defaults still apply


def test_defaults_can_be_turned_off():
    assert not PathFilter.from_config({"excluded_paths": [], "exclude_defaults": False}).excluded("yarn.lock")


# ── Config ───────────────────────────────────────────────────────────────────

def test_config_and_guidelines_are_read_at_the_base_commit(gh, tmp_path):
    (tmp_path / ".paul.yml").write_text("severity_threshold: minor\n", encoding="utf-8")  # the PR's copy
    gh.base_files.update({
        ".paul.yml": "severity_threshold: critical\nlanguage: Spanish\n",
        "CLAUDE.md": "Use the service layer.",
        ".agents/rules/sql.md": "Never build SQL with f-strings.",
    })
    config = load_config()
    assert config["severity_threshold"] == "critical"
    assert config["config_source"] == ".paul.yml@bbbbbbb"
    assert "Use the service layer." in config["repo_context"]
    assert "### .agents/rules/sql.md" in config["repo_context"]


def test_without_base_sha_the_working_directory_is_used(monkeypatch, tmp_path):
    monkeypatch.delenv("BASE_SHA")
    (tmp_path / ".paul.yml").write_text("severity_threshold: minor\n", encoding="utf-8")
    config = load_config()
    assert config["severity_threshold"] == "minor"
    assert config["config_source"] == ".paul.yml (working directory)"


def test_unknown_keys_warn_and_lists_are_accepted(gh, capsys):
    gh.base_files[".paul.yml"] = 'severity_treshold: critical\ncustom_instructions:\n  - "Check auth."\n  - "Check SQL."\nexcluded_paths: "docs/**"\n'
    config = load_config()
    assert "Ignoring unknown setting 'severity_treshold'" in capsys.readouterr().out
    assert config["custom_instructions"] == "Check auth.\nCheck SQL."
    assert config["excluded_paths"] == ["docs/**"]


@pytest.mark.parametrize("text", [
    "on_incomplete: maybe\n",
    "forks: allow\n",
    "temperature: hot\n",
    "max_tokens: 10\n",
    "time_budget_minutes: 0\n",
    "- just\n- a list\n",
    "severity_threshold: [major]\n",   # used to escape as a TypeError
    "provider: {name: anthropic}\n",
])
def test_invalid_settings_are_rejected(gh, text):
    gh.base_files[".paul.yml"] = text
    with pytest.raises(ValueError):
        load_config()


def test_non_utf8_guidelines_do_not_crash(monkeypatch, tmp_path):
    # B22
    monkeypatch.delenv("BASE_SHA")
    (tmp_path / "README.md").write_bytes("Café".encode("latin-1"))
    assert "Caf" in config_module.read_repo_context()


def test_trusted_paths():
    assert is_trusted_path(".paul.yml", ".paul.yml")
    assert is_trusted_path("CLAUDE.md", ".paul.yml")
    assert is_trusted_path(".agents/rules/sql.md", ".paul.yml")
    assert not is_trusted_path(".agents/rules/nested/x.md", ".paul.yml")
    assert not is_trusted_path("docs/CLAUDE.md", ".paul.yml")


# ── Files ────────────────────────────────────────────────────────────────────

def test_files_are_fetched_across_pages(gh):
    gh.pr_files = [pr_file(f"src/f{i}.py", "@@ -1 +1 @@\n+x\n") for i in range(250)]
    changes = fetch_file_changes()
    assert len(changes) == 250 and changes[-1].path == "src/f249.py"


def _change(path="a.py", patch="@@ -1 +1 @@\n+x\n", status="modified", additions=1, deletions=0):
    return FileChange(path=path, status=status, additions=additions, deletions=deletions, patch=patch)


@pytest.mark.parametrize("change, reason", [
    (_change("yarn.lock"), "excluded"),
    (_change(status="removed"), "removed"),
    (_change(patch=None, additions=0), "binary"),
    (_change(patch=None, status="renamed", additions=0), "renamed"),
    (_change(patch=None, additions=9000), "too_large"),  # GitHub omitted a real diff
    (_change(), None),
])
def test_skip_reasons(change, reason):
    assert skip_reason(change, PathFilter.from_config({"excluded_paths": [], "exclude_defaults": True})) == reason


def test_patches_are_annotated_with_new_file_line_numbers():
    patch = "@@ -10,3 +20,3 @@ def f():\n context\n-removed\n+added\n context2\n\\ No newline at end of file"
    lines = annotate_patch(patch).splitlines()
    assert lines[0] == "@@ -10,3 +20,3 @@ def f():"
    assert lines[1] == "    20  context"
    assert lines[2] == "       -removed"
    assert lines[3] == "    21 +added"
    assert lines[4] == "    22  context2"
    assert lines[5] == "       \\ No newline at end of file"


def _hunks(count, size):
    return "".join(f"@@ -{i},1 +{i},1 @@\n" + "+" + "y" * size + "\n" for i in range(1, count + 1))


def test_large_patches_are_split_between_hunks():
    patch = _hunks(6, 15_000)
    parts = split_patch(patch)
    assert len(parts) > 1
    assert "".join(parts) == patch
    assert all(p.startswith("@@") and len(p) <= MAX_PART_CHARS for p in parts)
    assert split_patch(_hunks(2, 100)) == [_hunks(2, 100)]


def test_unusual_line_breaks_inside_a_line_do_not_shift_line_numbers():
    # Review finding R13: str.splitlines() also broke on form feeds and U+2028.
    patch = "@@ -1,3 +1,4 @@\n a = 1\n+b = 'x\x0cy z'\n+c = 3\n d = 4"
    lines = annotate_patch(patch).split("\n")
    assert lines[-1] == "     4  d = 4"
    assert split_patch(patch) == [patch]


def test_new_line_range_covers_every_hunk():
    patch = "@@ -1,2 +5,3 @@\n a\n+b\n c\n@@ -40 +44 @@\n-x\n+y\n"
    assert new_line_range(patch) == (5, 44)
    assert new_line_range("no hunks") == (None, None)


def test_split_in_two_keeps_every_hunk():
    patch = _hunks(5, 100)
    halves = split_in_two(patch)
    assert len(halves) == 2 and "".join(halves) == patch
    assert split_in_two(_hunks(1, 100)) == [_hunks(1, 100)]


def test_walkthrough_skips_files_that_do_not_fit_instead_of_stopping():
    # B1: one oversized file used to `break` the loop and drop every smaller file.
    huge = _change("data/dump.py", "@@ -1 +1 @@\n+" + "z" * 100_000 + "\n")
    small = _change("app/views.py")
    test = _change("tests/test_views.py")
    text, omitted = walkthrough_diff([test, huge, small])
    assert omitted == [huge]
    assert text.index("app/views.py") < text.index("tests/test_views.py")  # source first
