import time
import uuid
from typing import Optional, List, Any
from pydantic import BaseModel

from local_coder.types import Message

class DraftPrediction(BaseModel):
    prediction_id: str
    prediction_type: str  # "edit", "action", "completion"
    content: str
    file_path: Optional[str] = None
    confidence: float = 0.0
    latency_ms: float = 0.0


class DraftStats(BaseModel):
    total: int = 0
    accepted: int = 0
    rejected: int = 0
    total_latency_ms: float = 0.0
    
    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.total if self.total > 0 else 0.0
    
    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total if self.total > 0 else 0.0


class SpeculativeDrafter:
    """Predictive drafting using a small fast model.
    
    Generates candidate patches/completions while the main agent reasons.
    The main agent validates and accepts/rejects predictions.
    """
    
    def __init__(self, model: Any, enabled: bool = True, min_confidence: float = 0.5):
        # model is typically a LocalModel or an adapter representing the small fast model
        self._model = model
        self._enabled = enabled
        self._min_confidence = min_confidence
        self._stats = DraftStats()
    
    async def predict_edit(
        self,
        file_path: str,
        file_content: str,
        objective: str,
        context: str = "",
    ) -> Optional[DraftPrediction]:
        """Predict likely edits for a file given an objective."""
        if not self.should_predict:
            return None
            
        start_time = time.time()
        
        prompt = f"Objective: {objective}\nContext: {context}\nFile: {file_path}\nContent:\n{file_content}\n\nProvide the required patch or edit:"
        
        try:
            response = await self._model.generate([Message(role="user", content=prompt)])
            content = response.content if hasattr(response, 'content') else str(response)
            
            latency = (time.time() - start_time) * 1000
            
            prediction = DraftPrediction(
                prediction_id=str(uuid.uuid4()),
                prediction_type="edit",
                content=content,
                file_path=file_path,
                confidence=0.8,
                latency_ms=latency
            )
            
            self._stats.total += 1
            self._stats.total_latency_ms += latency
            
            if prediction.confidence >= self._min_confidence:
                return prediction
            return None
            
        except Exception:
            return None
    
    async def predict_next_action(
        self,
        conversation_history: List[Any],
        available_tools: List[str],
    ) -> Optional[DraftPrediction]:
        """Predict the next tool call an agent will make."""
        if not self.should_predict:
            return None
            
        start_time = time.time()
        
        try:
            prompt = f"History: {conversation_history}\nTools: {available_tools}\nPredict next tool:"
            response = await self._model.generate([Message(role="user", content=prompt)])
            content = response.content if hasattr(response, 'content') else str(response)
            
            latency = (time.time() - start_time) * 1000
            
            prediction = DraftPrediction(
                prediction_id=str(uuid.uuid4()),
                prediction_type="action",
                content=content,
                confidence=0.7,
                latency_ms=latency
            )
            
            self._stats.total += 1
            self._stats.total_latency_ms += latency
            
            if prediction.confidence >= self._min_confidence:
                return prediction
            return None
        except Exception:
            return None
    
    def accept_prediction(self, prediction_id: str) -> None:
        """Record that a prediction was accepted."""
        self._stats.accepted += 1
    
    def reject_prediction(self, prediction_id: str) -> None:
        self._stats.rejected += 1
    
    @property
    def should_predict(self) -> bool:
        """Whether prediction is worth doing based on hit rate."""
        if not self._enabled:
            return False
        if self._stats.total < 10:
            return True  # Not enough data
        return self._stats.acceptance_rate > 0.3  # Only if >30% acceptance
    
    def get_stats(self) -> DraftStats:
        return self._stats
