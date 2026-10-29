import asyncio
import logging
import os
import tempfile
from pathlib import Path

import httpx

from .config import DEFAULT_MODEL, load_environment, request_timeout, review_timeout
from .github import GitHub, Superseded, fetch_snapshot
from .jobs import JobStore
from .models import Report, ROLES, AgentResult
from .reviewers import ModelReviewer
from .workflow import review_repository

LOG = logging.getLogger("review-worker")


async def process_job(store: JobStore, job: dict):
    payload = job["payload"]
    LOG.info("job_started job=%s pr=%s head=%s", job["id"], payload["number"], payload["head_sha"])
    async with httpx.AsyncClient(base_url="https://api.github.com", timeout=30,
                                headers={"Accept": "application/vnd.github+json",
                                         "X-GitHub-Api-Version": "2026-03-10"}) as client:
        github = await GitHub.authenticate(client, payload["installation_id"], payload["repository_id"])
        await github.ensure_current(payload)
        # Reconcile an ambiguous prior publish before potentially repeating model calls.
        marker = f"<!-- learning-review-agent:{job['id']} -->"
        existing = await github.existing_review(payload, marker)
        if existing:
            store.finish(job["id"], "completed", existing)
            return
        if job["report"]:
            report = Report.model_validate_json(job["report"])
        else:
            with tempfile.TemporaryDirectory(prefix="review-job-") as directory:
                snapshot = await asyncio.to_thread(fetch_snapshot, Path(directory) / "repository.git", payload, github.token)
                if snapshot.files:
                    deadline = review_timeout()
                    reviewer = ModelReviewer(os.environ.get("REVIEW_MODEL", DEFAULT_MODEL),
                                             timeout=request_timeout(deadline))
                    report = await review_repository(snapshot, reviewer, timeout=deadline)
                else:
                    report = Report(mode="live", base_sha=snapshot.base_sha, head_sha=snapshot.head_sha,
                                    status="partial", agents=[AgentResult(role=role, status="failed", error="NoSupportedFiles")
                                                              for role in ROLES], findings=[], limitations=snapshot.limitations)
            store.save_report(job["id"], report.model_dump_json())
        review_id = await github.publish(payload, job["id"], report)
        store.finish(job["id"], "completed", review_id)
        LOG.info("job_published job=%s review_id=%s analysis=%s findings=%d",
                 job["id"], review_id, report.status, len(report.findings))


async def run_worker():
    load_environment()
    review_timeout()  # Fail invalid configuration before claiming any work.
    for name in ("GITHUB_APP_ID", "GITHUB_PRIVATE_KEY_FILE", "NEBIUS_API_KEY"):
        if not os.environ.get(name):
            raise ValueError(f"Missing required worker configuration: {name}")
    store = JobStore(Path(os.environ.get("REVIEW_DB_PATH", "data/reviews.sqlite3")))
    while True:
        job = store.claim()
        if job is None:
            await asyncio.sleep(2)
            continue
        try:
            await process_job(store, job)
        except Superseded:
            store.finish(job["id"], "superseded")
        except Exception as exc:
            store.fail(job["id"], type(exc).__name__)
            LOG.warning("job=%s error=%s", job["id"], type(exc).__name__)
            await asyncio.sleep(10)


def main():
    logging.basicConfig(level=logging.WARNING)
    LOG.setLevel(logging.INFO)
    logging.getLogger("review_agent").setLevel(logging.INFO)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
