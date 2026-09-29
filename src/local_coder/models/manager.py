"""Model manager for loading and handling backends."""
from __future__ import annotations
import asyncio
from typing import Dict

from local_coder.types import ProjectConfig, ModelConfig, AgentRole, ModelBackend, GPUStatus
from local_coder.models.base import LocalModel
from local_coder.models.ollama import OllamaBackend
from local_coder.models.openai_compat import OpenAICompatibleBackend
from local_coder.models.router import fallback_chain, resolve_model_name


class ModelManager:
    """Manages model instances, queues, and resources."""
    
    def __init__(self, config: ProjectConfig):
        self.config = config
        self._models: Dict[str, LocalModel] = {}
        self._lock = asyncio.Lock()
        # Reachability of models that have fallbacks, probed once per manager.
        self._availability: Dict[str, bool] = {}
        
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

    async def get_model(
        self,
        role: AgentRole | str,
        model_name: str | None = None,
        *,
        escalate: bool = False,
    ) -> LocalModel:
        """Get the model for an agent role (see models.router for the order).

        escalate=True asks for routing.escalate_to, used when a step is being
        retried after a failure. When routing.fallbacks lists alternatives
        for the chosen model, its server is probed once per run and the first
        reachable model in the chain is used instead.
        """
        if not self._models:
            await self.initialize()

        selected = resolve_model_name(self.config, role, model_name, escalate=escalate)
        chain = fallback_chain(self.config, selected)
        if len(chain) == 1:
            return self._models[selected]
        for name in chain:
            if await self._is_reachable(name):
                return self._models[name]
        return self._models[selected]

    async def _is_reachable(self, name: str) -> bool:
        async with self._lock:
            if name in self._availability:
                return self._availability[name]
        try:
            available = bool(await self._models[name].is_available())
        except Exception:
            available = False
        async with self._lock:
            self._availability[name] = available
        return available

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
