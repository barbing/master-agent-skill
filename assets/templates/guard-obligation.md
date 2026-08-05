# Guard Obligation

## Root Authorization

- Source kind: current-user-request | current-goal | user-approved-plan
- Source ref:
- Grant id:
- Objective:
- Approved production owners:
- Approved production file scopes:
- Approved material behavior domains: none
- Explicit exclusions:

## Observation And Mutation

- Observation outside owner allowed: yes
- Production mutation requires root grant: yes
- External mutation domain status: external_mutation_domain_identified
- Authority violation status: authority_required

## Guard Activation

- Guard activation required: yes
- Activation source: current-user-request | current-goal | user-approved-plan
- Explicit autonomous loop requested: yes
- Guard state mutation allowed: yes
- Unrelated bounded requests passivate predecessor: yes
- Missing activation status: loop_guard_not_required
- Same manifest status: already_armed
- Distinct active-loop conflict status: active_guard_exists

## Obligation Contract

- Schema version: 6
- Obligation id:
- Original target error:
- Acceptance metric:
- Completion maturity: diagnostic
- Required gate ids:
- Contract docs:
- Required gates source: current-user-request | current-goal | user-approved-plan
- Broad extra revalidation allowed: no

## Loop Budget

- Maximum implementation attempts:
- Maximum reassessments:
- Maximum recovery transitions:
- Budgets reset by reassessment: no

## Loop Type And Progress

- Loop type: implementation | validation
- Git-visible progress scope:
- Ignored paths are progress: no
- Validation-only closeout allowed: no

## Structured Validation

- Validation uses argv: yes
- Expected write roots declared: yes
- Native receipts update gates: yes
- Shell string allowed: no

## Validation Support

- Validation support roots:
- Assertion policy: preserve | strengthen
- Exact support files:
- Production frozen during support: yes

## Visual Gate Boundary

- Visual review external: yes
- Receipt requires contract id: yes
- Receipt requires candidate fingerprint: yes
- Receipt requires coverage: yes
- Evidence index opaque: yes

## Status Semantics

- Authorization invalid status: authorization_invalid
- Manifest correction status: manifest_correction_required
- In-root transition status: in_root_transition_required
- External mutation domain status: external_mutation_domain_identified
- Infrastructure retry status: infrastructure_retry_ready
- Authority required status: authority_required

## Manifest Correction Policy

- Correctable defect status: manifest_correction_required
- Authorization reference defect status: authorization_invalid
- Active state changed by correction refusal: no
- Correction may widen contract: no
- Same refusal stop required: yes

## Infrastructure Retry

- Infrastructure retry status: infrastructure_retry_ready
- Maximum infrastructure retries: 1
- Production frozen during retry: yes
- Requires unchanged manifest commands gates authority: yes
- Requires unchanged production fingerprints: yes
