# Implementation Guard Adapter

## Purpose

- Map Master Agent governance packets to an optional implementation-loop guard without making that guard a hard dependency.

## Optional Hook

- Hook name:
- Enabled: yes | no
- Reason:

## Guard Activation Mapping

- Guard activation required: yes | no
- Activation source: current-user-request | current-goal | user-approved-plan | none
- Explicit autonomous loop requested: yes | no
- Guard state mutation allowed: yes | no
- Missing activation result: loop_guard_not_required
- Same manifest result: already_armed
- Distinct active-loop conflict result: active_guard_exists

## Hook Ownership Mapping

- Interaction boundary source: UserPromptSubmit
- PostToolUse is conversation boundary: no
- Missing or foreign task identity is passive: yes
- Unrelated bounded requests passivate predecessor: yes
- Compact probe before full policy: yes

## Root Authorization Mapping

- Source kind:
- Grant id:
- Approved file scopes:
- Approved owners:
- Explicit exclusions:

## Observation And Mutation Mapping

- Root governs production mutation: yes
- Cross-module observation allowed: yes
- External mutation domain status: external_mutation_domain_identified
- Authority violation status: authority_required

## Schema V6 Obligation Mapping

- Schema version: 6
- Obligation id:
- Loop type: implementation | validation
- Required gate ids:
- Required gates source:
- Broad extra revalidation allowed: no
- Git-visible progress scope:
- Validation support roots:

## Structured Validation Mapping

- Validation steps use argv: yes
- Expected write roots declared: yes
- Native receipts update gates: yes
- Shell string allowed: no

## Validation Support Mapping

- Support lane status: validation_support_required
- Assertion policy: preserve | strengthen
- Exact support files:
- Production frozen during support: yes

## Visual Gate Mapping

- Visual review external: yes
- Receipt fields: review_contract_id, candidate_fingerprint, coverage, verdict, evidence_index, reviewed_at
- Opaque evidence index: yes

## Acceptance Gate Mapping

- Scope id:
- Required maturity:
- Lower gates required: yes
- Evidence artifact:

## Authority Required Mapping

- Status value: authority_required
- Authorization invalid status: authorization_invalid
- Manifest correction status: manifest_correction_required
- In-root transition status: in_root_transition_required
- External mutation domain status: external_mutation_domain_identified
- Infrastructure retry status: infrastructure_retry_ready
- Event log: state/governance-events.jsonl
- Required packet: obstacle-recovery-packet.md

## Manifest Correction Mapping

- Correctable defect status: manifest_correction_required
- Authorization reference defect status: authorization_invalid
- Active state changed by correction refusal: no
- Correction may widen contract: no
- Same refusal stop required: yes

## Infrastructure Retry Mapping

- Infrastructure retry status: infrastructure_retry_ready
- Maximum infrastructure retries: 1
- Production frozen during retry: yes
- Requires unchanged manifest commands gates authority: yes
- Requires unchanged production fingerprints: yes

## Non-Goals

- Do not infer autonomous loop authority from this adapter.
- Do not edit production code as a learning or governance update.
- Do not promote diagnostic evidence to production acceptance.
- Do not create guard manifests, budgets, successors, or guard-owned gates for ordinary bounded tasks.
