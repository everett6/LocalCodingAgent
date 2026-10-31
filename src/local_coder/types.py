"""Core types for the local coding agent system."""
from __future__ import annotations
import enum
import uuid
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field, SecretStr


# === Enums ===

class AgentRole(str, enum.Enum):
    ORCHESTRATOR = "orchestrator"
    EXPLORER = "explorer"
    PLANNER = "planner"
    CODER = "coder"
    DEBUGGER = "debugger"
    TESTER = "tester"
    REVIEWER = "reviewer"
    SECURITY = "security"
    BUG_HUNTER = "bug_hunter"
    RED_TEAM = "red_team"
    EXPLOIT_VALIDATOR = "exploit_validator"
    EXPLOIT_GENERATOR = "exploit_generator"


class SafetyTier(str, enum.Enum):
    """5 selectable safety tiers for the execution harness."""
    TIER_1_PARANOIA = "tier_1_paranoia"      # Read-only / Zero side-effects allowed
    TIER_2_STRICT = "tier_2_strict"          # Human approval required on all edits and commands
    TIER_3_BALANCED = "tier_3_balanced"      # Aider standard: Output AST syntax filter + checkpoints + approval on risky commands
    TIER_4_AUTO_VALID = "tier_4_auto_valid"  # Auto-apply if AST valid + auto-checkpoint + non-destructive commands
    TIER_5_YOLO = "tier_5_yolo"              # Autonomous unrestricted execution


class AgentMode(str, enum.Enum):
    """Operating modes for the agent harness."""
    CODING = "coding"                    # Feature development, refactoring, implementation
    CYBERSECURITY = "cybersecurity"      # AppSec auditing, taint tracking, secret & config scanning
    RED_TEAM = "red_team"                # Scoped adversary simulation and evidence-led engagements
    BUG_HUNTING = "bug_hunting"          # Edge cases, race conditions, logic flaws, regression testing


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
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    LLAMACPP = "llama.cpp"
    OPENAI_COMPAT = "openai_compatible"  # vLLM, LMStudio, etc.


class ToolName(str, enum.Enum):
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    EDIT_FILE = "edit_file"
    APPLY_PATCH = "apply_patch"
    LIST_FILES = "list_files"
    SEARCH_FILES = "search_files"
    GREP = "grep"
    CODE_SEARCH = "code_search"
    REPO_MAP = "repo_map"
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
    RUN_SAST = "run_sast"
    AUDIT_DEPENDENCIES = "audit_dependencies"
    AUDIT_CONFIG = "audit_config"
    RUN_BUG_SCAN = "run_bug_scan"
    REDTEAM_WORKSPACE = "redteam_workspace"
    REDTEAM_HTTP = "redteam_http_check"
    REDTEAM_FINDING = "redteam_record_finding"
    REDTEAM_WORKFLOW = "redteam_workflow"
    REDTEAM_IMPORT = "redteam_import_findings"
    SECURITY_PLAYBOOK = "security_playbook"
    RECOVER_CONTEXT = "recover_context"
    SECURITY_SCAN = "security_scan"
    RECORD_FINDING = "record_finding"
    VISUAL_VERIFY = "visual_verify"


# === Messages ===

class Message(BaseModel):
    """A single message in a conversation."""
    role: str  # "system", "user", "assistant", "tool"
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    reasoning_content: str | None = Field(default=None, repr=False)
    reasoning_details: list[dict[str, Any]] | None = Field(default=None, repr=False)


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
    data: dict[str, Any] = Field(default_factory=dict)


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
    reasoning_content: str | None = Field(default=None, repr=False)
    reasoning_details: list[dict[str, Any]] | None = Field(default=None, repr=False)


class ModelConfig(BaseModel):
    """Configuration for a model."""
    name: str
    backend: ModelBackend = ModelBackend.OLLAMA
    model_id: str  # The model identifier for the backend (e.g., 'qwen2.5-coder:7b')
    base_url: str = "http://localhost:11434"  # Default Ollama URL
    context_length: int = 8192
    temperature: float = 0.2
    strategy_attempt_limit: int = Field(default=3, ge=1, le=10)
    max_tokens: int = 4096
    quantization: str | None = None  # e.g., 'Q4_K_M'
    gpu_layers: int | None = None
    estimated_vram_mb: int | None = None
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    api_key_env: str | None = None
    profile: Literal["generic", "glm53", "qwen38", "openrouter_deepseek", "openrouter_glm", "openrouter_kimi"] = "generic"

    def resolve_api_key(self) -> str | None:
        import os
        if self.api_key and self.api_key.get_secret_value():
            return self.api_key.get_secret_value()
        env = self.api_key_env
        if env is None and self.backend == ModelBackend.ANTHROPIC:
            env = "ANTHROPIC_API_KEY"
        return os.environ.get(env) if env else None

    def output_budget(self, requested: int | None = None) -> int:
        """Keep generation within the model's context while reserving prompt space."""
        limit = max(1, self.context_length // 2)
        desired = self.max_tokens if requested is None else requested
        return max(1, min(int(desired), limit))


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
    model_name: str | None = None
    engagement_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)


class TaskContext(BaseModel):
    """Context provided to an agent for a task."""
    architecture: str = ""
    guidelines: str = ""
    security_lessons: str = ""  # SECURITY_LESSONS.md, for security roles
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
    tool_calls_used: int = 0
    max_tool_calls: int = 100
    test_runs: int = 0
    max_test_runs: int = 20
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
    """Controls whether ASK-risk actions pause for human approval.

    When the relevant flag is True and no approval callback is wired in
    (e.g. a headless/remote run), the action is denied rather than silently
    allowed -- approval is opt-out only when explicitly configured.
    """
    require_approval_for_commands: bool = True
    require_approval_for_commits: bool = True


class VerificationConfig(BaseModel):
    """Verification loop configuration."""
    max_fix_iterations: int = 3
    run_tests_after_changes: bool = True
    require_review: bool = True
    rollback_on_failure: bool = False


class AgenticConfig(BaseModel):
    """Controls routing, context compaction, and resumable sessions."""
    role_models: dict[str, str] = Field(default_factory=dict)
    context_window_chars: int = 180000
    compact_context_chars: int = 90000
    max_parallel_agents: int = 1
    # Disk-backed KV cache for the exploration prefix (llama-server's
    # /slots save/restore API; requires the server to be started with
    # --slot-save-path). Off by default: it requires that specific server
    # support and is a no-op (silently ignored) otherwise. See README.md's
    # "Local model server" section.
    session_cache: bool = False
    # Size in tokens of the ranked repo map given to agents at the start of
    # a task (0 turns it off). See context/repo_map.py.
    repo_map_tokens: int = 1024
    # Use the actor model for multi-agent task execution (spawns isolated
    # child actors per plan task with mailbox-based messaging).
    use_actor_model: bool = False


class RoutingConfig(BaseModel):
    """Multi-model routing beyond the per-role map in AgenticConfig.role_models.

    escalate_to: model a step is rerun on after it fails (a failed plan task,
    or the second and later debugger attempts in the fix loop).
    fallbacks: model name -> ordered alternatives to use when that model's
    server is unreachable.
    """
    escalate_to: str | None = None
    fallbacks: dict[str, list[str]] = Field(default_factory=dict)


class ToolSettings(BaseModel):
    """Tool settings from the config file's `tools:` section.

    Tool names are kept as plain strings so a config written for a newer
    tool set still loads; names this build doesn't know are ignored.
    """
    disabled: list[str] = Field(default_factory=list)
    command_timeout: int | None = None
    command_sandbox: bool = False
    sandbox_runtime: Literal["auto", "docker", "podman"] = "auto"
    sandbox_image: str = Field(default="python:3.12-slim", min_length=1)
    # role -> the complete list of tools that role may use (replaces the
    # built-in default for that role).
    roles: dict[str, list[str]] = Field(default_factory=dict)


class ProjectConfig(BaseModel):
    """Top-level project configuration."""
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    resources: ResourceConfig = Field(default_factory=ResourceConfig)
    permissions: list[AgentPermissions] = Field(default_factory=list)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    agentic: AgenticConfig = Field(default_factory=AgenticConfig)
    safety_tier: SafetyTier = SafetyTier.TIER_3_BALANCED
    mode: AgentMode = AgentMode.CODING
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    # Config files that were merged to build this config, lowest priority first.
    sources: list[str] = Field(default_factory=list)
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
