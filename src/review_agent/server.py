"""Signed GitHub webhook intake. No model calls inside HTTP requests."""
import hashlib
import hmac
import json
import os
import re
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from .config import load_environment
from .jobs import JobStore

MAX_WEBHOOK_BYTES = 1_000_000


class ReviewEvent(BaseModel):
    installation_id: int = Field(gt=0)
    repository_id: int = Field(gt=0)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    number: int = Field(gt=0)
    base_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    head_sha: str = Field(pattern=r"^[a-f0-9]{40}$")


def create_app(*, store: JobStore | None = None, webhook_secret: str | None = None,
               api_token: str | None = None) -> FastAPI:
    load_environment()
    secret = webhook_secret if webhook_secret is not None else os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    token = api_token if api_token is not None else os.environ.get("REVIEW_API_TOKEN", "")
    if len(secret) < 32:
        raise ValueError("GITHUB_WEBHOOK_SECRET must contain at least 32 characters")
    jobs = store or JobStore(Path(os.environ.get("REVIEW_DB_PATH", "data/reviews.sqlite3")))
    app = FastAPI(title="Code Review Agent", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    def index():
        return {"service": "Code Review Agent", "health": "/healthz", "webhook": "/webhooks/github",
                "message": "API is running. PR reviews also require the configured GitHub worker."}

    @app.get("/healthz")
    def health():
        with jobs.connect() as db:
            db.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/jobs/{job_id}")
    def job_status(job_id: str, authorization: str = Header(default="")):
        if not token or not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "Unauthorized")
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return {key: job[key] for key in ("id", "status", "attempts", "error", "review_id", "created", "updated")}

    @app.post("/webhooks/github", status_code=202)
    async def webhook(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_WEBHOOK_BYTES:
                raise HTTPException(413, "Webhook payload too large")
        expected = "sha256=" + hmac.new(secret.encode(), bytes(body), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("x-hub-signature-256", "").encode(), expected.encode()):
            raise HTTPException(401, "Invalid webhook signature")
        event = request.headers.get("x-github-event", "")
        if event == "ping":
            return {"status": "ok"}
        if event != "pull_request":
            return {"status": "ignored"}
        delivery = request.headers.get("x-github-delivery", "")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", delivery):
            raise HTTPException(400, "Missing or invalid delivery ID")
        try:
            payload = json.loads(body)
            if payload.get("action") not in {"opened", "synchronize", "reopened", "ready_for_review"}:
                return {"status": "ignored"}
            pr = payload["pull_request"]
            if pr.get("draft") or pr["state"] != "open":
                return {"status": "ignored"}
            parsed = ReviewEvent(
                installation_id=payload["installation"]["id"],
                repository_id=payload["repository"]["id"],
                repository=payload["repository"]["full_name"], number=payload["number"],
                base_sha=pr["base"]["sha"], head_sha=pr["head"]["sha"],
            )
        except (KeyError, TypeError, AttributeError, ValueError, ValidationError):
            raise HTTPException(400, "Malformed pull request event") from None
        job_id, created = jobs.enqueue(delivery, parsed.model_dump())
        return {"job_id": job_id, "status": "queued" if created else "duplicate"}

    return app
