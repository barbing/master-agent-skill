# Implementation Guard Adapter

## Purpose

- Map Master Agent governance packets to an optional implementation-loop guard without making that guard a hard dependency.

## Optional Hook

- Hook name:
- Enabled: yes | no
- Reason:

## Root Authorization Mapping

- Source kind:
- Grant id:
- Approved file scopes:
- Approved owners:
- Explicit exclusions:

## Acceptance Gate Mapping

- Scope id:
- Required maturity:
- Lower gates required: yes
- Evidence artifact:

## Authority Required Mapping

- Status value: authority_required
- Event log: state/governance-events.jsonl
- Required packet: obstacle-recovery-packet.md

## Non-Goals

- Do not infer autonomous loop authority from this adapter.
- Do not edit production code as a learning or governance update.
- Do not promote diagnostic evidence to production acceptance.
