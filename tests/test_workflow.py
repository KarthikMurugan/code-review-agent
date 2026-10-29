import asyncio

from review_agent.demo import sample_snapshot
from review_agent.models import ReviewOutput, ROLES
from review_agent.reviewers import DemoReviewer
from review_agent.workflow import review_repository


def test_parallel_join_and_sorted_demo():
    class BarrierReviewer(DemoReviewer):
        def __init__(self):
            self.started = set()
            self.ready = asyncio.Event()

        async def review(self, role, snapshot):
            self.started.add(role)
            if self.started == set(ROLES):
                self.ready.set()
            # A serial graph deadlocks here and hits the specialist timeout.
            await self.ready.wait()
            return await super().review(role, snapshot)

    report = asyncio.run(review_repository(sample_snapshot(), BarrierReviewer(), timeout=2, mode="demo"))
    assert len(report.agents) == 3
    assert all(agent.status == "completed" for agent in report.agents)
    assert len(report.findings) == 3
    assert report.findings[0].role == "security"
    assert report.mode == "demo"


def test_failure_and_timeout_do_not_hide_successful_results():
    class BrokenReviewer(DemoReviewer):
        async def review(self, role, snapshot):
            if role == "security":
                raise RuntimeError("Sensitive provider payload should not reach report")
            if role == "tests":
                await asyncio.sleep(10)
            return await super().review(role, snapshot)

    report = asyncio.run(review_repository(sample_snapshot(), BrokenReviewer(), timeout=0.05))
    outcomes = {agent.role: agent for agent in report.agents}
    assert report.status == "partial"
    assert outcomes["security"].error == "RuntimeError"
    assert outcomes["tests"].error == "TimeoutError"
    assert outcomes["tests"].error_stage == "deadline"
    assert outcomes["security"].error_stage == "provider"
    assert outcomes["bugs"].status == "completed"
    assert len(report.findings) == 1
    assert "Sensitive" not in report.model_dump_json()


def test_bad_evidence_is_rejected_and_empty_reviews_are_valid():
    class InvalidReviewer(DemoReviewer):
        async def review(self, role, snapshot):
            if role != "bugs":
                return ReviewOutput()
            result = await super().review(role, snapshot)
            result.findings[0].evidence = "this code is not present"
            return result

    report = asyncio.run(review_repository(sample_snapshot(), InvalidReviewer()))
    assert report.status == "partial"
    assert not report.findings
    assert any("invalid location or excerpt" in note for note in report.limitations)


def test_zero_findings_with_no_limits_is_completed():
    class EmptyReviewer:
        async def review(self, role, snapshot):
            return ReviewOutput()

    report = asyncio.run(review_repository(sample_snapshot(), EmptyReviewer()))
    assert report.status == "completed"
    assert report.findings == []


def test_reviewer_failures_and_validation_rejections_are_logged_without_payload(caplog):
    class BrokenReviewer(DemoReviewer):
        async def review(self, role, snapshot):
            if role == 'security':
                raise TimeoutError('PRIVATE PROVIDER REQUEST')
            result = await super().review(role, snapshot)
            if role == 'tests':
                result.findings[0].evidence = 'PRIVATE GENERATED CODE'
            return result

    with caplog.at_level('INFO'):
        report = asyncio.run(review_repository(sample_snapshot(), BrokenReviewer()))
    assert 'role=security' in caplog.text and 'TimeoutError' in caplog.text
    assert 'evidence_mismatch' in caplog.text
    assert 'PRIVATE' not in caplog.text
    assert all(agent.elapsed_seconds is not None for agent in report.agents)
