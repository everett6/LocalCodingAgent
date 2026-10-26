# Reliability Workflows Implementation Plan

> **For agentic workers:** Use subagent-driven development for independent modules; parent integrates shared dispatch and navigation.

**Goal:** Implement the eight approved improvements with observable results and bounded execution.

**Architecture:** Extend existing Python stores, model budgets, tool registry and authenticated workbench. Separate domain API mixins and React panels keep shared integration owned by the parent.

**Tech Stack:** Existing Python standard library/Pydantic/SQLite and React/TypeScript; optional already-installed tokenizer adapters, no mandatory new dependencies.

**Spec:** docs/superpowers/specs/2026-10-26-reliability-workflows-design.md

## Global Constraints

Workspace containment, secret redaction, local-only security review, bounded inputs/results and existing human approval/cost gates. Preserve primary checkout changes and existing installed sandbox helpers. No automatic model/data downloads, paid inference or destructive repairs.

## Review Focus

Unknown-completion mutations must not retry; changed branches/files must not hydrate stale evidence; exact-count labels must require matching templates; unavailable tools/data must remain explicit; background jobs must release workspace locks on cancellation/error.

### 1. Evaluations
- [x] Add harness/evaluation.py, evaluation_api.py, EvaluationPanel.tsx and focused tests: fixed case IDs, negative controls, quality/cost/timing metrics, stable suite identity and paired improvement evaluator. Test fabricated/stale results rejected and no implicit provider calls.
### 2. Retrieval and memory
- [x] Extend memory/context_store.py and memory inspector with branch/content freshness, bounded audit explanations, retrieval quality tests and deletion/branch-switch regressions.
### 3. Findings workflow
- [x] Add findings_api.py and FindingsWorkbench.tsx around canonical security/evidence.py. Test review/prepare/verify transitions, stale evidence, explicit execution fields, cancellation and lock cleanup.
### 4. Token accounting
- [x] Add optional local context/token_count.py adapters and integrate core/budgets.py/providers/context_policy.py. Count tool schemas and output reserve; label exact/estimated/unavailable. Test mismatched templates, secrets and boundary admission.
### 5. Dependencies
- [x] Extend security_intelligence.py and DependencyReview.tsx with ecosystem range semantics and explicit bounded advisory updates. Test prereleases, withdrawn records, malformed ranges, network restrictions and incomplete coverage.
### 6. Inference tuning
- [x] Add inference_tuning.py, inference_tuning_api.py and local controls. Explicitly benchmark configured runtime/model with bounded candidate settings, cancellation, measurements and safe activation. Test speculative settings require support and a measured improvement.
### 7. Doctor
- [x] Add doctor.py/doctor_api.py and DoctorPanel.tsx. Diagnose runtime/models/ports/sandbox/provider/IDE without secret disclosure; expose bounded explicit repairs and verify outcomes. Test unsupported repairs, stale paths and credential-safe errors.
### 8. Tool scheduling/recovery
- [x] Extend tools/base.py and agents/base.py with declared effects, bounded concurrent reads and serialized mutation barriers. Test ordering, permission checks, cancellation and no replay after unknown mutation completion.
### 9. Integration and delivery
- [ ] Wire mixins, navigation, native route allowlists and docs. Review independent modules, run focused/full Python and Node suites, frontend build/lint, package smoke checks, push PR and update installed apps with rollback.
