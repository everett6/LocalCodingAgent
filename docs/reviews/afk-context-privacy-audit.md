# Provider-context privacy audit

Read-only source audit, 2026-10-29 UTC. Synthetic local probes used a newly created, disposable project, an inert `sk-`-shaped fixture value, and a stub model that records message arguments without opening any network connection. No real environment variables, API keys, credentials, user state, or external targets were read. No source behavior was changed in this audit.

## 1. Generic workspace tools do not enforce provider egress privacy

`ToolRegistry.execute_tool` in `tools/base.py` validates schemas, role permissions, leaf scopes and write scopes. Its private `.git`/`.local-coder` path check applies to mutation targets. It does not reject private read targets or filter successful result text before returning it. `ReadFileTool` confines and bounds the regular-file read, but intentionally permits ordinary files anywhere inside the workspace, including `.env` and private engine state. `BaseAgent` then fits the raw `ToolResult.output` into the next tool-role model message.

**Observed:** an ordinary registered `read_file` call on the synthetic `.env` returned its fake credential, and the next stub-model input contained that credential unchanged. `CodeSearchTool` excluded private paths, but an ordinary indexed `config.py` containing the same fake credential exposed it in a matching source snippet. Optional memory ingestion redacts/rejects some private data, and archive creation redacts known formats, but both happen separately from the already populated active provider message. They do not protect this boundary.

The scope checks are working as currently written; this is not a workspace-escape finding. The policy gap is that an agent permitted to read a project can also place its known credential-like contents in provider context. Hosted transmission was not performed; the message handoff was verified locally. Restricting all workspace reads would affect legitimate coding/security workflows, so a host-controlled egress policy and safe credential inspection should be designed explicitly. Do not describe generic read permission, private local storage, or archive redaction as proof that provider prompts are secret-free.

Recorded rule: `context/unfiltered-tool-egress`.

## 2. Generic reads bypass dedicated context-archive restrictions

`RecoverContextTool` checks registered archive ownership by agent role, validates content-addressed integrity and removes `reasoning_content`/`reasoning_details` before returning recovered text. Those controls are useful within that tool. The physical archive remains an ordinary file under private engine state; it retains protocol fields for recovery, and generic `read_file` has no equivalent archive policy.

**Observed:** the CODER fixture agent created an archive containing an opaque synthetic protocol block. EXPLORER recovery through `recover_context` was denied. The same EXPLORER role successfully read the archive using registered `read_file` and received the opaque block as tool text. No provider reasoning or real secrets were accessed.

This is an actual alternate path around the dedicated tool's filtering/role contract, not a forged caller identity: model-supplied `_caller_role` fields remain rejected at the registry gateway. Align private state reads with the controlled recovery API, or explicitly document/gate the broader capability. Do not silently remove recoverable archive records.

Recorded rule: `context/archive-read-policy-bypass`.

## 3. Search fallback changes the hidden-file boundary

The default ripgrep invocation normally ignores hidden files; fallback `grep -rnE` recursively reads them. Neither branch defines a common private-source policy. Explicit read/search paths and file globs are also separate from automatic index eligibility.

**Observed:** forcing only the local `rg` availability check to unavailable caused registered EXPLORER `grep` with its default project path to return the fake credential line from `.env`. This is an automatic search behavior change based on an installed dependency, unlike an explicitly requested private-file read.

Use one host-defined selection policy for both backends rather than relying on ripgrep defaults. Keep secret scanners' redacted detection outputs and intended local configuration analysis available. Recorded rule: `context/grep-fallback-private-files`.

## Subprocess boundary and positive controls

- `RunCommandTool`'s normal host subprocess and the `process_options` helper do not pass a sanitized `env`; by Python subprocess semantics, the command inherits the process environment. The inspected code therefore does not establish a credential-free child environment. This was source tracing only: no real environment values were enumerated, printed or collected. Generated-tool sandbox execution has a separate explicit minimal environment and must not be generalized to normal shell/test tools.
- Shell approval/risk classification and filesystem write scopes do not enforce output confidentiality. Process-tree cleanup and output byte caps solve lifecycle/resource problems, not secret egress.
- Automatic code indexing and the updated context-memory/explicit-attachment paths reject common private sources and use bounded snapshots. Source hashes remain tied to actual bytes. These are useful boundaries even though generic tools have broader permissions.
- `ModelConfig.api_key` is excluded from ordinary model serialization. MCP results have credential-form filtering. Context archives are redacted before persistence, and controlled recovery strips protocol fields. These controls do not filter every raw generic tool response.

## Evidence and limits

The offline probe is retained in the ignored scratchpad at `.local-coder/tmp/context_privacy_probe.py`. Its five assertions observed: private fixture text in the next stub-model message; fake credential text in ordinary code-search snippets; hidden private text in grep fallback; denied foreign-role controlled archive recovery; successful generic archive read of the synthetic protocol block.

Findings are recorded in the private review ledger. This audit did not test a real cloud provider, arbitrary secret recognition, every subprocess tool, or OS-level isolation. Source fixes require a consistent product policy; no new approval workflow or blanket scanner restriction was introduced here.
