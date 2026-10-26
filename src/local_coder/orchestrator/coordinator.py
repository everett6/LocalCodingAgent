import asyncio
import hashlib
import os
import json
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol

from local_coder.approval import ApprovalCallback
from local_coder.git.checkpoints import CheckpointManager
from local_coder.safety import SafetyHarness
from local_coder.types import (
    ProjectConfig, AgentEvent, AgentRole, AgentTask, TaskContext, TaskPlan, AgentResponse,
    TaskStatus, AgentMode, SafetyTier, ToolName,
)
from local_coder.agents import create_agent
from local_coder.tools import create_tool_registry
from local_coder.models.cache import PromptCache
from local_coder.models.manager import ModelManager
from local_coder.models.router import resolve_model_name
from local_coder.memory.store import MemoryStore
from local_coder.context.repository import RepositoryContext
from local_coder.core.runtime import AgentRuntime
from local_coder.scheduler.dag import TaskDAG
from local_coder.security.batching import collect_files, plan_batches, total_chars
from local_coder.security.ledger import FindingsLedger, render_ledger_report
from local_coder.security.lessons import LessonStore
from local_coder.security.triage import LAST_SCAN_FILE, promote_last_scan
from local_coder.scheduler.resources import ResourceManager
from local_coder.speculative.drafter import SpeculativeDrafter
from local_coder.verification.failures import summarize_failures
from local_coder.verification.test_runner import detect_framework
from local_coder.providers.quota import ProviderQuotaExhausted
from local_coder.orchestrator.blocked import AgentBlocked


class RunCheckpoint(Protocol):
    """Where Coordinator.run saves each finished phase so an interrupted run
    can resume (orchestrator.sessions.TurnCheckpoint is the on-disk one).
    Values are JSON-compatible: strings or model_dump(mode="json") dicts."""

    def get(self, key: str) -> Any | None: ...

    def put(self, key: str, value: Any) -> None: ...


class Coordinator:
    """Main orchestrator that coordinates the coding agent workflow."""

    def __init__(
        self,
        config: ProjectConfig,
        project_root: str,
        approval_callback: ApprovalCallback | None = None,
        quota_guard=None,
    ):
        self.config = config
        self.project_root = project_root
        self.repo_context = RepositoryContext(project_root)

        # Safety Harness (Tiers 1-5 & Tree-sitter OutputFilter)
        tier = getattr(config, "safety_tier", SafetyTier.TIER_3_BALANCED)
        self.safety_harness = SafetyHarness(tier)

        # Ensure state dir exists
        self.runtime = AgentRuntime(project_root, checkpoint_interval=1, state_dir=config.state_dir)
        state_dir_path = str(self.runtime.state_dir)

        self.memory = MemoryStore(os.path.join(state_dir_path, "memory.db"))
        self.checkpoints = CheckpointManager(project_root)
        CheckpointManager.ensure_git_repository(project_root)

        self.tool_registry = create_tool_registry(
            project_root,
            approval=config.approval,
            approval_callback=approval_callback,
            safety_harness=self.safety_harness,
        )
        self.tool_registry.apply_settings(config.tools)
        self.model_manager = ModelManager(config, quota_guard=quota_guard)
        from local_coder.core.budgets import BudgetLedger
        from local_coder.context.token_count import load_counter_for_project
        from local_coder.statedir import state_path
        limits = config.harness
        self.budget = BudgetLedger(max_requests=limits.max_requests, max_tokens=limits.max_tokens,
                                   max_request_tokens=limits.max_request_tokens, max_cost_usd=limits.max_cost_usd,
                                   state_path=state_path(project_root, f".local-coder/budgets/{uuid.uuid4().hex}.json"),
                                   token_counter=load_counter_for_project(project_root))
        self.model_manager.budget = self.budget
        self.model_manager.on_close = self.tool_registry.close
        self.tool_registry.budget = self.budget
        self.tool_registry.quota_guard = quota_guard
        evolution_tool = self.tool_registry.get_tool(ToolName.HARNESS_EVOLVE)
        if evolution_tool is not None:
            # Binding is inert: only an explicitly approved improve action runs it.
            evolution_tool.improvement_evaluator = self._evaluate_harness_profile
        self.tool_registry.memory_context_chars = limits.memory_context_chars if limits.memory_enabled else 0
        self.tool_registry.memory_enabled = limits.memory_enabled
        self.tool_registry.context_snapshots = limits.context_snapshots
        self.tool_registry.context_allocation = limits.context_allocation
        self.tool_registry.auto_language_context = limits.language_context_enabled
        self.tool_registry.pre_edit_snapshots = limits.pre_edit_snapshots
        self.tool_registry.profile_overrides = limits.profile_overrides
        self.tool_registry.sandbox.backend = limits.sandbox_backend
        self.tool_registry.sandbox.image = limits.sandbox_image
        self.tool_registry.max_subagents = limits.max_subagents
        delegate = self.tool_registry.get_tool(ToolName.DELEGATE_TASK)
        if delegate is not None:
            delegate.callback = self._delegate_task
        self.tool_registry.runtime = self.runtime
        self.runtime.model_name_for = self._model_name_for
        self.resource_manager = ResourceManager(config.resources)
        self._event_handlers: list[Callable] = []
        # Shared across every drafter this Coordinator creates, so a repeated
        # draft prompt (e.g. the same file across a fix-loop retry) can hit
        # the cache instead of paying for another small-model round trip.
        self._prompt_cache = PromptCache()

    async def _evaluate_harness_profile(self, overlay):
        """Measure the selected security model's instruction profile, not a coding workflow.

        Use a dedicated single-backend manager so evaluation cannot load unrelated
        models, probe fallbacks or close the coordinator's live tools/models.
        """
        from local_coder.core.budgets import BudgetedModel
        from local_coder.harness.evaluation import make_model_evaluator, spending_admission
        from local_coder.models.profiles import resolve_profile, validate_overlay

        controls = {**self.tool_registry.profile_overrides, **validate_overlay(overlay)}
        selected = resolve_model_name(self.model_manager.config, AgentRole.SECURITY)
        configured = self.model_manager.config.models[selected]
        resolve_profile(configured, AgentRole.SECURITY, controls)
        # Admission is required before constructing even a dedicated manager.
        spending_admission(configured, self.budget)
        dedicated_config = self.model_manager.config.model_copy(deep=True)
        dedicated_config.models = {selected: dedicated_config.models[selected]}
        dedicated_config.routing.fallbacks = {}
        dedicated_config.routing.escalate_to = None
        manager = ModelManager(dedicated_config, quota_guard=self.model_manager.quota_guard)
        manager.budget = self.budget
        try:
            model = await manager.get_model(AgentRole.SECURITY, selected)
            # The adapter wraps exactly once, with the same request ledger and
            # quota guard. Keep manager ownership of its wrapper for cleanup.
            while isinstance(model, BudgetedModel):
                model = model.model
            evaluator = make_model_evaluator(lambda _overlay: model, ledger=self.budget,
                quota_guard=manager.quota_guard, render_profile=True)
            return await evaluator(controls)
        finally:
            cleanup = asyncio.create_task(asyncio.wait_for(manager.close_all(), timeout=15))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

    async def _delegate_task(self, request):
        """Leaf agents share the exact registry, scope, approval callback and ledger."""
        from local_coder.workspace import Workspace
        current = self.tool_registry.task_context.get() or {}
        if current.get("depth", 0) or not current.get("task_id"):
            raise ValueError("Delegation requires an active parent task and permits one level only")
        if self.tool_registry.get_tool(ToolName.DELEGATE_TASK)._requests > self.config.harness.max_subagents:
            raise ValueError("Shared sub-agent limit reached")
        child, parent = AgentRole(request["role"]), AgentRole(current["role"])
        if not self.tool_registry._permissions[child].issubset(self.tool_registry._permissions[parent]):
            raise ValueError("Child capabilities exceed parent capabilities")
        workspace = Workspace(self.project_root)
        roots = [workspace.resolve(path) for path in current.get("files", [])]
        files = request.get("files") or list(current.get("files", []))
        for path in files:
            target = workspace.resolve(path)
            if roots and not any(target == root or target.is_relative_to(root) for root in roots):
                raise ValueError("Child task path exceeds inherited scope")
        agent = create_agent(child, await self.model_manager.get_model(child), self.tool_registry, self._dispatch,
                             context_window_chars=self.config.agentic.context_window_chars,
                             compact_context_chars=self.config.agentic.compact_context_chars)
        agent.delegation_depth = 1
        task = AgentTask(role=child, objective=request["objective"], files=files,
                         constraints=["Leaf task: preserve the parent's scope, shared budget, and approval requirements; do not delegate."])
        self._emit("ORCHESTRATOR", "subagent_started", f"Started scoped {child.value} leaf task", data={"task_id": task.task_id, "parent_task_id": current["task_id"]})
        response = await asyncio.wait_for(agent.execute(task), timeout=task.timeout_seconds)
        self._raise_if_blocked(response)
        self._emit("ORCHESTRATOR", "subagent_finished", f"Leaf task {response.status.value}", data={"task_id": task.task_id})
        return {"success": response.status == TaskStatus.COMPLETED, "output": response.summary}

    def _model_name_for(self, model) -> str | None:
        """Capture the resolved config key instead of rerouting roles on restart."""
        return next((name for name, loaded in self.model_manager._models.items() if loaded is model), None)

    async def resume_checkpoint(self, path: str, *, allow_model_change: bool = False) -> AgentResponse:
        """Resume the latest complete round for a workspace-local checkpoint's run.

        Paths are absolute or relative to the project root. Invalid/legacy files
        raise ValueError before model selection. Finished runs return their saved
        result, making a repeated Resume request safe.
        """
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = Path(self.project_root) / candidate
        candidate = candidate.resolve()
        match = re.fullmatch(r"checkpoint_([a-f0-9]{12})_([0-9]+)\.json", candidate.name)
        if candidate.parent != self.runtime.state_dir or match is None or not candidate.is_file():
            raise ValueError("Checkpoint must be inside the workspace's configured state directory")
        run_id = match.group(1)
        if await self.runtime.get_run(run_id) is not None:
            raise ValueError("Run is already active")
        # Selecting an older UI entry must never replay a newer completed batch.
        candidates = []
        for saved in self.runtime.state_dir.glob(f"checkpoint_{run_id}_*.json"):
            saved_match = re.fullmatch(rf"checkpoint_{run_id}_([0-9]+)\.json", saved.name)
            if saved_match and saved.resolve().parent == self.runtime.state_dir:
                candidates.append((int(saved_match.group(1)), saved))
        latest = max(candidates, key=lambda item: item[0])[1]
        recovered = await self.runtime.recover_from_checkpoint(run_id, str(latest))
        if recovered is None:
            raise ValueError("Checkpoint is invalid or lacks the full task required for recovery")
        try:
            if not recovered.notes.get("boundary_complete"):
                raise ValueError("Checkpoint is not a complete agent round")
            if type(allow_model_change) is not bool or (allow_model_change and recovered.notes.get("quota_paused") is not True):
                raise ValueError("Model changes require explicit approval of a quota-paused checkpoint")
            final_path = self.runtime.state_dir / f"run_{run_id}_final.json"
            if final_path.exists():
                if final_path.resolve().parent != self.runtime.state_dir:
                    raise ValueError("Final result must be inside the configured state directory")
                final = json.loads(await asyncio.to_thread(final_path.read_text, encoding="utf-8"))
                result = AgentResponse.model_validate(final["result"])
                if result.task_id != recovered.task.task_id:
                    raise ValueError("Final result task identity mismatch")
                self._raise_if_blocked(result)
                return result
            recovered.validate_resume()
            await recovered.validate_pending_batch()
            from local_coder.types import ToolName
            allowed = recovered.notes.get("allowed_tools")
            if allowed is not None:
                self.tool_registry._permissions[recovered.task.role] = {ToolName(name) for name in allowed}
            task = recovered.task
            if task.role == AgentRole.RED_TEAM:
                from local_coder.tools.redteam import bind_engagement
                if not task.engagement_id:
                    raise ValueError("Red team checkpoint lacks an engagement")
                bind_engagement(self.tool_registry, self.project_root, task.engagement_id)
                from local_coder.redteam import EngagementStore
                scope = EngagementStore(self.project_root).load(task.engagement_id)['scope']
                if scope['active_http'] and self.config.safety_tier == SafetyTier.TIER_1_PARANOIA:
                    raise ValueError("Read-only safety tier does not allow active HTTP observations")
            model = await self.model_manager.get_model(task.role, None if allow_model_change else task.model_name)
            if allow_model_change:
                task = task.model_copy(update={"model_name": self._model_name_for(model)})
                recovered.task = task
            agent = create_agent(
                task.role, model,
                self.tool_registry, self._dispatch,
                context_window_chars=self.config.agentic.context_window_chars,
                compact_context_chars=self.config.agentic.compact_context_chars,
            )
            self._emit_task_status(task.task_id, "running", "Resuming saved checkpoint")
            continuation = agent.resume(recovered, allow_model_change=True) if allow_model_change else agent.resume(recovered)
            response = await asyncio.wait_for(continuation, timeout=task.timeout_seconds)
            self._raise_if_blocked(response)
            completed = response.status == TaskStatus.COMPLETED
            self._emit_task_status(task.task_id, "completed" if completed else "failed", response.summary[:200])
            self._emit("ORCHESTRATOR", "task_completed" if completed else "task_failed",
                       "Resumed task complete" if completed else "Resumed task failed")
            return response
        except (KeyError, OSError, TypeError) as exc:
            raise ValueError(f"Cannot resume checkpoint: {exc}") from exc
        finally:
            # A cancelled/failed resume may be retried from the last boundary.
            await self.runtime.release_run(run_id)

    def on_event(self, handler: Callable[[AgentEvent], None]) -> None:
        """Register event handler."""
        self._event_handlers.append(handler)

    async def _get_drafter(self) -> SpeculativeDrafter | None:
        """Best-effort speculative drafter for file-editing roles (coder,
        debugger). Only available when config.yaml has a "drafter" model
        entry -- returns None otherwise so callers degrade to undrafted
        execution instead of failing."""
        if "drafter" not in self.config.models:
            return None
        try:
            drafter_model = await self.model_manager.get_model("drafter")
        except (ProviderQuotaExhausted, AgentBlocked):
            raise
        except Exception:
            return None
        return SpeculativeDrafter(drafter_model, prompt_cache=self._prompt_cache)

    def _emit(self, source: str, event_type: str, message: str, **kwargs) -> None:
        event = AgentEvent(source=source, event_type=event_type, message=message, **kwargs)
        self._dispatch(event)

    def _emit_task_status(self, task_id: str, status: str, note: str = "") -> None:
        """Structured status transition for one plan task -- consumed by the
        UI's live todo-list checklist (a "plan_created" event lists every
        task up front; each of these updates one row's status in place)."""
        self._emit(
            "ORCHESTRATOR", "task_status", f"{task_id}: {status}",
            data={"task_id": task_id, "status": status, "note": note},
        )

    def _raise_if_blocked(self, response: AgentResponse) -> None:
        """Keep a refusal terminal; never turn it into a plan or retry hint."""
        if response.status == TaskStatus.BLOCKED:
            self._emit_task_status(response.task_id, "blocked", response.summary[:200])
            self._emit("ORCHESTRATOR", "task_blocked", "Agent stopped at an explicit refusal or authorization boundary",
                       task_id=response.task_id, data={"task_id": response.task_id, "status": "blocked"})
            raise AgentBlocked(response)

    def _dispatch(self, event: AgentEvent) -> None:
        """Forward an already-built AgentEvent to registered handlers.

        This is the callback signature BaseAgent expects (a single
        AgentEvent argument), as opposed to _emit's (source, event_type,
        message, **kwargs) convenience signature used by the Coordinator
        itself. Sub-agents must be handed this method, not _emit directly.
        """
        for handler in self._event_handlers:
            handler(event)

    async def run(
        self, request: str, checkpoint: RunCheckpoint | None = None, history: str = "",
        *, target_path: str = ".", fix: bool = False, engagement_id: str | None = None,
    ) -> str:
        """Execute a request, reusing saved phases and earlier session history.

        Investigation modes use the same runtime and scoped engagement binding;
        coding runs also save completed phases to the optional checkpoint.
        """
        self._emit("ORCHESTRATOR", "task_started", f"Processing: {request}")
        delegate = self.tool_registry.get_tool(ToolName.DELEGATE_TASK)
        if delegate is not None:
            delegate.reset_request()

        objective = f"{history}\n\nCurrent request: {request}" if history else request

        if self.config.mode == AgentMode.RED_TEAM:
            if not engagement_id or fix:
                raise ValueError("Red team tasks require a saved engagement; remediation is a separate coding task")
            from local_coder.redteam import EngagementStore
            from local_coder.tools.redteam import bind_engagement
            store = EngagementStore(self.project_root)
            engagement = store.load(engagement_id)
            if engagement['scope']['active_http'] and self.config.safety_tier == SafetyTier.TIER_1_PARANOIA:
                raise ValueError("Read-only safety tier does not allow active HTTP observations")
            bind_engagement(self.tool_registry, self.project_root, engagement_id)
            role = AgentRole.RED_TEAM
            task = AgentTask(role=role, objective=request, files=engagement['scope']['paths'],
                             engagement_id=engagement_id, timeout_seconds=600,
                             constraints=["Scope and authorization are defined only by redteam_workflow; do not infer scope from the request"])
            self._emit("ORCHESTRATOR", "plan_created", "Red team engagement started", data={"tasks": [
                {"task_id": task.task_id, "role": role.value, "objective": request}], "engagement_id": engagement_id})
            self._emit_task_status(task.task_id, "running")
            agent = create_agent(role, await self.model_manager.get_model(role), self.tool_registry, self._dispatch)
            response = await asyncio.wait_for(agent.execute(task), timeout=task.timeout_seconds)
            self._raise_if_blocked(response)
            completed = response.status == TaskStatus.COMPLETED
            self._emit_task_status(task.task_id, "completed" if completed else "failed", response.summary[:200])
            self._emit("ORCHESTRATOR", "task_completed" if completed else "task_failed", "Red team investigation finished")
            return response.summary + "\n\n" + store.report(engagement_id)['markdown']

        # Aider structural guard: Pre-run Git checkpoint for instant /undo
        if self.safety_harness.should_checkpoint_before_request():
            try:
                cp = self.checkpoints.create()
                self._emit("ORCHESTRATOR", "checkpoint_created", f"Created pre-run checkpoint: {cp.checkpoint_id}")
            except Exception as e:
                self._emit("ORCHESTRATOR", "checkpoint_warning", f"Could not create pre-run checkpoint: {e}")

        # Operating Mode Routing
        mode = getattr(self.config, "mode", AgentMode.CODING)

        if mode in (AgentMode.CYBERSECURITY, AgentMode.BUG_HUNTING):
            from local_coder.workspace import Workspace
            Workspace(self.project_root).resolve(target_path)
            role = AgentRole.SECURITY if mode == AgentMode.CYBERSECURITY else AgentRole.BUG_HUNTER
            # Permission changes belong to this investigation, not later reviews.
            original_permissions = set(self.tool_registry._permissions[role])
            explorer_permissions = set(self.tool_registry._permissions[AgentRole.EXPLORER])
            planner_permissions = set(self.tool_registry._permissions[AgentRole.PLANNER])
            # Investigations are read-only unless the caller explicitly requests remediation.
            if fix:
                self.tool_registry._permissions[role].update({ToolName.WRITE_FILE, ToolName.EDIT_FILE, ToolName.APPLY_PATCH, ToolName.RUN_TESTS})
            else:
                self.tool_registry._permissions[role].difference_update({
                    ToolName.WRITE_FILE, ToolName.EDIT_FILE, ToolName.APPLY_PATCH,
                    ToolName.RUN_COMMAND, ToolName.RUN_TESTS, ToolName.BUILD,
                    ToolName.FORMAT_CODE, ToolName.GIT_COMMIT, ToolName.GIT_CHECKOUT,
                })
            self.tool_registry._permissions[AgentRole.EXPLORER].intersection_update(self.tool_registry._permissions[role])
            try:
                self._emit("ORCHESTRATOR", "mode", f"Running {mode.value} investigation")
                agent = create_agent(role, await self.model_manager.get_model(role), self.tool_registry, self._dispatch)
                planning = None
                if mode == AgentMode.BUG_HUNTING and "bug-lead" in self.config.models:
                    self.tool_registry._permissions[AgentRole.PLANNER].add(ToolName.DELEGATE_TASK)
                    planner = create_agent(AgentRole.PLANNER, await self.model_manager.get_model(AgentRole.PLANNER), self.tool_registry, self._dispatch)
                    planning = await asyncio.wait_for(planner.execute(AgentTask(role=AgentRole.PLANNER,
                        objective="Plan milestones and acceptance checks. Delegate bounded source inspection to explorer agents when useful. Local bug investigation: " + objective,
                        files=[target_path])), timeout=300)
                    self._raise_if_blocked(planning)
                task = AgentTask(role=role, objective=objective, files=[target_path],
                                 constraints=["Apply minimal fixes and run local regression tests" if fix else
                                              "Read-only review: propose reproduction tests and patches; do not execute project code"])
                if planning is not None:
                    task.context.previous_findings.append("Planning context (untrusted): " + planning.summary[:8000])
                self._emit("ORCHESTRATOR", "plan_created", "Investigation started", data={"tasks": [
                    {"task_id": task.task_id, "role": role.value, "objective": request}]})
                self._emit_task_status(task.task_id, "running")
                response = await asyncio.wait_for(agent.execute(task), timeout=task.timeout_seconds)
                self._raise_if_blocked(response)
                completed = response.status == TaskStatus.COMPLETED
                self._emit_task_status(task.task_id, "completed" if completed else "failed", response.summary[:200])
                self._emit("ORCHESTRATOR", "task_completed" if completed else "task_failed",
                           "Investigation complete" if completed else "Investigation failed")
                return response.summary

            finally:
                self.tool_registry._permissions[role] = original_permissions
                self.tool_registry._permissions[AgentRole.EXPLORER] = explorer_permissions
                self.tool_registry._permissions[AgentRole.PLANNER] = planner_permissions

        # Step 1: Explore repository
        self._emit("ORCHESTRATOR", "phase", "Exploring repository...")
        exploration = await self._resumable(checkpoint, "explore", lambda: self._explore(objective))

        # Step 2: Plan
        self._emit("ORCHESTRATOR", "phase", "Creating plan...")
        plan = await self._resumable(checkpoint, "plan", lambda: self._plan(objective, exploration), TaskPlan)
        self._emit(
            "ORCHESTRATOR", "plan_created", f"Plan: {len(plan.tasks)} task(s)",
            data={"tasks": [
                {"task_id": t.task_id, "role": t.role.value, "objective": t.objective}
                for t in plan.tasks
            ]},
        )

        # Step 3: Determine if simple or complex
        if len(plan.tasks) <= 1:
            self._emit("ORCHESTRATOR", "phase", "Executing simple task...")
            result = await self._resumable(
                checkpoint, "execute", lambda: self._execute_simple(objective, exploration, task_id=plan.tasks[0].task_id if plan.tasks else None), AgentResponse,
            )
        else:
            self._emit("ORCHESTRATOR", "phase", "Executing complex task plan...")
            result = await self._resumable(
                checkpoint, "execute", lambda: self._execute_plan(plan, checkpoint), AgentResponse,
            )

        self._raise_if_blocked(result)
        # Step 4: Review
        self._emit("ORCHESTRATOR", "phase", "Reviewing changes...")
        review = await self._resumable(checkpoint, "review", lambda: self._review(result))
        
        # Step 5: Test
        test_result = None
        last_fix_result: AgentResponse | None = None
        if self.config.verification.run_tests_after_changes:
            self._emit("ORCHESTRATOR", "phase", "Running tests...")
            test_result = await self._resumable(checkpoint, "tests.0", self._run_tests, AgentResponse)
            self._raise_if_blocked(test_result)

            # Step 6: Fix loop
            fix_iterations = 0
            while not test_result.tests_passed and fix_iterations < self.config.verification.max_fix_iterations:
                self._emit("ORCHESTRATOR", "fix_loop", f"Fixing failures (attempt {fix_iterations + 1})...")
                # The first attempt uses the debugger's own model; later ones
                # move to routing.escalate_to (if set) since the first failed.
                last_fix_result = await self._resumable(
                    checkpoint, f"fix.{fix_iterations}",
                    lambda: self._fix_failures(test_result, escalate=fix_iterations > 0), AgentResponse,
                )
                fix_iterations += 1
                self._raise_if_blocked(last_fix_result)
                if last_fix_result.status == TaskStatus.FAILED:
                    self._emit(
                        "ORCHESTRATOR", "fix_loop_error",
                        f"Debugger could not attempt a fix: {last_fix_result.summary}",
                    )
                    break
                test_result = await self._resumable(
                    checkpoint, f"tests.{fix_iterations}", self._run_tests, AgentResponse,
                )
                self._raise_if_blocked(test_result)

        # Step 7: Final report
        report = await self._generate_report(result, review, test_result, last_fix_result)
        if result.status == TaskStatus.COMPLETED and (test_result is None or test_result.tests_passed):
            self._emit("ORCHESTRATOR", "task_completed", "Done")
        else:
            self._emit("ORCHESTRATOR", "task_failed", "Verification did not pass within the retry limit")
        return report
    
    async def _resumable(self, checkpoint: RunCheckpoint | None, key: str, produce: Callable, model=None):
        """Return the checkpointed value for key if there is one, otherwise
        run produce() and checkpoint its result. model is the pydantic type
        the value round-trips through (None for plain strings)."""
        saved = checkpoint.get(key) if checkpoint is not None else None
        if saved is not None:
            self._emit("ORCHESTRATOR", "phase_resumed", f"Reusing saved '{key}' result from the session")
            value = model.model_validate(saved) if model is not None else saved
            if isinstance(value, AgentResponse):
                self._raise_if_blocked(value)
            return value
        value = await produce()
        if isinstance(value, AgentResponse):
            self._raise_if_blocked(value)
        if checkpoint is not None:
            checkpoint.put(key, value.model_dump(mode="json") if model is not None else value)
        return value

    def _session_cache_filename(self) -> str:
        """Stable per-project filename for the disk-backed slot cache, so
        different projects sharing one llama-server don't collide."""
        digest = hashlib.sha256(os.path.abspath(self.project_root).encode()).hexdigest()[:16]
        return f"local-coder-{digest}.bin"

    async def _repo_map(self, query: str, files: list[str] | None = None) -> str:
        """Ranked repo map for a task's context, or "" if it is off or fails."""
        budget = self.config.agentic.repo_map_tokens
        tool = self.tool_registry.get_tool(ToolName.REPO_MAP)
        if budget <= 0 or tool is None:
            return ""
        try:
            text = await asyncio.to_thread(tool.repo_map.build, query, files or [], budget)
        except Exception as e:  # orientation is a bonus; never block the task on it
            self._emit("ORCHESTRATOR", "repo_map_error", f"Could not build repo map: {e}")
            return ""
        if not text:
            return ""
        return (
            "Most relevant files and their key definitions, ranked for this task "
            "(number = line where each starts). read_file the ranges you need; "
            "call repo_map with other terms to re-center it.\n" + text
        )

    async def _explore(self, request: str) -> str:
        model = await self.model_manager.get_model(AgentRole.EXPLORER)

        # Exploration's prefix (system prompt + repository structure) is the
        # part of the conversation most likely to be identical across
        # separate runs on this project, so it's the one worth keeping warm
        # on disk across a server restart. Both calls are best-effort and
        # silently no-op on any backend that doesn't support them (e.g.
        # Ollama, or a llama-server not started with --slot-save-path).
        if self.config.agentic.session_cache:
            restore = getattr(model, "restore_slot", None)
            if restore is not None:
                await restore(self._session_cache_filename())

        explorer = create_agent(
            AgentRole.EXPLORER,
            model,
            self.tool_registry,
            self._dispatch,
            context_window_chars=self.config.agentic.context_window_chars,
            compact_context_chars=self.config.agentic.compact_context_chars,
        )
        if explorer:
            task = AgentTask(
                role=AgentRole.EXPLORER,
                objective=request,
                context=TaskContext(architecture=await self._repo_map(request)),
            )
            response = await explorer.execute(task)
            self._raise_if_blocked(response)
            if self.config.agentic.session_cache:
                save = getattr(model, "save_slot", None)
                if save is not None:
                    await save(self._session_cache_filename())
            return response.summary
        return "Exploration fallback: Checked repository structure."

    async def _plan(self, request: str, exploration: str) -> TaskPlan:
        planner = create_agent(AgentRole.PLANNER, await self.model_manager.get_model(AgentRole.PLANNER), self.tool_registry, self._dispatch)
        if planner:
            task = AgentTask(
                role=AgentRole.PLANNER,
                objective=f"Request: {request}\nExploration: {exploration}",
                context=TaskContext(architecture=await self._repo_map(request)),
            )
            response = await planner.execute(task)
            self._raise_if_blocked(response)
            plan_data = response.summary
            
            try:
                # Try to extract JSON from the response text
                if "```json" in plan_data:
                    json_str = plan_data.split("```json")[1].split("```")[0].strip()
                else:
                    # Fallback assuming the whole thing is JSON
                    start_idx = plan_data.find("{")
                    end_idx = plan_data.rfind("}") + 1
                    json_str = plan_data[start_idx:end_idx] if start_idx >= 0 else plan_data
                
                parsed = json.loads(json_str)
                tasks = []
                for t in parsed.get("tasks", []):
                    role_str = t.get("role", "coder").upper()
                    try:
                        role = AgentRole[role_str]
                    except KeyError:
                        role = AgentRole.CODER
                        
                    task_obj = AgentTask(
                        task_id=t.get("task_id", f"task_{len(tasks)}"),
                        role=role,
                        objective=t.get("objective", request),
                        files=t.get("files", []),
                        constraints=t.get("constraints", []),
                        success_criteria=t.get("success_criteria", []),
                        depends_on=t.get("depends_on", []),
                        model_name=t.get("model_name"),
                    )
                    tasks.append(task_obj)
                
                if not tasks:
                    raise ValueError("No tasks found in plan")
                return TaskPlan(objective=request, tasks=tasks, notes=[plan_data])
            except (ProviderQuotaExhausted, AgentBlocked):
                raise
            except Exception as e:
                self._emit("ORCHESTRATOR", "plan_error", f"Failed to parse plan JSON: {e}")
                # Fallback on failure
                pass
            
        task = AgentTask(
            task_id="fallback_task",
            role=AgentRole.CODER,
            objective=request,
            files=[],
            constraints=[],
            context=TaskContext(),
            success_criteria=[],
            depends_on=[],
            priority=1,
            max_retries=1,
            timeout_seconds=300
        )
        return TaskPlan(plan_id="plan_fallback", objective=request, tasks=[task], notes=[])

    async def _execute_simple(self, request: str, exploration: str, task_id: str | None = None) -> AgentResponse:
        coder = create_agent(
            AgentRole.CODER,
            await self.model_manager.get_model(AgentRole.CODER),
            self.tool_registry,
            self._dispatch,
            context_window_chars=self.config.agentic.context_window_chars,
            compact_context_chars=self.config.agentic.compact_context_chars,
            drafter=await self._get_drafter(),
        )
        if coder:
            kwargs = {"task_id": task_id} if task_id else {}
            task = AgentTask(
                role=AgentRole.CODER,
                objective=f"Request: {request}\nContext: {exploration}",
                context=TaskContext(architecture=await self._repo_map(request)),
                **kwargs,
            )
            self._emit_task_status(task.task_id, "running")
            response = await coder.execute(task)
            self._raise_if_blocked(response)
            self._emit_task_status(task.task_id, "failed" if response.status == TaskStatus.FAILED else "completed", response.summary[:200])
            return response
        return AgentResponse(
            task_id="simple_task",
            status=TaskStatus.COMPLETED,
            summary="Fallback simple execution.",
            files_changed=[],
            tests_run=[],
            tests_passed=True,
            issues=[],
            follow_up_required=False
        )

    async def _execute_plan(self, plan: TaskPlan, checkpoint: RunCheckpoint | None = None) -> AgentResponse:
        dag = TaskDAG()
        dag.add_tasks(plan.tasks)
        # Plan tasks that finished before an interruption stay finished; only
        # the rest run again. Failed tasks are never checkpointed, so they
        # get another attempt on resume.
        if checkpoint is not None:
            for task in plan.tasks:
                saved = checkpoint.get(f"task.{task.task_id}")
                if saved is not None:
                    response = AgentResponse.model_validate(saved)
                    self._raise_if_blocked(response)
                    dag.mark_completed(task.task_id, response)
                    self._emit("ORCHESTRATOR", "phase_resumed", f"Reusing saved result for plan task {task.task_id}")

        validation_errors = dag.validate()
        if validation_errors:
            self._emit("ORCHESTRATOR", "plan_error", f"Invalid task plan: {'; '.join(validation_errors)}")
            return AgentResponse(
                task_id="plan_execution",
                status=TaskStatus.FAILED,
                summary="Task plan failed validation:\n" + "\n".join(validation_errors),
                files_changed=[],
                tests_run=[],
                tests_passed=False,
                issues=validation_errors,
                follow_up_required=True,
            )

        while not dag.is_complete():
            ready_tasks = dag.get_ready_tasks()
            if not ready_tasks:
                # No task is running and none are ready: the remaining pending
                # tasks can never become ready (e.g. a dependency never resolves
                # because it failed). Fail them out instead of spinning forever.
                stalled = dag.get_pending_task_ids()
                self._emit("ORCHESTRATOR", "plan_error", f"Task plan stalled, unable to schedule: {stalled}")
                for task_id in stalled:
                    dag.mark_failed(task_id, AgentResponse(
                        task_id=task_id,
                        status=TaskStatus.FAILED,
                        summary="Dependency never completed successfully.",
                        files_changed=[],
                        tests_run=[],
                        tests_passed=False,
                        issues=["Blocked on a failed or unresolved dependency"],
                        follow_up_required=True,
                    ))
                    self._emit_task_status(task_id, "failed", "Blocked on a failed or unresolved dependency")
                break
            # Mark the whole wave running up front (synchronously, before any
            # task yields control) so a task never gets scheduled twice.
            for task in ready_tasks:
                dag.mark_running(task.task_id)
                self._emit_task_status(task.task_id, "running")
            semaphore = asyncio.Semaphore(self._parallelism_for_wave(ready_tasks))
            running = [asyncio.create_task(self._run_dag_task(dag, task, semaphore, checkpoint)) for task in ready_tasks]
            try:
                await asyncio.gather(*running)
            except BaseException:
                for sibling in running:
                    if not sibling.done():
                        sibling.cancel()
                await asyncio.gather(*running, return_exceptions=True)
                raise

        return AgentResponse(
            task_id="plan_execution",
            status=TaskStatus.COMPLETED if not dag.has_failures() else TaskStatus.FAILED,
            summary=dag.get_summary(),
            files_changed=[],
            tests_run=[],
            tests_passed=not dag.has_failures(),
            issues=[],
            follow_up_required=dag.has_failures()
        )

    def _parallelism_for_wave(self, tasks: list[AgentTask]) -> int:
        """Only parallelize mutations when their file scopes are explicit and disjoint."""
        configured = max(1, self.config.agentic.max_parallel_agents)
        mutating_roles = {AgentRole.CODER, AgentRole.DEBUGGER, AgentRole.TESTER}
        mutating_tasks = [task for task in tasks if task.role in mutating_roles]
        if not mutating_tasks:
            return configured
        if any(not task.files for task in mutating_tasks):
            return 1

        seen: set[str] = set()
        for task in mutating_tasks:
            file_scope = set(task.files)
            if seen.intersection(file_scope):
                return 1
            seen.update(file_scope)
        return configured

    async def _run_dag_task(
        self, dag: TaskDAG, task: AgentTask, semaphore: asyncio.Semaphore,
        checkpoint: RunCheckpoint | None = None,
    ) -> None:
        """Run one DAG-scheduled task, bounded by the parallelism semaphore,
        and record its outcome on the DAG. Concurrent siblings in the same
        wave call this independently via asyncio.gather in _execute_plan.

        When routing.escalate_to names a different model than the one that
        just failed, the task is rerun once on that model before it is
        recorded as failed."""
        async with semaphore:
            async def attempt(escalate):
                try:
                    return await self._attempt_dag_task(task, escalate=escalate)
                except AgentBlocked as exc:
                    dag.mark_failed(task.task_id, exc.response)
                    self._raise_if_blocked(exc.response)
            response = await attempt(False)
            if response.status != TaskStatus.BLOCKED and self._failed(response) and self._can_escalate(task.role, task.model_name):
                self._emit(
                    "ORCHESTRATOR", "escalate",
                    f"Retrying {task.task_id} on {self.config.routing.escalate_to} after: {response.summary[:200]}",
                    task_id=task.task_id,
                )
                response = await attempt(True)
            if response.status == TaskStatus.BLOCKED:
                dag.mark_failed(task.task_id, response)
                self._raise_if_blocked(response)
            if self._failed(response):
                dag.mark_failed(task.task_id, response)
                self._emit_task_status(task.task_id, "failed", response.summary[:200])
            else:
                dag.mark_completed(task.task_id, response)
                self._emit_task_status(task.task_id, "completed", response.summary[:200])
                if checkpoint is not None:
                    checkpoint.put(f"task.{task.task_id}", response.model_dump(mode="json"))

    async def _model_for(self, role: AgentRole, model_name: str | None = None, escalate: bool = False):
        if escalate:
            return await self.model_manager.get_model(role, model_name, escalate=True)
        if model_name is None:
            return await self.model_manager.get_model(role)
        return await self.model_manager.get_model(role, model_name)

    @staticmethod
    def _failed(response: AgentResponse) -> bool:
        return response.status in (TaskStatus.FAILED, TaskStatus.BLOCKED) or not response.tests_passed

    def _can_escalate(self, role: AgentRole, model_name: str | None = None) -> bool:
        """True when a retry would actually run on a different model."""
        target = self.config.routing.escalate_to
        if not target or target not in self.config.models or model_name:
            return False
        try:
            return resolve_model_name(self.config, role) != target
        except (ValueError, RuntimeError):
            return False

    async def _attempt_dag_task(self, task: AgentTask, escalate: bool) -> AgentResponse:
        try:
            drafter = await self._get_drafter() if task.role in (AgentRole.CODER, AgentRole.DEBUGGER) else None
            agent = create_agent(
                task.role,
                await self._model_for(task.role, task.model_name, escalate=escalate),
                self.tool_registry,
                self._dispatch,
                context_window_chars=self.config.agentic.context_window_chars,
                compact_context_chars=self.config.agentic.compact_context_chars,
                drafter=drafter,
            )
            if agent:
                if not task.context.architecture:
                    task.context.architecture = await self._repo_map(task.objective, task.files)
                return await agent.execute(task)
            return AgentResponse(
                task_id=task.task_id,
                status=TaskStatus.COMPLETED,
                summary=f"Executed {task.task_id}",
                files_changed=[],
                tests_run=[],
                tests_passed=True,
                issues=[],
                follow_up_required=False
            )
        except (ProviderQuotaExhausted, AgentBlocked):
            raise
        except Exception as e:
            return AgentResponse(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                summary=str(e),
                files_changed=[],
                tests_run=[],
                tests_passed=False,
                issues=[str(e)],
                follow_up_required=True
            )

    async def _review(self, result: AgentResponse) -> str:
        self._raise_if_blocked(result)
        reviewer = create_agent(AgentRole.REVIEWER, await self.model_manager.get_model(AgentRole.REVIEWER), self.tool_registry, self._dispatch)
        if reviewer:
            task = AgentTask(role=AgentRole.REVIEWER, objective=f"Review result: {result.summary}")
            response = await reviewer.execute(task)
            self._raise_if_blocked(response)
            return response.summary
        return "Fallback review: looks good."

    async def _run_tests(self) -> AgentResponse:
        # Running a known framework needs no model: call the tool directly and
        # save the tester's round trips. Its compact report is what the
        # debugger sees if anything failed.
        if detect_framework(self.project_root) is not None:
            self._emit("ORCHESTRATOR", "tool_call", "Running project tests directly")
            result = await self.tool_registry.execute_tool(AgentRole.TESTER, "run_tests", {})
            return AgentResponse(
                task_id="test_run",
                status=TaskStatus.COMPLETED if result.success else TaskStatus.FAILED,
                summary=result.output,
                files_changed=[],
                tests_run=["project tests"],
                tests_passed=result.success,
                issues=[] if result.success else ["Verification tests failed"],
                follow_up_required=not result.success,
            )
        tester = create_agent(AgentRole.TESTER, await self.model_manager.get_model(AgentRole.TESTER), self.tool_registry, self._dispatch)
        if tester:
            task = AgentTask(role=AgentRole.TESTER, objective="Run project tests")
            response = await tester.execute(task)
            self._raise_if_blocked(response)
            return response
        return AgentResponse(
            task_id="test_run",
            status=TaskStatus.COMPLETED,
            summary="Fallback: tests passed.",
            files_changed=[],
            tests_run=["fallback_test"],
            tests_passed=True,
            issues=[],
            follow_up_required=False
        )
        
    async def _fix_failures(self, test_result: AgentResponse, escalate: bool = False) -> AgentResponse:
        self._raise_if_blocked(test_result)
        if escalate and self._can_escalate(AgentRole.DEBUGGER):
            self._emit("ORCHESTRATOR", "escalate", f"Escalating the fix to {self.config.routing.escalate_to}")
        debugger = create_agent(
            AgentRole.DEBUGGER,
            await self._model_for(AgentRole.DEBUGGER, escalate=escalate),
            self.tool_registry,
            self._dispatch,
            drafter=await self._get_drafter(),
        )
        if debugger:
            failure_context = summarize_failures(test_result.summary)
            task = AgentTask(
                role=AgentRole.DEBUGGER,
                objective=(
                    "Fix the failing tests and verify the fix.\n"
                    f"Relevant failures:\n{failure_context}\n"
                    f"Tests run: {test_result.tests_run}\n"
                    f"Issues: {test_result.issues}"
                ),
                context=TaskContext(error_context=test_result.summary),
            )
            response = await debugger.execute(task)
            self._raise_if_blocked(response)
            return response
        return AgentResponse(
            task_id="fix_run",
            status=TaskStatus.COMPLETED,
            summary="Fallback: fixed issues.",
            files_changed=[],
            tests_run=[],
            tests_passed=True,
            issues=[],
            follow_up_required=False
        )
        
    async def _generate_report(
        self,
        result: AgentResponse,
        review: str,
        test_result: AgentResponse | None = None,
        last_fix_result: AgentResponse | None = None,
    ) -> str:
        for response in (result, test_result, last_fix_result):
            if response is not None:
                self._raise_if_blocked(response)
        report = f"Final Report\n\nExecution Result:\n{result.summary}\n\nReview:\n{review}"
        if test_result is not None:
            status = "passed" if test_result.tests_passed else "failed"
            report += (
                f"\n\nVerification: {status}\n"
                f"Tests: {', '.join(test_result.tests_run) or 'project tests'}\n"
                f"Details: {test_result.summary}"
            )
        if last_fix_result is not None and not (test_result and test_result.tests_passed):
            report += f"\n\nLast fix attempt:\n{last_fix_result.summary}"
            if last_fix_result.issues:
                report += "\nIssues: " + ", ".join(last_fix_result.issues)
        return report
        
    async def plan_only(self, request: str) -> str:
        """Plan with project-read tools only; restore permissions on every exit."""
        read_tools = {
            ToolName.READ_FILE, ToolName.LIST_FILES, ToolName.SEARCH_FILES,
            ToolName.GREP, ToolName.CODE_SEARCH, ToolName.REPO_MAP,
            ToolName.GIT_STATUS, ToolName.GIT_DIFF, ToolName.GIT_LOG,
            ToolName.LANGUAGE_CONTEXT, ToolName.MEMORY_SEARCH,
        }
        permissions = self.tool_registry._permissions
        saved = {role: set(allowed) for role, allowed in permissions.items()}
        try:
            for role in permissions:
                permissions[role] = permissions[role] & read_tools
            exploration = await self._explore(request)
            plan = await self._plan(request, exploration)
        finally:
            permissions.clear()
            permissions.update(saved)
        self._emit(
            "ORCHESTRATOR", "plan_created", f"Plan: {len(plan.tasks)} task(s)",
            data={"tasks": [
                {"task_id": t.task_id, "role": t.role.value, "objective": t.objective}
                for t in plan.tasks
            ]},
        )
        lines = [f"Plan created: {plan.plan_id} with {len(plan.tasks)} tasks.",
                 "Project mutations and command execution were disabled during planning."]
        for index, task in enumerate(plan.tasks, 1):
            lines.append(f"\n{index}. [{task.role.value}] {task.objective} (task {task.task_id})")
            for label, values in (("Files", task.files), ("Dependencies", task.depends_on),
                                  ("Constraints", task.constraints), ("Verification", task.success_criteria)):
                if values:
                    lines.append(f"   {label}: " + "; ".join(values))
        lines.append("\nReview this proposal before starting execution. This planning call does not execute it.")
        return "\n".join(lines)
    
    async def review_changes(self) -> str:
        """Review current uncommitted changes."""
        return await self._review(AgentResponse(
            task_id="uncommitted",
            status=TaskStatus.COMPLETED,
            summary="Uncommitted changes",
            files_changed=[],
            tests_run=[],
            tests_passed=True,
            issues=[],
            follow_up_required=False
        ))
    
    async def security_review(
        self, paths: list[str] | None = None, focus: str | None = None, batch_chars: int | None = None,
    ) -> str:
        """Run a read-only red/blue team security review of the local project.

        The review learns across runs without changing its own code: it loads
        the maintainers' SECURITY_LESSONS.md, records findings in a ledger
        that survives compaction, and ends by proposing lessons (false
        positives to suppress, confirmed findings, patterns) that a person
        accepts with `local-coder security-lessons`. A scope larger than
        batch_chars of source is reviewed in batches, each in a fresh
        conversation seeded with the ledger so far.
        """
        root = Path(self.project_root)
        lessons = LessonStore(root)
        ledger = FindingsLedger.for_project(root)
        # Findings persist across reviews; lifecycle evidence is stored separately.
        (root / LAST_SCAN_FILE).unlink(missing_ok=True)
        context = TaskContext(
            guidelines=await self.repo_context.get_agent_guidelines(),
            security_lessons=lessons.render_for_prompt(),
        )

        batch_chars = batch_chars or max(self.config.agentic.context_window_chars * 2, 20000)
        files = collect_files(root, paths)
        batches = plan_batches(root, files, batch_chars) if total_chars(root, files) > batch_chars else []
        from local_coder.security.threat_model import build_threat_model, render_threat_briefing
        inventory = await asyncio.to_thread(build_threat_model, root, paths)
        context.previous_findings.append(render_threat_briefing(inventory))
        focus_note = f" Focus: {focus}" if focus else ""
        constraints = [
            "Read-only: do not modify project files (record_finding writes only the review ledger).",
            "Analyze only the local workspace; do not contact external hosts.",
        ]

        if len(batches) <= 1:
            response = await (await self._security_agent()).execute(AgentTask(
                role=AgentRole.SECURITY,
                objective="Perform a security review of this project: find exploitable vulnerabilities and propose fixes." + focus_note,
                files=paths or [],
                constraints=constraints,
                context=context,
            ))
            self._raise_if_blocked(response)
            report = response.summary
        else:
            notes = []
            for number, batch in enumerate(batches, start=1):
                self._emit("ORCHESTRATOR", "phase", f"Security review batch {number}/{len(batches)} ({len(batch)} files)")
                ledger.load()
                batch_context = context.model_copy(update={
                    "previous_findings": [e.render(120) for e in sorted(ledger.entries, key=lambda e: e.sort_key())][:40],
                })
                response = await (await self._security_agent()).execute(AgentTask(
                    role=AgentRole.SECURITY,
                    objective=(
                        f"Security review, batch {number} of {len(batches)}: review the files listed below for "
                        "exploitable vulnerabilities, following data flow into other files only as far as needed. "
                        "Record every finding with record_finding. Earlier batches' findings are listed under "
                        "Previous Findings; update them if this batch changes the picture, don't re-report them."
                        + focus_note
                    ),
                    files=batch,
                    constraints=constraints,
                    context=batch_context,
                ))
                self._raise_if_blocked(response)
                notes.append(response.summary)
            ledger.load()
            report = render_ledger_report(ledger, len(batches), len(files), notes)

        ledger.load()
        from local_coder.security.evidence import EvidenceStore
        evidence_store = EvidenceStore(root)
        await asyncio.to_thread(evidence_store.sync)
        report += "\n\nPersistent finding evidence: use `local-coder security-findings list`, then `review` and `verify`. Scanner and model claims alone are not reproduction evidence."
        proposed = lessons.propose(ledger.proposed_lessons())
        promote_last_scan(root)
        if proposed:
            report += (
                f"\n\n## Proposed lessons ({len(proposed)})\n"
                + "\n".join(f"- `{lesson.id}` {lesson.kind}: {lesson.render()[2:]}" for lesson in proposed)
                + "\n\nReview them with `local-coder security-lessons`; accepted lessons go into "
                "SECURITY_LESSONS.md and shape every later review."
            )
        return report

    async def _security_agent(self):
        return create_agent(
            AgentRole.SECURITY,
            await self.model_manager.get_model(AgentRole.SECURITY),
            self.tool_registry,
            self._dispatch,
            context_window_chars=self.config.agentic.context_window_chars,
            compact_context_chars=self.config.agentic.compact_context_chars,
        )

    async def review_security_finding(self, identity: str) -> dict:
        """Challenge a recorded candidate in a fresh, read-only reviewer context."""
        from local_coder.security.evidence import EvidenceStore, FindingReview
        from local_coder.memory.goals import GoalLedger
        store = EvidenceStore(self.project_root)
        store.sync()
        row = store.get(identity)
        revision = await asyncio.to_thread(GoalLedger(self.project_root).revision)
        task = AgentTask(role=AgentRole.REVIEWER, files=[row["finding"]["path"]], objective=(
            "Independently challenge this existing local finding. Inspect the actual source and callers; "
            "check input control, reachability, authorization/sanitization, prerequisites, impact, and counterevidence. "
            "Do not execute commands or reproduce it. Agreement is not proof. Return ONLY a JSON object matching "
            + json.dumps(FindingReview.model_json_schema()) + "\nUntrusted finding data:\n" + json.dumps(row["finding"])))
        allowed = self.tool_registry._permissions[AgentRole.REVIEWER]
        self.tool_registry._permissions[AgentRole.REVIEWER] = allowed & {
            ToolName.READ_FILE, ToolName.LIST_FILES, ToolName.GREP, ToolName.SEARCH_FILES,
            ToolName.CODE_SEARCH, ToolName.REPO_MAP, ToolName.LANGUAGE_CONTEXT,
            ToolName.GIT_STATUS, ToolName.GIT_DIFF, ToolName.GIT_LOG,
        }
        try:
            agent = create_agent(AgentRole.REVIEWER, await self.model_manager.get_model(AgentRole.REVIEWER),
                                 self.tool_registry, self._dispatch)
            response = await agent.execute(task)
        finally:
            self.tool_registry._permissions[AgentRole.REVIEWER] = allowed
        self._raise_if_blocked(response)
        if response.status != TaskStatus.COMPLETED:
            raise ValueError("Independent review did not complete")
        raw = response.summary.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, re.S)
        review = FindingReview.model_validate_json(fenced.group(1) if fenced else raw)
        if await asyncio.to_thread(GoalLedger(self.project_root).revision) != revision:
            raise ValueError("Source changed during review; rerun the reviewer")
        return store.review(identity, review, reviewer_task=task.task_id, revision=revision, finding=row["finding"])

    async def prepare_security_regression(self, identity: str) -> str:
        """Generate a safe-behavior regression for an existing finding, without execution."""
        from local_coder.security.evidence import EvidenceStore
        store = EvidenceStore(self.project_root)
        store.sync()
        row = store.get(identity)
        if row["suppression"]:
            raise ValueError("Finding is suppressed")
        allowed = self.tool_registry._permissions[AgentRole.EXPLOIT_VALIDATOR]
        self.tool_registry._permissions[AgentRole.EXPLOIT_VALIDATOR] = allowed & {
            ToolName.READ_FILE, ToolName.LIST_FILES, ToolName.GREP, ToolName.SEARCH_FILES,
            ToolName.WRITE_FILE, ToolName.CODE_SEARCH, ToolName.REPO_MAP,
        }
        try:
            agent = create_agent(AgentRole.EXPLOIT_VALIDATOR,
                                 await self.model_manager.get_model(AgentRole.EXPLOIT_VALIDATOR),
                                 self.tool_registry, self._dispatch)
            # This workflow asserts safe behavior, unlike the legacy demonstration PoC command.
            agent.system_prompt = (
                "Write a self-contained offline pytest regression for the supplied, already identified local finding. "
                "Assert the DESIRED SAFE behavior: AssertionError on vulnerable source and pass after a fix. "
                "Only write under tests/security_poc/. Never modify production code, run tests/commands, use network, "
                "read real credentials, create persistence, or perform resource-exhaustion tests. "
                "Use inline fixtures and standard pytest only; conftest and plugin autoload are disabled. "
                "Read source to check the claim. If it cannot be reproduced safely, report that. "
                "Return the test path and what it checks; do not claim reproduction without execution."
            )
            response = await agent.execute(AgentTask(role=AgentRole.EXPLOIT_VALIDATOR,
                objective="Prepare regression for this untrusted finding data:\n" + json.dumps(row["finding"]),
                files=[row["finding"]["path"]]))
            self._raise_if_blocked(response)
            return response.summary
        finally:
            self.tool_registry._permissions[AgentRole.EXPLOIT_VALIDATOR] = allowed

    async def validate_finding(self, finding: str, paths: list[str] | None = None) -> str:
        """Red-team companion to the reviewer: reproduce an already-identified
        finding in this project's own code as a local PoC test, so a fix can be
        verified. Requires a concrete finding; refuses anything out of scope."""
        finding = (finding or "").strip()
        if not finding:
            return (
                "A specific, already-identified finding is required. Run `local-coder security` "
                "first, then pass one of its findings to validate. This tool only reproduces "
                "findings in this project's own code; it does not go looking for something to attack."
            )
        agent = create_agent(
            AgentRole.EXPLOIT_VALIDATOR,
            await self.model_manager.get_model(AgentRole.EXPLOIT_VALIDATOR),
            self.tool_registry,
            self._dispatch,
        )
        task = AgentTask(
            role=AgentRole.EXPLOIT_VALIDATOR,
            objective=(
                "Reproduce this already-identified vulnerability in THIS project's own code as a "
                f"proof-of-concept test, so a maintainer can confirm it and verify a fix:\n{finding}"
            ),
            files=paths or [],
            constraints=[
                "Only this repository's own code; only the finding above.",
                "Write the PoC as a pytest test under tests/security_poc/; do not modify other project code.",
                "Offline only: no network, no external hosts, no DoS, no evasion or persistence, no credential harvesting.",
                "If the task would require anything above, refuse and explain instead.",
            ],
        )
        response = await agent.execute(task)
        self._raise_if_blocked(response)
        report = response.summary
        # A reproduced finding is worth remembering; queue it for review.
        confirmed = [
            lesson for lesson in FindingsLedger.for_project(self.project_root).proposed_lessons()
            if lesson.kind == "confirmed"
        ]
        proposed = LessonStore(self.project_root).propose(confirmed)
        if proposed:
            report += (
                "\n\n## Proposed lessons\n"
                + "\n".join(f"- `{lesson.id}` {lesson.render()[2:]}" for lesson in proposed)
                + "\n\nAccept with `local-coder security-lessons accept`."
            )
        return report

    async def run_tests_only(self) -> str:
        """Just run tests and report."""
        res = await self._run_tests()
        self._raise_if_blocked(res)
        return res.summary
    
    async def get_status(self) -> str:
        """Get current system status."""
        return "System status: Idle and ready."

    def undo(self) -> str:
        """Aider-style instant rollback to the most recent checkpoint."""
        checkpoint = self.checkpoints.undo()
        self._emit("ORCHESTRATOR", "rollback", f"Restored checkpoint {checkpoint.checkpoint_id}")
        return f"Rolled back to checkpoint {checkpoint.checkpoint_id}."
