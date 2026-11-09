"""Strict evidence-qualified executive summary; no execution authority."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Claim(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['observed', 'supported_hypothesis', 'insufficient_evidence', 'not_applicable', 'refuted']
    statement: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    counterevidence_ids: list[str] = Field(default_factory=list, max_length=12)
    unknowns: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode='after')
    def qualified_claim(self):
        if not self.statement.strip():
            raise ValueError('Explain the observation or limitation')
        if self.status in {'observed', 'supported_hypothesis', 'refuted'} and not (self.evidence_ids or self.counterevidence_ids):
            raise ValueError('Evidence-qualified claims require a citation')
        return self


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['readonly_check', 'manual_change', 'observe']
    description: str = Field(min_length=1, max_length=300)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    risk: Literal['low', 'medium', 'high', 'unknown']
    requires_human_approval: bool = Field(strict=True)
    prerequisites: list[str] = Field(default_factory=list, max_length=3)
    verification: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode='after')
    def deliberate_change(self):
        if not self.description.strip():
            raise ValueError('An action needs a description')
        if self.kind == 'manual_change' and (
            not self.requires_human_approval
            or not any(item.strip() for item in self.prerequisites)
            or not any(item.strip() for item in self.verification)
        ):
            raise ValueError('Manual change requires human approval, prerequisites and verification')
        return self


class ExecutiveSummary(BaseModel):
    model_config = ConfigDict(extra='forbid')
    headline: str = Field(min_length=1, max_length=100)
    phenomenon: Claim
    surface_causes: list[Claim] = Field(max_length=3)
    joint_root_cause: Claim
    conclusion: Claim
    recommended_actions: list[Action] = Field(max_length=4)

    def citation_ids(self):
        claims = [self.phenomenon, *self.surface_causes, self.joint_root_cause, self.conclusion]
        return {ref for claim in claims for ref in [*claim.evidence_ids, *claim.counterevidence_ids]} | {
            ref for action in self.recommended_actions for ref in action.evidence_ids
        }
