# Independent AFK reliability review

Reviewed 2026-10-29 UTC against the AFK changes from `2c74f0f`, with committed startup baseline `f63b1c3` and concurrent parent integration edits. Scope: provider completion/usage parsing, shared budgets and telemetry, persistent sessions, host task deadlines, plan aggregation and the BaseAgent truncation boundary. This was a read-only source review; only this report was written.

## Findings delivered to the parent

### P1: validate bounded usage before settling financial observations

The reviewed OpenAI parser accepted any nonnegative Python integer when the reported total matched. A synthetic HTTP response with `prompt_tokens=10**400`, `completion_tokens=1` and a matching total passed that check. During `BudgetLedger.settle`, conversion through float pricing raised `OverflowError: int too large to convert to float` after the entry had already changed. Observed result: memory said `completed`, retained 401-digit token counts and `cost_known=True`, while the persisted ledger still said `reserved` with the original conservative count. The model result was lost to an accounting exception.

The same huge number independently raised in `ContextTelemetry._number`; a custom backend without a ledger could therefore still fail an otherwise successful request through best-effort diagnostics. A second synthetic case used bounded token counts (`10**12`) and finite pricing (`1e300`): the computed telemetry cost became Infinity, was serialized, and caused `_load` to reject the entire diagnostic ring. Two saved rows became zero visible rows; a later request could replace that history.

Recommended regressions cover provider and custom-backend counts, conservative reservation retention, finite computed cost, valid JSON, and preservation of earlier telemetry rows. The parent is implementing shared usage bounds and safe numeric/cost handling, including `allow_nan=False`; post-fix integration verification belongs to the parent.

### P2: omitted phase events must not override newer checkpoint progress

The new event append cap correctly disclosed omitted observations, but `Session.progress` still let an older retained phase event replace the checkpoint's newer phase. The offline reproduction recorded “Exploring repository”, capped the event file at its current size, saved an `execute` checkpoint, and attempted a later phase event that was omitted. Progress reported `current_phase="Exploring repository"`, `completed_phases=["execute"]` and `event_history_truncated=True`, while the saved turn phase was `execute`.

This affects long-running task tracking rather than execution replay. The parent has separated the observed phase from authoritative saved progress; the cap-plus-newer-checkpoint case needs to remain covered in integration tests.

## Checks and remaining limits

The reviewed snapshot passed **95 focused Python tests** across provider completion integrity, generic truncation, persistent session boundaries, host deadlines and plan result aggregation. Additional reproductions above used only `httpx.MockTransport`, temporary project fixtures and synthetic counters/prices; no provider request, paid model call, installed app or user profile was used.

No additional concrete introduced critical cancellation or session-schema regression was established in this bounded review. This is not a claim of a complete codebase audit, live provider compatibility, financial reconciliation or universal checkpoint rollback. Parent edits continued during review, so final regression results and the full suite must be recorded at the final integration revision.
