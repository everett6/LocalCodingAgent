# Local Coding Agent

Local Coding Agent is a Python coding agent for local model backends. It combines repository context, role-specific tools, and a bounded model/tool loop that can inspect files, apply edits, run tests, and feed results back into the model.

## Requirements

- Python 3.12+
- A local OpenAI-compatible, Ollama, or llama.cpp model server
- `pip install -e ".[dev]"` for development dependencies

## Run

Configure models in [config/config.yaml](config/config.yaml), then run:

```bash
local-coder "Fix the failing tests"
local-coder plan "Add authentication"
local-coder test
local-coder review
```

The default configuration expects an OpenAI-compatible server at `http://localhost:8090/v1`. Change the endpoint and model ID to match your local server.

## Safety

All filesystem paths are resolved relative to the selected project workspace. Parent traversal and symlink escapes are rejected. Shell commands are classified as safe, approval-required, or blocked; tests and read-only inspection commands are safe by default, while package installation, Git mutation, privilege escalation, and destructive commands are restricted.

## Architecture

- `src/local_coder/agents/`: role-specific agents and the bounded tool loop
- `src/local_coder/tools/`: filesystem, search, shell, Git, and test tools
- `src/local_coder/context/`: repository discovery and task context
- `src/local_coder/models/`: local model backend adapters
- `src/local_coder/orchestrator/`: planning, execution, review, and test/fix coordination

Run the tests with:

```bash
PYTHONPATH=src pytest -q
```