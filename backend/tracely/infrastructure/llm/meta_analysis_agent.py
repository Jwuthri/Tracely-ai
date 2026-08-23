"""The LLM half of meta-analysis: turn the per-metric results + precomputed statistics into a
natural-language synthesis (patterns, recommendations, summary, confidence).

Every chat call goes through `provider.run_structured_agent` (LangChain `create_agent` on
OpenRouter) per the architecture rules — no raw HTTP, no bespoke client. The deterministic
correlations/outliers are computed in `domain.analysis.statistics` and merged in by the service;
this agent reasons over them but never invents the numbers.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from tracely.config import settings


class Pattern(BaseModel):
    description: str = Field(description="the cross-metric pattern in one or two sentences")
    correlation_strength: float | None = Field(
        default=None, description="0..1 strength if this pattern is correlation-driven, else null"
    )
    evidence: str = Field(description="the concrete evidence (metrics/values) this rests on")
    affected_metrics: list[str] = Field(default_factory=list)


class Correlation(BaseModel):
    metric_a: str
    metric_b: str
    coefficient: float = Field(description="Spearman coefficient, -1..1")
    p_value: float | None = None
    interpretation: str = Field(description="what this correlation means in plain language")


class Outlier(BaseModel):
    conversation_id: str
    reason: str = Field(description="why this conversation stands out")
    metrics_affected: list[str] = Field(default_factory=list)
    severity: str = Field(default="low", description="one of: low, medium, high")


class MetaAnalysisOutput(BaseModel):
    patterns: list[Pattern] = Field(default_factory=list)
    correlations: list[Correlation] = Field(default_factory=list)
    outliers: list[Outlier] = Field(default_factory=list)
    recommendations: list[str] = Field(
        default_factory=list, description="concrete, actionable next steps"
    )
    summary: str = Field(default="", description="natural-language key findings, 3-6 sentences")
    confidence: float = Field(default=0.0, description="0..1 confidence given the data volume")


_SYSTEM = (
    "You are a senior ML evaluation analyst. You are given the results of several evaluation "
    "metrics, each scored across many conversations of a single AI agent, plus precomputed "
    "statistics (Spearman correlations between metric pairs and z-score outlier conversations). "
    "Your job is META-analysis: what do the metrics say TOGETHER, not what any single score says.\n\n"
    "Produce:\n"
    "- patterns: cross-metric patterns grounded ONLY in the evidence (e.g. 'faithfulness and "
    "tool-success move together; runs that fail tool calls also score low on faithfulness'). "
    "Never invent numbers; cite the metrics involved.\n"
    "- correlations: interpret the strongest precomputed correlations in plain language. Do not "
    "fabricate coefficients — use the ones provided.\n"
    "- outliers: explain the flagged outlier conversations (why they likely stand out).\n"
    "- recommendations: concrete, actionable steps an engineer could take next.\n"
    "- summary: 3-6 sentences of the key findings.\n"
    "- confidence: 0..1, lower when there are few conversations or few metrics.\n\n"
    "Be precise and evidence-bound. If the data is thin, say so and keep confidence low.\n\n"
    "Metrics prefixed `ops.` are operational, not judged: per-turn latency in ms, per-turn "
    "tokens, per-turn errored tool calls, and turns per conversation. Connecting them to the "
    "quality metrics is the most valuable kind of pattern — 'the low-groundedness conversations "
    "are the slow, tool-error-heavy ones' is a finding; restating one metric alone is not. When "
    "mean shifts since the previous analysis are provided, say which metrics moved and whether "
    "the movement is an improvement or a regression."
)


def synthesize(prompt: str) -> MetaAnalysisOutput:
    """One structured `create_agent` call returning the analyst's synthesis. Temperature 0.1 —
    a touch of latitude for prose while keeping findings stable. Raises on transport/validation
    errors (the service catches and falls back to a stats-only result)."""
    from tracely.infrastructure.llm import provider

    return provider.run_structured_agent(
        prompt,
        response_format=MetaAnalysisOutput,
        system_prompt=_SYSTEM,
        model=settings.meta_analysis_model,
        temperature=0.1,
    )
