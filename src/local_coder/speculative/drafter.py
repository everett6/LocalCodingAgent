import time
import uuid
import re
from collections import defaultdict
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
    total_reward: float = 0.0
    
    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.total if self.total > 0 else 0.0
    
    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total if self.total > 0 else 0.0


class DeltaAttention:
    """Application-level attention over changed and task-relevant code regions.

    This prioritizes deltas in the prompt; it does not modify transformer
    attention weights or claim native DeltaNet/speculative decoding support.
    """

    def focus(self, objective: str, content: str, max_lines: int = 80) -> str:
        objective_terms = {
            term.lower()
            for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", objective)
            if len(term) > 2
        }
        lines = content.splitlines()
        scored = []
        for index, line in enumerate(lines):
            score = 0
            if line.startswith(("+", "-")):
                score += 4
            if any(term in line.lower() for term in objective_terms):
                score += 2
            if line.strip():
                score += 1
            scored.append((score, index, line))
        selected = sorted(scored, key=lambda item: (-item[0], item[1]))[:max_lines]
        return "\n".join(f"{index + 1}: {line}" for _, index, line in sorted(selected, key=lambda item: item[1]))


class PredictionPolicy:
    """Online reward policy for deciding when predictive drafting pays off."""

    def __init__(self):
        self._successes: dict[str, float] = defaultdict(lambda: 1.0)
        self._failures: dict[str, float] = defaultdict(lambda: 1.0)

    def confidence(self, prediction_type: str) -> float:
        successes = self._successes[prediction_type]
        failures = self._failures[prediction_type]
        return successes / (successes + failures)

    def record(self, prediction_type: str, reward: float) -> None:
        bucket = self._successes if reward > 0 else self._failures
        bucket[prediction_type] += max(abs(reward), 0.1)

    def should_predict(self, prediction_type: str, minimum: float) -> bool:
        return self.confidence(prediction_type) >= minimum


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
        self._policy = PredictionPolicy()
        self._attention = DeltaAttention()
        self._pending: dict[str, str] = {}
    
    async def predict_edit(
        self,
        file_path: str,
        file_content: str,
        objective: str,
        context: str = "",
    ) -> Optional[DraftPrediction]:
        """Predict likely edits for a file given an objective."""
        if not self.should_predict("edit"):
            return None
            
        start_time = time.time()
        
        focused = self._attention.focus(objective, f"{context}\n{file_content}")
        prompt = f"Objective: {objective}\nFile: {file_path}\nDelta-focused context:\n{focused}\n\nProvide the required patch or edit:"
        
        try:
            response = await self._model.generate([Message(role="user", content=prompt)])
            content = response.content if hasattr(response, 'content') else str(response)
            
            latency = (time.time() - start_time) * 1000
            
            prediction = DraftPrediction(
                prediction_id=str(uuid.uuid4()),
                prediction_type="edit",
                content=content,
                file_path=file_path,
                confidence=self._policy.confidence("edit"),
                latency_ms=latency
            )
            
            self._stats.total += 1
            self._stats.total_latency_ms += latency
            self._pending[prediction.prediction_id] = prediction.prediction_type
            
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
        if not self.should_predict("action"):
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
                confidence=self._policy.confidence("action"),
                latency_ms=latency
            )
            
            self._stats.total += 1
            self._stats.total_latency_ms += latency
            self._pending[prediction.prediction_id] = prediction.prediction_type
            
            if prediction.confidence >= self._min_confidence:
                return prediction
            return None
        except Exception:
            return None
    
    def accept_prediction(self, prediction_id: str, latency_saved_ms: float = 1.0) -> None:
        """Record positive reward when a prediction was useful."""
        self._stats.accepted += 1
        prediction_type = self._pending.pop(prediction_id, "edit")
        self._stats.total_reward += max(latency_saved_ms, 0.1)
        self._policy.record(prediction_type, max(latency_saved_ms, 0.1))
    
    def reject_prediction(self, prediction_id: str, cost_ms: float = 1.0) -> None:
        """Record negative reward when a prediction was not useful."""
        self._stats.rejected += 1
        prediction_type = self._pending.pop(prediction_id, "edit")
        self._stats.total_reward -= max(cost_ms, 0.1)
        self._policy.record(prediction_type, -max(cost_ms, 0.1))
    
    @property
    def should_predict(self, prediction_type: str = "edit") -> bool:
        """Whether prediction is worth doing based on hit rate."""
        if not self._enabled:
            return False
        if self._stats.total < 10:
            return True
        return self._policy.should_predict(prediction_type, self._min_confidence)
    
    def get_stats(self) -> DraftStats:
        return self._stats
