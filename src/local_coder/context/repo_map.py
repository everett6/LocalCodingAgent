"""Ranked repository map.

A compact outline of the files and definitions that matter most for a task,
in the spirit of aider's repo map. It is built from the code index
(``code_index.py``) rather than a second parser: the index already knows
every function and class in each file, and each chunk's term counts show
which names a file mentions.

Files form a graph: file A links to file B when A mentions a name B defines.
PageRank over that graph, personalized toward the files and names the task
mentions, ranks the files; each file's rank is then shared out over the
definitions its referrers use. The top definitions are rendered as
``path:`` headers with numbered signature lines, trimmed to a token budget.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from local_coder.context.code_index import CodeIndex, tokenize

DEFAULT_MAP_TOKENS = 1024
CHARS_PER_TOKEN = 4
MAX_LINE_CHARS = 100
MAX_EXTRA_FILES = 10  # bare file names listed after the outline

DAMPING = 0.85
ITERATIONS = 40

# Edge weight multipliers, after aider's.
MENTIONED_MUL = 10.0  # the task names this identifier
DISTINCT_MUL = 3.0  # long compound names (parse_config, CodeIndex) are rarely coincidental
PRIVATE_MUL = 0.1  # _helpers are rarely what another file is "about"
COMMON_MUL = 0.1  # a name defined in many files says little about any of them
COMMON_DEFS = 5
# One-word names (get, run, status) collide with ordinary words in comments
# and other code, so a mention is weak evidence of a real reference.
SINGLE_WORD_MUL = 0.05

# Out-weight below which a file keeps part of its rank for teleporting
# instead of pushing it all through a few weak links.
MIN_OUT_WEIGHT = 1.0

QUERY_HITS = 30  # code index matches used to steer the map
# Bonuses, as fractions of the top structural rank, for what the task
# matches (scaled by match quality), names outright, or is about.
QUERY_BONUS = 1.0
NAMED_BONUS = 2.0
FOCUS_BONUS = 0.5


@dataclass
class Definition:
    path: str
    symbol: str  # dotted, e.g. CodeIndex.search
    kind: str
    line: int
    rank: float = 0.0

    @property
    def name(self) -> str:
        return self.symbol.rsplit(".", 1)[-1]


def ref_key(name: str) -> str | None:
    """The index term a mention of ``name`` produces.

    Compound names (``parse_config``, ``CodeIndex``) are indexed whole, so
    the whole lowered name is the key; a one-word name is indexed stemmed.
    """
    if not name or (name.startswith("__") and name.endswith("__")):
        return None
    terms = tokenize(name)
    return terms[0] if terms else None


def _is_compound(name: str) -> bool:
    return len(tokenize(name)) > 1


class RepoMap:
    """Builds ranked maps of a project from its code index."""

    def __init__(self, project_root: str, index: CodeIndex | None = None):
        self.index = index or CodeIndex(project_root)
        self.project_root = self.index.project_root

    # --- ranking ---

    def rank(
        self,
        query: str = "",
        focus_files: list[str] | None = None,
    ) -> tuple[list[Definition], dict[str, float]]:
        """Rank definitions and files for a task.

        ``query`` is the task text: names and paths it mentions pull the
        ranking toward them, and the code index's best files for it seed
        PageRank. ``focus_files`` are files the task is known to be about.
        """
        files = self.index.file_chunks()

        defs: dict[tuple[str, str], Definition] = {}
        definers: dict[str, set[str]] = defaultdict(set)
        key_names: dict[str, str] = {}
        for path, chunks in files.items():
            for start, _end, kind, symbol, _tf in chunks:
                if not symbol or kind not in ("function", "class"):
                    continue
                name = symbol.rsplit(".", 1)[-1]
                key = ref_key(name)
                if key is None:
                    continue
                existing = defs.get((path, symbol))
                if existing is None or start < existing.line:
                    defs[(path, symbol)] = Definition(path, symbol, kind, start)
                definers[key].add(path)
                key_names.setdefault(key, name)

        query_terms = set(tokenize(query)) if query else set()
        focus = {os.path.normpath(f).replace(os.sep, "/") for f in (focus_files or [])}
        focus &= set(files)
        focus.update(self._mentioned_paths(query, files))

        # How often each file mentions each defined name.
        mentions: dict[str, Counter[str]] = {}
        file_df: Counter[str] = Counter()
        for path, chunks in files.items():
            counts: Counter[str] = Counter()
            for chunk in chunks:
                for term, n in chunk[4].items():
                    if term in definers:
                        counts[term] += n
            mentions[path] = counts
            file_df.update(counts.keys())

        # Edge weights: referencing file -> {(defining file, key): weight}
        n_files = len(files)
        edges: dict[str, dict[tuple[str, str], float]] = defaultdict(dict)
        for path, counts in mentions.items():
            for key, n in counts.items():
                targets = definers[key] - {path}
                if not targets:
                    continue
                name = key_names[key]
                mul = 1.0
                if key in query_terms:
                    mul *= MENTIONED_MUL
                if not _is_compound(name):
                    mul *= SINGLE_WORD_MUL
                elif len(name) >= 8:
                    mul *= DISTINCT_MUL
                if name.startswith("_"):
                    mul *= PRIVATE_MUL
                if len(definers[key]) > COMMON_DEFS:
                    mul *= COMMON_MUL
                # A name most files mention (test, model, config) is
                # background vocabulary, not a dependency.
                idf = math.log((n_files + 1) / file_df[key])
                weight = mul * idf * math.sqrt(n) / len(targets)
                if weight <= 0:
                    continue
                for target in targets:
                    edges[path][(target, key)] = weight

        hit_files, hit_defs = self._query_hits(query, files)
        personalization = dict(hit_files)
        for f in focus:
            personalization[f] = personalization.get(f, 0.0) + 1.0
        file_rank = _pagerank(list(files), edges, personalization)

        # Share each file's rank over the definitions it points at.
        key_rank: dict[tuple[str, str], float] = defaultdict(float)
        for src, out in edges.items():
            denom = max(sum(out.values()), MIN_OUT_WEIGHT)
            for target_key, w in out.items():
                key_rank[target_key] += file_rank.get(src, 0.0) * w / denom

        defs_by_file: dict[str, list[Definition]] = defaultdict(list)
        for d in defs.values():
            defs_by_file[d.path].append(d)
        for path, file_defs in defs_by_file.items():
            # A small floor from the file's own rank orders definitions
            # nothing references yet (new code, entry points).
            floor = file_rank.get(path, 0.0) * 0.01 / len(file_defs)
            for d in file_defs:
                d.rank = key_rank.get((path, ref_key(d.name)), 0.0) + floor

        # What the task names or matches ranks alongside what is central:
        # boosts are added on the scale of the top structural rank, since
        # the matching code may be new or referenced by nothing.
        top = max((d.rank for d in defs.values()), default=0.0) or 1.0
        for d in defs.values():
            bonus = QUERY_BONUS * hit_defs.get((d.path, d.symbol), 0.0)
            if _is_compound(d.name) and ref_key(d.name) in query_terms:
                bonus += NAMED_BONUS
            if d.path in focus:
                bonus += FOCUS_BONUS
            d.rank += bonus * top

        ranked = sorted(defs.values(), key=lambda d: (-d.rank, d.path, d.line))
        return ranked, file_rank

    def _mentioned_paths(self, query: str, files: dict[str, list]) -> set[str]:
        if not query:
            return set()
        words = set(re.findall(r"[\w./-]+", query))
        found = set()
        for path in files:
            base = os.path.basename(path)
            if path in words or (base in words and "." in base):
                found.add(path)
        return found

    def _query_hits(self, query: str, files: dict[str, list]) -> tuple[dict[str, float], dict[tuple[str, str], float]]:
        """Score files and definitions by how well the code index matches the task.

        Returns per-file weights (for PageRank's teleport vector) and
        per-definition boosts, both scaled so the best match is 1.0.
        """
        if not query:
            return {}, {}
        try:
            hits = self.index.search(query, max_results=QUERY_HITS, refresh=False)
        except Exception:
            return {}, {}
        if not hits:
            return {}, {}
        top = hits[0].score or 1.0
        file_w: dict[str, float] = defaultdict(float)
        sym_w: dict[tuple[str, str], float] = {}
        for hit in hits:
            if hit.path not in files:
                continue
            w = hit.score / top
            file_w[hit.path] = max(file_w[hit.path], w)
            if hit.symbol:
                key = (hit.path, hit.symbol)
                sym_w[key] = max(sym_w.get(key, 0.0), w)
        return dict(file_w), sym_w

    # --- rendering ---

    def build(
        self,
        query: str = "",
        focus_files: list[str] | None = None,
        max_tokens: int = DEFAULT_MAP_TOKENS,
    ) -> str:
        """Render the highest-ranked definitions that fit ``max_tokens``."""
        if max_tokens <= 0:
            return ""
        ranked, file_rank = self.rank(query, focus_files)
        budget = max_tokens * CHARS_PER_TOKEN
        lines_cache: dict[str, list[str]] = {}
        by_symbol = {(d.path, d.symbol): d for d in ranked}

        def render(n: int) -> str:
            return self._render(ranked[:n], by_symbol, file_rank, lines_cache, budget)

        # Binary search the number of definitions whose rendering fits.
        lo, hi, best = 0, len(ranked), ""
        while lo <= hi:
            mid = (lo + hi) // 2
            text = render(mid)
            if len(text) <= budget:
                best = text
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _render(
        self,
        chosen: list[Definition],
        by_symbol: dict[tuple[str, str], Definition],
        file_rank: dict[str, float],
        lines_cache: dict[str, list[str]],
        budget: int,
    ) -> str:
        groups: dict[str, dict[str, Definition]] = {}
        for d in chosen:
            group = groups.setdefault(d.path, {})
            group[d.symbol] = d
            # Show a method's class so the method reads in context.
            if "." in d.symbol:
                parent = by_symbol.get((d.path, d.symbol.rsplit(".", 1)[0]))
                if parent:
                    group[parent.symbol] = parent

        out: list[str] = []
        for path, file_defs in groups.items():
            lines = self._lines(path, lines_cache)
            out.append(f"{path}:")
            for d in sorted(file_defs.values(), key=lambda d: d.line):
                n = self._signature_line(d, lines)
                text = lines[n - 1].rstrip() if 0 < n <= len(lines) else d.symbol
                out.append(f"{n:>5}| {text[:MAX_LINE_CHARS]}")

        # Leftover room lists the next most central files by name only.
        shown = set(groups)
        rest = [p for p, _ in sorted(file_rank.items(), key=lambda kv: (-kv[1], kv[0])) if p not in shown]
        text = "\n".join(out)
        extra = []
        for path in rest[:MAX_EXTRA_FILES]:
            if len(text) + sum(len(e) + 1 for e in extra) + len(path) + 1 > budget:
                break
            extra.append(path)
        return "\n".join(out + extra)

    def _lines(self, path: str, cache: dict[str, list[str]]) -> list[str]:
        if path not in cache:
            text = self.index._read_text(self.project_root / path) or ""
            cache[path] = text.splitlines()
        return cache[path]

    @staticmethod
    def _signature_line(d: Definition, lines: list[str]) -> int:
        """The line naming the definition, past any decorators."""
        pattern = re.compile(r"\b" + re.escape(d.name) + r"\b")
        for n in range(d.line, min(d.line + 8, len(lines)) + 1):
            if pattern.search(lines[n - 1]):
                return n
        return d.line


def _pagerank(
    nodes: list[str],
    edges: dict[str, dict[tuple[str, str], float]],
    personalization: dict[str, float],
) -> dict[str, float]:
    """Weighted personalized PageRank by power iteration."""
    if not nodes:
        return {}
    total_p = sum(personalization.values())
    if total_p > 0:
        teleport = {n: personalization.get(n, 0.0) / total_p for n in nodes}
    else:
        teleport = {n: 1.0 / len(nodes) for n in nodes}

    # A file whose links are all weak passes only part of its rank along
    # them; the rest teleports, as a file with no links would.
    out: dict[str, list[tuple[str, float]]] = {}
    kept: dict[str, float] = {}
    for src, targets in edges.items():
        merged: dict[str, float] = defaultdict(float)
        for (dst, _key), w in targets.items():
            merged[dst] += w
        total = sum(merged.values())
        if total > 0:
            denom = max(total, MIN_OUT_WEIGHT)
            out[src] = [(dst, w / denom) for dst, w in merged.items()]
            kept[src] = total / denom

    rank = dict(teleport)
    for _ in range(ITERATIONS):
        nxt = {n: (1 - DAMPING) * teleport[n] for n in nodes}
        dangling = sum(rank[n] * (1 - kept.get(n, 0.0)) for n in nodes)
        for n in nodes:
            nxt[n] += DAMPING * dangling * teleport[n]
        for src, targets in out.items():
            r = rank.get(src, 0.0)
            for dst, share in targets:
                if dst in nxt:
                    nxt[dst] += DAMPING * r * share
        delta = sum(abs(nxt[n] - rank[n]) for n in nodes)
        rank = nxt
        if delta < 1e-8:
            break
    return rank
