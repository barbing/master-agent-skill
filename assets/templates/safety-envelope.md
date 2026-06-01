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
