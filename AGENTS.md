# Agent guidelines

This file is loaded into every task's context. Follow it when working in this repository.

## Editing files

- `edit_file` — replace an exact snippet: pass `old_string` (copied verbatim from the file,
  including indentation) and `new_string`. Set `replace_all` to change every occurrence.
  Prefer this for changes to existing files. If it reports the snippet is not unique or
  differs only in whitespace, re-read the named lines and copy them exactly.
- `write_file` — create a new file or fully rewrite one.
- `apply_patch` — apply a unified diff; it reports the files it changed.

Read a file before editing it, and keep each edit small.

## Checking your work

- `run_tests` — run the project's tests. Do this after code changes.
- `lint` — report problems without changing files (ruff/flake8, eslint, go vet, clippy).
- `format_code` — format in place and report the files it rewrote (ruff/black, prettier,
  gofmt, cargo fmt).
- `build` — run the project's build.

Neither `lint` nor `format_code` runs an arbitrary command: you choose a known tool and
optional paths, nothing else.

## Security work (authorized review of THIS repository only)

These tools analyze the local project the agent is pointed at. They never scan, probe,
or target external hosts or networks.

- `security_scan` — find committed secrets (values are redacted), plus bandit and semgrep
  when installed. Reports `path:line` findings.
- `local-coder security` (agent role `security`) — blue-team review: find vulnerabilities in
  this project's code and propose fixes. Read-only.
- `local-coder validate-finding` (agent role `exploit_validator`) — red-team companion: take a
  vulnerability the security review already found in this project's own code and write a
  proof-of-concept reproduction, run as a local test, so the fix can be verified. It operates
  only on this repository's code and only on already-identified findings.

Stay inside these bounds. Do not attempt anything that targets systems outside this
repository, automates attacks against third parties, causes denial of service, evades
detection, establishes persistence, or harvests credentials. If a task seems to require any
of that, stop and report it instead of doing it.

## Conventions

- Match the style, naming, and type hints of the surrounding code.
- Filesystem paths are resolved inside the project workspace; paths that escape it are rejected.
- Risky shell commands, git commits, and git checkouts pause for approval unless `--yolo` is set.
