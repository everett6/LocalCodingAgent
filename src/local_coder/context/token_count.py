"""Offline tokenizer measurements alongside conservative admission bounds.

Provider adapters may rewrite messages and tool schemas. A local chat template
therefore measures a component, never establishes an exact provider request.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat


_MAX_MEASUREMENT_BYTES = 64 * 1024
_MAX_MESSAGES = 256
_MAX_TOOLS = 128
_MAX_TEMPLATE_BYTES = 256 * 1024
_MAX_ASSET_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_ASSET_BYTES = 64 * 1024 * 1024
_MAX_ASSET_FILES = 64
_ASSET_NAMES = {
    'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json',
    'added_tokens.json', 'vocab.json', 'vocab.txt', 'merges.txt',
    'tokenizer.model', 'spiece.model', 'sentencepiece.bpe.model',
    'config.json', 'chat_template.jinja',
}


def _serialized_bytes(value):
    # Keep the conservative byte count complete without retaining a second full
    # request string. Oversized input still reserves every serialized UTF-8 byte.
    return sum(len(chunk.encode('utf-8')) for chunk in json.JSONEncoder(
        ensure_ascii=False, default=str).iterencode(value))


def _input_reason(messages, tools):
    if not isinstance(messages, list) or len(messages) > _MAX_MESSAGES:
        return 'Local tokenizer message count exceeds its bounded input limit'
    if tools is not None and (not isinstance(tools, list) or len(tools) > _MAX_TOOLS):
        return 'Local tokenizer tool count exceeds its bounded input limit'
    try:
        if _serialized_bytes({'messages': messages, 'tools': tools}) > _MAX_MEASUREMENT_BYTES:
            return 'Local tokenizer serialized input exceeds its bounded byte limit; conservative estimate retained'
    except (ValueError, TypeError, RecursionError):
        return 'Local tokenizer input is not compatible; conservative estimate retained'
    return None


def _validate_assets(path):
    """Reject external references and unbounded or executable loader assets."""
    folder = Path(os.path.abspath(path))
    if any(part.is_symlink() for part in (folder, *folder.parents)) or not folder.is_dir():
        raise ValueError('Choose a regular local tokenizer directory without links')
    total = count = 0
    for directory, dirs, files in os.walk(folder, followlinks=False):
        base = Path(directory)
        for name in dirs:
            child = base / name
            if child.is_symlink() or not child.is_dir() or child.relative_to(folder).parts != ('chat_templates',):
                raise ValueError('Unsupported tokenizer asset directory')
        for name in files:
            asset = base / name
            relative = asset.relative_to(folder)
            if not (len(relative.parts) == 1 and name in _ASSET_NAMES or len(relative.parts) == 2 and relative.parts[0] == 'chat_templates' and asset.suffix == '.jinja'):
                raise ValueError('Only tokenizer assets are supported in the selected directory')
            descriptor = os.open(asset, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(descriptor, 'rb') as stream:
                details = os.fstat(stream.fileno())
                if asset.is_symlink() or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or not asset.resolve().is_relative_to(folder):
                    raise ValueError('Tokenizer assets must be workspace-contained regular files without links')
                limit = _MAX_TEMPLATE_BYTES if asset.suffix == '.jinja' else 1024 * 1024 if name in {'config.json', 'tokenizer_config.json', 'special_tokens_map.json', 'added_tokens.json'} else _MAX_ASSET_BYTES
                count += 1
                total += details.st_size
                if details.st_size > limit or count > _MAX_ASSET_FILES or total > _MAX_TOTAL_ASSET_BYTES:
                    raise ValueError('Tokenizer assets exceed the bounded file or byte limit')
                if name in {'config.json', 'tokenizer_config.json', 'special_tokens_map.json', 'added_tokens.json'}:
                    raw = stream.read(limit + 1)
                    if len(raw) > limit:
                        raise ValueError('Tokenizer configuration exceeds its byte limit')
                    value = json.loads(raw)
                    pending = [(None, value)]
                    visited = 0
                    while pending:
                        key, item = pending.pop()
                        visited += 1
                        if visited > 10000:
                            raise ValueError('Tokenizer configuration exceeds its structure limit')
                        if isinstance(item, dict):
                            pending.extend(item.items())
                        elif isinstance(item, list):
                            pending.extend((key, child) for child in item)
                        elif key in {'chat_template', 'template'} and isinstance(item, str) and len(item.encode()) > _MAX_TEMPLATE_BYTES:
                            raise ValueError('Tokenizer chat template exceeds its byte limit')
                        elif isinstance(key, str) and (key.endswith(('_file', '_files')) or key in {'tokenizer_file', 'vocab_file', 'merges_file'}) and isinstance(item, str):
                            reference = Path(item)
                            if reference.is_absolute() or '..' in reference.parts or not (folder / reference).resolve().is_relative_to(folder):
                                raise ValueError('Tokenizer configuration references an external asset')
    return folder


def request_accounting(messages, tools, output, config, counter=None):
    payload = [m.model_dump(mode="json") if hasattr(m, "model_dump") else m for m in messages]
    prompt = _serialized_bytes({"messages": payload, "tools": tools}) + 512
    tool_bytes = _serialized_bytes(tools) if tools else 0
    result = {
        "method": "conservative_utf8", "source": "serialized_messages_tools_with_framing",
        "semantics": "estimated", "prompt_reserved": prompt,
        "tool_schema_bytes": tool_bytes, "tool_schema_reserved": tool_bytes,
        "tool_reserve_included": True,
        "framing_reserved": 512, "output_reserved": output,
        "reason": "No local tokenizer configured",
    }
    if counter is not None:
        reason = _input_reason(payload, tools)
        result.update({"reason": reason} if reason else counter.measure(payload, tools, str(getattr(config, "model_id", ""))))
        result["prompt_reserved"] = max(prompt, result.get("component_tokens", 0))
    return result


class LocalTokenCounter:
    """Load only explicitly selected local files, with remote code disabled."""

    def __init__(self, path, model_id, template_sha256, *, tokenizer=None):
        self.model_id = model_id
        self.template_sha256 = template_sha256
        self.tokenizer = tokenizer
        self.reason = None
        if tokenizer is None:
            try:
                path = _validate_assets(path)
                from transformers import AutoTokenizer
                self.tokenizer = AutoTokenizer.from_pretrained(
                    str(path), local_files_only=True, trust_remote_code=False,
                )
            except Exception:
                # Loader errors can contain paths, template contents or credentials.
                self.reason = "Local tokenizer unavailable or incompatible"

    def status(self):
        return {"configured": True, "available": self.tokenizer is not None,
                "adapter": "transformers_local", "semantics": "estimated",
                "reason": self.reason,
                "provider_serialization_verified": False}

    def measure(self, messages, tools, model_id):
        input_reason = _input_reason(messages, tools)
        if input_reason:
            return {"reason": input_reason}
        fallback = {"reason": self.reason or "Model or chat template compatibility not verified"}
        if self.tokenizer is None or model_id != self.model_id:
            return fallback
        template = getattr(self.tokenizer, "chat_template", None)
        if not isinstance(template, str) or len(template) > _MAX_TEMPLATE_BYTES or len(template.encode()) > _MAX_TEMPLATE_BYTES:
            return {"reason": "Local chat template exceeds its bounded byte limit or is unavailable"}
        if hashlib.sha256(template.encode()).hexdigest() != self.template_sha256:
            return fallback
        if tools and (not isinstance(tools, list) or any(
            not isinstance(tool, dict) or tool.get("type") != "function"
            or not isinstance(tool.get("function"), dict)
            or not isinstance(tool["function"].get("name"), str)
            or not isinstance(tool["function"].get("parameters"), dict)
            for tool in tools
        )):
            return {"reason": "Tool schema format is not compatible with the local chat template adapter"}
        try:
            options = dict(tokenize=True, add_generation_prompt=True)
            if tools:
                options["tools"] = tools
            ids = self.tokenizer.apply_chat_template(messages, **options)
            if tools and ids == self.tokenizer.apply_chat_template(
                messages, tokenize=True, add_generation_prompt=True,
            ):
                return {"reason": "Selected chat template does not account for tool schemas"}
            if not isinstance(ids, list) or len(ids) > 1_000_000 or any(type(item) is not int for item in ids):
                return {"reason": "Unsupported local tokenizer result"}
            # This component includes role markers, generation framing and tools
            # passed to the template, but cannot prove the provider's rendering.
            return {"method": "local_chat_template_with_conservative_admission",
                    "source": "transformers_local", "component_tokens": len(ids),
                    "component_method": "exact_local_chat_template",
                    "component_scope": "local_template_rendering_only",
                    "reason": "Provider serialization is unverified; admission remains conservative"}
        except Exception:
            return {"reason": "Local tokenizer failed; conservative estimate retained"}


def tokenizer_config(root, body):
    if not isinstance(body, dict) or set(body) - {"path", "model_id", "template_sha256", "enabled"}:
        raise ValueError("Unsupported tokenizer settings")
    enabled = body.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    if not enabled:
        return {"enabled": False}
    path, model, digest = (body.get(key) for key in ("path", "model_id", "template_sha256"))
    if not isinstance(path, str) or not 1 <= len(path) <= 2048:
        raise ValueError("Select a local tokenizer directory")
    candidate = Path(os.path.abspath(Path(root) / path))
    if any(part.is_symlink() for part in (candidate, *candidate.parents)):
        raise ValueError("Tokenizer directory must be inside the workspace without links")
    if not candidate.resolve().is_relative_to(Path(root).resolve()) or not candidate.is_dir():
        raise ValueError("Tokenizer directory must exist inside the workspace")
    if not isinstance(model, str) or not 1 <= len(model) <= 256 or any(ord(c) < 32 for c in model):
        raise ValueError("Specify the matching model identity")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Specify the SHA-256 of the compatible chat template")
    return {"enabled": True, "path": str(candidate.resolve().relative_to(Path(root).resolve())),
            "model_id": model, "template_sha256": digest}


def adapter_availability():
    return {"adapter": "transformers_local", "installed": importlib.util.find_spec("transformers") is not None,
            "downloads_allowed": False, "remote_code_allowed": False}


def load_counter_for_project(root):
    """Load optional host settings; invalid/unavailable files retain estimates."""
    try:
        target = Path(root).resolve() / ".local-coder/context/tokenizer.json"
        if any(path.is_symlink() for path in (target, *target.parents)):
            return None
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64000 or info.st_nlink != 1:
                return None
            if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
                return None
            settings = tokenizer_config(root, json.loads(stream.read(64001)))
        if not settings.get("enabled"):
            return None
        return LocalTokenCounter(Path(root) / settings["path"], settings["model_id"], settings["template_sha256"])
    except Exception:
        return None
