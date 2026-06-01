# Runtime Supervisor

## Operating Mode

- Mode: one-cycle | continuous
- State file: state/runtime.json
- Status file: runtime-status.md

## Poll Cadence

- Poll seconds:
- Maximum cycles:
- Run until stopped: no

## Critical Checks

- Validate state pack structure and semantic state.
- Check stale heartbeats.
- Check token budget pressure.
- Check accepted strategy sync.
- Audit monitored agents for drift, loop, evidence, scope, and token anomalies.

## Recovery Policy

- Remediate only inside the safety envelope.
- Stop an agent after the same remediation repeats three times.
- Stop affected agents immediately on critical safety breach.
- Preserve packets and event log entries for every intervention.

## Quiet Periods

- Defer noncritical remediation during configured quiet periods.
- Critical safety breaches are never deferred.

## Operator Handoff

- Summarize active plan, active agents, handled anomalies, deferred actions, stopped agents, and next automatic action.

## Stop Conditions

- Stop requested in state/runtime.json.
- No active agents or active work orders.
- Safety envelope blocks required recovery.
- Repeated remediation limit reached.
