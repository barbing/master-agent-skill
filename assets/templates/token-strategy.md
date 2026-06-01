# Token Strategy

## Optimization Objective

- Prevent runaway session creation.
- Keep sub-agents inside explicit budgets.
- Preserve only the context needed for the next decision.
- Require sub-agents to reduce their own context before asking for more budget.

## Master Constraints

- Project token budget:
- Assignment token budget:
- Maximum sub-agent count:
- Maximum heartbeats per sub-agent:
- Warning threshold:
- Hard-stop threshold:
- Required usage source: measured | estimated | self-reported
- Required usage confidence: low | medium | high
- Allowed context tier:
- Allowed evidence paths:
- Required compression command or packet:

## Sub-Agent Autonomous Strategies

- Carry forward accepted packets and artifact paths instead of raw conversation.
- Summarize large tool outputs before reusing them.
- Prefer targeted file reads/searches over broad context loading.
- Stop and request compression or budget review before large loops.
- Report token usage in heartbeats and receipts.
- Label token usage as measured, estimated, or self-reported with confidence.
- Ask for a higher context tier only with exact missing evidence and expected token cost.
- Stop before repeating a failed approach that would consume another large budget block.

## Context Tiers

| Tier | Allowed Context | Use When |
| --- | --- | --- |
| Minimal | Current packet, ledger excerpt, exact authority section | Narrow coding or review task |
| Focused | Minimal plus directly cited files/artifacts | Diagnosis or implementation with evidence |
| Expanded | Focused plus one compact prior summary | Strategy or policy work with history dependency |

Use the lowest tier that can answer the assigned question. Do not promote a task to Expanded just because raw conversation history is convenient.

## Compression Triggers

- Warning threshold reached.
- Heartbeat cap nearly reached.
- Tool output is large but only a small part is needed.
- Sub-agent repeats a failed approach.
- Context begins relying on raw conversation instead of accepted packets.
- Expected next step consumes a material share of remaining project or agent budget.
- Usage is estimated, self-reported, missing, or low confidence before a large continuation.

## Escalation Rules

- At warning threshold: compress, narrow scope, or split work.
- At hard threshold: stop or request explicit budget approval.
- If token usage is unknown: require a usage report before continuing.
- If a sub-agent cannot reduce context safely: return to Master for strategy review.

## Research Boundary

- Use external projects and papers as principle sources only.
- Do not copy external code, prompts, templates, or config without explicit licensing review.
- Record any adopted principle as a project-neutral rule, not as a dependency on one framework.
