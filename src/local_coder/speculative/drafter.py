import time
import uuid
import re
from collections import defaultdict
from typing import Optional, List, Any
from pydantic import BaseModel

from local_coder.types import Message
from local_coder.models.cache import PromptCache, prompt_cache_key

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
    cache_hits: int = 0
    
    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.total if self.total > 0 else 0.0
    
    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total if self.total > 0 else 0.0


_DEFINITION_LINE = re.compile(r"^\s*(def|class|async def|function|const|let|var)\s")


class DeltaAttention:
    """Application-level attention over changed and task-relevant code regions.

    This prioritizes deltas in the prompt; it does not modify transformer
    attention weights or claim native DeltaNet/speculative decoding support.
    """

    def focus(
        self,
        objective: str,
        content: str,
        max_lines: int = 80,
        context_radius: int = 2,
    ) -> str:
        """Score every line, then keep the highest-scoring ones AND a small
        window of their neighbors (context_radius on each side).

        A changed or objective-relevant line taken in isolation usually
        doesn't mean much to a draft model -- "return timeout" only makes
        sense next to the function it's returning from. Selecting isolated
        single lines (the previous behavior) threw that context away.
        """
        objective_terms = {
            term.lower()
            for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", objective)
            if len(term) > 2
        }
        # Word-boundary patterns, not substring checks: a plain `term in
        # line` match on "class" would also fire on "subclass" or
        # "classify", diluting the score with false positives.
        term_patterns = [re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE) for term in objective_terms]

        lines = content.splitlines()
        scored = []
        for index, line in enumerate(lines):
            score = 0.0
            if line.startswith(("+", "-")):
                score += 4.0
            # Weight by how many DISTINCT objective terms a line matches,
            # not just whether any single one does -- a line hitting two
            # or three terms from the objective is more likely to be the
            # relevant one than a line that happens to share one common word.
            matching_terms = sum(1 for pattern in term_patterns if pattern.search(line))
            score += matching_terms * 2.0
            if _DEFINITION_LINE.match(line):
                # Definitions anchor whatever's selected near them -- worth
                # a small bonus even with no other signal, since a selected
                # line inside a function is far more useful alongside its
                # own `def` line than floating with no header at all.
                score += 1.5
            if line.strip():
                score += 0.5
            scored.append((score, index, line))

        ranked = sorted(scored, key=lambda item: (-item[0], item[1]))
        selected_indices: set[int] = set()
        for score, index, _line in ranked:
            if len(selected_indices) >= max_lines:
                break
            if score <= 0:
                continue
            selected_indices.add(index)
            for offset in range(1, context_radius + 1):
                if len(selected_indices) >= max_lines:
                    break
                for neighbor in (index - offset, index + offset):
                    if 0 <= neighbor < len(lines):
                        selected_indices.add(neighbor)

        if not selected_indices:
            # Nothing scored (e.g. an objective with no matching terms and
            # a diff-free file): fall back to the head of the file instead
            # of returning an empty focus window.
            selected_indices = set(range(min(max_lines, len(lines))))

        return "\n".join(f"{i + 1}: {lines[i]}" for i in sorted(selected_indices))


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
    
    def __init__(
        self,
        model: Any,
        enabled: bool = True,
        min_confidence: float = 0.5,
        prompt_cache: PromptCache | None = None,
    ):
        # model is typically a LocalModel or an adapter representing the small fast model
        self._model = model
        self._enabled = enabled
        self._min_confidence = min_confidence
        self._stats = DraftStats()
        self._policy = PredictionPolicy()
        self._attention = DeltaAttention()
        self._pending: dict[str, str] = {}
        self._prompt_cache = prompt_cache or PromptCache()
    
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
        messages = [Message(role="user", content=prompt)]
        cache_key = prompt_cache_key(self._model.config, messages) if hasattr(self._model, "config") else None
        
        try:
            response = self._prompt_cache.get(cache_key) if cache_key else None
            if response is not None:
                self._stats.cache_hits += 1
            else:
                response = await self._model.generate(messages)
                if cache_key:
                    self._prompt_cache.set(cache_key, response)
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
            messages = [Message(role="user", content=prompt)]
            cache_key = prompt_cache_key(self._model.config, messages) if hasattr(self._model, "config") else None
            response = self._prompt_cache.get(cache_key) if cache_key else None
            if response is not None:
                self._stats.cache_hits += 1
            else:
                response = await self._model.generate(messages)
                if cache_key:
                    self._prompt_cache.set(cache_key, response)
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
    
    def should_predict(self, prediction_type: str = "edit") -> bool:
        """Whether prediction is worth doing based on hit rate."""
        if not self._enabled:
            return False
        if self._stats.total < 10:
            return True
        return self._policy.should_predict(prediction_type, self._min_confidence)
    
    def get_stats(self) -> DraftStats:
        return self._stats
