"""Core types for the local coding agent system."""
from __future__ import annotations
import enum
import uuid
from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field


# === Enums ===

class AgentRole(str, enum.Enum):
    ORCHESTRATOR = "orchestrator"
    EXPLORER = "explorer"
    PLANNER = "planner"
    CODER = "coder"
    DEBUGGER = "debugger"
    TESTER = "tester"
    REVIEWER = "reviewer"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class AgentPhase(str, enum.Enum):
    """Lifecycle phase for one agent task execution."""
    IDLE = "idle"
    DISCOVERING = "discovering"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    REFLECTING = "reflecting"
    DONE = "done"
    FAILED = "failed"


class ModelBackend(str, enum.Enum):
    OLLAMA = "ollama"
    LLAMACPP = "llama.cpp"
    OPENAI_COMPAT = "openai_compatible"  # vLLM, LMStudio, etc.


class ToolName(str, enum.Enum):
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    APPLY_PATCH = "apply_patch"
    LIST_FILES = "list_files"
    SEARCH_FILES = "search_files"
    GREP = "grep"
    GIT_STATUS = "git_status"
    GIT_DIFF = "git_diff"
    GIT_LOG = "git_log"
    GIT_COMMIT = "git_commit"
    GIT_CHECKOUT = "git_checkout"
    RUN_COMMAND = "run_command"
    RUN_TESTS = "run_tests"
    BUILD = "build"
    LINT = "lint"
    FORMAT_CODE = "format_code"


# === Messages ===

class Message(BaseModel):
    """A single message in a conversation."""
    role: str  # "system", "user", "assistant", "tool"
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ToolCall(BaseModel):
    """A tool call made by the model."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Result from executing a tool."""
    tool_call_id: str = ""
    success: bool
    output: str = ""
    error: str | None = None
    exit_code: int | None = None
    duration_ms: float | None = None
    files_changed: list[str] = Field(default_factory=list)


# === Model Interface ===

class ModelResponse(BaseModel):
    """Response from a model."""
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    model: str = ""


class ModelConfig(BaseModel):
    """Configuration for a model."""
    name: str
    backend: ModelBackend = ModelBackend.OLLAMA
    model_id: str  # The model identifier for the backend (e.g., 'qwen2.5-coder:7b')
    base_url: str = "http://localhost:11434"  # Default Ollama URL
    context_length: int = 8192
    temperature: float = 0.2
    max_tokens: int = 4096
    quantization: str | None = None  # e.g., 'Q4_K_M'
    gpu_layers: int | None = None
    estimated_vram_mb: int | None = None


# === Agent Protocol ===

class AgentTask(BaseModel):
    """A task assigned to an agent."""
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    role: AgentRole
    objective: str
    files: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    context: TaskContext = Field(default_factory=lambda: TaskContext())
    success_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    priority: int = 0  # Higher = more important
    max_retries: int = 3
    timeout_seconds: int = 300
    created_at: datetime = Field(default_factory=datetime.now)


class TaskContext(BaseModel):
    """Context provided to an agent for a task."""
    architecture: str = ""
    relevant_symbols: list[str] = Field(default_factory=list)
    previous_findings: list[str] = Field(default_factory=list)
    file_contents: dict[str, str] = Field(default_factory=dict)
    git_history: list[str] = Field(default_factory=list)
    test_results: list[TestResult] | None = None
    error_context: str | None = None
    related_tasks: list[str] = Field(default_factory=list)


class AgentResponse(BaseModel):
    """Response from an agent after completing a task."""
    task_id: str
    status: TaskStatus
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    tests_passed: bool = True
    issues: list[str] = Field(default_factory=list)
    follow_up_required: bool = False
    follow_up_tasks: list[AgentTask] = Field(default_factory=list)
    patches: list[FilePatch] = Field(default_factory=list)
    discoveries: list[str] = Field(default_factory=list)
    metrics: AgentMetrics = Field(default_factory=lambda: AgentMetrics())


class FilePatch(BaseModel):
    """A patch to apply to a file."""
    file_path: str
    original_content: str | None = None
    new_content: str | None = None
    patch_text: str | None = None  # unified diff format
    action: str = "modify"  # create, modify, delete


class TestResult(BaseModel):
    """Result from running tests."""
    test_name: str
    passed: bool
    duration_ms: float = 0.0
    error_message: str | None = None
    stdout: str = ""
    stderr: str = ""


class AgentMetrics(BaseModel):
    """Metrics from agent execution."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    latency_ms: float = 0.0
    files_read: int = 0
    files_written: int = 0


class AgentState(BaseModel):
    """Observable state accumulated during one agent execution."""
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    task_id: str
    objective: str
    phase: AgentPhase = AgentPhase.IDLE
    iteration: int = 0
    max_iterations: int = 15
    messages: list[Message] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    files_read: set[str] = Field(default_factory=set)
    files_changed: set[str] = Field(default_factory=set)
    test_results: list[TestResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None


# === Task DAG ===

class TaskNode(BaseModel):
    """A node in the task dependency graph."""
    task: AgentTask
    status: TaskStatus = TaskStatus.PENDING
    result: AgentResponse | None = None
    retries: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None


class TaskPlan(BaseModel):
    """A plan consisting of multiple tasks with dependencies."""
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    objective: str
    tasks: list[AgentTask] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    notes: list[str] = Field(default_factory=list)


# === Resource Management ===

class GPUStatus(BaseModel):
    """Current GPU status."""
    total_vram_mb: int = 0
    used_vram_mb: int = 0
    free_vram_mb: int = 0
    gpu_utilization_percent: float = 0.0
    temperature_c: int = 0
    loaded_models: list[str] = Field(default_factory=list)


class ResourceConfig(BaseModel):
    """Resource management configuration."""
    max_concurrent_gpu_agents: int = 1
    max_concurrent_cpu_agents: int = 4
    gpu_memory_target_percent: float = 85.0
    model_unload_timeout_seconds: int = 300


# === Configuration ===

class AgentPermissions(BaseModel):
    """Tool permissions for an agent role."""
    role: AgentRole
    allowed_tools: list[ToolName] = Field(default_factory=list)


class ApprovalConfig(BaseModel):
    """What operations require human approval."""
    destructive_shell_commands: bool = True
    dependency_changes: bool = True
    database_migrations: bool = True
    large_file_deletions: bool = True
    git_push: bool = True


class VerificationConfig(BaseModel):
    """Verification loop configuration."""
    max_fix_iterations: int = 3
    run_tests_after_changes: bool = True
    require_review: bool = True


class ProjectConfig(BaseModel):
    """Top-level project configuration."""
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    resources: ResourceConfig = Field(default_factory=ResourceConfig)
    permissions: list[AgentPermissions] = Field(default_factory=list)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    project_root: str = "."
    log_level: str = "INFO"
    log_dir: str = ".local-coder/logs"
    state_dir: str = ".local-coder/state"


# === Events / Observability ===

class AgentEvent(BaseModel):
    """An event emitted by the system for observability."""
    timestamp: datetime = Field(default_factory=datetime.now)
    source: str  # e.g., "ORCHESTRATOR", "CODER-1"
    event_type: str  # e.g., "task_started", "tool_called", "task_completed"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None
