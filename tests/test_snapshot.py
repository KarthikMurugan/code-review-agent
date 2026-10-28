import subprocess

import pytest

from review_agent.snapshot import added_lines, capture


def run(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path):
    run(tmp_path, "init")
    run(tmp_path, "config", "user.name", "Test")
    run(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "app.py").write_text("def value():\n    return 1\n")
    (tmp_path / "test_app.py").write_text("def test_value():\n    assert True\n")
    run(tmp_path, "add", ".")
    run(tmp_path, "commit", "-m", "base")
    base = run(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "app.py").write_text("def value():\n    return 2\n")
    run(tmp_path, "add", ".")
    run(tmp_path, "commit", "-m", "head")
    return tmp_path, base


def test_capture_reads_commits_not_dirty_worktree(repo):
    path, base = repo
    (path / "app.py").write_text("working tree should never be read\n")
    snapshot = capture(path, base, "HEAD")
    assert len(snapshot.base_sha) == len(snapshot.head_sha) == 40
    assert snapshot.files[0].content == "def value():\n    return 2\n"
    assert snapshot.files[0].changed_lines == [2]
    assert snapshot.tests[0].path == "test_app.py"


def test_symlink_and_deleted_files_are_reported_as_skipped(repo):
    path, base = repo
    (path / "app.py").unlink()
    (path / "external.py").symlink_to("/etc/passwd")
    run(path, "add", ".")
    run(path, "commit", "-m", "delete and link")
    snapshot = capture(path, base, "HEAD")
    assert not snapshot.files
    assert any("deleted file" in note for note in snapshot.limitations)
    assert any("symlink" in note for note in snapshot.limitations)


def test_diff_line_mapping():
    diff = "@@ -1,2 +1,3 @@\n context\n-old\n+new\n+extra\n\\ No newline at end of file\n"
    assert added_lines(diff) == [2, 3]


def test_invalid_ref_is_rejected(repo):
    path, _ = repo
    with pytest.raises(ValueError):
        capture(path, "--help", "HEAD")


def test_javascript_java_and_unchanged_test_context(repo):
    from review_agent.reviewers import context

    path, _ = repo
    sources = {
        "web/app.js": "export const value = 1;\n",
        "web/view.jsx": "export const View = () => <div>1</div>;\n",
        "node/app.mjs": "export const value = 1;\n",
        "node/legacy.cjs": "module.exports = 1;\n",
        "service/src/main/java/Value.java": "class Value { int get() { return 1; } }\n",
    }
    support = {
        "web/app.test.js": "// regression test\n",
        "web/view.spec.jsx": "// JSX test\n",
        "tests/browser.mjs": "// browser test\n",
        "web/__tests__/render.cjs": "// CommonJS test\n",
        "service/src/test/java/Scenario.java": "class Scenario {}\n",
        "service/checks/ValueIT.java": "class ValueIT {}\n",
        "web/package.json": '{"type":"module"}\n',
        "service/pom.xml": "<project/>\n",
        "service/build.gradle.kts": "plugins { java }\n",
    }
    for name, body in {**sources, **support}.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    run(path, "add", ".")
    run(path, "commit", "-m", "multilanguage base")
    base = run(path, "rev-parse", "HEAD")
    for name, body in sources.items():
        (path / name).write_text(body.replace("1", "2"))
    (path / "web/app.test.js").write_text("// changed regression test\n")
    run(path, "add", ".")
    run(path, "commit", "-m", "multilanguage change")
    (path / "node/app.mjs").write_text("UNCOMMITTED CONTENT\n")
    snapshot = capture(path, base, "HEAD")
    assert {file.path for file in snapshot.files} == set(sources) | {"web/app.test.js"}
    assert {file.path for file in snapshot.configuration} == {"web/package.json", "service/pom.xml", "service/build.gradle.kts"}
    assert {file.path for file in snapshot.tests} == {"test_app.py", "web/view.spec.jsx", "tests/browser.mjs",
                                                   "web/__tests__/render.cjs", "service/src/test/java/Scenario.java",
                                                   "service/checks/ValueIT.java"}
    sent = context(snapshot)
    assert all(name in sent for name in sources.keys() | support.keys())
    assert "UNCOMMITTED CONTENT" not in sent
