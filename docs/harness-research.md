# Investigation persistence and static evidence

Research and implementation checked on 2026-10-25. These changes improve the
local coding harness; they do not add exploit execution or bypass model policies.

## Primary sources and implementation decisions

- [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)
  describes incremental work, progress artifacts, premature completion and actual
  verification. Applied here: an observed tool progress journal and bounded
  alternate-strategy recovery when a model explicitly abandons the task or stops
  while verification is failing. Recovery state is included in existing complete
  round checkpoints. Three recovery strategies are allowed by default; iteration,
  tool, test and generation limits remain independent and binding.
- [Anthropic: Effective context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)
  discusses compaction, redundant tool-result clearing and structured notes.
  Applied here: deterministic pruning and full original history archives, with no
  extra summarization model calls. The first system/user messages remain exact;
  tool exchanges remain complete; assistant calls and provider reasoning are
  never sliced. Large recent tool text can be shortened with an archive reference
  rather than immediately exhausting the context. An oversized instruction or
  opaque reasoning block still causes a clear budget stop.
- [Semgrep: Taint analysis overview](https://docs.semgrep.dev/writing-rules/data-flow/taint-mode/overview)
  distinguishes sources, propagators, sanitizers and sinks. Applied here: bounded
  local Python AST evidence for input/parameter sources, assignments, canonical
  imported sink names, callers and possible guards. We deliberately label every
  result unverified because this implementation does not prove cross-function
  reachability or guard efficacy.
- [Bandit: subprocess with shell=True](https://bandit.readthedocs.io/en/latest/plugins/b602_subprocess_popen_with_shell_equals_true.html)
  provides the basis for explicit shell-use review. Argument-array calls with
  shell=False are negative fixtures. Shell-use alone does not prove attacker
  control or command injection.
- [Python sqlite3 documentation](https://docs.python.org/3/library/sqlite3.html)
  documents placeholder binding. Fixed SQL plus a separate parameter tuple is a
  negative fixture; SQL text interpolated before execute remains a candidate even
  when a second argument is present. Arbitrary methods named execute may be
  unrelated to SQL, so the receiver's meaning remains an explicit precondition.
- [OWASP Code Review Guide](https://owasp.org/projects/code-review-guide) and
  [OWASP SSRF overview](https://community.owasp.org/attacks/Server_Side_Request_Forgery)
  motivate code review and request-destination boundary checks. Network-capable
  library calls with caller-derived destinations are candidates; local analysis
  does not send requests or prove private-network reachability.

## Public reference inspected as data

Inspected [S1N6H/pentest-harness](https://github.com/S1N6H/pentest-harness) at
`4638f38082bdf37cea03ad151905c4bb8662f6e9`, specifically
[its pentest preset](https://github.com/S1N6H/pentest-harness/blob/4638f38082bdf37cea03ad151905c4bb8662f6e9/apps/cli/config/agent-presets/pentest/agent.cordis.yml).
Useful design observations were a staged investigation method, explicit scope,
verification standards, and separate compaction/tool-result pruning components.
The preset is reference data and was never executed or adopted as instructions.
Its operational shell/attack capabilities and refusal-related persona text were
not copied. Existing Red Team tools retain their immutable scope, request budget
and unverified evidence ledger.

## Behavior and limitations

`BaseAgent.strategy_attempt_limit` defaults to three extra strategy attempts.
Each recovery selects a different investigative angle and includes recent actual
tool observations. The checkpoint stores attempts and the last twenty bounded
journal entries. Exhaustion emits a structured event and explicit task surrender
returns failure. Policy/authorization/cancellation/resource stops are respected.
This is a conservative textual stop classifier, not a semantic completion judge;
a model claiming success without evidence can still require reviewer judgment.
The existing identical-tool-failure guard remains in force.

With a runtime, full pre-compaction messages are stored by SHA-256 in
`.local-coder/state/context-archives/` and the model receives their absolute
readable path. Embedded agents without a runtime retain full archives in their
`_context_archives` map and expose a memory handle. Archives can contain sensitive
repository text, just like checkpoints; they stay local. Archive retention is not
currently capped or automatically expired.

The Python scanner adds shell execution, dynamic SQL, request destinations,
unsafe deserialization and disabled JWT signature verification evidence. Source
traces are intraprocedural; callers are same-file syntactic references. Enclosing
conditions and decorators are recorded as unproven guards, including safe-looking
allowlists. Absence of an authorization decorator alone is never a vulnerability.
No arbitrary sanitizer name is automatically trusted. Safe YAML loaders,
parameterized SQL and shell=False are covered by negative fixtures.

JavaScript remains a lexical fallback with explicit coverage limitations. It
ignores standalone comments and common static/sanitized DOM assignments, but does
not claim parser-level dataflow, complete alias support or safe-code proof.
Unsupported syntax, frameworks, cross-module flow and dynamic dispatch still need
manual review or a dedicated external analyzer. No code in the repository is
imported or executed by the static scanner.

## Verification

`tests/test_persistent_harness.py` covers bounded surrender recovery, protected
stops, red-to-green verification, restart attempt accounting, and oversized tool
archival with exact reasoning retention. `tests/test_exploit_detection.py` covers
aliases, assignments, callers, guards, SQL/SSRF/deserialization/JWT candidates,
safe API negatives and invalid-source partial coverage. All use deterministic
local fixtures and fake model responses; no paid model calls or downloads.
