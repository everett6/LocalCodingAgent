"""Semantic code indexer using Tree-sitter.

Provides:
- Multi-language code parsing (Python, JavaScript/TypeScript)
- Symbol extraction (functions, classes, imports, exports)
- Incremental indexing on file changes
- Vector-ready embeddings for semantic search
- Cross-reference resolution (imports, definitions, references)
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel

from tree_sitter import Language, Parser, Node
from tree_sitter_python import language as python_language
from tree_sitter_javascript import language as javascript_language
try:
    from tree_sitter_typescript import language_typescript as typescript_language
except ImportError:
    try:
        from tree_sitter_typescript import language as typescript_language
    except ImportError:
        typescript_language = None

logger = logging.getLogger(__name__)


# Language cache
_language_cache: Dict[str, Language] = {}


def get_language(language_id: str) -> Language:
    """Get or create a Tree-sitter language parser."""
    if language_id not in _language_cache:
        if language_id == "python":
            _language_cache[language_id] = Language(python_language())
        elif language_id == "javascript":
            _language_cache[language_id] = Language(javascript_language())
        elif language_id == "typescript":
            if typescript_language:
                _language_cache[language_id] = Language(typescript_language())
            else:
                raise ValueError("tree_sitter_typescript is not available")
        else:
            raise ValueError(f"Unsupported language: {language_id}")
    
    return _language_cache[language_id]


def _get_start_pos(node: Node) -> Tuple[int, int]:
    """Compatibility helper for tree-sitter start position across versions."""
    if hasattr(node, "start_point"):
        return node.start_point.row, node.start_point.column
    return getattr(node, "start_position", (0, 0))


def _get_end_pos(node: Node) -> Tuple[int, int]:
    """Compatibility helper for tree-sitter end position across versions."""
    if hasattr(node, "end_point"):
        return node.end_point.row, node.end_point.column
    return getattr(node, "end_position", (0, 0))


class SymbolInfo(BaseModel):
    """Information about a symbol in the codebase."""
    name: str
    type: str  # "function", "class", "method", "import", "export", "variable", etc.
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    file_path: str
    qualified_name: str  # e.g., "module.submodule.Class.method"
    docstring: Optional[str] = None


class ImportInfo(BaseModel):
    """Information about an import statement."""
    module: str
    alias: Optional[str] = None
    start_line: int
    end_line: int
    file_path: str


class CodeIndexer:
    """Index code files using Tree-sitter for semantic understanding."""
    
    def __init__(self, languages: Optional[List[str]] = None):
        """Initialize the code indexer.
        
        Args:
            languages: List of language IDs to support. Defaults to ["python", "javascript", "typescript"]
        """
        self.languages = languages or ["python", "javascript", "typescript"]
        self.parsers: Dict[str, Parser] = {}
        self.index: Dict[str, Dict[str, Any]] = {}  # file_path -> indexed data
        self._file_timestamps: Dict[str, float] = {}
        self._file_hashes: Dict[str, str] = {}
        
        # Initialize parsers and languages
        for lang_id in self.languages:
            try:
                language = get_language(lang_id)
                try:
                    parser = Parser(language)
                except (TypeError, ValueError):
                    parser = Parser()
                    if hasattr(parser, "set_language"):
                        parser.set_language(language)
                    else:
                        parser.language = language
                self.parsers[lang_id] = parser
                logger.info(f"Initialized parser for {lang_id}")
            except Exception as e:
                logger.warning(f"Failed to initialize parser for {lang_id}: {e}")
    
    async def index_file(self, file_path: str) -> Dict[str, Any]:
        """Index a single file, extracting symbols and imports.
        
        Returns dict with symbols, imports, and language info.
        """
        path = Path(file_path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1048576:
            self.index.pop(file_path, None)
            self._file_hashes.pop(file_path, None)
            self._file_timestamps.pop(file_path, None)
            return {"error": f"File unavailable or exceeds local index size limit: {file_path}"}
        
        # Check if re-indexing is needed
        mtime = path.stat().st_mtime
        # Timestamps alone cannot detect same-stat edits or branch rewrites.
        
        # Read file content
        try:
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if self._file_hashes.get(file_path) == digest and file_path in self.index:
                return self.index[file_path]
        except Exception as e:
            return {"error": f"Failed to read file: {e}"}
        
        # Determine language and parse
        language_id = self._detect_language(path)
        if language_id not in self.parsers:
            return {"error": f"Unsupported language: {language_id}", "file": file_path}
        
        try:
            result = await asyncio.to_thread(self._parse_file, content, language_id, path)
            self.index[file_path] = result
            self._file_timestamps[file_path] = mtime
            self._file_hashes[file_path] = digest
            return result
        except Exception as e:
            logger.error(f"Error indexing {file_path}: {e}")
            return {"error": str(e), "file": file_path}
    
    def _detect_language(self, path: Path) -> str:
        """Detect the language of a file based on extension."""
        ext = path.suffix.lower()
        mapping = {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
        }
        return mapping.get(ext, "python")  # Default to Python
    
    def _parse_file(self, content: bytes, language_id: str, path: Path) -> Dict[str, Any]:
        """Parse a file and extract symbols and imports."""
        parser = self.parsers[language_id]
        if hasattr(parser, "parse"):
            tree = parser.parse(content)
        else:
            tree = parser.parse_bytes(content)
        root_node = tree.root_node
        
        result: Dict[str, Any] = {
            "language": language_id,
            "symbols": [],
            "imports": [],
            "exports": [],
            "size": len(content),
        }
        
        # Extract symbols and imports in one traversal
        self._traverse_node(root_node, content, language_id, path, result)
        
        return result
    
    def _traverse_node(
        self,
        node: Node,
        content: bytes,
        language_id: str,
        path: Path,
        result: Dict[str, Any],
        prefix: str = "",
    ) -> None:
        """Recursively traverse AST nodes to extract symbols and imports."""
        node_type = node.type
        
        if language_id == "python":
            self._process_python_node(node, content, path, result, prefix)
        elif language_id in ("javascript", "typescript"):
            self._process_js_node(node, content, path, result, prefix)
        
        # Continue traversing children
        for child in node.children:
            self._traverse_node(child, content, language_id, path, result, prefix + node_type + ".")
    
    def _process_python_node(self, node: Node, content: bytes, path: Path, result: Dict[str, Any], prefix: str) -> None:
        """Process a Python AST node."""
        node_type = node.type
        
        # Extract function definitions
        if node_type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = content[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                start_row, start_col = _get_start_pos(node)
                end_row, end_col = _get_end_pos(node)
                
                # Try to get docstring
                docstring = self._extract_python_docstring(node, content)
                
                symbol = SymbolInfo(
                    name=name,
                    type="function",
                    start_line=start_row,
                    end_line=end_row,
                    start_column=start_col,
                    end_column=end_col,
                    file_path=str(path),
                    qualified_name=f"{prefix}{name}",
                    docstring=docstring,
                )
                result["symbols"].append(symbol.model_dump() if hasattr(symbol, 'model_dump') else dict(symbol))
        
        # Extract class definitions
        elif node_type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = content[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                start_row, start_col = _get_start_pos(node)
                end_row, end_col = _get_end_pos(node)
                
                symbol = SymbolInfo(
                    name=name,
                    type="class",
                    start_line=start_row,
                    end_line=end_row,
                    start_column=start_col,
                    end_column=end_col,
                    file_path=str(path),
                    qualified_name=f"{prefix}{name}",
                )
                result["symbols"].append(symbol.model_dump() if hasattr(symbol, 'model_dump') else dict(symbol))
        
        # Extract import statements
        elif node_type == "import_statement":
            # Get the module name
            module_node = node.child_by_field_name("module")
            if module_node:
                module_text = content[module_node.start_byte:module_node.end_byte].decode("utf-8", errors="replace")
                start_row, start_col = _get_start_pos(node)
                end_row, end_col = _get_end_pos(node)
                
                # Parse for alias
                alias = None
                # Handle: "import x as y" or "from x import y"
                if " as " in module_text:
                    parts = module_text.split(" as ")
                    module = parts[0].strip()
                    alias = parts[1].strip() if len(parts) > 1 else None
                else:
                    module = module_text.strip()
                
                import_info = ImportInfo(
                    module=module,
                    alias=alias,
                    start_line=start_row,
                    end_line=end_row,
                    file_path=str(path),
                )
                result["imports"].append(import_info.model_dump() if hasattr(import_info, 'model_dump') else dict(import_info))
        
        # Extract from imports
        elif node_type == "import_from_statement":
            module_node = node.child_by_field_name("module")
            if module_node:
                module_text = content[module_node.start_byte:module_node.end_byte].decode("utf-8", errors="replace")
                start_row, start_col = _get_start_pos(node)
                end_row, end_col = _get_end_pos(node)
                
                # Get imported names
                names_node = node.child_by_field_name("names")
                imported_names = []
                if names_node:
                    for name_child in names_node.children:
                        if name_child.type == "identifier":
                            name = content[name_child.start_byte:name_child.end_byte].decode("utf-8", errors="replace")
                            imported_names.append(name)
                
                from_import = {
                    "module": module_text,
                    "imported_names": imported_names,
                    "start_line": start_row,
                    "end_line": end_row,
                    "file_path": str(path),
                }
                result["imports"].append(from_import)
        
        # Extract return statements (for understanding control flow)
        elif node_type == "return_statement":
            pass  # Could be used for deeper analysis
    
    def _process_js_node(self, node: Node, content: bytes, path: Path, result: Dict[str, Any], prefix: str) -> None:
        """Process a JavaScript/TypeScript AST node."""
        node_type = node.type
        
        # Function declarations/arrows
        if node_type in ("function_declaration", "method_definition", "arrow_function"):
            name_node = node.child_by_field_name("name")
            name = ""
            if name_node:
                name = content[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
            
            start_row, start_col = _get_start_pos(node)
            end_row, end_col = _get_end_pos(node)
            
            symbol = {
                "name": name,
                "type": "function",
                "start_line": start_row,
                "end_line": end_row,
                "start_column": start_col,
                "end_column": end_col,
                "file_path": str(path),
                "qualified_name": f"{prefix}{name}" if name else prefix,
            }
            result["symbols"].append(symbol)
        
        # Class declarations
        elif node_type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = content[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
            else:
                name = ""
            
            start_row, start_col = _get_start_pos(node)
            end_row, end_col = _get_end_pos(node)
            
            symbol = {
                "name": name,
                "type": "class",
                "start_line": start_row,
                "end_line": end_row,
                "start_column": start_col,
                "end_column": end_col,
                "file_path": str(path),
                "qualified_name": f"{prefix}{name}" if name else prefix,
            }
            result["symbols"].append(symbol)
        
        # Import statements
        elif node_type == "import_statement":
            source_node = node.child_by_field_name("source")
            if source_node:
                module = content[source_node.start_byte:source_node.end_byte].decode("utf-8", errors="replace")
                start_row, start_col = _get_start_pos(node)
                end_row, end_col = _get_end_pos(node)
                
                # Check for alias
                alias = None
                import_clause = node.child_by_field_name("import_clause")
                if import_clause:
                    # Check for "import x as y"
                    alias_node = import_clause.child_by_field_name("alias")
                    if alias_node:
                        alias = content[alias_node.start_byte:alias_node.end_byte].decode("utf-8", errors="replace")
                
                import_info = {
                    "module": module,
                    "alias": alias,
                    "start_line": start_row,
                    "end_line": end_row,
                    "file_path": str(path),
                }
                result["imports"].append(import_info)
        
        # Export statements
        elif node_type == "export_statement":
            start_row, start_col = _get_start_pos(node)
            end_row, end_col = _get_end_pos(node)
            
            # Get exported name
            exported_name = ""
            export_default = node.child_by_field_name("default")
            if export_default:
                exported_name = "default"
            else:
                name_node = node.child_by_field_name("name")
                if name_node:
                    exported_name = content[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
            
            export_info = {
                "name": exported_name,
                "type": "export",
                "start_line": start_row,
                "end_line": end_row,
                "start_column": start_col,
                "end_column": end_col,
                "file_path": str(path),
            }
            result["exports"].append(export_info)
        
        # Variable declarations (let, const, var)
        elif node_type in ("variable_declaration", "const_declaration", "let_declaration"):
            declarations = node.child_by_field_name("declarations")
            if declarations:
                for decl in declarations.children:
                    if decl.type == "variable_declarator":
                        name_node = decl.child_by_field_name("name")
                        if name_node:
                            var_name = content[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                            start_row, start_col = _get_start_pos(decl)
                            end_row, end_col = _get_end_pos(decl)
                            
                            var_info = {
                                "name": var_name,
                                "type": node.type,
                                "start_line": start_row,
                                "end_line": end_row,
                                "start_column": start_col,
                                "end_column": end_col,
                                "file_path": str(path),
                            }
                            result["symbols"].append(var_info)
    
    def _extract_python_docstring(self, node: Node, content: bytes) -> Optional[str]:
        """Extract docstring from a Python function/class node."""
        # Look for triple-quoted string as first child
        for child in node.children:
            if child.type == "string":
                text = content[child.start_byte:child.end_byte].decode("utf-8", errors="replace").strip()
                if text.startswith('"""') or text.startswith("'''"):
                    return text[3:-3].strip() if len(text) > 6 else text
        return None
    
    async def index_directory(self, directory: str, pattern: str = "*.py") -> Dict[str, Dict[str, Any]]:
        """Index all files matching a pattern in a directory."""
        path = Path(directory)
        results = {}
        
        if not path.is_dir():
            return {"error": f"Directory not found: {directory}"}
        
        # Remove deleted sources only within this requested directory/pattern.
        for indexed in list(self.index):
            candidate = Path(indexed)
            if candidate.resolve().is_relative_to(path.resolve()) and candidate.match(pattern) and not candidate.is_file():
                self.index.pop(indexed, None)
                self._file_hashes.pop(indexed, None)
                self._file_timestamps.pop(indexed, None)
        # Bound discovery independently of parser work.
        files = []
        for candidate in path.rglob(pattern):
            files.append(candidate)
            if len(files) >= 5000:
                break
        
        # Filter out excluded directories
        exclude_dirs = {".git", "__pycache__", ".venv", "venv", "node_modules", "dist", "build"}
        
        filtered_files = []
        for f in files:
            # Check if any parent dir is excluded
            excluded = False
            for part in f.parts:
                if part in exclude_dirs or part.startswith('.env') or f.is_symlink():
                    excluded = True
                    break
            if not excluded:
                filtered_files.append(str(f))
        
        # Index files concurrently
        semaphore = asyncio.Semaphore(10)  # Limit concurrency
        
        async def index_one(file_path: str) -> Tuple[str, Dict[str, Any]]:
            async with semaphore:
                result = await self.index_file(file_path)
                return (file_path, result)
        
        tasks = [index_one(f) for f in filtered_files]
        results_list = await asyncio.gather(*tasks)
        
        for file_path, result in results_list:
            results[file_path] = result
        
        return results
    
    def get_symbols_for_file(self, file_path: str) -> List[Dict[str, Any]]:
        """Get all symbols indexed for a specific file."""
        if file_path in self.index:
            return self.index[file_path].get("symbols", [])
        return []
    
    def get_imports_for_file(self, file_path: str) -> List[Dict[str, Any]]:
        """Get all imports indexed for a specific file."""
        if file_path in self.index:
            return self.index[file_path].get("imports", [])
        return []
    
    def get_all_symbols(self) -> List[Dict[str, Any]]:
        """Get all symbols from all indexed files."""
        all_symbols = []
        for file_data in self.index.values():
            all_symbols.extend(file_data.get("symbols", []))
        return all_symbols
    
    def get_all_imports(self) -> List[Dict[str, Any]]:
        """Get all imports from all indexed files."""
        all_imports = []
        for file_data in self.index.values():
            all_imports.extend(file_data.get("imports", []))
        return all_imports
    
    def find_symbol(self, name: str, exclude_file: Optional[str] = None) -> List[Dict[str, Any]]:
        """Find all occurrences of a symbol by name."""
        results = []
        for file_path, file_data in self.index.items():
            if exclude_file and file_path == exclude_file:
                continue
            for symbol in file_data.get("symbols", []):
                if symbol.get("name") == name:
                    results.append({
                        "file": file_path,
                        "symbol": symbol,
                    })
        return results
    
    def find_definitions(self, file_path: str, line: int, column: int) -> List[Dict[str, Any]]:
        """Find definitions at a specific location (stub implementation).
        
        In a full implementation, this would use the AST to find which
        symbol is defined at the given position.
        """
        results = []
        if file_path not in self.index:
            return results
        
        # Simple approach: find symbols whose range includes the position
        for symbol in self.index[file_path].get("symbols", []):
            if symbol.get("start_line", 0) <= line <= symbol.get("end_line", 0):
                results.append({
                    "symbol": symbol,
                    "file": file_path,
                })
        
        return results
    
    def find_references(self, file_path: str, line: int, column: int) -> List[Dict[str, Any]]:
        """Find references to a symbol at a specific location (stub implementation)."""
        results = []
        if file_path not in self.index:
            return results
        
        # Simple approach: find all symbols in the file that reference the same name
        # In a full implementation, this would do proper name resolution
        for symbol in self.index[file_path].get("symbols", []):
            results.append({
                "symbol": symbol,
                "file": file_path,
                "note": "Reference lookup - full resolution not implemented in stub",
            })
        
        return results

    def find_security_flows(self, file_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """Identify potential untrusted input sources flowing into dangerous sinks.

        Maps source-to-sink data paths (e.g. web route parameters reaching
        database queries, command execution, or eval calls).
        """
        flows: List[Dict[str, Any]] = []
        targets = [file_path] if file_path and file_path in self.index else list(self.index.keys())

        source_patterns = [
            (r"@(app|router|bp)\.(route|get|post|put|delete|patch)", "Web route endpoint"),
            (r"\brequest\.(args|form|values|json|data|headers|cookies|GET|POST)\b", "HTTP request parameter/body"),
            (r"\breq\.(body|query|params|headers)\b", "Express/Node request parameter"),
            (r"\bsys\.argv\b", "CLI argument input"),
        ]

        sink_patterns = [
            (r"\b(eval|exec)\s*\(", "Dynamic code evaluation sink", "CRITICAL"),
            (r"\bos\.system\s*\(", "Direct OS shell execution sink", "HIGH"),
            (r"\bsubprocess\.(Popen|run|call|check_output)\s*\(.*shell\s*=\s*True", "Subprocess shell execution sink", "HIGH"),
            (r"\bcursor\.execute\s*\(.*[+%]", "Unparameterized raw SQL execution sink", "CRITICAL"),
            (r"\bpickle\.loads?\s*\(", "Insecure deserialization sink", "HIGH"),
            (r"\bopen\s*\(.*['\"]w", "Filesystem write sink", "MEDIUM"),
        ]

        for target in targets:
            try:
                p = Path(target)
                if not p.is_file():
                    continue
                content = p.read_text(encoding="utf-8", errors="replace")
                lines = content.splitlines()
            except Exception:
                continue

            symbols = self.get_symbols_for_file(target)
            functions = [s for s in symbols if s.get("type") in ("function", "method")]

            for fn in functions:
                start_l = fn.get("start_line", 0)
                end_l = min(fn.get("end_line", len(lines)), len(lines))
                fn_lines = lines[start_l:end_l + 1]
                fn_body = "\n".join(fn_lines)

                # Check if this function has sources
                detected_sources = []
                for pat, desc in source_patterns:
                    if re.search(pat, fn_body):
                        detected_sources.append(desc)

                # Also if this function has a route decorator before it
                decorator_window = "\n".join(lines[max(0, start_l - 4):start_l])
                for pat, desc in source_patterns:
                    if re.search(pat, decorator_window) and desc not in detected_sources:
                        detected_sources.append(desc)

                # Check if this function has sinks
                detected_sinks = []
                highest_severity = "LOW"
                for pat, desc, sev in sink_patterns:
                    match = re.search(pat, fn_body)
                    if match:
                        detected_sinks.append((desc, sev))
                        if sev == "CRITICAL" or (sev == "HIGH" and highest_severity != "CRITICAL"):
                            highest_severity = sev

                # If both source and sink are present in the same execution scope:
                if detected_sources and detected_sinks:
                    flows.append({
                        "file": target,
                        "function": fn.get("name"),
                        "start_line": start_l + 1,
                        "end_line": end_l + 1,
                        "sources": detected_sources,
                        "sinks": [s[0] for s in detected_sinks],
                        "severity": highest_severity,
                        "flow_type": "Direct Source-to-Sink Path",
                    })

        return flows

    def get_security_posture_summary(self) -> Dict[str, Any]:
        """Aggregate high-level security metrics across indexed repository files."""
        flows = self.find_security_flows()
        all_symbols = self.get_all_symbols()
        function_count = sum(1 for s in all_symbols if s.get("type") in ("function", "method"))

        return {
            "indexed_files": len(self.index),
            "total_functions": function_count,
            "detected_flows": len(flows),
            "critical_flows": sum(1 for f in flows if f.get("severity") == "CRITICAL"),
            "high_flows": sum(1 for f in flows if f.get("severity") == "HIGH"),
            "flows": flows,
        }


# Convenience function
def create_indexer(languages: Optional[List[str]] = None) -> CodeIndexer:
    """Factory function to create a CodeIndexer instance."""
    return CodeIndexer(languages=languages)