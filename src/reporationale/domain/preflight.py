"""Preflight result contract for repository onboarding (see architecture.md, §1).

This module defines only the platform-independent outcome contract. The
workflow that produces a `PreflightResult` for a user-supplied reference
(parsing the reference, looking up the repository, and mapping the outcome)
lives in `reporationale.application.preflight`, since it depends on the
GitHub adapter and must not be imported from here.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reporationale.domain.repository_identity import RepositoryIdentity

PreflightStatus = Literal["ready", "indexing_required", "unsupported"]


class PreflightResult(BaseModel):
    """The outcome of preflight for one user-supplied repository reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: PreflightStatus
    repository: RepositoryIdentity | None = None
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    message: str | None = None

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str | None) -> str | None:
        """Reject a whitespace-only message; `None` is handled separately."""
        if value is not None and not value.strip():
            raise ValueError("message must not be empty or whitespace-only")
        return value

    @model_validator(mode="after")
    def validate_status_contract(self) -> "PreflightResult":
        """Keep each outcome's required and forbidden fields consistent."""
        if self.status == "unsupported":
            if not self.reason_code or not self.message:
                raise ValueError(
                    "an unsupported result requires a reason_code and message"
                )
        else:
            if self.repository is None:
                raise ValueError(
                    f"a {self.status} result requires a repository identity"
                )
            if self.reason_code is not None or self.message is not None:
                raise ValueError(
                    f"a {self.status} result must not carry a rejection reason"
                )
        return self
