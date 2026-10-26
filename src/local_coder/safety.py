"""Safety harness, execution tiers, and Aider-style Tree-sitter output filtering."""
from __future__ import annotations

import ast
import enum
import json
import logging
import re
from pathlib import Path
from typing import Any, Tuple

import yaml

from local_coder.types import SafetyTier

logger = logging.getLogger(__name__)


def parse_safety_tier(val: Any) -> SafetyTier:
    """Parse integer, string, or SafetyTier enum into SafetyTier."""
    if isinstance(val, SafetyTier):
        return val
    str_val = str(val).strip().lower()
    mapping = {
        "1": SafetyTier.TIER_1_PARANOIA,
        "paranoia": SafetyTier.TIER_1_PARANOIA,
        "tier_1": SafetyTier.TIER_1_PARANOIA,
        "tier_1_paranoia": SafetyTier.TIER_1_PARANOIA,
        "2": SafetyTier.TIER_2_STRICT,
        "strict": SafetyTier.TIER_2_STRICT,
        "tier_2": SafetyTier.TIER_2_STRICT,
        "tier_2_strict": SafetyTier.TIER_2_STRICT,
        "3": SafetyTier.TIER_3_BALANCED,
        "balanced": SafetyTier.TIER_3_BALANCED,
        "tier_3": SafetyTier.TIER_3_BALANCED,
        "tier_3_balanced": SafetyTier.TIER_3_BALANCED,
        "4": SafetyTier.TIER_4_AUTO_VALID,
        "auto_valid": SafetyTier.TIER_4_AUTO_VALID,
        "tier_4": SafetyTier.TIER_4_AUTO_VALID,
        "tier_4_auto_valid": SafetyTier.TIER_4_AUTO_VALID,
        "5": SafetyTier.TIER_5_YOLO,
        "yolo": SafetyTier.TIER_5_YOLO,
        "autonomous": SafetyTier.TIER_5_YOLO,
        "tier_5": SafetyTier.TIER_5_YOLO,
        "tier_5_yolo": SafetyTier.TIER_5_YOLO,
    }
    if str_val in mapping:
        return mapping[str_val]
    return SafetyTier.TIER_3_BALANCED


class CommandRisk(str, enum.Enum):
    SAFE = "safe"
    ASK = "ask"
    BLOCK = "block"


class CommandPolicy:
    """Classify commands before they reach a local shell."""

    BLOCK_PATTERNS = (
        r"(^|\s)rm\s+(-[rf]+\s+)*(/|~|\$HOME)(\s|$)",
        r"(^|\s)(sudo|su)\b",
        r"(^|\s)(mkfs|fdisk|parted)\b",
        r"(^|\s)dd\s+if=",
        r":\(\)\s*\{",
        r"(^|\s)(curl|wget)\b[^|\n]*\|\s*(sh|bash|zsh)\b",
    )
    ASK_PREFIXES = (
        "pip install", "pip3 install", "npm install", "yarn add", "pnpm add",
        "docker", "git commit", "git push", "git reset", "git checkout",
        "chmod", "chown", "curl", "wget",
    )
    SAFE_PREFIXES = (
        "pytest", "python", "python3", "npm test", "npm run test", "cargo test",
        "go test", "make test", "git status", "git diff", "git log", "ls", "pwd",
    )

    def classify(self, command: str) -> CommandRisk:
        normalized = command.strip()
        if not normalized:
            return CommandRisk.BLOCK
        if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in self.BLOCK_PATTERNS):
            return CommandRisk.BLOCK
        if normalized.startswith(self.ASK_PREFIXES):
            return CommandRisk.ASK
        if normalized.startswith(self.SAFE_PREFIXES):
            return CommandRisk.SAFE
        return CommandRisk.ASK


class OutputFilter:
    """Aider-style strict syntax output filter.

    Uses Tree-sitter and AST validation to intercept malformed code blocks or broken
    syntax before files on disk can be corrupted.
    """

    _parsers: dict[str, Any] = {}

    @classmethod
    def _get_ts_parser(cls, lang_id: str):
        if lang_id in cls._parsers:
            return cls._parsers[lang_id]

        try:
            from tree_sitter import Language, Parser

            if lang_id == "python":
                import tree_sitter_python
                language = Language(tree_sitter_python.language())
            elif lang_id in ("javascript", "js"):
                import tree_sitter_javascript
                language = Language(tree_sitter_javascript.language())
            elif lang_id in ("typescript", "ts"):
                import tree_sitter_typescript
                fn = getattr(tree_sitter_typescript, "language_typescript", None) or getattr(tree_sitter_typescript, "language")
                language = Language(fn())
            else:
                return None

            try:
                parser = Parser(language)
            except (TypeError, ValueError):
                parser = Parser()
                if hasattr(parser, "set_language"):
                    parser.set_language(language)
                else:
                    parser.language = language

            cls._parsers[lang_id] = parser
            return parser
        except Exception as e:
            logger.debug(f"Tree-sitter parser init failed for {lang_id}: {e}")
            return None

    @classmethod
    def validate_syntax(cls, file_path: str, content: str) -> Tuple[bool, str | None]:
        """Validate code syntax before writing to disk.

        Returns (is_valid, error_message).
        """
        ext = Path(file_path).suffix.lower()

        # 1. Python validation: Python AST + Tree-sitter
        if ext == ".py":
            try:
                ast.parse(content, filename=file_path)
            except SyntaxError as e:
                line = e.lineno or 1
                col = e.offset or 1
                return False, f"Python SyntaxError on line {line}, col {col}: {e.msg}\n-> {e.text or ''}".strip()

            ts_parser = cls._get_ts_parser("python")
            if ts_parser:
                try:
                    tree = ts_parser.parse(content.encode("utf-8")) if hasattr(ts_parser, "parse") else ts_parser.parse_bytes(content.encode("utf-8"))
                    if tree.root_node.has_error:
                        return False, "Python AST validation error: Tree-sitter detected incomplete or malformed syntax nodes."
                except Exception:
                    pass
            return True, None

        # 2. JavaScript / TypeScript validation via Tree-sitter
        if ext in (".js", ".jsx", ".mjs", ".cjs"):
            ts_parser = cls._get_ts_parser("javascript")
            if ts_parser:
                tree = ts_parser.parse(content.encode("utf-8")) if hasattr(ts_parser, "parse") else ts_parser.parse_bytes(content.encode("utf-8"))
                if tree.root_node.has_error:
                    error_node = cls._find_first_error_node(tree.root_node)
                    point = getattr(error_node, "start_point", None) if error_node else None
                    pos_str = f"line {point.row + 1}, col {point.column + 1}" if point else "unknown line"
                    return False, f"JavaScript SyntaxError: Tree-sitter found malformed syntax near {pos_str}."
            return True, None

        if ext in (".ts", ".tsx"):
            ts_parser = cls._get_ts_parser("typescript")
            if ts_parser:
                tree = ts_parser.parse(content.encode("utf-8")) if hasattr(ts_parser, "parse") else ts_parser.parse_bytes(content.encode("utf-8"))
                if tree.root_node.has_error:
                    error_node = cls._find_first_error_node(tree.root_node)
                    point = getattr(error_node, "start_point", None) if error_node else None
                    pos_str = f"line {point.row + 1}, col {point.column + 1}" if point else "unknown line"
                    return False, f"TypeScript SyntaxError: Tree-sitter found malformed syntax near {pos_str}."
            return True, None

        # 3. JSON validation
        if ext == ".json":
            try:
                json.loads(content)
                return True, None
            except json.JSONDecodeError as e:
                return False, f"JSON SyntaxError on line {e.lineno}, col {e.colno}: {e.msg}"

        # 4. YAML validation
        if ext in (".yaml", ".yml"):
            try:
                yaml.safe_load(content)
                return True, None
            except Exception as e:
                return False, f"YAML SyntaxError: {e}"

        # Other file types pass by default
        return True, None

    @classmethod
    def _find_first_error_node(cls, node: Any) -> Any | None:
        if getattr(node, "is_error", False) or getattr(node, "type", "") == "ERROR" or getattr(node, "is_missing", False):
            return node
        for child in getattr(node, "children", []):
            err = cls._find_first_error_node(child)
            if err:
                return err
        return None


class SafetyHarness:
    """Manages the 5 safety tiers, approval gating, and structural guards."""

    def __init__(self, tier: SafetyTier | str = SafetyTier.TIER_3_BALANCED):
        self.tier = parse_safety_tier(tier)
        self.policy = CommandPolicy()
        self.output_filter = OutputFilter()

    def set_tier(self, tier: SafetyTier | str | int) -> None:
        self.tier = parse_safety_tier(tier)

    def can_modify_files(self) -> Tuple[bool, bool]:
        """Check if file edits are allowed.

        Returns (allowed, requires_approval).
        """
        if self.tier == SafetyTier.TIER_1_PARANOIA:
            return False, False
        if self.tier == SafetyTier.TIER_2_STRICT:
            return True, True
        # Tiers 3, 4, 5 allow file writes without per-file approval prompts (guarded by OutputFilter)
        return True, False

    def can_execute_command(self, command: str) -> Tuple[bool, bool]:
        """Check if a shell command is allowed.

        Returns (allowed, requires_approval).
        """
        risk = self.policy.classify(command)

        if self.tier == SafetyTier.TIER_1_PARANOIA:
            # Only strictly safe commands (like ls, git status) allowed in read-only mode
            if risk == CommandRisk.SAFE and not any(k in command for k in ("commit", "push", "checkout")):
                return True, False
            return False, False

        if self.tier == SafetyTier.TIER_2_STRICT:
            if risk == CommandRisk.BLOCK:
                return False, False
            return True, True  # All commands require explicit human confirmation

        if self.tier == SafetyTier.TIER_3_BALANCED:
            if risk == CommandRisk.BLOCK:
                return False, False
            if risk == CommandRisk.SAFE:
                return True, False
            return True, True  # ASK commands prompt for approval

        if self.tier == SafetyTier.TIER_4_AUTO_VALID:
            if risk == CommandRisk.BLOCK:
                return False, False
            if risk == CommandRisk.SAFE:
                return True, False
            # Safe package installs or dev commands auto-approved
            return True, False

        # TIER_5_YOLO: Unrestricted autonomous execution
        if risk == CommandRisk.BLOCK:
            return False, False
        return True, False

    def filter_output(self, file_path: str, content: str) -> Tuple[bool, str | None]:
        """Run AST/Tree-sitter syntax validation according to tier rules."""
        # Tier 5 (YOLO) bypasses filter; Tiers 1-4 strictly enforce syntax integrity
        if self.tier == SafetyTier.TIER_5_YOLO:
            return True, None
        return self.output_filter.validate_syntax(file_path, content)

    def should_checkpoint_before_request(self) -> bool:
        """Determines if automatic git checkpoints should be taken."""
        # Checkpointing is enabled for Tiers 2, 3, 4, 5 to enable one-click /undo
        return self.tier != SafetyTier.TIER_1_PARANOIA
