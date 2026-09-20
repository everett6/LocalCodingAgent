import yaml
from pathlib import Path
from typing import Optional

from local_coder.types import ProjectConfig, ModelConfig, ModelBackend, ResourceConfig, VerificationConfig, ApprovalConfig, AgenticConfig

def load_config(config_path: Optional[str] = None, project_root: str = ".") -> ProjectConfig:
    """Load configuration from YAML file.
    
    Search order:
    1. Explicit path
    2. .local-coder/config.yaml in project root
    3. config/config.yaml in project root  
    4. Default config
    """
    search_paths = []
    if config_path:
        search_paths.append(Path(config_path))
    
    search_paths.extend([
        Path(project_root) / ".local-coder" / "config.yaml",
        Path(project_root) / "config" / "config.yaml",
    ])
    
    data = {}
    for path in search_paths:
        if path.is_file():
            try:
                with open(path, "r") as f:
                    data = yaml.safe_load(f) or {}
                break
            except Exception:
                pass
                
    # Accept the keyed mapping used by the project config and legacy lists.
    models_data = data.get("models", {})
    if isinstance(models_data, list):
        model_items = ((m.get("name", "model"), m) for m in models_data)
    else:
        model_items = models_data.items()

    models = {}
    for name, model_data in model_items:
        backend_value = model_data.get("backend", ModelBackend.OLLAMA.value)
        try:
            backend = ModelBackend(backend_value)
        except ValueError:
            backend = ModelBackend.OLLAMA
        models[name] = ModelConfig(
            model_id=model_data.get("model_id", "default"),
            name=model_data.get("name", name),
            backend=backend,
            base_url=model_data.get("base_url", "http://localhost:11434"),
            context_length=model_data.get("context_length", 8192),
            temperature=model_data.get("temperature", 0.2),
            max_tokens=model_data.get("max_tokens", 4096),
            quantization=model_data.get("quantization"),
            gpu_layers=model_data.get("gpu_layers"),
            estimated_vram_mb=model_data.get("estimated_vram_mb"),
        )
        
    resources_data = data.get("resources", {})
    resources = ResourceConfig(
        max_concurrent_gpu_agents=resources_data.get("max_concurrent_gpu_agents", 1),
        max_concurrent_cpu_agents=resources_data.get("max_concurrent_cpu_agents", 2),
        require_gpu=resources_data.get("require_gpu", False)
    )
    
    verification_data = data.get("verification", {})
    verification = VerificationConfig(
        run_tests_after_changes=verification_data.get("run_tests_after_changes", True),
        max_fix_iterations=verification_data.get("max_fix_iterations", 3),
        test_command=verification_data.get("test_command")
    )
    
    approval_data = data.get("approval", {})
    approval = ApprovalConfig(
        require_approval_for_commands=approval_data.get("require_approval_for_commands", False),
        require_approval_for_commits=approval_data.get("require_approval_for_commits", False)
    )

    agentic_data = data.get("agentic", {})
    agentic = AgenticConfig(
        role_models=agentic_data.get("role_models", {}),
        context_window_chars=agentic_data.get("context_window_chars", 24000),
        compact_context_chars=agentic_data.get("compact_context_chars", 12000),
        max_parallel_agents=agentic_data.get("max_parallel_agents", 1),
    )
    
    return ProjectConfig(
        models=models,
        resources=resources,
        verification=verification,
        approval=approval,
        agentic=agentic,
        state_dir=data.get("state_dir", ".local-coder")
    )
