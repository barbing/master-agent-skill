# Master Boundary

## Purpose

- Enforce the Master Agent as a non-implementing control plane.

## Allowed Master Write Paths

- docs/master-agent/**

## Forbidden Master Write Paths

- production source code
- tests
- runtime configuration
- migrations
- packaging or deployment behavior

## Enforcement Policy

- Run `enforce-master-boundary` before accepting Master-led work as complete.
- Fail closed when Git evidence is unavailable.
- Record an incident when changed paths exceed the allowed write paths.

## Escalation

- Production edits require a Coding Agent work order and independent review.
