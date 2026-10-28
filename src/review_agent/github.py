"""GitHub App authentication, read-only fetching, and COMMENT-only publication."""
import os
import subprocess
import time
from pathlib import Path

import httpx
import jwt

from .models import Report


class Superseded(Exception):
    pass


class GitHub:
    def __init__(self, client: httpx.AsyncClient, token: str, slug: str):
        self.client, self.token, self.slug = client, token, slug

    @classmethod
    async def authenticate(cls, client: httpx.AsyncClient, installation_id: int, repository_id: int):
        now = int(time.time())
        private_key = Path(os.environ["GITHUB_PRIVATE_KEY_FILE"]).read_text()
        signed = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": os.environ["GITHUB_APP_ID"]},
                            private_key, algorithm="RS256")
        headers = {"Authorization": f"Bearer {signed}"}
        app = await client.get("/app", headers=headers)
        app.raise_for_status()
        response = await client.post(f"/app/installations/{installation_id}/access_tokens", headers=headers,
                                     json={"repository_ids": [repository_id],
                                           "permissions": {"contents": "read", "pull_requests": "write"}})
        response.raise_for_status()
        return cls(client, response.json()["token"], app.json()["slug"])

    async def request(self, method: str, path: str, **kwargs):
        response = await self.client.request(method, path, headers={"Authorization": f"Bearer {self.token}"}, **kwargs)
        response.raise_for_status()
        return response.json()

    async def ensure_current(self, payload: dict):
        pr = await self.request("GET", f"/repos/{payload['repository']}/pulls/{payload['number']}")
        if (pr["state"] != "open" or pr.get("draft") or
                pr["base"]["repo"]["id"] != payload["repository_id"] or
                pr["base"]["sha"] != payload["base_sha"] or pr["head"]["sha"] != payload["head_sha"]):
            raise Superseded()

    async def existing_review(self, payload: dict, marker: str):
        for page in range(1, 11):
            reviews = await self.request("GET", f"/repos/{payload['repository']}/pulls/{payload['number']}/reviews",
                                         params={"per_page": 100, "page": page})
            for review in reviews:
                if (review.get("user", {}).get("login") == f"{self.slug}[bot]" and
                        marker in (review.get("body") or "") and
                        review.get("commit_id") == payload["head_sha"]):
                    return review["id"]
            if len(reviews) < 100:
                return None
        raise ValueError("Review history exceeds reconciliation limit")

    async def publish(self, payload: dict, job_id: str, report: Report) -> int:
        marker = f"<!-- learning-review-agent:{job_id} -->"
        existing = await self.existing_review(payload, marker)
        if existing:
            return existing
        await self.ensure_current(payload)
        # A PR review is bound to a commit even if another push races this check.
        body = comment_body(report, marker)
        review = await self.request("POST", f"/repos/{payload['repository']}/pulls/{payload['number']}/reviews",
                                    json={"commit_id": payload["head_sha"], "event": "COMMENT", "body": body})
        return review["id"]


def comment_body(report: Report, marker: str) -> str:
    lines = [marker, "## Agent code review", "", f"Reviewed revision: `{report.head_sha}`",
             f"Analysis: **{report.status}**. Static model review; no tests were executed.", ""]
    for agent in sorted(report.agents, key=lambda agent: agent.role):
        lines.append(f"- **{agent.role}**: {agent.status}" + (f" ({agent.error})" if agent.error else ""))
    lines.extend(["", "### Findings", ""])
    for item in report.findings[:10]:
        finding = item.finding
        lines.extend([f"**{finding.severity.upper()} · {item.role}: {finding.title}**",
                      f"`{finding.path}:{finding.start_line}-{finding.end_line}` · confidence: {finding.confidence}",
                      "", finding.explanation, "", f"Trigger: {finding.trigger}", ""])
    if not report.findings:
        lines.append("No findings passed validation. This does not establish that the change is safe.")
    lines.extend(["", "### Limitations", ""])
    lines.extend(f"- {note}" for note in report.limitations)
    body = "\n".join(lines)
    return body if len(body) <= 50_000 else body[:49_800] + "\n\nReport truncated to fit the comment limit."


def fetch_snapshot(repo: Path, payload: dict, token: str):
    """Fetch commits without checkout, submodules, LFS, hooks, or target programs."""
    from .snapshot import capture

    askpass = repo.parent / "askpass.sh"
    askpass.write_text('#!/bin/sh\ncase "$1" in\n*Username*) printf "%s" "x-access-token" ;;\n*) printf "%s" "$REVIEW_GIT_TOKEN" ;;\nesac\n')
    askpass.chmod(0o700)
    env = {**os.environ, "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
           "REVIEW_GIT_TOKEN": token, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

    def git(*args):
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                                env=env, capture_output=True, timeout=120)
        if result.returncode:
            # Never propagate stderr: it may contain auth or request data.
            raise RuntimeError("Git fetch or history operation failed")
        return result.stdout.decode().strip()

    git("init", "--bare", str(repo))
    git("-C", str(repo), "fetch", "--no-tags", "--depth=100",
        f"https://github.com/{payload['repository']}.git",
        f"+{payload['base_sha']}:refs/review/base",
        f"+refs/pull/{payload['number']}/head:refs/review/head")
    head = git("-C", str(repo), "rev-parse", "refs/review/head")
    if head != payload["head_sha"]:
        raise Superseded()
    base = git("-C", str(repo), "merge-base", "refs/review/base", "refs/review/head")
    return capture(repo, base, head)
