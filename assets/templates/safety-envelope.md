# Safety Envelope

## Autonomous Authority

- read-state
- validate-state
- update-ledger
- record-event
- create-context-packet
- create-work-order
- monitor-heartbeats
- monitor-budget
- recommend-token-strategy
- governance-lint
- record-governance-status
- record-authority-required
- record-acceptance-gate
- round-log-status
- record-round-log-evidence
- require-round-log-evidence
- round-log-export
- repair-log-status
- repair-log-init
- record-task
- open-repair-cycle
- record-repair-attempt
- require-current-repair-row

## Requires Human Decision

- change-production-behavior
- change-default-behavior
- change-validation-gate
- increase-hard-budget
- approve-policy-conflict
- activate-unreviewed-role

## Forbidden Autonomous Actions

- edit-production-code
- bypass-validation
- suppress-errors
- continue-hard-token-limit
- register-inactive-role
- overwrite-user-work
- round-log-restore
- use-repair-log-as-root-authority
- continue-from-blocked-repair-row

## Budget And Role Limits

- Warning budget impact:
- Hard budget impact:
- Maximum active agents:
- Maximum parallel agents:
- Custom role activation requires accepted proposal: yes

## Remediation Permissions

- reinforce-context: allowed
- stop-agent: allowed
- spawn-successor: allowed-with-review
- split-task: allowed-with-review

## Escalation Triggers

- Safety status is unknown.
- Action is outside autonomous authority.
- Action is forbidden.
- Role is undefined or inactive.
- Budget impact exceeds the hard threshold.
- User direction conflicts with authority docs.
