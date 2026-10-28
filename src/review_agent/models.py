from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["bugs", "security", "tests"]
Severity = Literal["critical", "high", "medium", "low"]
ROLES: tuple[Role, ...] = ("bugs", "security", "tests")
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    severity: Severity
    confidence: Literal["high", "medium", "low"]
    explanation: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    evidence: str = Field(min_length=1, description="Exact, nonempty code excerpt from the head file at the cited line range")
    suggested_fix: str = Field(min_length=1)
    suggested_test: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_range(self):
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class ReviewOutput(BaseModel):
    findings: list[Finding] = Field(default_factory=list, max_length=10)
    limitations: list[str] = Field(default_factory=list)


class FileView(BaseModel):
    path: str
    content: str
    diff: str = ""
    changed_lines: list[int] = Field(default_factory=list)


class Snapshot(BaseModel):
    base_sha: str
    head_sha: str
    files: list[FileView]
    tests: list[FileView] = Field(default_factory=list)
    configuration: list[FileView] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class AgentResult(BaseModel):
    role: Role
    status: Literal["completed", "failed"]
    output: ReviewOutput = Field(default_factory=ReviewOutput)
    error: str | None = None


class AcceptedFinding(BaseModel):
    role: Role
    finding: Finding


class Report(BaseModel):
    mode: Literal["live", "demo"]
    base_sha: str
    head_sha: str
    status: Literal["completed", "partial"]
    agents: list[AgentResult]
    findings: list[AcceptedFinding]
    limitations: list[str]
