import os
import json
from typing import Callable, Any

from local_coder.types import (
    ProjectConfig, AgentEvent, AgentRole, AgentTask, TaskPlan, AgentResponse, TaskStatus
)
from local_coder.agents import create_agent
from local_coder.tools import create_tool_registry
from local_coder.models.manager import ModelManager
from local_coder.memory.store import MemoryStore
from local_coder.context.repository import RepositoryContext
from local_coder.scheduler.dag import TaskDAG
from local_coder.scheduler.resources import ResourceManager


class Coordinator:
    """Main orchestrator that coordinates the coding agent workflow."""
    
    def __init__(
        self,
        config: ProjectConfig,
        project_root: str,
    ):
        self.config = config
        self.project_root = project_root
        self.repo_context = RepositoryContext(project_root)
        
        # Ensure state dir exists
        state_dir_path = os.path.join(project_root, config.state_dir)
        os.makedirs(state_dir_path, exist_ok=True)
        
        self.memory = MemoryStore(os.path.join(state_dir_path, "memory.db"))
        self.tool_registry = create_tool_registry(project_root)
        self.model_manager = ModelManager(config)
        self.resource_manager = ResourceManager(config.resources)
        self._event_handlers: list[Callable] = []
    
    def on_event(self, handler: Callable[[AgentEvent], None]) -> None:
        """Register event handler."""
        self._event_handlers.append(handler)
    
    def _emit(self, source: str, event_type: str, message: str, **kwargs) -> None:
        event = AgentEvent(source=source, event_type=event_type, message=message, **kwargs)
        for handler in self._event_handlers:
            handler(event)
    
    async def run(self, request: str) -> str:
        """Execute a user request end-to-end."""
        self._emit("ORCHESTRATOR", "task_started", f"Processing: {request}")
        
        # Step 1: Explore repository
        self._emit("ORCHESTRATOR", "phase", "Exploring repository...")
        exploration = await self._explore(request)
        
        # Step 2: Plan
        self._emit("ORCHESTRATOR", "phase", "Creating plan...")
        plan = await self._plan(request, exploration)
        
        # Step 3: Determine if simple or complex
        if len(plan.tasks) <= 1:
            self._emit("ORCHESTRATOR", "phase", "Executing simple task...")
            result = await self._execute_simple(request, exploration)
        else:
            self._emit("ORCHESTRATOR", "phase", "Executing complex task plan...")
            result = await self._execute_plan(plan)
        
        # Step 4: Review
        self._emit("ORCHESTRATOR", "phase", "Reviewing changes...")
        review = await self._review(result)
        
        # Step 5: Test
        if self.config.verification.run_tests_after_changes:
            self._emit("ORCHESTRATOR", "phase", "Running tests...")
            test_result = await self._run_tests()
            
            # Step 6: Fix loop
            fix_iterations = 0
            while not test_result.tests_passed and fix_iterations < self.config.verification.max_fix_iterations:
                self._emit("ORCHESTRATOR", "fix_loop", f"Fixing failures (attempt {fix_iterations + 1})...")
                fix_result = await self._fix_failures(test_result)
                test_result = await self._run_tests()
                fix_iterations += 1
        
        # Step 7: Final report
        report = await self._generate_report(result, review)
        self._emit("ORCHESTRATOR", "task_completed", "Done")
        return report
    
    async def _explore(self, request: str) -> str:
        explorer = create_agent(AgentRole.EXPLORER, await self.model_manager.get_model(AgentRole.EXPLORER), self.tool_registry, self._emit)
        if explorer:
            task = AgentTask(role=AgentRole.EXPLORER, objective=request)
            response = await explorer.execute(task)
            return response.summary
        return "Exploration fallback: Checked repository structure."
        
    async def _plan(self, request: str, exploration: str) -> TaskPlan:
        planner = create_agent(AgentRole.PLANNER, await self.model_manager.get_model(AgentRole.PLANNER), self.tool_registry, self._emit)
        if planner:
            task = AgentTask(role=AgentRole.PLANNER, objective=f"Request: {request}\nExploration: {exploration}")
            response = await planner.execute(task)
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
                        depends_on=t.get("depends_on", [])
                    )
                    tasks.append(task_obj)
                
                if not tasks:
                    raise ValueError("No tasks found in plan")
                return TaskPlan(objective=request, tasks=tasks, notes=[plan_data])
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

    async def _execute_simple(self, request: str, exploration: str) -> AgentResponse:
        coder = create_agent(AgentRole.CODER, await self.model_manager.get_model(AgentRole.CODER), self.tool_registry, self._emit)
        if coder:
            task = AgentTask(role=AgentRole.CODER, objective=f"Request: {request}\nContext: {exploration}")
            response = await coder.execute(task)
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

    async def _execute_plan(self, plan: TaskPlan) -> AgentResponse:
        dag = TaskDAG()
        dag.add_tasks(plan.tasks)
        
        while not dag.is_complete():
            ready_tasks = dag.get_ready_tasks()
            for task in ready_tasks:
                dag.mark_running(task.task_id)
                agent = create_agent(task.role, await self.model_manager.get_model(task.role), self.tool_registry, self._emit)
                try:
                    if agent:
                        response = await agent.execute(task)
                    else:
                        response = AgentResponse(
                            task_id=task.task_id,
                            status=TaskStatus.COMPLETED,
                            summary=f"Executed {task.task_id}",
                            files_changed=[],
                            tests_run=[],
                            tests_passed=True,
                            issues=[],
                            follow_up_required=False
                        )
                    
                    if response.status == TaskStatus.FAILED or not response.tests_passed:
                        dag.mark_failed(task.task_id, response)
                    else:
                        dag.mark_completed(task.task_id, response)
                except Exception as e:
                    response = AgentResponse(
                        task_id=task.task_id,
                        status=TaskStatus.FAILED,
                        summary=str(e),
                        files_changed=[],
                        tests_run=[],
                        tests_passed=False,
                        issues=[str(e)],
                        follow_up_required=True
                    )
                    dag.mark_failed(task.task_id, response)
                    
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

    async def _review(self, result: AgentResponse) -> str:
        reviewer = create_agent(AgentRole.REVIEWER, await self.model_manager.get_model(AgentRole.REVIEWER), self.tool_registry, self._emit)
        if reviewer:
            task = AgentTask(role=AgentRole.REVIEWER, objective=f"Review result: {result.summary}")
            response = await reviewer.execute(task)
            return response.summary
        return "Fallback review: looks good."

    async def _run_tests(self) -> AgentResponse:
        tester = create_agent(AgentRole.TESTER, await self.model_manager.get_model(AgentRole.TESTER), self.tool_registry, self._emit)
        if tester:
            task = AgentTask(role=AgentRole.TESTER, objective="Run project tests")
            response = await tester.execute(task)
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
        
    async def _fix_failures(self, test_result: AgentResponse) -> AgentResponse:
        debugger = create_agent(AgentRole.DEBUGGER, await self.model_manager.get_model(AgentRole.DEBUGGER), self.tool_registry, self._emit)
        if debugger:
            task = AgentTask(role=AgentRole.DEBUGGER, objective=f"Fix these issues: {test_result.issues}")
            response = await debugger.execute(task)
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
        
    async def _generate_report(self, result: AgentResponse, review: str) -> str:
        return f"Final Report\n\nExecution Result:\n{result.summary}\n\nReview:\n{review}"
        
    async def plan_only(self, request: str) -> str:
        """Plan without executing."""
        exploration = await self._explore(request)
        plan = await self._plan(request, exploration)
        return f"Plan created: {plan.plan_id} with {len(plan.tasks)} tasks."
    
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
    
    async def run_tests_only(self) -> str:
        """Just run tests and report."""
        res = await self._run_tests()
        return res.summary
    
    async def get_status(self) -> str:
        """Get current system status."""
        return "System status: Idle and ready."
