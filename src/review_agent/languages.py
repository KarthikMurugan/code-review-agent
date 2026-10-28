"""File selection conventions; no language runtimes or build tools are executed."""
from pathlib import PurePosixPath

JAVASCRIPT_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs"}
SOURCE_SUFFIXES = {".py", ".java"} | JAVASCRIPT_SUFFIXES
CONFIG_SUFFIXES = {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".xml", ".gradle", ".properties"}
PROJECT_CONFIG_NAMES = {
    "pyproject.toml", "package.json", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts", "gradle.properties", "jsconfig.json",
}


def supported_file(path: str) -> bool:
    file = PurePosixPath(path)
    return file.suffix in SOURCE_SUFFIXES | CONFIG_SUFFIXES or file.name in PROJECT_CONFIG_NAMES


def test_file(path: str) -> bool:
    file = PurePosixPath(path)
    directories = file.parts[:-1]
    in_test_directory = bool({"test", "tests", "__tests__", "e2e"}.intersection(directories))
    if file.suffix == ".py":
        return in_test_directory or file.name.startswith("test_") or file.name.endswith("_test.py")
    if file.suffix in JAVASCRIPT_SUFFIXES:
        return in_test_directory or file.stem.endswith((".test", ".spec"))
    if file.suffix == ".java":
        return (in_test_directory or "integrationTest" in directories or
                file.stem.startswith("Test") or file.stem.endswith(("Test", "Tests", "TestCase", "IT")))
    return False
