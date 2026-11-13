"""SQLite-backed graph store — drop-in replacement for JSON-backed Soma.

A path ending in ``.json`` is also accepted: the JSON graph
(``{"nodes": {...}, "edges": [...]}``) is loaded into an in-memory SQLite
database so every read works through the same query API. Reads are in-memory;
call ``Soma.save()`` to write a JSON graph back.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

_NATURAL_RE = re.compile(r"(\d+)")

# The nodes table lists its permitted node types explicitly. Matches the
# ``node_type ... CHECK(node_type IN ('A', 'B', ...))`` clause so a new node
# type can be added to the list without touching the rest of the definition.
_NODE_TYPE_CHECK_RE = re.compile(
    r"node_type\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*node_type\s+IN\s*\("
    r"(?P<types>[^)]*)\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)

# The edges table lists its permitted relationship types the same way. Node
# types widen automatically; relationship types did not, so introducing a new
# edge type failed at insert time with a CHECK constraint error.
_REL_TYPE_CHECK_RE = re.compile(
    r"rel_type\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*rel_type\s+IN\s*\("
    r"(?P<types>[^)]*)\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)


def natural_key(value: Any) -> tuple:
    """Sort key that orders ``a.2`` before ``a.10``."""
    parts = _NATURAL_RE.split(str(value if value is not None else ""))
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)


def _now_iso() -> str:
    """ISO-8601 UTC timestamp with microseconds (graph-node format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _message_text(msg: Any) -> str:
    """Flatten a message's ``content`` (string or content-part list) to text.

    Stored Message nodes and the ChatSet ``chars`` count both go through this
    helper, so ``chars`` always equals the sum of the stored message lengths.
    """
    content = (msg or {}).get("content", "")
    if isinstance(content, list):
        content = "\n".join(
            str(part.get("text", part.get("content", "")))
            if isinstance(part, dict) else str(part)
            for part in content)
    return str(content)


@dataclass
class Node:
    node_id: str
    node_type: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    source: str
    rel_type: str
    target: str
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])


class Soma:
    """SQLite-backed graph store. Same API as the old JSON-backed version."""

    def __init__(self, path: str | None = None, force_empty: bool = False) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._batch_mode = False
        self._batch_dirty = False
        if path:
            self._load_or_init(Path(path), force=force_empty)
        else:
            self._conn = self._make_conn()
            self._init_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Soma has no connection — call with a path")
        return self._conn

    # ── connection helpers ───────────────────────────────────────────────

    @staticmethod
    def _make_conn() -> sqlite3.Connection:
        c = sqlite3.connect(":memory:", check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        # FK OFF — graph edges don't always reference existing nodes
        c.row_factory = sqlite3.Row
        return c

    def _load_or_init(self, path: Path, force: bool = False) -> None:
        self._path = str(path)

        # JSON graph files are read into an in-memory SQLite database.
        # ``force`` means "do not load the file", never "delete it".
        if path.suffix.lower() == ".json":
            self._conn = self._make_conn()
            self._init_schema()
            if not force and path.exists():
                self._load_json_graph(path)
            return

        path.parent.mkdir(parents=True, exist_ok=True)

        # If force_empty, delete existing file and start fresh
        if force and Path(self._path).exists():
            Path(self._path).unlink()

        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # FK OFF — graph edges don't always reference existing nodes
        self._conn.row_factory = sqlite3.Row

        # Check if tables exist
        tables = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if not tables:
            self._init_schema()

    def _init_schema(self) -> None:
        tpl = Path(__file__).parent / "soma_template.sql"
        if tpl.exists():
            sql = tpl.read_text()
            self._conn.executescript(sql)
        else:
            # Minimal inline schema (fallback)
            self._conn.executescript("""
                CREATE TABLE nodes (
                    node_id TEXT PRIMARY KEY,
                    node_type TEXT NOT NULL,
                    properties TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE edges (
                    edge_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    rel_type TEXT NOT NULL,
                    target TEXT NOT NULL
                );
                CREATE INDEX idx_nodes_type ON nodes(node_type);
            """)
        # FK OFF — graph edges don't always reference existing nodes
        # (template.sql sets foreign_keys=ON, we override it)
        self._conn.execute("PRAGMA foreign_keys=OFF")

    def _load_json_graph(self, path: Path) -> None:
        """Load the legacy JSON graph shape into the SQLite connection.

        An empty file is not a malformed graph; it is a store that has not
        been written yet. Treating it as corrupt made every fresh temp-path
        store raise on construction, which is exactly how a new store is
        created.
        """
        try:
            text = path.read_text()
        except OSError as exc:
            raise ValueError(f"Cannot read SOMA graph {path}: {exc}") from exc
        if not text.strip():
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Cannot read SOMA graph {path}: {exc}") from exc

        for nid, ndata in (data.get("nodes") or {}).items():
            if isinstance(ndata, dict) and "properties" in ndata:
                node_type = ndata.get("type") or ndata.get("node_type") or ""
                props = dict(ndata.get("properties") or {})
            else:
                props = dict(ndata or {})
                node_type = str(props.pop("type", props.pop("node_type", "")))
            props.setdefault("id", nid)
            self._insert_node_row(nid, node_type, props)

        for edata in data.get("edges") or []:
            rel = edata.get("rel_type", "")
            params = (
                edata.get("id") or str(uuid.uuid4())[:8],
                edata.get("source", ""),
                rel,
                edata.get("target", ""),
            )
            sql = ("INSERT OR REPLACE INTO edges "
                   "(edge_id, source, rel_type, target) VALUES (?,?,?,?)")
            # Loading a graph that was written by a newer schema must widen
            # the CHECK list, exactly as create_edge does, or the store cannot
            # reopen a file it produced itself.
            try:
                self.conn.execute(sql, params)
            except sqlite3.IntegrityError:
                if not self._widen_rel_type_check(rel):
                    raise
                self.conn.execute(sql, params)
        self.conn.commit()

    # ── CRUD (same signatures as old store.py) ───────────────────────────

    def create_node(self, type_name: str, properties: dict[str, Any]) -> Node:
        node_id = properties.get("id", uuid.uuid4().hex)
        node = Node(node_id=node_id, node_type=type_name, properties=dict(properties))
        self._ensure_node(node)
        self._batch_dirty = True
        if not self._batch_mode:
            self._persist_node(node)
        return node

    def get_node(self, node_id: str) -> Node | None:
        row = self.conn.execute(
            "SELECT node_id, node_type, properties FROM nodes WHERE node_id=?",
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        props = json.loads(row["properties"]) if row["properties"] else {}
        return Node(node_id=row["node_id"], node_type=row["node_type"], properties=props)

    def delete_node(self, node_id: str) -> None:
        self.conn.execute("DELETE FROM nodes WHERE node_id=?", (node_id,))
        self.conn.execute("DELETE FROM edges WHERE source=? OR target=?", (node_id, node_id))
        self.conn.commit()
        self._batch_dirty = True
        if not self._batch_mode:
            self._persist()

    # ── edges ────────────────────────────────────────────────────────────

    def create_edge(self, source: str, rel_type: str, target: str) -> str:
        eid = str(uuid.uuid4())[:8]
        try:
            self.conn.execute(
                "INSERT INTO edges (edge_id, source, rel_type, target) VALUES (?,?,?,?)",
                (eid, source, rel_type, target),
            )
        except sqlite3.IntegrityError:
            if not self._widen_rel_type_check(rel_type):
                raise
            self.conn.execute(
                "INSERT INTO edges (edge_id, source, rel_type, target) VALUES (?,?,?,?)",
                (eid, source, rel_type, target),
            )
        self.conn.commit()
        self._batch_dirty = True
        if not self._batch_mode:
            self._persist()
        return eid

    def get_edges_for(self, node_id: str) -> list[Edge]:
        return self.get_edges(node_id)

    def get_edges(self, node_id: str) -> list[Edge]:
        rows = self.conn.execute(
            "SELECT edge_id, source, rel_type, target FROM edges "
            "WHERE source=? OR target=?",
            (node_id, node_id),
        ).fetchall()
        return [Edge(id=r["edge_id"], source=r["source"],
                     rel_type=r["rel_type"], target=r["target"]) for r in rows]

    def get_all_edges(self) -> list[Edge]:
        rows = self.conn.execute(
            "SELECT edge_id, source, rel_type, target FROM edges"
        ).fetchall()
        return [Edge(id=r["edge_id"], source=r["source"],
                     rel_type=r["rel_type"], target=r["target"]) for r in rows]

    # ── queries (same API as old store.py) ───────────────────────────────

    def query_by_type(self, type_name: str) -> list[Node]:
        rows = self.conn.execute(
            "SELECT node_id, node_type, properties FROM nodes WHERE node_type=?",
            (type_name,),
        ).fetchall()
        result = []
        for r in rows:
            props = json.loads(r["properties"]) if r["properties"] else {}
            result.append(Node(node_id=r["node_id"], node_type=r["node_type"], properties=props))
        return result

    def query_session(self, type_name: str, session_id: str) -> list[Node]:
        """All nodes of ``type_name`` belonging to one session."""
        return [
            n for n in self.query_by_type(type_name)
            if n.properties.get("session_id") == session_id
        ]

    def get_messages(self, session_id: str) -> list[Node]:
        """Raw chat messages for a session, in conversation order.

        Ordering is chatset id first (natural, so ``x.2`` precedes ``x.10``),
        then ``turn_index``, then node id. Timestamps are metadata only.
        """
        nodes = self.query_session("Message", session_id)
        nodes.sort(key=lambda n: (
            natural_key(n.properties.get("chat_set_id", "")),
            int(n.properties.get("turn_index") or 0),
            natural_key(n.node_id),
        ))
        return nodes

    def get_chatsets(self, session_id: str) -> list[Node]:
        """ChatSet nodes for a session, in canonical chunk order."""
        nodes = self.query_session("ChatSet", session_id)
        nodes.sort(key=lambda n: (
            int(n.properties.get("set_number") or 0),
            int(n.properties.get("chunk_index") or 0),
            natural_key(n.properties.get("chat_set_id", n.node_id)),
        ))
        return nodes

    def get_session_anchor(self, session_id: str) -> Node | None:
        for node in self.query_session("SessionAnchor", session_id):
            return node
        return None

    # ── stage instructions (the design's instruction source) ─────────────

    def get_stage_instruction(self, stage_name: str) -> dict[str, Any] | None:
        """One stage's instruction row, or None when the stage is unknown.

        This is where a stage gets its instructions. The machine-talk
        markdown files on disk are an input to ``build_soma_template.py``,
        not a runtime source: the database copy is the one with a version
        and a ``loaded_from`` trail.
        """
        try:
            row = self.conn.execute(
                "SELECT stage_name, tb_number, body, handoff, loaded_from, "
                "version FROM stage_instructions WHERE stage_name=?",
                (stage_name,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        return {
            "stage_name": row["stage_name"],
            "tb_number": row["tb_number"],
            "body": row["body"],
            "handoff": bool(row["handoff"]),
            "loaded_from": row["loaded_from"],
            "version": int(row["version"] or 1),
        }

    def list_stage_instructions(self) -> list[dict[str, Any]]:
        """Every instruction row, ordered by TB number."""
        try:
            rows = self.conn.execute(
                "SELECT stage_name, tb_number, length(body) AS chars, handoff, "
                "loaded_from, version FROM stage_instructions "
                "ORDER BY tb_number"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def query_view(self, view_name: str) -> list[Node]:
        """Rows from one of the template's per-stage projections.

        Each stage reads its view and nothing else, so this is the only
        read path a stage is given. The view name is validated against the
        database's own schema rather than interpolated from caller input.
        """
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name=?",
            (view_name,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"unknown stage view {view_name!r}; the template defines the "
                f"per-stage projections and no other view may be queried")
        rows = self.conn.execute(
            f'SELECT * FROM "{view_name}"'
        ).fetchall()
        out: list[Node] = []
        for r in rows:
            keys = r.keys()
            props = json.loads(r["properties"]) if "properties" in keys and r["properties"] else {}
            node_type = r["node_type"] if "node_type" in keys else str(props.get("type") or "")
            out.append(Node(node_id=r["node_id"], node_type=node_type,
                            properties=props))
        return out

    # ── chatset writes ───────────────────────────────────────────────────

    def _ensure_edge(self, source: str, rel_type: str, target: str) -> str:
        """Return the existing edge id for this triple, or create the edge."""
        row = self.conn.execute(
            "SELECT edge_id FROM edges WHERE source=? AND rel_type=? AND target=?",
            (source, rel_type, target),
        ).fetchone()
        if row is not None:
            return row["edge_id"]
        return self.create_edge(source, rel_type, target)

    def _purge_messages(self, chatset_id: str) -> None:
        """Drop the Message nodes previously written for this chatset."""
        prefix = f"msg-{chatset_id}-"
        rows = self.conn.execute(
            "SELECT node_id FROM nodes WHERE node_id LIKE ?", (prefix + "%",)
        ).fetchall()
        for row in rows:
            self.delete_node(row["node_id"])

    def upsert_chatset(self, session_id: str, chatset_id: str,
                       set_number: int, raw_chat_set: list,
                       chatset_hash: str = "",
                       provider: str = "unknown") -> dict[str, Any]:
        """Store one ChatSet, its raw Message nodes and their graph edges.

        ``chatset_hash`` identifies the exact raw chat set. It is always
        recomputed with ``chatset_hash_for`` and a supplied value that does
        not match the supplied chatset is rejected, so the hash stored on the
        ChatSet node can never drift from the bytes stored beside it.
        Re-running with the same chatset overwrites the same node ids instead
        of duplicating them, and keeps the provider/models_used already on
        the node when the caller passes defaults.
        """
        from .machinetalk.hashes import chatset_hash_for

        raw_chat_set = list(raw_chat_set or [])
        computed_hash = chatset_hash_for(raw_chat_set)
        if chatset_hash and chatset_hash != computed_hash:
            raise ValueError(
                f"chatset_hash {chatset_hash!r} does not match the canonical "
                f"hash of the supplied chatset {chatset_id!r} "
                f"({computed_hash})")
        chatset_hash = computed_hash

        contents = [_message_text(msg) for msg in raw_chat_set]
        chars = sum(len(text) for text in contents)

        existing = self.get_node(chatset_id)
        prev = existing.properties if existing is not None else {}
        if not provider or provider == "unknown":
            provider = prev.get("provider") or "unknown"
        models_used = prev.get("models_used") or "[]"

        anchor_id = f"anchor-{session_id}"
        anchor = self.get_node(anchor_id)
        prev_sets = int(anchor.properties.get("total_sets") or 0) if anchor else 0
        prev_chars = (int(anchor.properties.get("total_session_chars") or 0)
                      if anchor else 0)
        self.create_node("SessionAnchor", {
            "id": anchor_id,
            "session_id": session_id,
            "project_context": ((anchor.properties.get("project_context")
                                 or "Dendritic") if anchor else "Dendritic"),
            "total_sets": max(prev_sets, set_number),
            "total_session_chars": max(prev_chars, chars),
            "created_at": ((anchor.properties.get("created_at") or _now_iso())
                           if anchor else _now_iso()),
        })
        self.create_node("ChatSet", {
            "id": chatset_id,
            "session_id": session_id,
            "set_number": set_number,
            "chars": chars,
            "provider": provider,
            "models_used": models_used,
            "chatset_hash": chatset_hash,
            "message_count": len(raw_chat_set),
        })
        self._ensure_edge(anchor_id, "CONTAINS", chatset_id)

        self._purge_messages(chatset_id)
        for i, (msg, content) in enumerate(zip(raw_chat_set, contents)):
            msg = msg or {}
            mid = f"msg-{chatset_id}-{i}"
            self.create_node("Message", {
                "id": mid,
                "session_id": session_id,
                "chat_set_id": chatset_id,
                "turn_index": i,
                "role": str(msg.get("role") or ""),
                "chars": len(content),
                "content": content,
                "timestamp": str(msg.get("timestamp") or ""),
            })
            self._ensure_edge(chatset_id, "HAS_MESSAGE", mid)

        return {"ChatSet": 1, "Message": len(raw_chat_set),
                "chatset_hash": chatset_hash, "chars": chars}

    def save(self, path: str | None = None) -> str | None:
        """Flush the in-memory graph back to a JSON graph file.

        ``.json`` graphs are loaded into memory, so writes only reach disk
        when this is called (or when ``path`` names a new file). SQLite
        stores are already durable and return ``None``.
        """
        dest = Path(path or self._path or "")
        if dest.suffix.lower() != ".json":
            return None
        nodes: dict[str, Any] = {}
        for row in self.conn.execute(
            "SELECT node_id, node_type, properties FROM nodes ORDER BY node_id"
        ).fetchall():
            nodes[row["node_id"]] = {
                "type": row["node_type"],
                "properties": (json.loads(row["properties"])
                               if row["properties"] else {}),
            }
        edges = [
            {"id": r["edge_id"], "source": r["source"],
             "rel_type": r["rel_type"], "target": r["target"]}
            for r in self.conn.execute(
                "SELECT edge_id, source, rel_type, target FROM edges "
                "ORDER BY edge_id"
            ).fetchall()
        ]
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"nodes": nodes, "edges": edges}, f, indent=2)
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return str(dest)

    # ── batch mode ───────────────────────────────────────────────────────

    def start_batch(self) -> None:
        self._batch_mode = True
        self._batch_dirty = False

    def commit_batch(self) -> None:
        self._batch_mode = False
        if self._batch_dirty:
            self._persist()
            self._batch_dirty = False

    # ── counts ───────────────────────────────────────────────────────────

    def node_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    def edge_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    # ── internal helpers ─────────────────────────────────────────────────

    def _persist_node(self, node: Node) -> None:
        self._insert_node_row(node.node_id, node.node_type, node.properties)
        self.conn.commit()

    def _insert_node_row(self, node_id: str, node_type: str,
                         properties: dict[str, Any]) -> None:
        """INSERT OR REPLACE one node, widening the type CHECK list if needed.

        The nodes table enumerates its permitted node types, so a type added
        after the store was created — or after the template that created it —
        must be added to that list before its first row can be written.
        """
        sql = ("INSERT OR REPLACE INTO nodes (node_id, node_type, properties) "
               "VALUES (?,?,?)")
        params = (node_id, node_type,
                  json.dumps(properties, separators=(",", ":")))
        try:
            self.conn.execute(sql, params)
        except sqlite3.IntegrityError:
            if not self._widen_node_type_check(node_type):
                raise
            self.conn.execute(sql, params)

    def _widen_node_type_check(self, type_name: str) -> bool:
        """Add ``type_name`` to the nodes table's allowed node types.

        The schema enumerates the permitted node types, so a type added after
        a store was created cannot be inserted. The table is rebuilt with the
        same definition, the same rows, columns and user indexes, plus the new
        type in the CHECK list. Returns ``True`` when the table was rebuilt
        (the caller retries the insert) and ``False`` when there is nothing to
        widen — the type is already allowed, or the table has no type CHECK —
        in which case the caller's original error stands.
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='nodes'"
        ).fetchone()
        if row is None or not row["sql"]:
            return False
        create_sql = row["sql"]
        match = _NODE_TYPE_CHECK_RE.search(create_sql)
        if match is None:
            return False
        allowed = re.findall(r"'([^']*)'", match.group("types"))
        if type_name in allowed or not type_name or "'" in type_name:
            return False

        widened = ", ".join(
            f"'{t}'" for t in [*allowed, type_name])
        new_sql = (create_sql[:match.start("types")] + widened
                   + create_sql[match.end("types"):])

        indexes = self.conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='nodes' AND sql IS NOT NULL"
        ).fetchall()
        columns = [c["name"] for c in self.conn.execute(
            "PRAGMA table_info(nodes)").fetchall()]
        if not columns:
            return False
        column_list = ", ".join(f'"{c}"' for c in columns)
        backup = "_nodes_schema_backup"

        # The table is rebuilt in place: copy the rows aside, drop, recreate
        # with the widened CHECK list, put the rows and the indexes back. The
        # table is never renamed, so views that select from ``nodes`` keep
        # their definition and stay usable.
        self.conn.commit()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(f"DROP TABLE IF EXISTS {backup}")
            self.conn.execute(
                f"CREATE TABLE {backup} AS SELECT * FROM nodes")
            self.conn.execute("DROP TABLE nodes")
            self.conn.execute(new_sql)
            self.conn.execute(
                f"INSERT INTO nodes ({column_list}) "
                f"SELECT {column_list} FROM {backup}")
            self.conn.execute(f"DROP TABLE {backup}")
            for index in indexes:
                self.conn.execute(index["sql"])
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return True

    def _widen_rel_type_check(self, rel_type: str) -> bool:
        """Add ``rel_type`` to the edges table's allowed relationship types.

        The schema enumerates permitted edge kinds, so an edge type introduced
        after a store was created cannot be inserted. The table is rebuilt with
        its rows and indexes preserved plus the new type, exactly as
        ``_widen_node_type_check`` does for nodes. Returns ``True`` when the
        table was rebuilt so the caller can retry the insert.
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='edges'"
        ).fetchone()
        if row is None or not row["sql"]:
            return False
        create_sql = row["sql"]
        match = _REL_TYPE_CHECK_RE.search(create_sql)
        if match is None:
            return False
        allowed = re.findall(r"'([^']*)'", match.group("types"))
        if rel_type in allowed or not rel_type or "'" in rel_type:
            return False

        widened = ", ".join(f"'{t}'" for t in [*allowed, rel_type])
        new_sql = (create_sql[:match.start("types")] + widened
                   + create_sql[match.end("types"):])

        indexes = self.conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND tbl_name='edges' AND sql IS NOT NULL"
        ).fetchall()
        columns = [c["name"] for c in self.conn.execute(
            "PRAGMA table_info(edges)").fetchall()]
        if not columns:
            return False
        column_list = ", ".join(f'"{c}"' for c in columns)
        backup = "_edges_schema_backup"

        self.conn.commit()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(f"DROP TABLE IF EXISTS {backup}")
            self.conn.execute(
                f"CREATE TABLE {backup} AS SELECT * FROM edges")
            self.conn.execute("DROP TABLE edges")
            self.conn.execute(new_sql)
            self.conn.execute(
                f"INSERT INTO edges ({column_list}) "
                f"SELECT {column_list} FROM {backup}")
            self.conn.execute(f"DROP TABLE {backup}")
            for index in indexes:
                self.conn.execute(index["sql"])
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return True

    def _ensure_node(self, node: Node) -> None:
        """Ensure node exists in DB (insert or update)."""
        existing = self.conn.execute(
            "SELECT node_id FROM nodes WHERE node_id=?",
            (node.node_id,),
        ).fetchone()
        if existing:
            self._persist_node(node)
        else:
            self._persist_node(node)

    def _persist(self) -> None:
        """No-op for SQLite — changes are persistent immediately."""
        pass

    # ── cleanup ──────────────────────────────────────────────────────────

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Soma":
        return self

    def __exit__(self, *args) -> None:
        if self._conn is not None:
            try:
                self._conn.commit()
            except Exception:
                pass
            self._conn.close()
            self._conn = None
