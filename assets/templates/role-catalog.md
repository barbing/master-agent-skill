# Role Catalog

## Role Governance

- Register agents only with active roles from this catalog.
- Prefer default roles before defining a custom role.
- Define custom roles only when existing roles cannot cover a recurring or specialized project responsibility.
- Keep every role bounded by allowed work, forbidden work, return packet, scope, token budget, heartbeat cap, activation approval, deactivation condition, and activation status.

## Active Roles

| Role | Type | Purpose | Return Packet | Role Skill | Token Budget | Heartbeat Cap |
| --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |

## Inactive Or Proposed Roles

| Role | Status | Type | Purpose | Activation Reason |
| --- | --- | --- | --- | --- |
|  |  |  |  |  |

## Role Creation Rules

- A new role must explain why existing active roles are insufficient.
- A new role must define allowed work, forbidden work, return packet, scope, positive token budget, positive heartbeat cap, activation approval, and stop conditions.
- A new role must not become a permanent memory store or broad discussion agent.
- A new role must remain subordinate to the Master Agent and project policy pack.

## Role Activation Rules

- Activate a custom role only after the role proposal is accepted, the Master records an approval basis, or the user explicitly approves it.
- Activation commands must include `--approval` evidence for custom roles.
- Deactivate roles that are stale, overlapping, too broad, or no longer needed.
- Do not register a sub-agent with an undefined, proposed, or inactive role.
- Optional role skills may be scaffolded for reusable roles, but project-local role catalog state remains authoritative.
