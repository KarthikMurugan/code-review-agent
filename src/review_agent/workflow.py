import asyncio
import operator
import logging
import time
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from .models import AcceptedFinding, AgentResult, Report, ROLES, Role, SEVERITY_ORDER, Snapshot
from .reviewers import Reviewer

LOG = logging.getLogger(__name__)


class State(TypedDict):
    snapshot: Snapshot
    results: Annotated[list[AgentResult], operator.add]
    report: Report


def combine(snapshot: Snapshot, results: list[AgentResult], mode: str) -> Report:
    files = {file.path: file for file in snapshot.files}
    findings = []
    limitations = list(snapshot.limitations)
    seen = set()
    invalid = False
    for result in sorted(results, key=lambda r: ROLES.index(r.role)):
        limitations.extend(f"{result.role}: {note}" for note in result.output.limitations)
        if result.status == "failed":
            limitations.append(f"{result.role}: review failed ({result.error})")
        for index, finding in enumerate(result.output.findings):
            file = files.get(finding.path)
            lines = file.content.splitlines() if file else []
            excerpt = "\n".join(lines[finding.start_line - 1:finding.end_line])
            reason = ("unknown_file" if file is None else
                      "out_of_range" if finding.end_line > len(lines) else
                      "empty_excerpt" if not finding.evidence.strip() else
                      "evidence_mismatch" if finding.evidence not in excerpt else None)
            if reason:
                invalid = True
                LOG.warning("finding_rejected role=%s index=%d reason=%s", result.role, index, reason)
                limitations.append(f"Rejected {result.role} finding '{finding.title}': invalid location or excerpt")
                continue
            # Exact duplicates only. Different titles/roles remain for human adjudication.
            key = (result.role, finding.path, finding.start_line, finding.end_line,
                   finding.title.casefold().strip())
            if key not in seen:
                seen.add(key)
                findings.append(AcceptedFinding(role=result.role, finding=finding))
    findings.sort(key=lambda item: (
        SEVERITY_ORDER[item.finding.severity],
        {"high": 0, "medium": 1, "low": 2}[item.finding.confidence],
        item.finding.path, item.finding.start_line, item.role,
    ))
    limitations.append("Locations and excerpts are checked mechanically; claims and severity remain model judgments. "
                       "Only exact duplicates within a specialist are removed; semantic adjudication is not implemented.")
    partial = (invalid or any(r.status == "failed" for r in results) or
               any(note.startswith("Skipped ") for note in snapshot.limitations) or
               any(r.output.limitations for r in results))
    return Report(mode=mode, base_sha=snapshot.base_sha, head_sha=snapshot.head_sha,
                  status="partial" if partial else "completed", agents=results,
                  findings=findings, limitations=limitations)


def build_graph(reviewer: Reviewer, *, timeout: float = 90, mode: str = "live"):
    graph = StateGraph(State)

    def specialist(role: Role):
        async def run(state: State):
            started = time.monotonic()
            deadline = asyncio.timeout(timeout)
            LOG.info("reviewer_started role=%s deadline_seconds=%s", role, timeout)
            try:
                async with deadline:
                    output = await reviewer.review(role, state["snapshot"])
                result = AgentResult(role=role, status="completed", output=output)
            except Exception as exc:
                # Exception text from providers can contain request data; retain only the type.
                result = AgentResult(role=role, status="failed", error=type(exc).__name__,
                                     error_stage="deadline" if deadline.expired() else "provider")
            result.elapsed_seconds = round(time.monotonic() - started, 3)
            LOG.log(logging.WARNING if result.status == "failed" else logging.INFO,
                    "reviewer_finished role=%s status=%s elapsed_seconds=%.3f error=%s stage=%s findings=%d",
                    role, result.status, result.elapsed_seconds, result.error or "none",
                    result.error_stage or "none", len(result.output.findings))
            return {"results": [result]}
        return run

    for role in ROLES:
        graph.add_node(role, specialist(role))
        graph.add_edge(START, role)
    graph.add_node("orchestrator", lambda state: {
        "report": combine(state["snapshot"], state["results"], mode),
    })
    graph.add_edge(list(ROLES), "orchestrator")
    graph.add_edge("orchestrator", END)
    return graph.compile()


async def review_repository(snapshot: Snapshot, reviewer: Reviewer, *, timeout: float = 90,
                            mode: str = "live") -> Report:
    result = await build_graph(reviewer, timeout=timeout, mode=mode).ainvoke({
        "snapshot": snapshot, "results": [],
    })
    return result["report"]
