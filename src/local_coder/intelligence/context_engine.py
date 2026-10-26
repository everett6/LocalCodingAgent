"""Semantic code context engine for agent reasoning.

Provides:
- Smart context selection for agent prompts
- @ mention resolution (files, symbols, git commits, PRs)
- Relevance scoring for context packing within token limits
- Dependency-aware context (imports, callers, callees)
- Cross-file context generation
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from local_coder.intelligence.indexer import CodeIndexer, SymbolInfo, ImportInfo


logger = logging.getLogger(__name__)


class ContextResolutionError(Exception):
    """Raised when context cannot be resolved."""
    pass


class MentionResolver:
    """Resolver for @ mentions in agent prompts."""
    
    def __init__(self, indexer: CodeIndexer):
        self.indexer = indexer
        self.file_cache: Dict[str, Dict[str, Any]] = {}
    
    async def resolve_mention(self, mention: str, context: Optional[dict] = None) -> Optional[dict]:
        """Resolve an @ mention to concrete information.
        
        Args:
            mention: The mention text (e.g., "@utils", "@calculate_average", "@main.py")
            context: Optional context dict with current file, task info, etc.
        
        Returns:
            Dict with resolved info, or None if not found
        """
        # Strip @ if present
        clean = mention.strip().lstrip("@")
        
        # Try special security context mention
        if clean in ("security", "flows", "taint"):
            return {
                "type": "security_summary",
                "value": clean,
                "resolved": True,
                "summary": self.indexer.get_security_posture_summary(),
            }

        # Try as file path
        file_result = self._try_resolve_file(clean, context)
        if file_result:
            return file_result
        
        # Try as symbol name
        symbol_result = self._try_resolve_symbol(clean, context)
        if symbol_result:
            return symbol_result
        
        # Try as function/class name
        return self._try_resolve_name(clean, context)
    
    def _try_resolve_file(self, mention: str, context: Optional[dict]) -> Optional[dict]:
        """Try to resolve as a file path."""
        # Check if file exists in project
        # Use the indexer's file finding
        # For now, just check if it looks like a path
        if "/" in mention or "\\" in mention or mention.endswith(".py") or mention.endswith(".js"):
            # Check if file is in the index
            symbols = self.indexer.get_symbols_for_file(mention)
            if symbols:
                return {
                    "type": "file",
                    "value": mention,
                    "resolved": True,
                    "symbols": symbols,
                }
        return None
    
    def _try_resolve_symbol(self, mention: str, context: Optional[dict]) -> Optional[dict]:
        """Try to resolve as a symbol name."""
        # Search for symbols with this name
        results = self.indexer.find_symbol(mention)
        if results:
            return {
                "type": "symbol",
                "value": mention,
                "resolved": True,
                "occurrences": len(results),
                "examples": results[:3],  # First 3 examples
            }
        return None
    
    def _try_resolve_name(self, mention: str, context: Optional[dict]) -> Optional[dict]:
        """Try to resolve as a function/class name."""
        # Search across all indexed files
        results = self.indexer.find_symbol(mention)
        if results:
            # Get the first occurrence's file
            first = results[0]
            file_path = first["file"]
            symbols = self.indexer.get_symbols_for_file(file_path)
            
            # Find the matching symbol
            for sym in symbols:
                if sym.get("name") == mention:
                    return {
                        "type": "name",
                        "value": mention,
                        "resolved": True,
                        "file": file_path,
                        "symbol": sym,
                    }
        return None


class ContextEngine:
    """Engine for generating smart context for agent prompts.
    
    Handles:
    - Context packing within token limits
    - Dependency-aware selection (imports, call graph)
    - Cross-file context generation
    - Relevance scoring
    """
    
    def __init__(self, indexer: CodeIndexer):
        self.indexer = indexer
        self.mention_resolver = MentionResolver(indexer)
    
    async def generate_context(
        self,
        objective: str,
        current_file: Optional[str] = None,
        max_tokens: int = 8000,
        include_imports: bool = True,
        include_dependencies: bool = True,
        max_files: int = 5,
    ) -> str:
        """Generate context for an agent based on an objective.
        
        Args:
            objective: The user's task/objective
            current_file: The file currently being worked on
            max_tokens: Maximum tokens for the context
            include_imports: Whether to include import statements
            include_dependencies: Whether to include dependency info
            max_files: Maximum number of files to include
        
        Returns:
            String containing formatted context for the agent
        """
        # Step 1: Index relevant files
        # Step 2: Score relevance to objective
        # Step 3: Select best files
        # Step 4: Format context
        
        # Get the codebase structure
        all_symbols = self.indexer.get_all_symbols()
        all_imports = self.indexer.get_all_imports()
        
        # Step 2: Score relevance
        relevant_files = self._score_relevance(objective, all_symbols, all_imports)
        
        # Step 3: Select best files (up to max_files)
        selected = [path for path, _score in relevant_files if path != current_file][:max(0, max_files - bool(current_file))]
        
        # Step 4: Format context
        context_parts = []
        
        # Add current file if specified and relevant
        if current_file and current_file in self.indexer.index:
            current_data = self.indexer.index[current_file]
            context_parts.append(self._format_file_context(current_data, current_file))
        
        # Add selected files
        for file_path in selected:
            if file_path in self.indexer.index:
                file_data = self.indexer.index[file_path]
                context_parts.append(self._format_file_context(file_data, file_path))
        
        # Combine into single context string
        context = "\n\n".join(context_parts)
        
        # Truncate if exceeds token limit (rough estimate: 4 chars per token)
        estimated_tokens = len(context) // 4
        if estimated_tokens > max_tokens:
            # Simple truncation - keep head
            chars_per_token = 4
            max_chars = max_tokens * chars_per_token
            context = context[:max_chars] + "\n\n...[truncated for token limit]..."
        
        return context
    
    def _score_relevance(
        self,
        objective: str,
        all_symbols: List[Dict[str, Any]],
        all_imports: List[Dict[str, Any]],
    ) -> List[Tuple[str, float]]:
        """Score files by relevance to the objective.
        
        Returns list of (file_path, relevance_score) sorted by relevance.
        """
        objective_lower = objective.lower()
        objective_terms = set(
            word for word in objective_lower.split() 
            if len(word) > 2
        )
        
        file_scores: Dict[str, float] = {}
        
        # Score based on symbol names matching objective terms
        for symbol in all_symbols:
            symbol_name = symbol.get("name", "").lower()
            file_path = symbol.get("file_path", "")
            
            if not symbol_name or not file_path:
                continue
            
            score = 0.0
            
            # Check if symbol name matches objective terms
            matching_terms = objective_terms & set(symbol_name.split())
            score += len(matching_terms) * 3.0
            
            # Check if any objective term appears in symbol's qualified name
            qualified = symbol.get("qualified_name", "").lower()
            if qualified:
                term_matches = sum(1 for term in objective_terms if term in qualified)
                score += term_matches * 2.0
            
            # Check symbol type relevance
            sym_type = symbol.get("type", "").lower()
            if any(term in sym_type for term in ["function", "class", "method"]):
                score += 1.0
            
            if file_path not in file_scores:
                file_scores[file_path] = 0.0
            file_scores[file_path] = max(file_scores[file_path], score)
        
        # Score based on import patterns
        for imp in all_imports:
            module = imp.get("module", "").lower()
            file_path = imp.get("file_path", "")
            
            if not module or not file_path:
                continue
            
            # Check if imported module name matches objective terms
            matching_terms = objective_terms & set(module.split())
            if matching_terms:
                score = len(matching_terms) * 2.0
                if file_path not in file_scores:
                    file_scores[file_path] = 0.0
                file_scores[file_path] = max(file_scores[file_path], score)
        
        # Sort by score (descending)
        sorted_files = sorted(file_scores.items(), key=lambda x: x[1], reverse=True)
        
        return sorted_files
    
    def _format_file_context(
        self,
        file_data: Dict[str, Any],
        file_path: str,
    ) -> str:
        """Format a file's indexed data into context string."""
        language = file_data.get("language", "python")
        symbols = file_data.get("symbols", [])
        imports = file_data.get("imports", [])
        
        parts = [f"File: {file_path} ({language})"]
        
        # Add imports
        if imports:
            import_lines = []
            for imp in imports[:10]:  # Limit to top 10 imports
                module = imp.get("module", "")
                alias = imp.get("alias")
                if alias:
                    import_lines.append(f"import {module} as {alias}")
                else:
                    import_lines.append(f"import {module}")
            parts.append("Imports: " + "; ".join(import_lines))
        
        # Add symbols
        if symbols:
            symbol_lines = []
            for sym in symbols[:15]:  # Limit to top 15 symbols
                name = sym.get("name", "")
                sym_type = sym.get("type", "unknown")
                line = sym.get("start_line", 0)
                qualified = sym.get("qualified_name", "")
                if qualified:
                    symbol_lines.append(f"  {sym_type} {name} (line {line}) - {qualified}")
                else:
                    symbol_lines.append(f"  {sym_type} {name} (line {line})")
            if symbol_lines:
                parts.append("Symbols: " + "\n".join(symbol_lines))
        
        return "\n".join(parts)
    
    async def resolve_context_mentions(
        self,
        text: str,
        project_root: str,
    ) -> str:
        """Resolve @ mentions in text and return resolved version.
        
        Finds @mentions in text and replaces them with resolved information.
        """
        import re
        
        # Find all @mentions
        mentions = re.findall(r'@\w+', text)
        
        resolved_text = text
        for mention in mentions:
            # Resolve the mention
            result = await self.mention_resolver.resolve_mention(mention, {"project_root": project_root})
            
            if result:
                # Replace @mention with resolved info
                resolution_str = self._format_resolution(result)
                resolved_text = resolved_text.replace(mention, resolution_str)
        
        return resolved_text
    
    def _format_resolution(self, result: dict) -> str:
        """Format a resolution result into a string."""
        rtype = result.get("type", "")
        
        if rtype == "file":
            return f"[File: {result.get('value', '')}]"
        elif rtype == "symbol":
            return f"[Symbol: {result.get('value', '')} - found in {result.get('occurrences', 0)} places]"
        elif rtype == "name":
            name = result.get("value", "")
            file = result.get("file", "")
            return f"[@{name} in {file}]"
        else:
            return "@" + str(result.get("value", ""))


# Convenience function
def create_context_engine(indexer: CodeIndexer) -> ContextEngine:
    """Factory function to create a ContextEngine instance."""
    return ContextEngine(indexer)