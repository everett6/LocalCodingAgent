# Project context memory

`ContextMemory(project_root)` stores bounded evidence chunks and structured task
state in `.local-coder/context/memory.sqlite`. It is independent of the existing
session archive and `MemoryStore`: it gives an agent relevant evidence without
putting the complete conversation back into the model's context window.

The database and its context directory use private filesystem permissions. The
project's state directory ignores itself in Git. Storage paths cannot be supplied
by a model, and storage symlinks or hard-linked database files are rejected.
Each database is bound to its canonical project root; copying one to another
project does not silently make its memories available there.

## Retrieval and embeddings

The default backend, `lexical-hash-v1`, creates normalized 256-dimensional vectors
from identifier words and character trigrams. These are **lexical hash embeddings,
not neural semantic embeddings**. Exact term postings select candidates; vector
similarity reranks those candidates alongside lexical overlap and exact phrase
matches. This handles identifier spelling and compound names without installing
models, accessing the network, or making paid calls. It does not claim to find
arbitrary conceptual synonyms.

An application may explicitly pass an already available local callable as
`embedding_backend=`. The callable must expose a stable `name` string and return
a finite numeric vector of at most 4096 dimensions. It may declare
`is_neural = True` when that description is accurate. The application is
responsible for ensuring its supplied callable stays local; the built-in memory
backend never downloads weights. Supplied backends may contribute vector-only
matches at cosine similarity of at least 0.65. Vectors from different backend
names or dimensions are never compared. Existing rows retain their actual backend
labels until corrected or reindexed.

Search considers at most 1000 lexical candidates and, for a supplied backend,
1000 recent vector candidates. Each result includes a source reference, content
hash, source snapshot hash, task, timestamps, recorded Git HEAD, freshness,
ranking reasons, related memory IDs, and an `untrusted` marker. A task filter
includes that task's evidence and shared project evidence, excluding other tasks.
Graph links record explicit references and related source/task chunks; graph
edges do not implicitly inject additional text into retrieval.

`search` caps the sum of returned content characters to `char_budget`, with at
most 50 results and at most 50000 content characters. `context` additionally
counts the source labels and its untrusted-evidence header, so its entire `text`
field fits the requested budget. `MemorySearchTool.output` uses this bounded
text; structured result data includes provenance for inspection. Very small
budgets may fit no evidence. Structured provenance is separate from the text
budget and should not be appended wholesale to a model prompt.

## Freshness and corrections

Code retrieval verifies the current source bytes against the captured SHA-256
hash and checks the current Git HEAD. Missing/changed files and changed HEADs
exclude old code from search immediately, even before indexing runs again.
Historical findings and long-lived decisions remain retrievable with an explicit
stale-evidence label, so a file edit does not erase the reason for a decision.
The index hashes the exact source bytes used for its content, avoiding a stale
chunk being marked fresh if a file changes while indexing.

Corrections replace content, lexical postings and vectors, and clear incident
graph links in one SQLite transaction. Deletion cascades to all postings,
vectors and graph edges. Retrieval stores no persistent result cache or duplicate
provenance table that can resurrect removed content. The caller's already
received outputs are ordinary historical copies; deleting memory does not edit
those earlier conversation messages.

## Privacy and indexing limits

Redaction happens before chunking, indexing, vector generation and persistence,
including corrections, metadata keys/values and structured task-state updates.
Recognizers remove private-key blocks, common provider token formats, JWTs,
authorization values, credential assignments and credentials inside URLs.
Sensitive metadata field names have their entire value redacted. Recognition is
heuristic: unlabelled arbitrary secret strings cannot reliably be distinguished
from ordinary prose. Do not intentionally ingest credentials.

`index_code` reads only project files, skips symlinks, binary data, common
dependency/build/state directories, `.env` variants, private-key files and common
credential files. Defaults are 500 indexed files per call and 256 KiB per file;
hard limits are 5000 files and 1 MiB. Individual ingestion is capped at 200000
characters and 128 chunks, normally 1800 characters each. Python top-level
functions/classes retain coherent source spans when they fit. Other text uses
bounded line windows. Directory enumeration is capped at five times the requested
file limit; the summary reports files, chunks and skipped files. Indexing is a
bounded local snapshot, not a complete repository parser or a substitute for a
secret scanner.

## Python and tool interfaces

All store methods are synchronous and use a connection per operation, shared
in-process locks per database, SQLite WAL and transactions. Async applications
should call them through `asyncio.to_thread`; the tool wrappers already do so.

```python
from local_coder.memory.context_store import ContextMemory

memory = ContextMemory(project_root)
rows = memory.ingest("decision", "Keep approval checks in the shared registry",
                     task_id="task-42", metadata={"links": []})
memory.index_code(["src"])
items = memory.search("approval registry", limit=8, char_budget=6000,
                      task_id="task-42")
context = memory.context("approval registry", char_budget=4000,
                         task_id="task-42")
memory.task_state("task-42", {
    "objective": "Preserve approval boundaries",
    "constraints": ["No automatic remote authorization"],
    "hypotheses": [], "failed_attempts": [], "evidence": [],
    "unfinished_work": ["Exercise denial path"],
})
memory.update(rows[0]["id"], "Corrected decision text")
memory.get(rows[0]["id"])
memory.delete(rows[0]["id"])
memory.list_memories(limit=50)
memory.status()
```

Supported chunk kinds are `conversation`, `code`, `tool`, `finding`, `decision`,
`evidence`, `hypothesis` and `failure`. Structured task state is stored separately
from embeddings: objective is text; constraints, hypotheses, failed attempts,
evidence and unfinished work are lists of text. Unknown fields and state larger
than 32000 serialized characters are rejected. Updates merge supplied fields and
replace supplied lists atomically.

`MemorySearchTool(memory)`, `MemoryWriteTool(memory)` and `TaskMemoryTool(memory)`
implement `memory_search`, `memory_write` and `task_memory`. Memory write actions
are `ingest` (default), `update`, `delete` and `index_code`; update/delete require
`memory_id`. Role permissions and engagement scope are enforced by the shared
registry, not by trusting retrieved text or model-supplied approval flags.

All retrieved text, including decisions and task evidence, remains untrusted
data. A memory's instruction-like content cannot grant tools, replace safety
policy, or authorize an external action.
