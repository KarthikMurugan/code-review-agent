import asyncio
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from review_agent.github import GitHub, Superseded, comment_body
from review_agent.jobs import JobStore
from review_agent.models import Report
from review_agent.server import create_app

SECRET = "synthetic-webhook-secret-32-characters"
PAYLOAD = {"installation_id": 1, "repository_id": 2, "repository": "owner/repository",
           "number": 3, "base_sha": "a" * 40, "head_sha": "b" * 40}


def test_browser_root_identifies_api_and_health_endpoint(tmp_path):
    with TestClient(create_app(store=JobStore(tmp_path / "jobs.sqlite3"), webhook_secret=SECRET)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.json()["service"] == "Code Review Agent"
        assert response.json()["health"] == "/healthz"
        assert response.json()["webhook"] == "/webhooks/github"


def event():
    return {"action": "opened", "installation": {"id": 1},
            "repository": {"id": 2, "full_name": "owner/repository"}, "number": 3,
            "pull_request": {"draft": False, "state": "open", "base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}}}


def signed_request(client, payload, delivery="delivery-1", valid=True):
    raw = json.dumps(payload).encode()
    signature = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/github", content=raw,
                       headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": delivery,
                                "X-Hub-Signature-256": "sha256=" + (signature if valid else "bad")})


def test_signed_intake_deduplication_and_authenticated_status(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    with TestClient(create_app(store=store, webhook_secret=SECRET, api_token="status-token")) as client:
        assert signed_request(client, event(), valid=False).status_code == 401
        response = signed_request(client, event())
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        assert signed_request(client, event()).json()["status"] == "duplicate"
        assert signed_request(client, event(), delivery="delivery-2").json()["job_id"] == job_id
        assert client.get(f"/jobs/{job_id}").status_code == 401
        status = client.get(f"/jobs/{job_id}", headers={"Authorization": "Bearer status-token"}).json()
        assert status["status"] == "queued"
        assert "payload" not in status
        assert client.get("/healthz").status_code == 200
    claimed = store.claim()
    assert claimed["payload"] == PAYLOAD
    assert store.claim() is None


def test_drafts_ignored_and_malformed_signed_payload_rejected(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    with TestClient(create_app(store=store, webhook_secret=SECRET)) as client:
        draft = event()
        draft["pull_request"]["draft"] = True
        assert signed_request(client, draft).json()["status"] == "ignored"
        malformed = event()
        malformed["repository"]["full_name"] = "https://attacker.invalid/repo"
        assert signed_request(client, malformed).status_code == 400
    assert store.claim() is None


def test_queue_retry_preserves_report_and_expired_lease_recovers(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    job_id, _ = store.enqueue("first", PAYLOAD)
    store.claim()
    store.save_report(job_id, '{"saved":true}')
    with store.connect() as db:
        db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job_id,))
    recovered = store.claim()
    assert recovered["report"] == '{"saved":true}'
    assert recovered["attempts"] == 2
    store.fail(job_id, "HTTPStatusError")
    assert store.claim()["attempts"] == 3
    store.fail(job_id, "HTTPStatusError")
    assert store.get(job_id)["status"] == "failed"
    assert store.claim() is None


def test_publication_is_comment_only_and_reconciles_existing_review():
    report = Report(mode="live", base_sha="a" * 40, head_sha="b" * 40, status="partial",
                    agents=[], findings=[], limitations=["One reviewer failed."])
    published = []

    def respond(request):
        if request.url.path.endswith("/reviews"):
            if request.method == "GET":
                return httpx.Response(200, json=published)
            body = json.loads(request.content)
            assert body["event"] == "COMMENT"
            assert body["commit_id"] == PAYLOAD["head_sha"]
            assert "comments" not in body  # No inline code suggestions.
            published.append({"id": 123, "body": body["body"], "commit_id": body["commit_id"],
                              "user": {"login": "test-app[bot]"}})
            return httpx.Response(200, json={"id": 123})
        return httpx.Response(200, json={"state": "open", "draft": False,
                                        "base": {"sha": PAYLOAD["base_sha"], "repo": {"id": 2}},
                                        "head": {"sha": PAYLOAD["head_sha"]}})

    async def run():
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(respond)) as client:
            github = GitHub(client, "fake", "test-app")
            assert await github.publish(PAYLOAD, "job-1", report) == 123
            assert await github.publish(PAYLOAD, "job-1", report) == 123
    asyncio.run(run())
    assert len(published) == 1
    assert "partial" in comment_body(report, "marker")


def test_newer_commit_prevents_publication():
    def respond(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"state": "open", "draft": False,
                                        "base": {"sha": PAYLOAD["base_sha"], "repo": {"id": 2}},
                                        "head": {"sha": "c" * 40}})

    async def run():
        async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(Superseded):
                await GitHub(client, "fake", "test-app").ensure_current(PAYLOAD)
    asyncio.run(run())


def test_publication_retry_reuses_saved_analysis(tmp_path, monkeypatch):
    from review_agent import worker
    from review_agent.demo import sample_snapshot
    from review_agent.models import ReviewOutput

    store = JobStore(tmp_path / "jobs.sqlite3")
    job_id, _ = store.enqueue("delivery-1", PAYLOAD)
    reviewer_calls = []
    publish_calls = []

    class Reviewer:
        async def review(self, role, snapshot):
            reviewer_calls.append(role)
            return ReviewOutput()

    class FakeGitHub:
        token = "synthetic"

        async def ensure_current(self, payload):
            pass

        async def existing_review(self, payload, marker):
            return None

        async def publish(self, payload, identifier, report):
            publish_calls.append(identifier)
            if len(publish_calls) == 1:
                raise RuntimeError("Simulated GitHub outage")
            return 456

    async def authenticate(*args):
        return FakeGitHub()

    monkeypatch.setattr(worker.GitHub, "authenticate", authenticate)
    monkeypatch.setattr(worker, "ModelReviewer", lambda *args, **kwargs: Reviewer())
    monkeypatch.setattr(worker, "fetch_snapshot", lambda *args: sample_snapshot())

    async def run():
        with pytest.raises(RuntimeError):
            await worker.process_job(store, store.claim())
        assert store.get(job_id)["report"] is not None
        store.fail(job_id, "RuntimeError")
        await worker.process_job(store, store.claim())

    asyncio.run(run())
    assert sorted(reviewer_calls) == ["bugs", "security", "tests"]
    assert len(publish_calls) == 2
    assert store.get(job_id)["status"] == "completed"
    assert store.get(job_id)["review_id"] == 456
