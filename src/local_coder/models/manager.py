"""Model manager for loading and handling backends."""
from __future__ import annotations
import asyncio
import subprocess
from typing import Dict, Optional

from local_coder.types import ProjectConfig, ModelConfig, AgentRole, ModelBackend, GPUStatus
from local_coder.models.base import LocalModel
from local_coder.models.ollama import OllamaBackend
from local_coder.models.openai_compat import OpenAICompatibleBackend


class ModelManager:
    """Manages model instances, queues, and resources."""
    
    def __init__(self, config: ProjectConfig):
        self.config = config
        self._models: Dict[str, LocalModel] = {}
        self._lock = asyncio.Lock()
        
        # Concurrency limit logic
        total_slots = config.resources.max_concurrent_gpu_agents + config.resources.max_concurrent_cpu_agents
        self._semaphore = asyncio.Semaphore(max(1, total_slots))

    async def initialize(self):
        """Initialize all configured models."""
        async with self._lock:
            for name, model_config in self.config.models.items():
                if name not in self._models:
                    self._models[name] = self._create_backend(model_config)

    def _create_backend(self, config: ModelConfig) -> LocalModel:
        """Factory for creating backend instances."""
        if config.backend == ModelBackend.OLLAMA:
            return OllamaBackend(config)
        elif config.backend == ModelBackend.OPENAI_COMPAT:
            return OpenAICompatibleBackend(config)
        elif config.backend == ModelBackend.LLAMACPP:
            # llama.cpp server uses an OpenAI-compatible API natively
            return OpenAICompatibleBackend(config)
        else:
            raise ValueError(f"Unsupported backend type: {config.backend}")

    async def get_model(self, role: AgentRole | str) -> LocalModel:
        """Get the appropriate model for an agent role."""
        if not self._models:
            await self.initialize()

        async with self._lock:
            # Map role to model name if possible, else fallback to 'default' or first available
            role_name = role.value if isinstance(role, AgentRole) else role
            
            # Very basic routing:
            if role_name in self.config.models:
                model_name = role_name
            elif "default" in self.config.models:
                model_name = "default"
            elif self._models:
                model_name = next(iter(self._models.keys()))
            else:
                raise RuntimeError("No models configured.")
                
            return self._models[model_name]

    async def check_gpu_status(self) -> GPUStatus:
        """Check GPU status via nvidia-smi if available."""
        try:
            # Run nvidia-smi via subprocess (in a thread to avoid blocking loop)
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-gpu=memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            
            if proc.returncode == 0:
                output = stdout.decode().strip().split('\n')[0]
                total, used, free, util, temp = map(float, output.split(', '))
                return GPUStatus(
                    total_vram_mb=int(total),
                    used_vram_mb=int(used),
                    free_vram_mb=int(free),
                    gpu_utilization_percent=util,
                    temperature_c=int(temp)
                )
        except (FileNotFoundError, ValueError, Exception):
            # nvidia-smi not available or parsing failed
            pass
            
        return GPUStatus()

    async def acquire_slot(self):
        """Wait for an available concurrency slot."""
        await self._semaphore.acquire()
        
    def release_slot(self):
        """Release a concurrency slot."""
        self._semaphore.release()
        
    async def close_all(self):
        """Close all instantiated model clients."""
        async with self._lock:
            for model in self._models.values():
                if hasattr(model, "close"):
                    await model.close()
            self._models.clear()
