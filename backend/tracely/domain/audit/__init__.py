"""Audits — Petri's auditor/target/judge loop and Bloom's behaviour → seeds idea, rebuilt on
Tracely's own machinery. Start here.

An **audit** names a behaviour to look for, a target (a model plus a system prompt and tools),
and how to judge it. A **seed** is one instruction to the attacker. A **run** drives every seed
(× repetitions); each **rollout** is one attacker ⇄ target conversation, judged by a matrix of
(dimension × model × sample) cells and reduced to one verdict.

Everything in this package is pure — no model call, no I/O:

| module | holds |
|---|---|
| `config` | the audit's validated configuration; which dimensions a run judges |
| `tools` | the attacker's tool schemas (Petri's set) |
| `prompts` | the attacker's instructions |
| `state` | one rollout's state and the reducer that advances it (a tree, for rollback) |
| `transcript` | the rollout as judges read it — every branch |
| `judge` | cells, panel aggregation, the rollout verdict, score rows |
| `dimensions` | built-in dimensions and behaviour presets |
| `metrics` | a run's elicitation rate and per-dimension numbers |
| `emit` | the surviving conversation as internal OTLP spans |

`services/audit_service.py` does the I/O: it advances rollouts in short leased slices
(`tracely.audit_tick`), calling the models through `provider.run_chat`.

Invariants (also in CLAUDE.md): audit spans are `internal_kind="audit"` with no agent; emission
is a pure function of the checkpoint; scores exist only for valid, complete, graded rollouts and
validity dimensions never fail anything; nothing graded is NO_COVERAGE, never a 0% rate.

Credits: the loop, the attacker's toolset and the dimension names follow Petri
(github.com/meridianlabs-ai/inspect_petri); behaviour presence scoring and the elicitation rate
follow Bloom (github.com/safety-research/bloom). Both MIT.
"""
