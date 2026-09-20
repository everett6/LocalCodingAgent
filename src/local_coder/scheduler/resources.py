import asyncio
import subprocess
import time
from typing import Optional

from local_coder.types import GPUStatus, ResourceConfig

class ResourceManager:
    """Manages GPU and CPU resources for model inference."""
    
    def __init__(self, config: ResourceConfig):
        self._config = config
        self._gpu_semaphore = asyncio.Semaphore(config.max_concurrent_gpu_agents)
        self._cpu_semaphore = asyncio.Semaphore(config.max_concurrent_cpu_agents)
    
    async def acquire_gpu(self) -> None:
        """Acquire GPU access (blocks if limit reached)."""
        await self._gpu_semaphore.acquire()
    
    def release_gpu(self) -> None:
        self._gpu_semaphore.release()
    
    async def acquire_cpu(self) -> None:
        await self._cpu_semaphore.acquire()
    
    def release_cpu(self) -> None:
        self._cpu_semaphore.release()
    
    async def get_gpu_status(self) -> GPUStatus:
        """Query nvidia-smi for current GPU status."""
        try:
            # Query nvidia-smi
            cmd = [
                "nvidia-smi", 
                "--query-gpu=memory.total,memory.free,memory.used,utilization.gpu", 
                "--format=csv,noheader,nounits"
            ]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, _ = await process.communicate()
            
            if process.returncode == 0 and stdout:
                lines = stdout.decode().strip().split('\n')
                if lines:
                    parts = [p.strip() for p in lines[0].split(',')]
                    if len(parts) >= 4:
                        total_vram = int(parts[0])
                        free_vram = int(parts[1])
                        used_vram = int(parts[2])
                        utilization = int(parts[3])
                        return GPUStatus(
                            available=True,
                            total_vram_mb=total_vram,
                            free_vram_mb=free_vram,
                            used_vram_mb=used_vram,
                            utilization_percent=utilization
                        )
        except Exception:
            pass
            
        return GPUStatus(
            available=False,
            total_vram_mb=0,
            free_vram_mb=0,
            used_vram_mb=0,
            utilization_percent=0
        )
    
    async def check_vram_available(self, required_mb: int) -> bool:
        """Check if enough VRAM is available."""
        status = await self.get_gpu_status()
        if not status.available:
            return False
        return status.free_vram_mb >= required_mb
    
    async def wait_for_vram(self, required_mb: int, timeout: int = 60) -> bool:
        """Wait until enough VRAM is available."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if await self.check_vram_available(required_mb):
                return True
            await asyncio.sleep(2)
        return False
