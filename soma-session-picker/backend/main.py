"""
Soma Session Picker — Backend.

FastAPI app that exposes session metadata from the vault DB and disk,
with full search, filter, and sort. A POST endpoint saves selected
sessions to a queue JSON file that Soma's runner reads.

Session metadata is extracted on-demand from source files using
soma.machinetalk.session_metadata for lean, frequency-based data.
"""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from db import connection_context

# ── Paths ──────────────────────────────────────────────────────────────────

SESSIONS_PATH = os.environ.get(
    "SESSIONS_PATH",
    "/media/storage/dendritic123/chat-logs/sessions",
)
QUEUE_OUTPUT_PATH = os.environ.get(
    "QUEUE_OUTPUT_PATH",
    str(Path.home() / ".soma_queue.json"),
)

# ── Consolidated sessions (chains) ────────────────────────────────────────
CONSOLIDATED_DIR = Path("/media/storage/dendritic123/chat-logs/lineage/consolidated")

# ── TurnKey profile path ──────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # soma-10-14
SOMA_DIR = PROJECT_ROOT / "soma"
TURN_KEY_PROFILES_DIR = SOMA_DIR / "turn-key-profiles"
CURRENT_PROFILE_PATH = TURN_KEY_PROFILES_DIR / "soma-current.json"

# ── Lazy import for extraction ─────────────────────────────────────────────
_session_metadata_module = None


def _import_session_metadata():
    global _session_metadata_module
    if _session_metadata_module is None:
        backend_dir = Path(__file__).resolve().parent
        root_dir = backend_dir.parent.parent  # soma-10-14
        root_str = str(root_dir)
        if root_str not in os.sys.path:
            os.sys.path.insert(0, root_str)
        try:
            from soma.machinetalk import session_metadata
        except ModuleNotFoundError:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "session_metadata",
                root_dir / "soma" / "machinetalk" / "session_metadata.py"
            )
            session_metadata = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(session_metadata)
        _session_metadata_module = session_metadata
    return _session_metadata_module


# ── App ────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with connection_context() as conn:
        await conn.execute("SELECT 1")

        # The project filter depends on tables built by build_projects.py in
        # the SearchMonkey backend. If they are missing, the dropdown renders
        # empty and project= silently matches nothing, which looks like a bug
        # in the UI. Report it once at startup instead.
        try:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM projects"
            )
            row = await cursor.fetchone()
            if not row or not row["n"]:
                print(
                    "[session-picker] WARNING: project index is empty. "
                    "Run build_projects.py to populate the Project filter.",
                    flush=True,
                )
        except Exception:
            print(
                "[session-picker] WARNING: project index missing "
                "(no 'projects' table). Run build_projects.py in the "
                "SearchMonkey backend to enable the Project filter.",
                flush=True,
            )
    yield


app = FastAPI(title="Soma Session Picker", version="0.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Session loading helpers ────────────────────────────────────────────────

def _read_session_json(session_dir: Path) -> dict:
    """Read session.json metadata if it exists."""
    sj = session_dir / "session.json"
    if sj.exists():
        try:
            return json.loads(sj.read_text())
        except Exception:
            return {}
    return {}


def _resolve_path(session_id: str) -> str:
    """Convert a DB session_id to its on-disk path."""
    dir_name = session_id.split("/")[-1]
    base = Path(SESSIONS_PATH)
    candidate = base / dir_name
    if candidate.exists():
        return str(candidate.resolve())
    if (base / session_id).exists():
        return str((base / session_id).resolve())
    return ""


def _count_chatsets(session_dir: Path) -> int:
    """Count JSON chatset files in a session directory."""
    if not session_dir.is_dir():
        return 0
    count = 0
    for f in session_dir.iterdir():
        if f.is_file() and f.suffix == ".json" and f.name != "session.json":
            count += 1
    return count


def _try_load_cached_meta(session_dir: Path) -> dict | None:
    """Try to load a pre-generated .meta.json file."""
    sj = session_dir / "session.json"
    if sj.exists():
        try:
            sid = json.loads(sj.read_text()).get("session_id", session_dir.name)
        except Exception:
            sid = session_dir.name
        meta_path = session_dir / f"{sid}.meta.json"
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text())
            except Exception:
                pass
    return None


def _build_full_session(sess: dict, meta: dict | None = None) -> dict:
    """Augment a DB row with disk metadata.

    ONLY uses DB fields + lightweight disk reads. Does NOT scan all sessions.
    """
    session_id = sess["session_id"]
    # Fallback: try trailing segment when exact lookup misses
    if meta is None:
        meta = _meta_cache.get(session_id.split("/")[-1])
    session_dir = Path(_resolve_path(session_id))
    session_json = _read_session_json(session_dir)
    file_count = _count_chatsets(session_dir)

    # A chat set is one user turn plus the assistant's reply. Sessions are
    # stored as chat-set files, and a single exchange can be split across
    # several files when it is long, so a file count is NOT a chat set count.
    # session.json's total_sets already counts logical exchanges and is
    # present for every session in the vault, so it is authoritative.
    # dev_sessions.chat_set_count stores a file count and must not be used
    # here; it inflated sessions by up to 6x (1756 files vs 303 exchanges).
    logical_sets = session_json.get("total_sets")
    if not isinstance(logical_sets, int) or logical_sets <= 0:
        logical_sets = 0

    return {
        "session_id": session_id,
        "total_score": sess.get("total_score") or 0,
        "code_blocks": sess.get("code_blocks") or 0,
        "inline_code": sess.get("inline_code") or 0,
        "code_extensions": sess.get("code_extensions") or 0,
        "imports": sess.get("imports") or 0,
        "functions": sess.get("functions") or 0,
        "dev_keywords": sess.get("dev_keywords") or 0,
        "config_files": sess.get("config_files") or 0,
        "heredoc_code_files": sess.get("heredoc_code_files") or 0,
        "combined_text_length": sess.get("combined_text_length") or 0,
        # Logical exchanges (user message + assistant reply).
        "chat_set_count": logical_sets,
        # Raw file count, kept separately so the two are never conflated.
        "chat_file_count": file_count,
        "chat_set_count_source": "session.json" if logical_sets else "missing",
        "provider": _normalize_provider(meta.get("provider")) if meta else "",
        "model": meta.get("model") if meta else "",
        "date": meta.get("date") if meta else "",
        "domain": meta.get("domain") if meta else "",
        "sub_domain": meta.get("sub_domain") if meta else "",
        "topics": meta.get("topics") if meta else "",
        "project_context": meta.get("project_context") if meta else "",
        "title": session_json.get("title") or session_json.get("name") or "",
        "total_sets": logical_sets,
        "total_session_chars": session_json.get("total_session_chars") or 0,
        "models_used": session_json.get("models_used") or {},
        "session_path": _resolve_path(session_id),
        "session_dir_name": session_id.split("/")[-1],
    }


# ── In-memory cache ────────────────────────────────────────────────────────
# Only stores DB rows + lightweight session.json reads.
# NO disk scanning of all sessions.

_full_cache: list[dict] | None = None
_meta_cache: dict[str, dict] | None = None


async def _build_meta_cache():
    """Build a lookup of session_id -> sessions-table row.

    Also indexes by trailing path segment so lookups succeed whether
    the lookup key is a full path (e.g.  github/.../aistudio) or just
    the segment (e.g.  aistudio).
    """
    global _meta_cache
    _meta_cache = {}
    async with connection_context() as conn:
        cursor = await conn.execute(
            "SELECT session_id, date, provider, model, domain, sub_domain, "
            "project_context, topics FROM sessions"
        )
        for row in await cursor.fetchall():
            d = dict(row)
            sid = d.pop("session_id")
            # Normalize provider for consistent filtering
            if "provider" in d and d["provider"]:
                d["provider"] = _normalize_provider(d["provider"])
            _meta_cache[sid] = d
            # Also index by trailing segment so full-path lookups land here
            tail = sid.split("/")[-1]
            if tail != sid and tail not in _meta_cache:
                _meta_cache[tail] = d


async def _build_full_cache():
    """Build the full session list from DB only.

    NO disk scanning. Only reads session.json files from each session directory
    for basic fields (total_sets, total_session_chars, title).
    This is fast even with 1000+ sessions.
    """
    global _full_cache
    if _meta_cache is None:
        await _build_meta_cache()

    async with connection_context() as conn:
        cursor = await conn.execute(
            "SELECT * FROM dev_sessions ORDER BY total_score DESC"
        )
        rows = await cursor.fetchall()

    _full_cache = [_build_full_session(dict(r), _meta_cache.get(r["session_id"])) for r in rows]


# ── Provider normalization ─────────────────────────────────────────────────

def _normalize_provider(value: str) -> str:
    """Canonicalize provider strings for comparison.

    "aistudio", "ai studio", "AI Studio", "ai-studio" → "aistudio"
    "OpenAI"       → "openai"
    All others pass through lowercase.
    """
    if not value:
        return ""
    return value.strip().lower().replace(" ", "").replace("-", "")


# ── Endpoints ──────────────────────────────────────────────────────────────


@app.get("/api/sessions/load")
async def load_sessions_from_json(path: str = ""):
    """Read a JSON file from disk and return its sessions.

    Accepts ?path=/absolute/or/relative/file.json
    Returns { sessions: [{ session_id, ... }, ...], error: "..." | null }
    """
    if not path:
        return JSONResponse(
            status_code=400,
            content={"sessions": [], "error": "No path provided"},
        )
    try:
        file_path = Path(path).expanduser().resolve()
        if not file_path.is_file():
            return JSONResponse(
                status_code=404,
                content={"sessions": [], "error": f"File not found: {path}"},
            )
        raw = json.loads(file_path.read_text(encoding="utf-8"))
        # Support both {"sessions": [...]} and a bare array
        if isinstance(raw, list):
            sessions = raw
        elif isinstance(raw, dict):
            # Try common container keys
            for key in ("sessions", "data", "records"):
                if key in raw and isinstance(raw[key], list):
                    sessions = raw[key]
                    break
            else:
                # Single object – wrap in array
                sessions = [raw]
        else:
            return JSONResponse(
                status_code=400,
                content={"sessions": [], "error": "Unexpected JSON structure"},
            )
        # Normalise: ensure each item has session_id
        normalised = []
        for item in sessions:
            sid = item.get("session_id") if isinstance(item, dict) else str(item)
            normalised.append({"session_id": sid, **item} if isinstance(item, dict) else {"session_id": sid})
        return {"sessions": normalised, "error": None}
    except json.JSONDecodeError as e:
        return JSONResponse(
            status_code=400,
            content={"sessions": [], "error": f"Invalid JSON: {e}"},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"sessions": [], "error": str(e)},
        )


@app.get("/api/health")
async def health():
    return {"status": "ok", "engine": "soma-session-picker"}


@app.get("/api/consolidated")
async def list_consolidated():
    """List consolidated sessions (chains) from the lineage/consolidated dir."""
    if not CONSOLIDATED_DIR.is_dir():
        return {"sessions": [], "total": 0}

    out = []
    for d in sorted(CONSOLIDATED_DIR.iterdir()):
        if not d.is_dir():
            continue
        sj = d / "session.json"
        if not sj.is_file():
            continue
        try:
            meta = json.loads(sj.read_text())
        except (OSError, json.JSONDecodeError):
            continue

        prov = {}
        pj = d / "provenance.json"
        if pj.is_file():
            try:
                prov = json.loads(pj.read_text())
            except (OSError, json.JSONDecodeError):
                prov = {}

        members = prov.get("source_members", []) or []
        out.append({
            "session_id": meta.get("session_id", d.name),
            "provider": meta.get("provider", ""),
            "total_sets": meta.get("total_sets", 0),
            "total_session_chars": meta.get("total_session_chars", 0),
            "models_used": meta.get("models_used", {}),
            "source_count": len(members),
            "source_members": members,
            "dropped_chunks": prov.get("dropped_chunks", 0),
            "emitted_chunks": prov.get("emitted_chunks", meta.get("total_sets", 0)),
            "path": str(d),
        })

    return {"sessions": out, "total": len(out)}


@app.get("/api/projects")
async def list_projects(
    search: str = "",
    min_sessions: int = 1,
    sort: str = "sessions",
    limit: int = 500,
):
    """List projects with their session counts.

    Projects come from the ``projects`` table, built by build_projects.py from
    two signals: the LLM-assigned ``project_context`` label and the destination
    paths of heredocs written inside the session. Continuation chains (the same
    session directory continued under another account after a context-limit
    handoff) are counted once.

    sort: sessions | score | heredocs | name | recent
    """
    sort_sql = {
        "sessions": "session_count DESC, project_name ASC",
        "score": "total_score DESC, project_name ASC",
        "heredocs": "heredoc_hits DESC, project_name ASC",
        "name": "project_name ASC",
        "recent": "latest_date DESC, project_name ASC",
    }.get(sort, "session_count DESC, project_name ASC")

    sql = (
        "SELECT project_key, project_name, session_count, heredoc_hits, "
        "total_score, dev_session_count, earliest_date, latest_date "
        "FROM projects WHERE session_count >= ?"
    )
    params: list = [min_sessions]

    if search:
        sql += " AND (lower(project_name) LIKE ? OR project_key LIKE ?)"
        like = f"%{search.lower()}%"
        params += [like, like]

    sql += f" ORDER BY {sort_sql} LIMIT ?"
    params.append(limit)

    async with connection_context() as conn:
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()

    return {
        "projects": [dict(r) for r in rows],
        "total": len(rows),
    }


@app.get("/api/profile")
async def get_current_profile():
    """Return the TurnKey current profile (read-only)."""
    if not CURRENT_PROFILE_PATH.is_file():
        return JSONResponse(
            status_code=404,
            content={"error": f"Profile file not found: {CURRENT_PROFILE_PATH}"},
        )
    try:
        raw = json.loads(CURRENT_PROFILE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Invalid JSON in profile: {e}"},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)},
        )

    name = raw.get("name", "")
    deck = raw.get("deck", [])
    models = [
        {"modelId": card.get("modelId", ""), "provider": card.get("provider", "")}
        for card in deck
    ]
    return {"name": name, "models": models}


@app.get("/api/sessions")
async def get_sessions(
    search: str = "",
    domain: str = "",
    provider: str = "",
    model: str = "",
    project: str = "",
    min_score: int = 0,
    max_score: int = 0,
    min_sets: int = 0,
    sort: str = "sets",
    order: str = "desc",
    limit: int = 500,
):
    """Return the full session list with search/filter/sort.

    NO metadata scanning — only DB fields + lightweight session.json reads.
    When ``project`` is supplied, results are restricted to sessions linked to
    that project key.
    """
    global _full_cache
    if _full_cache is None:
        await _build_full_cache()

    results = _full_cache[:]

    # Filter: project.
    #
    # session_projects stores full session_ids while dev_sessions (and this
    # cache) are keyed by directory name, so both forms are collected as
    # match keys. This keeps continuation chains intact: every account row
    # pointing at the same directory resolves to the same cached session.
    if project:
        linked: set[str] = set()
        async with connection_context() as conn:
            cursor = await conn.execute(
                "SELECT session_id FROM session_projects WHERE project_key = ?",
                (project,),
            )
            for r in await cursor.fetchall():
                sid = r["session_id"]
                linked.add(sid)
                tail = (sid or "").split("/")[-1]
                if tail:
                    linked.add(tail)

        results = [
            s for s in results
            if s["session_id"] in linked or s.get("session_dir_name") in linked
        ]

    # Search
    if search:
        q = search.lower()
        results = [
            s for s in results
            if q in s["session_id"].lower()
            or q in (s.get("title") or "").lower()
            or q in (s.get("domain") or "").lower()
            or q in (s.get("topics") or "").lower()
            or q in (s.get("session_dir_name") or "").lower()
        ]

    # Filter: domain
    if domain:
        results = [s for s in results if s.get("domain") == domain]

    # Filter: provider (normalized)
    if provider:
        norm = _normalize_provider(provider)
        results = [s for s in results if _normalize_provider(s.get("provider") or "") == norm]

    # Filter: model
    if model:
        results = [s for s in results if s.get("model") == model]

    # Filter: min_score
    if min_score > 0:
        results = [s for s in results if s["total_score"] >= min_score]

    # Filter: max_score
    if max_score > 0:
        results = [s for s in results if s["total_score"] <= max_score]

    # Filter: min_sets
    if min_sets > 0:
        results = [s for s in results if s["chat_set_count"] >= min_sets]

    # Sort
    sort_key_map = {
        "score": lambda s: s["total_score"],
        "sets": lambda s: s["chat_set_count"],
        "date": lambda s: s.get("date") or "",
        "name": lambda s: s["session_dir_name"].lower(),
        "functions": lambda s: s["functions"],
        "provider": lambda s: s.get("provider") or "",
        "domain": lambda s: s.get("domain") or "",
    }
    key_fn = sort_key_map.get(sort, sort_key_map["score"])
    reverse = order == "desc"
    if sort == "name" and reverse:
        results.sort(key=key_fn, reverse=True)
    elif sort == "date" and reverse:
        results.sort(key=lambda s: s.get("date") or "", reverse=True)
    else:
        results.sort(key=key_fn, reverse=reverse)

    # `total` must describe the whole filtered set, not the page. Reporting
    # the truncated length made the UI claim there were only 500 sessions and
    # silently cut the list off mid-way (at 53 chat sets in score order).
    total_matched = len(results)
    results = results[:limit]
    return {
        "sessions": results,
        "total": total_matched,
        "returned": len(results),
        "has_more": len(results) < total_matched,
    }


@app.get("/api/sessions/grouped")
async def get_sessions_grouped(
    search: str = "",
    domain: str = "",
    provider: str = "",
    model: str = "",
    project: str = "",
    min_score: int = 0,
    max_score: int = 0,
    min_sets: int = 0,
    sort: str = "sets",
    order: str = "desc",
    limit: int = 5000,
):
    """Sessions grouped under their project, so repeats collapse into one tree.

    Sessions are keyed by directory name and one project owns many of them.
    A flat list repeated the project label on every row and buried the
    distinct projects. A session linked to several projects (e.g. the label
    "peacock, social-lube") appears under each, so group counts add up.
    """
    global _full_cache
    if _full_cache is None:
        await _build_full_cache()

    results = _full_cache[:]

    if search:
        q = search.lower()
        results = [
            s for s in results
            if q in s["session_id"].lower()
            or q in (s.get("title") or "").lower()
            or q in (s.get("domain") or "").lower()
            or q in (s.get("topics") or "").lower()
            or q in (s.get("session_dir_name") or "").lower()
            or q in (s.get("project_context") or "").lower()
        ]
    if domain:
        results = [s for s in results if s.get("domain") == domain]
    if provider:
        norm = _normalize_provider(provider)
        results = [s for s in results if _normalize_provider(s.get("provider") or "") == norm]
    if model:
        results = [s for s in results if s.get("model") == model]
    if min_score > 0:
        results = [s for s in results if s["total_score"] >= min_score]
    if max_score > 0:
        results = [s for s in results if s["total_score"] <= max_score]
    if min_sets > 0:
        results = [s for s in results if s["chat_set_count"] >= min_sets]

    # directory name -> [(project_key, project_name), ...] from the index
    links: dict[str, list[tuple[str, str]]] = {}
    async with connection_context() as conn:
        cursor = await conn.execute(
            "SELECT session_id, project_key, project_name FROM session_projects"
        )
        for row in await cursor.fetchall():
            sid = row["session_id"] or ""
            tail = sid.split("/")[-1]
            for key in {sid, tail}:
                if key:
                    links.setdefault(key, []).append(
                        (row["project_key"], row["project_name"])
                    )

    def sort_key(s):
        return {
            "sets": s["chat_set_count"],
            "score": s["total_score"],
            "date": s.get("date") or "",
            "name": s["session_dir_name"].lower(),
        }.get(sort, s["chat_set_count"])

    reverse = order == "desc"

    tree: dict[str, dict] = {}
    for s in results:
        dir_name = s.get("session_dir_name") or s["session_id"].split("/")[-1]
        pairs = links.get(dir_name) or links.get(s["session_id"]) or []
        if not pairs:
            label = (s.get("project_context") or "").strip() or "unassigned"
            pairs = [(_norm_project(label), label or "unassigned")]

        seen_keys: set[str] = set()
        for key, name in pairs:
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            bucket = tree.setdefault(key, {
                "project_key": key,
                "project_name": name,
                "sessions": [],
                "total_sets": 0,
                "total_score": 0,
            })
            bucket["sessions"].append(s)
            bucket["total_sets"] += s["chat_set_count"]
            bucket["total_score"] += s["total_score"]

    groups = []
    for key, bucket in tree.items():
        if project and key != project:
            continue
        bucket["sessions"].sort(key=sort_key, reverse=reverse)
        bucket["session_count"] = len(bucket["sessions"])
        groups.append(bucket)

    groups.sort(key=lambda g: g["total_sets"], reverse=True)

    return {
        "groups": groups,
        "project_count": len(groups),
        "total_sessions": sum(g["session_count"] for g in groups),
    }


def _norm_project(label: str) -> str:
    """Same normalisation build_projects.py uses, kept local to avoid a cycle."""
    s = (label or "").strip().lower()
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@app.get("/api/sessions/counts")
async def session_counts():
    """Return distinct values for filter dropdowns."""
    async with connection_context() as conn:
        rows = await conn.execute(
            "SELECT DISTINCT provider FROM sessions WHERE provider IS NOT NULL AND provider != ''"
        )
        raw_providers = [r["provider"] for r in await rows.fetchall()]

    # Deduplicate by normalized form, keeping the first raw value as canonical
    seen: set[str] = set()
    providers: list[str] = []
    for p in sorted(raw_providers, key=lambda x: _normalize_provider(x)):
        key = _normalize_provider(p)
        if key not in seen:
            seen.add(key)
            providers.append(key)

    async with connection_context() as conn:
        rows = await conn.execute(
            "SELECT DISTINCT domain FROM sessions WHERE domain IS NOT NULL AND domain != '' ORDER BY domain"
        )
        domains = [r["domain"] for r in await rows.fetchall()]

        rows = await conn.execute(
            "SELECT DISTINCT model FROM sessions WHERE model IS NOT NULL AND model != '' ORDER BY model"
        )
        models = [r["model"] for r in await rows.fetchall()]

    async with connection_context() as conn:
        row = await conn.execute(
            "SELECT MIN(total_score) as lo, MAX(total_score) as hi FROM dev_sessions"
        )
        score_range = dict(await row.fetchone())

    return {
        "providers": providers,
        "domains": domains,
        "models": models,
        "score_range": {
            "min": score_range.get("lo") or 0,
            "max": score_range.get("hi") or 0,
        },
    }


@app.get("/api/session/{session_id}/metadata")
async def get_session_metadata(session_id: str):
    """Return lean metadata for a single session.

    LAZY-LOADS from disk — only called when you click a session row.
    Returns cached .meta.json if available, otherwise generates on-the-fly.
    """
    session_dir = Path(_resolve_path(session_id))
    if not session_dir.is_dir():
        return JSONResponse(
            status_code=404,
            content={"error": "session directory not found", "session_id": session_id},
        )

    # Try cached file first
    cached = _try_load_cached_meta(session_dir)
    if cached:
        return cached

    # Generate on-the-fly
    sm = _import_session_metadata()
    try:
        meta = sm.extract_session_metadata(session_dir)
        # Write cache for next time
        sid = meta.get("session_id", session_dir.name)
        meta_path = session_dir / f"{sid}.meta.json"
        meta_path.write_text(json.dumps(meta, indent=2, default=str))
        return meta
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "session_id": session_id},
        )


@app.post("/api/select")
async def save_selected(request: dict):
    """Save selected session IDs to the queue JSON file."""
    session_ids = request.get("session_ids", [])
    queue_data = {
        "selected": session_ids,
        "count": len(session_ids),
        "timestamp": asyncio.get_event_loop().time(),
    }

    Path(QUEUE_OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_OUTPUT_PATH, "w") as f:
        json.dump(queue_data, f, indent=2)

    soma_queue = Path("/home/flintx/soma-10-14/data/soma_queue.json")
    soma_queue.parent.mkdir(parents=True, exist_ok=True)
    with open(soma_queue, "w") as f:
        json.dump(queue_data, f, indent=2)

    return queue_data


@app.post("/api/select-with-metadata")
async def save_selected_with_metadata(request: dict):
    """Save selected sessions to queue + write lean metadata JSON for each."""
    session_ids = request.get("session_ids", [])
    queue_data = {
        "selected": session_ids,
        "count": len(session_ids),
        "timestamp": asyncio.get_event_loop().time(),
    }

    Path(QUEUE_OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_OUTPUT_PATH, "w") as f:
        json.dump(queue_data, f, indent=2)

    soma_queue = Path("/home/flintx/soma-10-14/data/soma_queue.json")
    soma_queue.parent.mkdir(parents=True, exist_ok=True)
    with open(soma_queue, "w") as f:
        json.dump(queue_data, f, indent=2)

    # Build metadata for selected sessions
    sm = _import_session_metadata()
    metas = []
    for sid in session_ids:
        session_dir = Path(_resolve_path(sid))
        if session_dir.is_dir():
            try:
                meta = sm.extract_session_metadata(session_dir)
                meta_path = session_dir / f"{meta['session_id']}.meta.json"
                meta_path.parent.mkdir(parents=True, exist_ok=True)
                meta_path.write_text(json.dumps(meta, indent=2, default=str))
                metas.append(meta)
            except Exception as e:
                metas.append({"session_id": sid, "error": str(e)})
        else:
            metas.append({"session_id": sid, "error": "directory not found"})

    return {**queue_data, "metadata": metas}


LAUNCH_OUTPUT_PATH = "/home/flintx/soma-10-14/data/soma_launch.json"

# ── Export directory for session-picker JSON exports ────────────────────────


def _resolve_exports_dir() -> Path:
    """Resolve the session-picker exports directory from the saved base path.

    Reads PROJECT_ROOT/data/.base_path and appends session-picker-json/.
    Falls back to PROJECT_ROOT when the file does not exist.
    """
    bp = PROJECT_ROOT / "data" / ".base_path"
    if bp.is_file():
        try:
            base = Path(bp.read_text().strip())
            if base.is_absolute():
                return base / "session-picker-json"
            return PROJECT_ROOT / base / "session-picker-json"
        except Exception:
            pass
    return PROJECT_ROOT / "session-picker-json"


@app.post("/api/launch")
async def save_launch(request: dict):
    """Write a launch choice to soma_launch.json.

    Accepts { json_path, mode, count } and writes the body plus a
    timestamp.  Returns 400 when required fields are missing or
    values are of the wrong type.
    """
    json_path = request.get("json_path")
    mode = request.get("mode")
    count = request.get("count")

    # --- validation ---
    errors = []
    if not isinstance(json_path, str) or not json_path:
        errors.append("json_path must be a non-empty string")
    if not isinstance(mode, str) or not mode:
        errors.append("mode must be a non-empty string")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        errors.append("count must be a non-negative integer")

    if errors:
        return JSONResponse(status_code=400, content={"error": errors})

    # --- write ---
    launch = {
        "json_path": json_path,
        "mode": mode,
        "count": count,
        "timestamp": asyncio.get_event_loop().time(),
    }

    Path(LAUNCH_OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(LAUNCH_OUTPUT_PATH, "w") as f:
        json.dump(launch, f, indent=2)

    return {"status": "ok", "launch": launch}


@app.get("/api/select/export")
async def export_selected():
    """Return selected sessions as a downloadable JSON and persist to disk."""
    try:
        with open(QUEUE_OUTPUT_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"selected": [], "count": 0}

    # Write timestamped export to <base>/session-picker-json/
    exports_dir = _resolve_exports_dir()
    exports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    export_filename = exports_dir / f"export_{ts}.json"
    # Safety: never overwrite an existing file
    counter = 1
    while export_filename.exists():
        export_filename = exports_dir / f"export_{ts}_{counter}.json"
        counter += 1
    export_filename.write_text(json.dumps(data, indent=2, default=str))

    return JSONResponse(content=data)


# ── Serve frontend dist ────────────────────────────────────────────────────

FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if FRONTEND_DIST.exists() and FRONTEND_DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="static")
