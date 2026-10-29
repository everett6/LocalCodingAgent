"""Optional strict per-segment limits, in addition to the shared request ledger."""
from pydantic import BaseModel, Field, model_validator


class ContextAllocation(BaseModel):
    system: int = Field(default=15, ge=1, le=98)
    evidence: int = Field(default=45, ge=1, le=98)
    dialogue: int = Field(default=40, ge=1, le=98)

    @model_validator(mode="after")
    def total(self):
        if self.system + self.evidence + self.dialogue != 100:
            raise ValueError("Context segment percentages must sum to 100")
        return self

    def check(self, messages, byte_budget):
        from local_coder.context.compression import message_size
        used = {"system": 0, "evidence": 0, "dialogue": 0}
        for message in messages:
            segment = ("system" if message.role == "system" else "evidence" if
                       message.role == "tool" or message.content.startswith("Retrieved project memory") else "dialogue")
            # UTF-8 serialization is conservative for byte-level tokenizers.
            used[segment] += max(message_size(message), len(message.model_dump_json().encode()))
        exceeded = [name for name, amount in used.items() if amount > byte_budget * getattr(self, name) // 100]
        return used, exceeded
