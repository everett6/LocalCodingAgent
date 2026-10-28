"""Bounded local language context with optional installed parsers and LSP servers.

Adapters describe integration possibilities, not installed/live language support.
No server is installed, no workspace command/configuration is executed by this client.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlsplit

from local_coder.processes import process_options, terminate_tree


@dataclass(frozen=True)
class LanguageAdapter:
    language: str
    extensions: tuple[str, ...]
    server_command: tuple[str, ...]
    grammar: str | None = None
    grammar_function: str = "language"


ADAPTERS = (
    LanguageAdapter("python", (".py", ".pyi"), ("pylsp",), "tree_sitter_python"),
    LanguageAdapter("javascript", (".js", ".mjs", ".cjs", ".jsx"), ("typescript-language-server", "--stdio"), "tree_sitter_javascript"),
    LanguageAdapter("typescript", (".ts", ".mts", ".cts"), ("typescript-language-server", "--stdio"), "tree_sitter_typescript", "language_typescript"),
    LanguageAdapter("typescriptreact", (".tsx",), ("typescript-language-server", "--stdio"), "tree_sitter_typescript", "language_tsx"),
    LanguageAdapter("rust", (".rs",), ("rust-analyzer",), "tree_sitter_rust"),
    LanguageAdapter("go", (".go",), ("gopls",), "tree_sitter_go"),
    LanguageAdapter("c", (".c", ".h"), ("clangd",), "tree_sitter_c"),
    LanguageAdapter("cpp", (".cpp", ".cc", ".cxx", ".hpp", ".hh"), ("clangd",), "tree_sitter_cpp"),
    LanguageAdapter("java", (".java",), ("jdtls",), "tree_sitter_java"),
    LanguageAdapter("csharp", (".cs",), ("csharp-ls",), "tree_sitter_c_sharp"),
    LanguageAdapter("ruby", (".rb", ".rake"), ("ruby-lsp",), "tree_sitter_ruby"),
    LanguageAdapter("php", (".php",), ("phpactor", "language-server"), "tree_sitter_php", "language_php"),
    LanguageAdapter("swift", (".swift",), ("sourcekit-lsp",), "tree_sitter_swift"),
    LanguageAdapter("kotlin", (".kt", ".kts"), ("kotlin-language-server",), "tree_sitter_kotlin"),
    LanguageAdapter("scala", (".scala", ".sc"), ("metals",), "tree_sitter_scala"),
    LanguageAdapter("lua", (".lua",), ("lua-language-server",), "tree_sitter_lua"),
    LanguageAdapter("bash", (".sh", ".bash"), ("bash-language-server", "start"), "tree_sitter_bash"),
    LanguageAdapter("html", (".html", ".htm"), ("vscode-html-language-server", "--stdio"), "tree_sitter_html"),
    LanguageAdapter("css", (".css",), ("vscode-css-language-server", "--stdio"), "tree_sitter_css"),
    LanguageAdapter("json", (".json", ".jsonc"), ("vscode-json-language-server", "--stdio"), "tree_sitter_json"),
    LanguageAdapter("yaml", (".yaml", ".yml"), ("yaml-language-server", "--stdio"), "tree_sitter_yaml"),
    LanguageAdapter("toml", (".toml",), ("taplo", "lsp", "stdio"), "tree_sitter_toml"),
    LanguageAdapter("elixir", (".ex", ".exs"), ("elixir-ls",), "tree_sitter_elixir"),
    LanguageAdapter("haskell", (".hs", ".lhs"), ("haskell-language-server-wrapper", "--lsp"), "tree_sitter_haskell"),
    LanguageAdapter("ocaml", (".ml", ".mli"), ("ocamllsp",), "tree_sitter_ocaml"),
    LanguageAdapter("dart", (".dart",), ("dart", "language-server", "--protocol=lsp"), "tree_sitter_dart"),
    LanguageAdapter("r", (".r", ".R"), ("R", "--slave", "-e", "languageserver::run()"), "tree_sitter_r"),
    LanguageAdapter("sql", (".sql",), ("sqls",), "tree_sitter_sql"),
)

_EXCLUDED = {".git", ".local-coder", ".codex", ".agents", ".ssh", ".aws", ".azure", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore"}
_SECRET_NAMES = {"credentials", "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
_SYMBOL_NODES = {"function_definition", "function_declaration", "method_definition", "method_declaration", "class_definition", "class_declaration", "interface_declaration", "struct_item", "enum_item", "trait_item", "type_declaration", "function_item", "module_definition", "namespace_definition"}


def _installed(name: str | None) -> bool:
    if not name:
        return False
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _lines(text: str) -> list[str]:
    # Unlike str.splitlines(), LSP lines do not split at Unicode U+2028 or U+0085.
    return re.split(r"\r\n|\r|\n", text)


def _utf16_character(text: str, line: int, character: int) -> int:
    lines = _lines(text)
    current = lines[min(max(line - 1, 0), len(lines) - 1)]
    return len(current[:max(0, character)].encode("utf-16-le")) // 2


def _unicode_character(text: str, line: int, character: int) -> int:
    lines = _lines(text)
    current = lines[min(max(line - 1, 0), len(lines) - 1)]
    units = 0
    for index, char in enumerate(current):
        width = len(char.encode("utf-16-le")) // 2
        if units + width > character:
            return index
        units += width
    return len(current)


class _LSPClient:
    """One sequential JSON-RPC stream; only read-only requests are emitted."""

    def __init__(self, command: Sequence[str], root: Path, timeout: float, max_bytes: int):
        self.command, self.root = command, root
        self.timeout, self.max_bytes = timeout, max_bytes
        self.process: asyncio.subprocess.Process | None = None
        self.capabilities: dict[str, Any] = {}
        self.documents: dict[str, tuple[int, str]] = {}
        self.counter = 0
        self.diagnostics: dict[str, tuple[int, list]] = {}

    async def send(self, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise ValueError("LSP outgoing message exceeds size limit")
        assert self.process and self.process.stdin
        self.process.stdin.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode() + encoded)
        await self.process.stdin.drain()

    async def notify(self, method: str, params: dict) -> None:
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _read(self) -> dict:
        assert self.process and self.process.stdout
        headers = await self.process.stdout.readuntil(b"\r\n\r\n")
        if len(headers) > 8192:
            raise ValueError("LSP header exceeds size limit")
        values = [part.split(b":", 1)[1].strip() for part in headers.split(b"\r\n") if part.lower().startswith(b"content-length:")]
        if len(values) != 1:
            raise ValueError("LSP message requires exactly one Content-Length")
        size = int(values[0])
        if size < 0 or size > self.max_bytes:
            raise ValueError("LSP message exceeds size limit")
        message = json.loads(await self.process.stdout.readexactly(size))
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError("Malformed LSP JSON-RPC response")
        return message

    async def _exchange(self, method: str, params: dict) -> Any:
        self.counter += 1
        request_id = self.counter
        await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        for _ in range(256):
            message = await self._read()
            if "method" in message:
                if message["method"] == "textDocument/publishDiagnostics" and "id" not in message:
                    notification = message.get("params")
                    if isinstance(notification, dict):
                        uri, version = notification.get("uri"), notification.get("version")
                        document = self.documents.get(uri) if isinstance(uri, str) else None
                        items = notification.get("diagnostics")
                        if document and type(version) is int and version == document[0] and isinstance(items, list):
                            self.diagnostics[uri] = (version, items[:50])
                if "id" in message:
                    # Servers cannot ask this client to run commands or mutate files.
                    if message["method"] == "workspace/configuration":
                        params = message.get("params")
                        items = params.get("items", []) if isinstance(params, dict) else []
                        if not isinstance(items, list):
                            items = []
                        reply = {"result": [None] * min(len(items), 64)}
                    elif message["method"] == "workspace/applyEdit":
                        reply = {"result": {"applied": False, "failureReason": "Read-only language context"}}
                    else:
                        reply = {"error": {"code": -32601, "message": "Read-only client does not support server requests"}}
                    await self.send({"jsonrpc": "2.0", "id": message["id"], **reply})
                continue
            if message.get("id") != request_id:
                raise ValueError("Unexpected LSP response id")
            if "error" in message:
                raise ValueError("Language server rejected request")
            if "result" not in message:
                raise ValueError("LSP response is missing result")
            return message["result"]
        raise ValueError("Too many unsolicited LSP messages")

    async def request(self, method: str, params: dict) -> Any:
        try:
            return await asyncio.wait_for(self._exchange(method, params), self.timeout)
        except BaseException:
            # Includes caller cancellation, malformed messages and timed out requests.
            await self.close()
            raise

    async def start(self) -> None:
        env = {**os.environ, "DO_NOT_TRACK": "1", "OTEL_SDK_DISABLED": "true", "PYTHONNOUSERSITE": "1"}
        self.process = await asyncio.create_subprocess_exec(
            *self.command, cwd=self.root, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=8192, **process_options(),
        )
        result = await self.request("initialize", {
            "processId": os.getpid(), "rootUri": self.root.as_uri(),
            "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
            "clientInfo": {"name": "local-coder-readonly"},
            "capabilities": {"general": {"positionEncodings": ["utf-16"]},
                "workspace": {"applyEdit": False, "configuration": False},
                "textDocument": {"documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "diagnostic": {"dynamicRegistration": False, "relatedDocumentSupport": False},
                    "publishDiagnostics": {"versionSupport": True}}},
        })
        if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
            await self.close()
            raise ValueError("Malformed LSP initialize result")
        self.capabilities = result["capabilities"]
        if self.capabilities.get("positionEncoding", "utf-16") != "utf-16":
            await self.close()
            raise ValueError("Language server did not negotiate UTF-16 positions")
        await asyncio.wait_for(self.notify("initialized", {}), self.timeout)

    async def open_document(self, path: Path, language: str, text: str) -> int:
        uri = path.as_uri()
        previous = self.documents.get(uri)
        if previous and previous[1] == text:
            return previous[0]
        version = previous[0] + 1 if previous else 1
        self.diagnostics.pop(uri, None)
        if previous:
            sync = self.capabilities.get("textDocumentSync", 0)
            change = sync.get("change", 0) if isinstance(sync, dict) else sync
            if change != 1:
                # A range-less change is permitted for full sync only; reopen for incremental servers.
                await self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
                previous = None
            else:
                await self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]})
        if not previous:
            await self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": language, "version": version, "text": text}})
        self.documents[uri] = version, text
        return version

    async def close(self) -> None:
        process, self.process = self.process, None
        self.documents.clear()
        self.diagnostics.clear()
        if process:
            await terminate_tree(process)


class LanguageService:
    """Local context. Lines are 1-based; character offsets are Unicode codepoints."""

    adapters = ADAPTERS

    def __init__(self, project_root: str | Path, server_commands: dict[str, Sequence[str] | None] | None = None,
                 request_timeout: float = 3.0, max_message_bytes: int = 2_000_000,
                 max_file_bytes: int = 1_000_000, auto_discover: bool = True):
        self.project_root = Path(project_root).resolve()
        self.server_commands = server_commands or {}
        self.request_timeout = max(0.05, min(float(request_timeout), 30.0))
        self.max_message_bytes = max(1024, min(int(max_message_bytes), 8_000_000))
        self.max_file_bytes = max(1, min(int(max_file_bytes), self.max_message_bytes // 2))
        self.auto_discover = auto_discover
        self._clients: dict[str, _LSPClient] = {}
        self._errors: dict[str, str] = {}
        self._parser_errors: dict[str, str] = {}
        self._parsers: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, value: str | Path) -> Path:
        candidate = self.project_root / value
        if not candidate.is_relative_to(self.project_root):
            raise ValueError("Path escapes workspace")
        relative = candidate.relative_to(self.project_root)
        if ".." in relative.parts:
            raise ValueError("Path traversal is not allowed")
        current = self.project_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Language context does not traverse symlinks")
        if any(part.lower() in _EXCLUDED for part in relative.parts):
            raise ValueError("State or dependency paths are excluded")
        name = candidate.name.lower()
        if name.startswith(".env") or name in _SECRET_NAMES or candidate.suffix.lower() in _SECRET_SUFFIXES:
            raise ValueError("Secret files are excluded")
        if not candidate.is_file() or candidate.stat().st_size > self.max_file_bytes:
            raise ValueError("Source file is missing or exceeds size limit")
        return candidate

    def _read(self, path: Path) -> str:
        # Revalidate every read, including references returned by an untrusted server.
        path = self._path(path)
        if os.name != "nt" and hasattr(os, "O_NOFOLLOW"):
            # Directory descriptors prevent a concurrent symlink swap from escaping the root.
            directory = os.open(self.project_root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                relative = path.relative_to(self.project_root)
                for part in relative.parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = child
                descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(descriptor, "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise ValueError("Source must be a regular file")
                    data = handle.read(self.max_file_bytes + 1)
            finally:
                os.close(directory)
        else:
            with path.open("rb") as handle:
                data = handle.read(self.max_file_bytes + 1)
        if len(data) > self.max_file_bytes or b"\0" in data:
            raise ValueError("Source file is binary or exceeds size limit")
        return data.decode("utf-8")

    def _parser_installed(self, adapter: LanguageAdapter) -> bool:
        # Never import a similarly named grammar module from the workspace.
        for module in ("tree_sitter", adapter.grammar):
            if not _installed(module):
                return False
            spec = importlib.util.find_spec(module)
            if not spec or not spec.origin or Path(spec.origin).resolve().is_relative_to(self.project_root):
                return False
        return True

    @staticmethod
    def _adapter(path: Path) -> LanguageAdapter | None:
        return next((a for a in ADAPTERS if path.suffix.lower() in {e.lower() for e in a.extensions}), None)

    def _command(self, adapter: LanguageAdapter) -> tuple[str, ...] | None:
        if adapter.language in self.server_commands:
            configured = self.server_commands[adapter.language]
            if configured is None:
                return None
            if isinstance(configured, str) or not configured or not all(isinstance(s, str) for s in configured):
                raise ValueError("Server commands must be argument arrays")
            executable = Path(configured[0])
            if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
                raise ValueError("Configured language server executable must be an executable absolute path")
            return tuple(configured)
        if not self.auto_discover:
            return None
        executable = shutil.which(adapter.server_command[0])
        if not executable:
            return None
        resolved = Path(executable).resolve()
        if resolved.is_relative_to(self.project_root):
            return None
        return (str(resolved), *adapter.server_command[1:])

    def status(self) -> dict:
        languages = []
        for adapter in ADAPTERS:
            try:
                command = self._command(adapter)
                error = self._errors.get(adapter.language)
            except ValueError as exc:
                command, error = None, str(exc)
            client = self._clients.get(adapter.language)
            languages.append({"language": adapter.language, "extensions": list(adapter.extensions),
                "server_available": bool(command), "server_running": bool(client and client.process and client.process.returncode is None),
                "server_error": error, "parser": "python-ast" if adapter.language == "python" else "tree-sitter" if self._parser_installed(adapter) else None,
                "parser_error": self._parser_errors.get(adapter.language),
                "grammar_module": adapter.grammar})
        return {"adapter_count": len(ADAPTERS), "languages": languages,
                "note": "Adapters require installed local servers/parsers; availability does not guarantee live semantic support."}

    def syntax_symbols(self, path: str | Path) -> list[dict]:
        full = self._path(path)
        text = self._read(full)
        adapter = self._adapter(full)
        if adapter and adapter.language == "python":
            try:
                tree = ast.parse(text, filename=str(full))
            except SyntaxError:
                self._parser_errors["python"] = "Python AST could not parse this file"
                return []
            self._parser_errors.pop("python", None)
            return [{"name": node.name, "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                     "line": node.lineno, "end_line": node.end_lineno, "provider": "python-ast"}
                    for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))][:200]
        if not adapter or not self._parser_installed(adapter):
            return []
        try:
            parser = self._parsers.get(adapter.language)
            if parser is None:
                tree_sitter = importlib.import_module("tree_sitter")
                grammar = importlib.import_module(adapter.grammar)
                language = tree_sitter.Language(getattr(grammar, adapter.grammar_function)())
                parser = tree_sitter.Parser(language)
                self._parsers[adapter.language] = parser
            encoded = text.encode("utf-8")
            tree = parser.parse(encoded)
            stack, result = [tree.root_node], []
            while stack and len(result) < 200:
                node = stack.pop()
                if node.type in _SYMBOL_NODES:
                    name = node.child_by_field_name("name")
                    if name:
                        result.append({"name": encoded[name.start_byte:name.end_byte].decode("utf-8")[:200],
                                       "kind": node.type, "line": node.start_point[0] + 1,
                                       "end_line": node.end_point[0] + 1, "provider": "tree-sitter"})
                stack.extend(reversed(node.named_children))
            self._parser_errors.pop(adapter.language, None)
            return result
        except (ImportError, TypeError, ValueError, AttributeError) as exc:
            self._parser_errors[adapter.language] = f"Installed parser unavailable: {type(exc).__name__}"
            return []

    def _locations(self, raw: Any) -> list[dict]:
        result = []
        for location in (raw if isinstance(raw, list) else [raw])[:100]:
            if not isinstance(location, dict):
                continue
            uri = location.get("uri", location.get("targetUri", ""))
            if not isinstance(uri, str):
                continue
            try:
                parsed = urlsplit(uri)
                if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
                    continue
                path = self._path(Path(unquote(parsed.path)))
                if self._adapter(path) is None:
                    continue
                text = self._read(path)
                span = location.get("range", location.get("targetSelectionRange", {}))
                if not isinstance(span, dict) or not isinstance(span.get("start"), dict):
                    continue
                start = span["start"]
                line, character = int(start["line"]) + 1, int(start["character"])
                if line < 1 or line > len(_lines(text)) or character < 0:
                    continue
                result.append({"path": path.relative_to(self.project_root).as_posix(), "line": line,
                               "character": _unicode_character(text, line, character), "provider": "lsp"})
            except (KeyError, TypeError, ValueError, OSError, UnicodeError):
                continue
        return result

    def _symbols(self, raw: Any, path: Path) -> list[dict]:
        result, stack = [], list(reversed(raw if isinstance(raw, list) else []))
        while stack and len(result) < 200:
            symbol = stack.pop()
            if not isinstance(symbol, dict):
                continue
            if "location" in symbol:
                locations = self._locations(symbol["location"])
                if not locations:
                    continue
                line = locations[0]["line"]
            else:
                span = symbol.get("selectionRange", symbol.get("range", {}))
                start = span.get("start") if isinstance(span, dict) else None
                if not isinstance(start, dict) or not isinstance(start.get("line"), int) or start["line"] < 0:
                    continue
                line = start["line"] + 1
            result.append({"name": str(symbol.get("name", ""))[:200], "kind": symbol.get("kind"), "line": line, "provider": "lsp"})
            children = symbol.get("children", [])
            if isinstance(children, list):
                stack.extend(reversed(children))
        return result

    @staticmethod
    def _hover(raw: Any) -> str:
        contents = raw.get("contents", "") if isinstance(raw, dict) else ""
        def render(value: Any) -> str:
            if isinstance(value, str):
                return value
            return str(value.get("value", "")) if isinstance(value, dict) else ""
        return "\n".join(render(value) for value in contents)[:2000] if isinstance(contents, list) else render(contents)[:2000]

    @staticmethod
    def _diagnostics(items, text: str) -> list[dict]:
        """Bound and validate server evidence; never follow related document links."""
        if not isinstance(items, list):
            return []
        result = []
        lines = _lines(text)
        for item in items[:50]:
            if not isinstance(item, dict) or not isinstance(item.get("message"), str):
                continue
            span = item.get("range")
            start = span.get("start") if isinstance(span, dict) else None
            if not isinstance(start, dict):
                continue
            line, character = start.get("line"), start.get("character")
            severity = item.get("severity", 1)
            if (type(line) is not int or not 0 <= line < len(lines)
                    or type(character) is not int or character < 0
                    or type(severity) is not int or severity not in {1, 2, 3, 4}):
                continue
            result.append({"line": line + 1, "character": _unicode_character(text, line + 1, character),
                           "severity": severity, "message": re.sub(r"[\x00-\x1f\x7f]", " ", item["message"])[:1000],
                           "source": item.get("source", "")[:100] if isinstance(item.get("source", ""), str) else "",
                           "provider": "lsp"})
        return result

    @staticmethod
    def _python_relations(text: str, path: str, symbol: str) -> dict:
        """Bounded static candidates in one source snapshot, never runtime claims."""
        result = {"definitions": [], "references": [], "callers": []}
        if not symbol or not re.fullmatch(r"[A-Za-z_]\w*", symbol):
            return result
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return result
        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            location = {"path": path, "line": getattr(node, 'lineno', 1),
                        "character": getattr(node, 'col_offset', 0), "provider": "python-ast-static"}
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
                result['definitions'].append({**location, 'name': symbol, 'end_line': node.end_lineno})
            if isinstance(node, ast.Name) and node.id == symbol or isinstance(node, ast.Attribute) and node.attr == symbol:
                if len(result['references']) < 20:
                    result['references'].append(location)
            if isinstance(node, ast.Call) and (isinstance(node.func, ast.Name) and node.func.id == symbol or isinstance(node.func, ast.Attribute) and node.func.attr == symbol):
                containers = [function for function in functions if function.lineno <= node.lineno <= (function.end_lineno or function.lineno)]
                caller = min(containers, key=lambda function: (function.end_lineno or function.lineno) - function.lineno) if containers else None
                if len(result['callers']) < 20:
                    result['callers'].append({**location, 'name': caller.name if caller else '<module>', 'resolution': 'static-name-candidate; runtime dispatch unproven'})
        result['definitions'] = result['definitions'][:20]
        return result

    async def context(self, path: str | Path, line: int = 1, character: int = 0,
                      query: str | None = None, char_budget: int = 4000) -> dict:
        full = self._path(path)
        text = self._read(full)
        adapter = self._adapter(full)
        line = min(max(1, int(line)), len(_lines(text)))
        character = max(0, int(character))
        budget = max(128, min(int(char_budget), 16000))
        symbols = self.syntax_symbols(full)
        result = {"path": full.relative_to(self.project_root).as_posix(), "language": adapter.language if adapter else "unknown",
                  "symbols": symbols, "definitions": [], "references": [], "diagnostics": [], "hover": "", "providers": sorted({s["provider"] for s in symbols}),
                  "availability": {"semantic": False, "diagnostics": "unavailable", "parser": "python-ast" if adapter and adapter.language == "python" else None}}
        if adapter:
            lock = self._locks.setdefault(adapter.language, asyncio.Lock())
            async with lock:
                client = self._clients.get(adapter.language)
                try:
                    command = self._command(adapter)
                    result["availability"]["server_available"] = bool(command)
                    if command:
                        if not client or not client.process or client.process.returncode is not None:
                            client = _LSPClient(command, self.project_root, self.request_timeout, self.max_message_bytes)
                            self._clients[adapter.language] = client
                            await client.start()
                        result["version"] = await asyncio.wait_for(client.open_document(full, adapter.language, text), self.request_timeout)
                        position = {"line": line - 1, "character": _utf16_character(text, line, character)}
                        params = {"textDocument": {"uri": full.as_uri()}, "position": position}
                        for capability, method, key in (
                            ("documentSymbolProvider", "documentSymbol", "symbols"),
                            ("definitionProvider", "definition", "definitions"),
                            ("referencesProvider", "references", "references"),
                            ("hoverProvider", "hover", "hover"),
                        ):
                            advertised = client.capabilities.get(capability)
                            if advertised is None or advertised is False:
                                continue
                            arguments = {"textDocument": params["textDocument"]} if method == "documentSymbol" else dict(params)
                            if method == "references":
                                arguments["context"] = {"includeDeclaration": True}
                            raw = await client.request(f"textDocument/{method}", arguments)
                            result[key] = self._symbols(raw, full) if key == "symbols" else self._hover(raw) if key == "hover" else self._locations(raw)
                        diagnostic_provider = client.capabilities.get("diagnosticProvider")
                        if diagnostic_provider is not None and diagnostic_provider is not False:
                            raw = await client.request("textDocument/diagnostic", {"textDocument": params["textDocument"]})
                            if not isinstance(raw, dict) or raw.get("kind") != "full" or not isinstance(raw.get("items"), list):
                                raise ValueError("Expected a full LSP diagnostic report")
                            result["diagnostics"] = self._diagnostics(raw["items"], text)
                            result["availability"]["diagnostics"] = "pull"
                        else:
                            published = client.diagnostics.get(full.as_uri())
                            if published and published[0] == result["version"]:
                                result["diagnostics"] = self._diagnostics(published[1], text)
                                result["availability"]["diagnostics"] = "versioned-push"
                        if self._read(full) != text:
                            result["diagnostics"] = []
                            result["availability"]["diagnostics"] = "stale-source"
                        result["providers"] = sorted(set(result["providers"]) | {"lsp"})
                        result["availability"]["semantic"] = any(client.capabilities.get(k) is not None and client.capabilities.get(k) is not False for k in ("definitionProvider", "referencesProvider", "hoverProvider"))
                        self._errors.pop(adapter.language, None)
                except asyncio.CancelledError:
                    if client:
                        await client.close()
                    raise
                except (OSError, ValueError, TypeError, UnicodeError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
                    if client:
                        await client.close()
                    result["diagnostics"] = []
                    result["availability"]["diagnostics"] = "unavailable"
                    self._errors[adapter.language] = f"LSP unavailable: {type(exc).__name__}"
                if adapter.language in self._errors:
                    result["availability"]["error"] = self._errors[adapter.language]
                result["availability"]["parser"] = "python-ast" if adapter.language == "python" else "tree-sitter" if adapter.language in self._parsers else None
                if adapter.language in self._parser_errors:
                    result["availability"]["parser_error"] = self._parser_errors[adapter.language]
        result['callers'] = []
        result['availability']['reference_coverage'] = 'language-server' if result['availability']['semantic'] else 'unavailable'
        if adapter and adapter.language == 'python':
            cursor = _lines(text)[line - 1]
            selected_name = query or next((match.group() for match in re.finditer(r'[A-Za-z_]\w*', cursor)
                                          if match.start() <= character <= match.end()), '')
            relations = self._python_relations(text, result['path'], selected_name)
            for key in ('definitions', 'references'):
                if not result[key]:
                    result[key] = relations[key]
            result['callers'] = relations['callers']
            if not result['availability']['semantic']:
                result['availability']['reference_coverage'] = 'current-file-static-only'
        if query:
            result["symbols"] = sorted(result["symbols"], key=lambda s: query.lower() not in s["name"].lower())
        lines = _lines(text)
        focus = line
        if query:
            local_definition = next((definition for definition in result['definitions'] if definition['path'] == result['path']), None)
            if local_definition:
                focus = local_definition['line']
        excerpt = "\n".join(f"{i + 1}: {lines[i][:300]}" for i in range(max(0, focus - 1), min(len(lines), focus + 12)))[:2000]
        title = f"Language context: {result['path']} ({result['language']})\n"
        if not result["availability"]["semantic"]:
            title += "Semantic context unavailable; showing bounded fallback context.\n"
        rows = [f"Diagnostics: {result['availability']['diagnostics']} ({len(result['diagnostics'])} reported; capped at 50)"]
        rows.extend(f"diagnostic: {d['line']}:{d['character']} severity={d['severity']} {d['message']}" for d in result["diagnostics"])
        rows.extend(f"{s['line']}: {s['name']} [{s['provider']}]" for s in result["symbols"][:40])
        rows.extend(f"caller: {s['name']} at {s['path']}:{s['line']} [static candidate]" for s in result['callers'][:20])
        for key in ("definitions", "references"):
            rows.extend(f"{key}: {s['path']}:{s['line']}:{s['character']}" for s in result[key][:20])
        result["text"] = (title + "\n".join(rows) + "\n" + result["hover"] + "\n" + excerpt)[:budget]
        # Structured collections are bounded too; callers consume text for prompt injection.
        result["symbols"] = result["symbols"][:40]
        result["definitions"] = result["definitions"][:20]
        result["references"] = result["references"][:20]
        return result

    async def close(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        await asyncio.gather(*(client.close() for client in clients))
