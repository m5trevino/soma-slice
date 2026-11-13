"""Local invariant registry.

Scout queries this. Eagle does not write to it.
The registry is the source of truth for laws.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_DEFAULT_RULES = [
    {
        "rule_id": "INV-001",
        "family": "security",
        "statement": "No secrets or credentials in source.",
        "severity": "CRITICAL",
        "applies_to": ["py", "js", "ts", "go", "sh"],
    },
    {
        "rule_id": "INV-002",
        "family": "correctness",
        "statement": "Every public function must have a clear input and output.",
        "severity": "HIGH",
        "applies_to": ["py", "js", "ts", "go"],
    },
    {
        "rule_id": "INV-003",
        "family": "safety",
        "statement": "No unbounded recursion or loops without an exit condition.",
        "severity": "HIGH",
        "applies_to": ["py", "js", "ts", "go"],
    },
    {
        "rule_id": "INV-004",
        "family": "style",
        "statement": "No TODO or placeholder in committed code.",
        "severity": "MEDIUM",
        "applies_to": ["py", "js", "ts", "go", "md"],
    },
]


class InvariantRegistry:
    """Read-only store of laws. Scout is the only caller."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._rules = list(_DEFAULT_RULES)
        if path and path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, list):
                self._rules = data

    def query(self, extensions: list[str] | None = None) -> list[dict[str, Any]]:
        """Return rules that apply to the given file extensions."""
        if not extensions:
            return list(self._rules)
        wanted = {e.lower().lstrip(".") for e in extensions}
        out = []
        for rule in self._rules:
            applies = {a.lower() for a in rule.get("applies_to", [])}
            if not applies or applies & wanted:
                out.append(rule)
        return out


# ── corpus registry: the 604K-law database ───────────────────────────────

DEFAULT_LAW_DB = "/media/storage/invariants/invariants.db"

# FTS5 treats a space-separated query as a phrase, which almost never matches
# a slot mandate. Terms are OR-joined instead so a multi-word mandate behaves
# like a bag of words.
_TERM_RE = re.compile(r"[a-z0-9_]{4,}")


class LawCorpus:
    """Read-only access to the collapsed truth corpus.

    Backs onto the ``truth`` table (611,172 collapsed invariants) and its
    ``truth_fts`` index. These are the canonical records: each one carries a
    composite score, the scope it was collapsed in, and the sources behind it,
    which the raw ``laws`` table does not.

    Retrieval is BM25 over English words from a slot's mandate. There is no
    embedding step.
    """

    def __init__(self, path: str | Path = DEFAULT_LAW_DB) -> None:
        self.path = str(path)
        self._conn = None
        self._available = False
        self._table = ""
        self._fts = ""
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            names = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type IN ('table','view')").fetchall()
            }
            # Prefer the collapsed truth records; fall back to the raw laws
            # table when they have not been built yet.
            if {"truth", "truth_fts"} <= names:
                self._table, self._fts = "truth", "truth_fts"
            elif {"laws", "laws_fts"} <= names:
                self._table, self._fts = "laws", "laws_fts"
            else:
                self._conn = None
                self._available = False
                return
            self._conn = conn
            self._available = True
        except Exception:
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def source(self) -> str:
        return self._table

    def _terms(self, text: str) -> list[str]:
        seen: list[str] = []
        for t in _TERM_RE.findall((text or "").lower()):
            if t not in seen:
                seen.append(t)
        return seen

    def search(self, text: str, limit: int = 200,
               min_confidence: float = 0.0,
               min_score: float = 0.0,
               family: str | None = None) -> list[dict[str, Any]]:
        """Laws whose statement best matches ``text``, ranked by BM25.

        Returns dicts with the fields scout's ``dna_contracts`` needs plus the
        strength signals that make a law rankable rather than merely present.
        """
        if not self._available or not self._conn:
            return []
        terms = self._terms(text)
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)

        if self._table == "truth":
            sql = f"""
                SELECT t.statement, t.family, t.repo, t.scope, t.composite,
                       t.confidence, t.instance_count, t.source_count,
                       bm25({self._fts}) AS score
                FROM {self._fts} f
                JOIN truth t ON f.rowid = t.rowid
                WHERE {self._fts} MATCH ?
                  AND t.composite >= ?
            """
            params: list[Any] = [match, min_score]
            if family:
                sql += " AND t.family = ?"
                params.append(family)
            sql += " ORDER BY bm25({0}) LIMIT ?".format(self._fts)
            params.append(limit)
        else:
            sql = f"""
                SELECT l.statement, l.family, l.max_confidence AS confidence,
                       l.occurrences, l.repo_count, l.repos,
                       bm25({self._fts}) AS score
                FROM {self._fts} f
                JOIN laws l ON f.rowid = l.rowid
                WHERE {self._fts} MATCH ?
                  AND l.max_confidence >= ?
                ORDER BY bm25({self._fts})
                LIMIT ?
            """
            params = [match, min_confidence, limit]

        try:
            rows = self._conn.execute(sql, params).fetchall()
        except Exception:
            return []

        out: list[dict[str, Any]] = []
        for r in rows:
            keys = r.keys()
            item = {
                "statement": r["statement"],
                "family": r["family"],
                "confidence": float(r["confidence"] or 0.0),
                "score": float(r["score"] or 0.0),
            }
            if "composite" in keys:
                item["composite"] = float(r["composite"] or 0.0)
                item["repo"] = r["repo"]
                item["scope"] = r["scope"]
                item["occurrences"] = int(r["instance_count"] or 0)
                item["repo_count"] = int(r["source_count"] or 0)
                item["repos"] = ""
            else:
                item["occurrences"] = int(r["occurrences"] or 0)
                item["repo_count"] = int(r["repo_count"] or 0)
                item["repos"] = r["repos"] or ""
            out.append(item)
        return out

    def count(self) -> int:
        if not self._available or not self._conn:
            return 0
        try:
            return int(self._conn.execute(
                f"SELECT COUNT(*) FROM {self._table}").fetchone()[0])
        except Exception:
            return 0

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._available = False
