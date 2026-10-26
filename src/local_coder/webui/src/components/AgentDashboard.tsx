/* src/components/AgentDashboard.tsx */
import * as React from "react";

interface AgentDashboardProps {
  agents: AgentStatus[];
  onAgentSelect: (role: string) => void;
  running: boolean;
}

interface AgentStatus {
  role: string;
  name: string;
  status: "idle" | "running" | "completed" | "failed";
  progress: number;
  taskId?: string;
  resources: {
    cpu: number;
    memory: number;
    gpu: number;
  };
}

export const AgentDashboard: React.FC<AgentDashboardProps> = ({
  agents,
  onAgentSelect,
  running,
}) => {
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-medium">Agents</h3>
        <span className="text-xs text-muted capitalize">
          {running ? "Active" : "Idle"}
        </span>
      </div>

      {agents.map((agent) => (
        <div
          key={agent.role}
          className="card p-4 flex items-start gap-3"
        >
          <div className="flex-0 shrink-0 w-8 h-8 rounded-md flex items-center justify-center">
            <span className="text-sm font-bold">
              {agent.name.slice(0, 1)}
            </span>
          </div>
          <div className="flex-1 min-w-0">
            <div className="font-medium truncate">{agent.name}</div>
            <div className="text-xs text-muted">{agent.role}</div>
          </div>
          <div className="flex-0 shrink-0">
            <span 
              className={`px-2 py-1 text-xs rounded ${
                agent.status === "running" 
                  ? "bg-accent text-primary-foreground" 
                  : agent.status === "completed" 
                    ? "bg-green-100 text-green-800" 
                    : "bg-red-100 text-red-800"
              }`}
            >
              {agent.status}
            </span>
          </div>
        </div>
      ))}

      {!running && (
        <div>
          <button
            onClick={() => onAgentSelect("coder")}
            className="btn-secondary w-full mt-2 text-sm"
          >
            Start Coder Agent
          </button>
          <button
            onClick={() => onAgentSelect("planner")}
            className="btn-secondary w-full mt-1 text-sm"
          >
            Start Planner Agent
          </button>
        </div>
      )}
    </div>
  );
};