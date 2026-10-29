import argparse
import asyncio
import math
import os
from pathlib import Path
import sys

from .demo import sample_snapshot
from .config import DEFAULT_MODEL, DEFAULT_REVIEW_TIMEOUT, load_environment, request_timeout
from .reporting import markdown
from .reviewers import DemoReviewer, ModelReviewer
from .snapshot import capture
from .workflow import review_repository


def main() -> int:
    parser = argparse.ArgumentParser(description="Three parallel specialist code reviews with LangGraph")
    parser.add_argument("--demo", action="store_true", help="Run scripted sample; no API key or repository needed")
    parser.add_argument("--repo", type=Path, help="Local Git checkout (committed changes only)")
    parser.add_argument("--base", help="Base commit or ref")
    parser.add_argument("--head", default="HEAD", help="Head commit or ref (default: HEAD)")
    parser.add_argument("--model", help=f"provider:model identifier (default: {DEFAULT_MODEL})")
    parser.add_argument("--env-file", type=Path, help="Explicit .env path; defaults to the editable project .env")
    parser.add_argument("--timeout", type=float, default=DEFAULT_REVIEW_TIMEOUT,
                        help=f"Total seconds per specialist, including retry (default: {DEFAULT_REVIEW_TIMEOUT})")
    parser.add_argument("--out", type=Path, required=True, help="Directory for report.json and report.md")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite positive number")
    if args.demo and (args.repo or args.base or args.model):
        parser.error("--demo uses only its built-in snapshot; omit --repo, --base, and --model")
    if not args.demo and (not args.repo or not args.base):
        parser.error("live review requires --repo and --base")
    try:
        if not args.demo:
            load_environment(args.env_file)
        snapshot = sample_snapshot() if args.demo else capture(args.repo, args.base, args.head)
        if not snapshot.files:
            print("No supported changed head files to review. Check refs and file types.", file=sys.stderr)
            for note in snapshot.limitations:
                print(note, file=sys.stderr)
            return 2
        model = args.model or os.environ.get("REVIEW_MODEL", DEFAULT_MODEL)
        reviewer = DemoReviewer() if args.demo else ModelReviewer(model, request_timeout(args.timeout))
        mode = "demo" if args.demo else "live"
        report = asyncio.run(review_repository(snapshot, reviewer, timeout=args.timeout, mode=mode))
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "report.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        (args.out / "report.md").write_text(markdown(report), encoding="utf-8")
        print(f"{mode}: {report.status}; {len(report.findings)} accepted findings")
        print(f"Report: {(args.out / 'report.md').resolve()}")
        return 2 if report.status == "partial" and not args.demo else 0
    except Exception as exc:
        print(f"Cannot start review ({type(exc).__name__}). Check Git refs, provider installation, "
              "model configuration, credentials, and output path.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
