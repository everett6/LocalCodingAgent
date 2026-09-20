from typing import List, Dict, Optional
import time

from local_coder.types import AgentTask, TaskNode, AgentResponse, TaskStatus

class TaskDAG:
    """Directed acyclic graph for task scheduling."""
    
    def __init__(self):
        self._nodes: Dict[str, TaskNode] = {}
    
    def add_task(self, task: AgentTask) -> None:
        """Add a task to the DAG."""
        self._nodes[task.task_id] = TaskNode(
            task=task,
            status=TaskStatus.PENDING,
            result=None,
            retries=0,
            started_at=None,
            completed_at=None
        )
    
    def add_tasks(self, tasks: List[AgentTask]) -> None:
        """Add multiple tasks, validating dependencies."""
        for task in tasks:
            self.add_task(task)
    
    def get_ready_tasks(self) -> List[AgentTask]:
        """Get tasks whose dependencies are all completed."""
        ready = []
        for node in self._nodes.values():
            if node.status == TaskStatus.PENDING:
                all_deps_met = True
                for dep in node.task.depends_on:
                    if dep not in self._nodes or self._nodes[dep].status != TaskStatus.COMPLETED:
                        all_deps_met = False
                        break
                if all_deps_met:
                    ready.append(node.task)
        return ready
    
    def mark_running(self, task_id: str) -> None:
        if task_id in self._nodes:
            self._nodes[task_id].status = TaskStatus.RUNNING
            self._nodes[task_id].started_at = time.time()
            
    def mark_completed(self, task_id: str, result: AgentResponse) -> None:
        if task_id in self._nodes:
            self._nodes[task_id].status = TaskStatus.COMPLETED
            self._nodes[task_id].completed_at = time.time()
            self._nodes[task_id].result = result
            
    def mark_failed(self, task_id: str, result: AgentResponse) -> None:
        if task_id in self._nodes:
            self._nodes[task_id].status = TaskStatus.FAILED
            self._nodes[task_id].completed_at = time.time()
            self._nodes[task_id].result = result
    
    def is_complete(self) -> bool:
        """All tasks completed or failed."""
        return all(n.status in (TaskStatus.COMPLETED, TaskStatus.FAILED) for n in self._nodes.values())
    
    def has_failures(self) -> bool:
        return any(n.status == TaskStatus.FAILED for n in self._nodes.values())
    
    def get_node(self, task_id: str) -> Optional[TaskNode]:
        return self._nodes.get(task_id)

    def get_pending_task_ids(self) -> List[str]:
        """Task ids still waiting on a dependency (or stalled)."""
        return [tid for tid, node in self._nodes.items() if node.status == TaskStatus.PENDING]
    
    def get_all_results(self) -> List[AgentResponse]:
        return [n.result for n in self._nodes.values() if n.result is not None]
    
    def get_summary(self) -> str:
        """Human-readable summary of DAG state."""
        summary = ["Task DAG Summary:"]
        for task_id, node in self._nodes.items():
            summary.append(f"- {task_id}: {node.status.name}")
        return "\n".join(summary)
    
    def validate(self) -> List[str]:
        """Validate the DAG (check for cycles, missing deps)."""
        errors = []
        # Check for missing dependencies
        for node in self._nodes.values():
            for dep in node.task.depends_on:
                if dep not in self._nodes:
                    errors.append(f"Task {node.task.task_id} depends on non-existent task {dep}")
        
        # Use topological sort (DFS) to detect cycles
        visited = set()
        path = set()
        
        def visit(task_id: str):
            if task_id in path:
                errors.append(f"Cycle detected involving task {task_id}")
                return
            if task_id in visited:
                return
            visited.add(task_id)
            path.add(task_id)
            if task_id in self._nodes:
                for dep in self._nodes[task_id].task.depends_on:
                    visit(dep)
            path.remove(task_id)

        for task_id in self._nodes:
            visit(task_id)
            
        return errors
