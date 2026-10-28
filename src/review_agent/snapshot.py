"""Read committed blobs, never the working tree or repository executables."""
import re
import subprocess
from pathlib import Path

from .models import FileView, Snapshot
from .languages import PROJECT_CONFIG_NAMES, supported_file, test_file

MAX_FILE_BYTES = 24_000
MAX_CONTEXT_CHARS = 100_000


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, timeout=30,
    )
    if result.returncode:
        raise ValueError("Git operation failed; check repository path and commit references")
    return result.stdout


def resolve(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()


def added_lines(diff: str) -> list[int]:
    line = 0
    changed = []
    in_hunk = False
    for text in diff.splitlines():
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", text)
        if match:
            line, in_hunk = int(match[1]), True
        elif in_hunk:
            if text.startswith("+"):
                changed.append(line)
                line += 1
            elif text.startswith(" "):
                line += 1
    return changed


def capture(repo: Path, base: str, head: str) -> Snapshot:
    repo = repo.resolve()
    base_sha, head_sha = resolve(repo, base), resolve(repo, head)
    snapshot = Snapshot(base_sha=base_sha, head_sha=head_sha, files=[])
    tree = {}
    for entry in git(repo, "ls-tree", "-rz", "--full-tree", head_sha).split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        tree[raw_path.decode("utf-8", errors="surrogateescape")] = (mode, kind, oid)
    names = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                "--name-only", "-z", base_sha, head_sha, "--").split(b"\0")
    changed_paths = [p.decode("utf-8", errors="surrogateescape") for p in names if p]
    budget = MAX_CONTEXT_CHARS

    def read(path: str) -> str:
        if path not in tree:
            raise ValueError("deleted file; head-only review does not inspect deletions")
        mode, kind, oid = tree[path]
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError("symlink or non-regular file")
        if int(git(repo, "cat-file", "-s", oid)) > MAX_FILE_BYTES:
            raise ValueError("file exceeds 24 KB context limit")
        raw = git(repo, "cat-file", "blob", oid)
        if b"\0" in raw:
            raise ValueError("binary content")
        return raw.decode("utf-8")

    for path in changed_paths:
        try:
            if not supported_file(path):
                raise ValueError("unsupported file type")
            content = read(path)
            diff = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                       "--unified=5", base_sha, head_sha, "--", path).decode("utf-8")
            if len(content) + len(diff) > budget:
                raise ValueError("total context budget exhausted")
            snapshot.files.append(FileView(path=path, content=content, diff=diff,
                                           changed_lines=added_lines(diff)))
            budget -= len(content) + len(diff)
        except (ValueError, UnicodeError) as exc:
            snapshot.limitations.append(f"Skipped {path}: {exc}")

    loaded = {f.path for f in snapshot.files}
    # Build manifests help interpret dependencies, test commands, and module type.
    # Source changes retain priority, then manifests, then supporting tests.
    config_paths = sorted(p for p in tree if Path(p).name in PROJECT_CONFIG_NAMES)
    test_paths = sorted(p for p in tree if test_file(p))
    for label, paths, target in (("configuration", config_paths, snapshot.configuration),
                                 ("test", test_paths, snapshot.tests)):
        for path in paths:
            if path in loaded:
                continue
            try:
                content = read(path)
                if len(content) > budget:
                    raise ValueError("total context budget exhausted")
                target.append(FileView(path=path, content=content))
                loaded.add(path)
                budget -= len(content)
            except (ValueError, UnicodeError) as exc:
                snapshot.limitations.append(f"Skipped supporting {label} {path}: {exc}")
    snapshot.limitations.append(
        "Static, head-file review only. No tests or repository programs were executed. "
        "Unchanged application dependencies are not loaded; test coverage is not measured."
    )
    return snapshot
