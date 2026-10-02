"""
Fetches the PR's changed files from the GitHub API and prepares them for
review: path filtering, line-numbered patches, splitting oversized files at
hunk boundaries, and a size-budgeted diff for the walkthrough pass.

Nothing is dropped silently: every file is either reviewed or reported with the
reason it wasn't.
"""

import fnmatch
import re
from dataclasses import dataclass

import pathspec

import github_client
from config import DEFAULT_EXCLUDED_DIRS, DEFAULT_EXCLUDED_FILES

MAX_FILE_PATCH_CHARS = 60_000   # patches above this are reviewed in parts
MAX_PART_CHARS = 40_000         # target size of each part of a split patch
MAX_WALKTHROUGH_CHARS = 80_000  # diff budget for the Pass 1 walkthrough

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_STATUS_LETTER = {"added": "A", "removed": "D", "modified": "M", "renamed": "R", "copied": "C"}


@dataclass
class FileChange:
    path: str
    status: str          # added | removed | modified | renamed | copied | changed | unchanged
    additions: int
    deletions: int
    patch: str | None    # None when GitHub omits it: a binary file, or a diff too large for the API
    previous_path: str | None = None


def fetch_file_changes() -> list:
    """Every changed file in the PR, in GitHub's order (paginated, at most 3,000 files)."""
    return [
        FileChange(
            path=f["filename"],
            status=f.get("status", "modified"),
            additions=f.get("additions", 0),
            deletions=f.get("deletions", 0),
            patch=f.get("patch"),
            previous_path=f.get("previous_filename"),
        )
        for f in github_client.list_pr_files()
    ]


class PathFilter:
    """
    excluded_paths as gitignore-style patterns, on top of the built-in defaults.
    The default file-name patterns are matched against a file's own name only.
    """

    def __init__(self, patterns: list, file_name_patterns: list = ()):
        self._spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        self._file_names = list(file_name_patterns)

    @classmethod
    def from_config(cls, config: dict) -> "PathFilter":
        if config.get("exclude_defaults", True):
            return cls(DEFAULT_EXCLUDED_DIRS + list(config["excluded_paths"]), DEFAULT_EXCLUDED_FILES)
        return cls(list(config["excluded_paths"]))

    def excluded(self, path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in self._file_names):
            return True
        return self._spec.match_file(path)


def skip_reason(change: FileChange, path_filter: PathFilter) -> str | None:
    """Why a file gets no per-file review (a coverage reason), or None if it should be reviewed."""
    if path_filter.excluded(change.path):
        return "excluded"
    if change.status == "removed":
        return "removed"
    if change.patch is None:
        if change.additions or change.deletions:
            return "too_large"
        return "renamed" if change.status == "renamed" else "binary"
    if not change.patch.strip():
        return "renamed" if change.status == "renamed" else "no_changes"
    return None


def file_table(changes: list) -> str:
    """One line per changed file: status letter, path, line counts."""
    rows = []
    for change in changes:
        letter = _STATUS_LETTER.get(change.status, "M")
        moved = f" (from {change.previous_path})" if change.previous_path else ""
        rows.append(f"{letter}  {change.path}{moved}  +{change.additions} -{change.deletions}")
    return "\n".join(rows)


def annotate_patch(patch: str) -> str:
    """
    Prefix every line with its new-file line number so the model can cite lines
    without counting. Removed lines have no new-file number and get a blank.
    """
    out = []
    line_no = None
    for line in patch.rstrip("\n").split("\n"):
        header = _HUNK_HEADER.match(line)
        if header:
            line_no = int(header.group(1))
            out.append(line)
        elif line_no is None:
            out.append(line)
        elif line.startswith(("-", "\\")):
            out.append(f"{'':>6} {line}")
        else:
            out.append(f"{line_no:>6} {line}")
            line_no += 1
    return "\n".join(out)


def _lines(patch: str) -> list:
    """
    The patch's lines with their endings. Only "\\n" ends a line in a diff, unlike
    str.splitlines(), which also breaks on form feeds and Unicode line separators.
    """
    return re.findall(r"[^\n]*\n|[^\n]+$", patch)


def _split_hunks(patch: str) -> list:
    """The patch's @@ hunks, each with its header."""
    hunks, current = [], []
    for line in _lines(patch):
        if _HUNK_HEADER.match(line) and current:
            hunks.append("".join(current))
            current = []
        current.append(line)
    if current:
        hunks.append("".join(current))
    return hunks


def split_patch(patch: str) -> list:
    """A patch over MAX_FILE_PATCH_CHARS as parts of about MAX_PART_CHARS, cut between hunks."""
    if len(patch) <= MAX_FILE_PATCH_CHARS:
        return [patch]
    parts, current = [], ""
    for hunk in _split_hunks(patch):
        if current and len(current) + len(hunk) > MAX_PART_CHARS:
            parts.append(current)
            current = ""
        current += hunk
    if current:
        parts.append(current)
    return parts


def new_line_range(patch: str) -> tuple:
    """(first, last) new-file line covered by the patch's hunks, or (None, None) if it has none."""
    first = last = None
    for line in _lines(patch):
        header = _HUNK_HEADER.match(line)
        if not header:
            continue
        start = int(header.group(1))
        length = int(header.group(2)) if header.group(2) is not None else 1
        end = start + max(length, 1) - 1
        first = start if first is None else min(first, start)
        last = end if last is None else max(last, end)
    return first, last


def split_in_two(patch: str) -> list:
    """Two halves of a patch, cut between hunks; a single-hunk patch comes back whole."""
    hunks = _split_hunks(patch)
    if len(hunks) < 2:
        return [patch]
    half, size = len(patch) / 2, 0
    for i, hunk in enumerate(hunks[:-1], 1):
        size += len(hunk)
        if size >= half:
            break
    return ["".join(hunks[:i]), "".join(hunks[i:])]


def walkthrough_diff(changes: list) -> tuple:
    """
    The diffs for the walkthrough, source files first, within MAX_WALKTHROUGH_CHARS.
    Files that don't fit are skipped (not cut off) and returned so the prompt can name them.
    Returns (diff_text, omitted_changes).
    """
    blocks, omitted, used = [], [], 0
    for change in sorted(changes, key=_walkthrough_priority):
        block = f"--- {change.path} ({change.status}, +{change.additions} -{change.deletions})\n{change.patch}\n"
        if used + len(block) > MAX_WALKTHROUGH_CHARS:
            omitted.append(change)
            continue
        blocks.append(block)
        used += len(block)
    return "".join(blocks), omitted


def _walkthrough_priority(change: FileChange) -> int:
    path = change.path.lower()
    name = path.rsplit("/", 1)[-1]
    if re.search(r"(^|/)(tests?|__tests__|spec)/", path) or name.startswith("test_") or re.search(r"[._-](test|spec)\.", name):
        return 1
    if name.endswith((".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".xml")):
        return 2
    if name.endswith((".md", ".rst", ".txt")) or path.startswith("docs/"):
        return 3
    return 0
