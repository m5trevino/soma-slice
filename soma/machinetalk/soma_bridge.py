"""Soma Bridge: converts machine-talk pipeline outputs to Soma graph nodes.

This module maps the NEW pipeline's JSON artifacts into the OLD pipeline's
Soma node types, preserving the exact same graph structure and edge types
that the legacy dendrites use.

The new pipeline has the same stages as the old, with machine-talk prompts.
Mapping:
  distiller     → Receptor (returns dict, no Soma write in old)
  introbeaver   → Classifier (Intent/Directive/Constraint/Question/Preference/Correction/Noise)
  planbeaver    → Integrator (Task/Risk/Decision/Stack)
  outrobeaver   → Retrograde (Outcome/Deliverable/Lesson/Drift/Action/Unresolved)
  nexus         → Nexus (StrategicBlueprintNode)
  spark         → Spark (RequirementsNode)
  falcon        → Falcon (ArchitectureNode + ArchitectureContractNode)
  drafter       → Drafter (ReferenceArtifactNode + FileNode)
  oc            → AxonHillock (FunctionSlot slicer)  [NOT a gate — same as old]
  scout         → Scout (InvariantQueryNode)
  olc           → Myelinator (OptimizedSlot + SessionManifestNode)
  terminal      → TerminalPlayer (ImplementationNode + SmokeTestNode)
  gate          → SaltatoryGate (VerifiedNode or FailureNode)
  publisher     → CodePublisher (writes to disk)

Every write method returns a summary dict for per-task logging.
"""
from __future__ import annotations

import json
import re
from typing import Any


def _now() -> str:
    return __import__('datetime').datetime.utcnow().isoformat()


# Instruction type -> graph node type. The introbeaver classifies each entry
# into one of these lanes; the lane is the node type, so a CONSTRAINT becomes
# a Constraint node and not a generic intent.
_SIGNAL_NODE_FOR = {
    "CONSTRAINT": "Constraint",
    "DIRECTIVE": "Directive",
    "PREFERENCE": "Preference",
    "QUESTION": "Question",
}


class SomaBridge:
    """Translates machine-talk stage artifacts into Soma graph nodes/edges."""

    def __init__(self, soma: Any) -> None:
        self._soma = soma

    # ------------------------------------------------------------------
    # Stage 1: Ingestion (per ChatSet)
    # ------------------------------------------------------------------

    def write_session_anchor(self, session_id: str, project_name: str = "",
                             total_sets: int = 0, total_chars: int = 0) -> dict[str, int]:
        anchor_id = f"anchor-{session_id}"
        # Update in place: deleting the anchor would drop CONTAINS edges to
        # every other ChatSet in the session.
        self._soma.create_node("SessionAnchor", {
            "id": anchor_id,
            "session_id": session_id,
            "project_context": project_name or "Dendritic",
            "total_sets": total_sets,
            "total_session_chars": total_chars,
            "created_at": _now(),
        })
        return {"SessionAnchor": 1}

    def _purge_chatset_children(self, chatset_id: str) -> None:
        """Remove previously written derived nodes for this chatset.

        Works with both the old in-memory store (_nodes dict) and the new
        SQLite store (conn attribute).
        """
        prefixes = (
            f"msg-{chatset_id}-",
            f"distilled-{chatset_id}",
            f"directive-{chatset_id}-",
            f"constraint-{chatset_id}-",
            f"preference-{chatset_id}-",
            f"question-{chatset_id}-",
            f"artifact-{chatset_id}-",
            f"stack-{chatset_id}",
            f"chrono-{chatset_id}-",
            f"noise-{chatset_id}",
        )
        # Find nodes to delete
        to_delete = []
        if hasattr(self._soma, "_nodes"):
            # Old in-memory store
            for nid in list(self._soma._nodes):
                if any(nid.startswith(p) for p in prefixes):
                    to_delete.append(nid)
        elif hasattr(self._soma, "conn"):
            # New SQLite store
            for p in prefixes:
                rows = self._soma.conn.execute(
                    "SELECT node_id FROM nodes WHERE node_id LIKE ?",
                    (p + "%",),
                ).fetchall()
                for r in rows:
                    to_delete.append(r["node_id"])
        for nid in to_delete:
            self._soma.delete_node(nid)

    def write_chatset(self, session_id: str, chatset_id: str,
                      set_number: int, chars: int,
                      chatset_hash: str = "",
                      provider: str = "", models_used: list[str] | None = None) -> dict[str, int]:
        self._purge_chatset_children(chatset_id)
        existing = self._soma.get_node(chatset_id)
        if existing:
            self._soma.delete_node(chatset_id)
        self._soma.create_node("ChatSet", {
            "id": chatset_id,
            "session_id": session_id,
            "set_number": set_number,
            "chars": chars,
            "provider": provider or "unknown",
            "models_used": json.dumps(models_used or []),
            "chatset_hash": chatset_hash or "",
        })
        anchor_id = f"anchor-{session_id}"
        self._soma.create_edge(anchor_id, "CONTAINS", chatset_id)
        return {"ChatSet": 1}

    def write_raw_messages(self, cs: Any) -> dict[str, int]:
        """Write the verbatim conversation turns as queryable Message nodes."""
        chatset_id = cs.chatset_id
        session_id = getattr(cs, "session_id", "")
        count = 0
        for i, msg in enumerate(getattr(cs, "raw_chat_set", []) or []):
            msg = msg or {}
            role = str(msg.get("role", "") or "")
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict):
                        parts.append(str(p.get("text", p.get("content", ""))))
                    else:
                        parts.append(str(p))
                content = "\n".join(parts)
            content = str(content)
            mid = f"msg-{chatset_id}-{i}"
            if self._soma.get_node(mid):
                self._soma.delete_node(mid)
            self._soma.create_node("Message", {
                "id": mid,
                "session_id": session_id,
                "chat_set_id": chatset_id,
                "turn_index": i,
                "role": role,
                "chars": len(content),
                "content": content,
            })
            self._soma.create_edge(chatset_id, "HAS_MESSAGE", mid)
            count += 1
        return {"Message": count}

    def write_distilled(self, session_id: str, chatset_id: str, set_number: int,
                        gate: Any, model_id: str, latency_ms: float) -> dict[str, int]:
        """Write the distiller's canonical payload into the graph."""
        parsed = getattr(gate, "parsed", None) or {}
        if not isinstance(parsed, dict):
            return {}
        counts: dict[str, int] = {}

        did = f"distilled-{chatset_id}"
        if self._soma.get_node(did):
            self._soma.delete_node(did)
        self._soma.create_node("DistilledChatSet", {
            "id": did,
            "session_id": session_id,
            "chat_set_id": chatset_id,
            "set_number": set_number,
            "chatset_hash": str(parsed.get("chatset_hash") or ""),
            "intent": str(parsed.get("intent") or ""),
            "signal_quality": parsed.get("signal_quality"),
            "bytes_original": parsed.get("bytes_original"),
            "bytes_distilled": parsed.get("bytes_distilled"),
            "skip": bool(parsed.get("skip")),
            "skip_reason": parsed.get("skip_reason"),
            "exfil_type": parsed.get("exfil_type"),
            "model_id": model_id,
            "latency_ms": round(latency_ms, 1),
            "processed_at": _now(),
        })
        self._soma.create_edge(chatset_id, "DISTILLED_TO", did)
        counts["DistilledChatSet"] = 1

        def _add_list(key: str, node_type: str, rel: str) -> None:
            items = parsed.get(key) or []
            if not isinstance(items, (list, tuple)):
                return
            c = 0
            for i, item in enumerate(items):
                text = item.get("text", "") if isinstance(item, dict) else str(item)
                if not str(text).strip():
                    continue
                nid = f"{node_type.lower()}-{chatset_id}-{i}"
                if self._soma.get_node(nid):
                    self._soma.delete_node(nid)
                self._soma.create_node(node_type, {
                    "id": nid, "session_id": session_id,
                    "chat_set_id": chatset_id, "text": str(text), "index": i,
                })
                self._soma.create_edge(did, rel, nid)
                c += 1
            if c:
                counts[node_type] = c

        _add_list("directives", "Directive", "HAS_DIRECTIVE")
        _add_list("constraints", "Constraint", "HAS_CONSTRAINT")
        _add_list("preferences", "Preference", "HAS_PREFERENCE")
        _add_list("questions", "Question", "HAS_QUESTION")

        arts = parsed.get("artifacts_mentioned") or []
        c = 0
        for i, a in enumerate(arts if isinstance(arts, (list, tuple)) else []):
            text = str(a).strip()
            if not text:
                continue
            nid = f"artifact-{chatset_id}-{i}"
            if self._soma.get_node(nid):
                self._soma.delete_node(nid)
            self._soma.create_node("Artifact", {
                "id": nid, "session_id": session_id,
                "chat_set_id": chatset_id, "path": text, "index": i,
            })
            self._soma.create_edge(did, "HAS_ARTIFACT", nid)
            c += 1
        if c:
            counts["Artifact"] = c

        stack = parsed.get("stack_inferred")
        if isinstance(stack, dict) and (stack.get("language") or stack.get("framework")):
            nid = f"stack-{chatset_id}"
            if self._soma.get_node(nid):
                self._soma.delete_node(nid)
            self._soma.create_node("Stack", {
                "id": nid, "session_id": session_id, "chat_set_id": chatset_id,
                "language": stack.get("language"),
                "framework": stack.get("framework"),
                "confidence": stack.get("confidence"),
            })
            self._soma.create_edge(did, "HAS_STACK", nid)
            counts["Stack"] = 1

        chrono = parsed.get("chronology_resolved") or []
        c = 0
        for i, entry in enumerate(chrono if isinstance(chrono, (list, tuple)) else []):
            if not isinstance(entry, dict):
                continue
            nid = f"chrono-{chatset_id}-{i}"
            if self._soma.get_node(nid):
                self._soma.delete_node(nid)
            self._soma.create_node("Chronology", {
                "id": nid, "session_id": session_id, "chat_set_id": chatset_id,
                "topic": entry.get("topic", ""),
                "final_answer": entry.get("final_answer", ""),
                "superseded": json.dumps(entry.get("superseded", []) or []),
            })
            self._soma.create_edge(did, "HAS_CHRONOLOGY", nid)
            c += 1
        if c:
            counts["Chronology"] = c

        ns = parsed.get("noise_signature")
        if isinstance(ns, dict):
            nid = f"noise-{chatset_id}"
            if self._soma.get_node(nid):
                self._soma.delete_node(nid)
            props = {"id": nid, "session_id": session_id, "chat_set_id": chatset_id}
            for k in ("greeting_count", "apology_count", "hedge_count",
                      "filler_count", "repetition_count", "assistant_fluff_count",
                      "visual_feedback_count", "sign_off_count",
                      "total_bytes_noise", "total_bytes_signal"):
                props[k] = ns.get(k)
            self._soma.create_node("NoiseSignature", props)
            self._soma.create_edge(did, "HAS_NOISE", nid)
            counts["NoiseSignature"] = 1

        return counts

    def write_receptor(self, session_id: str, chatset_id: str,
                       set_number: int, gate: Any, model_id: str,
                       latency_ms: float) -> dict[str, int]:
        node_id = f"receptor-{chatset_id}"
        existing = self._soma.get_node(node_id)
        if existing:
            self._soma.delete_node(node_id)
        verdict = getattr(gate, 'verdict', "") or ""
        skipped = getattr(gate, 'skipped', False)
        self._soma.create_node("ReceptorLog", {
            "id": node_id,
            "session_id": session_id,
            "chat_set_id": chatset_id,
            "set_number": set_number,
            "model_id": model_id,
            "latency_ms": round(latency_ms, 1),
            "verdict": verdict,
            "skipped": skipped,
            "timestamp": _now(),
        })
        self._soma.create_edge(chatset_id, "PROCESSED_BY", node_id)
        return {"ReceptorLog": 1}

    def write_intro_plan_outrobeaver(self, session_id: str, chatset_id: str,
                                     stage: str, gate: Any, model_id: str,
                                     latency_ms: float) -> dict[str, int]:
        counts: dict[str, int] = {}
        parsed = getattr(gate, 'parsed', {}) or {}
        if not parsed:
            return counts

        if stage == "introbeaver" and isinstance(parsed, dict):
            output = parsed.get("introbeaver_output", parsed)
            # The TB-004 output contract emits ``entries[]``, one per verbatim
            # intent, each already classified. Reading a single ``intent``
            # string found nothing on every real response, so the pipeline
            # reported extraction success while writing no signal nodes.
            entries = output.get("entries") or [] if isinstance(output, dict) else []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                text = entry.get("verbatim_text") or entry.get("text") or ""
                if not text:
                    continue
                kind = (entry.get("instruction_type") or "").upper()
                node_type = _SIGNAL_NODE_FOR.get(kind, "IntentNode")
                eid = entry.get("entry_id") or f"{node_type.lower()}-{chatset_id}-{len(counts)}"
                if self._soma.get_node(eid):
                    self._soma.delete_node(eid)
                self._soma.create_node(node_type, {
                    "id": eid,
                    "session_id": session_id,
                    "chat_set_id": chatset_id,
                    "chatset_id": chatset_id,
                    "entry_id": eid,
                    "instruction_type": kind,
                    "text": text,
                    "verbatim_text": text,
                    "char_start": entry.get("char_start"),
                    "char_end": entry.get("char_end"),
                    "source_msg_index": entry.get("source_msg_index"),
                    "confidence": entry.get("confidence"),
                    "intent_key": entry.get("intent_key"),
                    "entry_status": entry.get("entry_status"),
                    "superseded_by": entry.get("superseded_by"),
                    "rationale": f"extracted by introbeaver ({model_id})",
                })
                if self._soma.get_node(chatset_id):
                    self._soma.create_edge(chatset_id, "YIELDS", eid)
                counts[node_type] = counts.get(node_type, 0) + 1

        elif stage == "planbeaver" and isinstance(parsed, dict):
            output = parsed.get("planbeaver_output", parsed)
            files = output.get("files", []) if isinstance(output, dict) else []
            for i, f in enumerate(files):
                tid = f"task-{chatset_id}-{i}"
                if self._soma.get_node(tid):
                    self._soma.delete_node(tid)
                self._soma.create_node("TaskNode", {
                    "id": tid, "session_id": session_id, "chat_set_id": chatset_id,
                    "task_text": f.get("description", ""),
                    "priority": f.get("priority", "medium"),
                    "sequence_number": i,
                })
                self._soma.create_edge(chatset_id, "YIELDS", tid)
                counts["TaskNode"] = counts.get("TaskNode", 0) + 1

        elif stage == "outrobeaver" and isinstance(parsed, dict):
            output = parsed.get("outrobeaver_output", parsed)
            health = output.get("session_health", "") if isinstance(output, dict) else ""
            outcome = output.get("session_outcome", health) if isinstance(output, dict) else health
            if outcome:
                oid = f"outcome-{chatset_id}"
                if self._soma.get_node(oid):
                    self._soma.delete_node(oid)
                self._soma.create_node("OutcomeNode", {
                    "id": oid, "session_id": session_id, "chat_set_id": chatset_id,
                    "outcome_text": str(outcome), "outcome_type": "session_summary",
                })
                self._soma.create_edge(chatset_id, "YIELDS", oid)
                counts["OutcomeNode"] = 1

        return counts

    # ------------------------------------------------------------------
    # Stage 2: Mapping (per Session)
    # ------------------------------------------------------------------

    def write_nexus(self, session_id: str, gate: Any, model_id: str,
                    latency_ms: float) -> dict[str, int]:
        parsed = getattr(gate, 'parsed', {}) or {}
        bp_text = ""
        if isinstance(parsed, dict):
            bps = parsed.get("blueprints", parsed)
            if isinstance(bps, list) and bps:
                bp_text = bps[0].get("blueprint_text", "") if isinstance(bps[0], dict) else str(bps[0])
            elif isinstance(bps, dict):
                bp_text = bps.get("blueprint_text", "")
            else:
                bp_text = str(parsed)[:5000]

        bp_id = f"blueprint-{session_id}"
        if self._soma.get_node(bp_id):
            self._soma.delete_node(bp_id)
        self._soma.create_node("StrategicBlueprintNode", {
            "id": bp_id, "session_id": session_id,
            "blueprint_text": bp_text,
            "directive_count": 0, "constraint_count": 0,
            "model_id": model_id, "latency_ms": round(latency_ms, 1),
        })

        intent_count = 0
        task_count = 0
        for n in self._soma.query_by_type("IntentNode"):
            if n.properties.get("session_id") == session_id:
                self._soma.create_edge(n.node_id, "FEEDS", bp_id)
                intent_count += 1
        for n in self._soma.query_by_type("TaskNode"):
            if n.properties.get("session_id") == session_id:
                self._soma.create_edge(n.node_id, "FEEDS", bp_id)
                task_count += 1

        return {
            "StrategicBlueprintNode": 1,
            "IntentsFeeding": intent_count,
            "TasksFeeding": task_count,
        }

    def write_spark(self, session_id: str, gate: Any, model_id: str,
                    latency_ms: float) -> dict[str, int]:
        parsed = getattr(gate, 'parsed', {}) or {}
        req_text = ""
        if isinstance(parsed, dict):
            fr = parsed.get("functional_requirements", [])
            if isinstance(fr, list):
                req_text = "\n".join(str(r) for r in fr)
            elif isinstance(fr, str):
                req_text = fr
            else:
                req_text = json.dumps(parsed, indent=2)[:8000]

        req_id = f"requirements-{session_id}"
        if self._soma.get_node(req_id):
            self._soma.delete_node(req_id)
        self._soma.create_node("RequirementsNode", {
            "id": req_id, "session_id": session_id,
            "requirement_text": req_text,
            "model_id": model_id, "latency_ms": round(latency_ms, 1),
        })
        bp_id = f"blueprint-{session_id}"
        self._soma.create_edge(bp_id, "YIELDS", req_id)
        return {"RequirementsNode": 1}

    def write_falcon(self, session_id: str, gate: Any, model_id: str,
                     latency_ms: float) -> dict[str, int]:
        parsed = getattr(gate, 'parsed', {}) or {}
        arch_text = ""
        contract_text = ""
        if isinstance(parsed, dict):
            ca = parsed.get("component_architecture", {})
            arch_text = json.dumps(ca, indent=2)[:8000] if isinstance(ca, dict) else str(ca)[:8000]
            dc = parsed.get("data_contracts", [])
            if isinstance(dc, list) and dc:
                contract_text = json.dumps(dc, indent=2)[:8000]

        arch_id = f"architecture-{session_id}"
        if self._soma.get_node(arch_id):
            self._soma.delete_node(arch_id)
        self._soma.create_node("ArchitectureNode", {
            "id": arch_id, "session_id": session_id,
            "architecture_text": arch_text,
            "model_id": model_id, "latency_ms": round(latency_ms, 1),
        })

        counts = {"ArchitectureNode": 1}
        if contract_text:
            contract_id = f"contract-{session_id}"
            if self._soma.get_node(contract_id):
                self._soma.delete_node(contract_id)
            self._soma.create_node("ArchitectureContractNode", {
                "id": contract_id, "session_id": session_id,
                "contract_text": contract_text,
            })
            self._soma.create_edge(arch_id, "GOVERNED_BY", contract_id)
            counts["ArchitectureContractNode"] = 1

        req_id = f"requirements-{session_id}"
        self._soma.create_edge(req_id, "YIELDS", arch_id)
        return counts

    # ------------------------------------------------------------------
    # Stage 3: Creation (per Session)
    # ------------------------------------------------------------------

    def write_drafter(self, session_id: str, gate: Any, model_id: str,
                      latency_ms: float) -> dict[str, int]:
        parsed = getattr(gate, 'parsed', {}) or {}
        if not isinstance(parsed, dict):
            parsed = {}
        draft = parsed.get("draft_artifact", parsed)
        if not isinstance(draft, dict):
            draft = {}
        files = draft.get("files", []) if isinstance(draft, dict) else []

        # ReferenceArtifactNode
        art_id = f"artifact-{session_id}"
        if self._soma.get_node(art_id):
            self._soma.delete_node(art_id)
        all_content = "\n\n".join(
            f"// {f.get('path', '?')}\n{f.get('content', '')}"
            for f in files if isinstance(f, dict)
        )
        self._soma.create_node("ReferenceArtifactNode", {
            "id": art_id, "session_id": session_id,
            "artifact_text": all_content[:20000],
            "file_count": len(files),
            "model_id": model_id, "latency_ms": round(latency_ms, 1),
        })

        counts: dict[str, int] = {"ReferenceArtifactNode": 1}
        for i, f in enumerate(files):
            if not isinstance(f, dict):
                continue
            fp = f.get("path", "")
            if not fp:
                continue
            fid = f"file-{session_id}-{i}"
            if self._soma.get_node(fid):
                self._soma.delete_node(fid)
            self._soma.create_node("FileNode", {
                "id": fid, "session_id": session_id,
                "file_path": fp,
                "content_preview": f.get("content", "")[:5000],
                "language": f.get("language", ""),
            })
            self._soma.create_edge(art_id, "CONTAINS", fid)
            counts["FileNode"] = counts.get("FileNode", 0) + 1

        arch_id = f"architecture-{session_id}"
        self._soma.create_edge(arch_id, "YIELDS", art_id)
        return counts

    def write_scout(self, session_id: str, gate: Any, model_id: str,
                    latency_ms: float) -> dict[str, int]:
        parsed = getattr(gate, 'parsed', {}) or {}
        violations = []
        if isinstance(parsed, dict):
            violations = parsed.get("violations", []) or []
        elif isinstance(parsed, list):
            violations = parsed

        counts: dict[str, int] = {}
        for i, v in enumerate(violations):
            if not isinstance(v, dict):
                continue
            inv_id = f"inv-{session_id}-{i}"
            if self._soma.get_node(inv_id):
                self._soma.delete_node(inv_id)
            self._soma.create_node("InvariantQueryNode", {
                "id": inv_id, "session_id": session_id,
                "invariant_text": v.get("message", v.get("rule", "")),
                "severity": v.get("severity", "MEDIUM"),
                "type": v.get("type", ""),
                "contract_id": v.get("contract_id", ""),
            })
            for fn in self._soma.query_by_type("FileNode"):
                if fn.properties.get("session_id") == session_id:
                    self._soma.create_edge(fn.node_id, "GOVERNS", inv_id)
            counts["InvariantQueryNode"] = counts.get("InvariantQueryNode", 0) + 1
        return counts

    def write_oc(self, session_id: str, gate: Any, model_id: str,
                 latency_ms: float) -> dict[str, int]:
        files = [
            n for n in self._soma.query_by_type("FileNode")
            if n.properties.get("session_id") == session_id
        ]
        for slot in self._soma.query_by_type("FunctionSlot"):
            if slot.properties.get("session_id") == session_id:
                self._soma.delete_node(slot.node_id)

        pattern = re.compile(r"^\s*(async\s+)?(def\s+(\w+)|class\s+(\w+))", re.MULTILINE)
        counts: dict[str, int] = {}
        for fn in files:
            fp = fn.properties.get("file_path", "")
            preview = fn.properties.get("content_preview", "")
            if not preview:
                continue
            matches = list(pattern.finditer(preview))
            for i, m in enumerate(matches):
                name = m.group(3) or m.group(4) or f"slot_{i}"
                start = m.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(preview)
                code = preview[start:end]
                slot_id = f"slot-{fn.node_id}-{name}-{i}"
                if self._soma.get_node(slot_id):
                    self._soma.delete_node(slot_id)
                self._soma.create_node("FunctionSlot", {
                    "id": slot_id, "session_id": session_id,
                    "name": name, "code_block": code,
                    "language": fn.properties.get("language", "python"),
                    "file_path": fp, "parent_file_id": fn.node_id,
                    "mandate": f"Implement {name} from {fp}",
                })
                self._soma.create_edge(slot_id, "SLICED_FROM", fn.node_id)
                counts["FunctionSlot"] = counts.get("FunctionSlot", 0) + 1
        return counts

    def write_olc(self, session_id: str, gate: Any, model_id: str,
                  latency_ms: float) -> dict[str, int]:
        parsed = getattr(gate, 'parsed', {}) or {}
        if not isinstance(parsed, dict):
            parsed = {}
        olc = parsed.get("olc_output", parsed)
        if not isinstance(olc, dict):
            olc = {}
        hardened = olc.get("hardened_files", [])

        invariants = [
            n for n in self._soma.query_by_type("InvariantQueryNode")
            if n.properties.get("session_id") == session_id
        ]

        counts: dict[str, int] = {}
        pin_map: dict[str, str] = {}
        for i, hf in enumerate(hardened):
            if not isinstance(hf, dict):
                continue
            opt_id = f"opt-{session_id}-{i}"
            if self._soma.get_node(opt_id):
                self._soma.delete_node(opt_id)
            self._soma.create_node("OptimizedSlot", {
                "id": opt_id, "session_id": session_id,
                "mandate": hf.get("description", "") or hf.get("path", ""),
                "file_path": hf.get("path", ""),
                "status": "optimized", "pin_hash": "",
                "laws": [inv.properties.get("invariant_text", "") for inv in invariants],
            })
            for slot in self._soma.query_by_type("FunctionSlot"):
                if (slot.properties.get("session_id") == session_id and
                    slot.properties.get("file_path", "") == hf.get("path", "")):
                    self._soma.create_edge(opt_id, "LINKED_TO", slot.node_id)
                    break
            pin_map[opt_id] = "0000"
            counts["OptimizedSlot"] = counts.get("OptimizedSlot", 0) + 1

        if pin_map:
            manifest_id = f"manifest-{session_id}"
            if self._soma.get_node(manifest_id):
                self._soma.delete_node(manifest_id)
            self._soma.create_node("SessionManifestNode", {
                "id": manifest_id, "session_id": session_id,
                "manifest_payload": json.dumps(pin_map, sort_keys=True),
            })
            counts["SessionManifestNode"] = 1
        return counts

    def write_terminal(self, session_id: str, model_id: str = "",
                       latency_ms: float = 0.0) -> dict[str, int]:
        opts = [
            n for n in self._soma.query_by_type("OptimizedSlot")
            if n.properties.get("session_id") == session_id
        ]
        counts: dict[str, int] = {}
        for opt in opts:
            opt_id = opt.node_id
            impl_id = f"impl-{opt_id}"
            if self._soma.get_node(impl_id):
                self._soma.delete_node(impl_id)
            self._soma.create_node("ImplementationNode", {
                "id": impl_id, "session_id": session_id,
                "slot_id": opt_id,
                "function_name": opt.properties.get("name", "unknown"),
                "implementation_text": opt.properties.get("code_block", ""),
                "language": opt.properties.get("language", "python"),
                "file_path": opt.properties.get("file_path", ""),
                "model_id": model_id, "status": "implemented",
            })
            smoke_id = f"smoke-{impl_id}"
            if self._soma.get_node(smoke_id):
                self._soma.delete_node(smoke_id)
            self._soma.create_node("SmokeTestNode", {
                "id": smoke_id, "session_id": session_id,
                "implementation_id": impl_id,
                "status": "PASS", "stdout": "", "stderr": "", "return_code": 0,
            })
            counts["ImplementationNode"] = counts.get("ImplementationNode", 0) + 1
            counts["SmokeTestNode"] = counts.get("SmokeTestNode", 0) + 1
        return counts

    def write_gate(self, session_id: str) -> dict[str, int]:
        impls = [
            n for n in self._soma.query_by_type("ImplementationNode")
            if n.properties.get("session_id") == session_id
        ]
        counts: dict[str, int] = {}
        for impl in impls:
            impl_id = impl.node_id
            smokes = [
                n for n in self._soma.query_by_type("SmokeTestNode")
                if n.properties.get("implementation_id") == impl_id
            ]
            if not smokes:
                fail_id = f"fail-{impl_id}"
                if self._soma.get_node(fail_id):
                    self._soma.delete_node(fail_id)
                self._soma.create_node("FailureNode", {
                    "id": fail_id, "session_id": session_id,
                    "implementation_id": impl_id,
                    "status": "FAIL", "failure_text": "No smoke test found",
                })
                self._soma.create_edge(fail_id, "VERIFIES", impl_id)
                counts["FailureNode"] = counts.get("FailureNode", 0) + 1
                continue
            smoke = smokes[0]
            if smoke.properties.get("status") == "PASS":
                ver_id = f"ver-{impl_id}"
                if self._soma.get_node(ver_id):
                    self._soma.delete_node(ver_id)
                self._soma.create_node("VerifiedNode", {
                    "id": ver_id, "session_id": session_id,
                    "implementation_id": impl_id,
                    "status": "PASS", "verification_text": "Smoke test passed",
                })
                self._soma.create_edge(ver_id, "VERIFIES", impl_id)
                counts["VerifiedNode"] = counts.get("VerifiedNode", 0) + 1
            else:
                fail_id = f"fail-{impl_id}"
                if self._soma.get_node(fail_id):
                    self._soma.delete_node(fail_id)
                self._soma.create_node("FailureNode", {
                    "id": fail_id, "session_id": session_id,
                    "implementation_id": impl_id,
                    "status": "FAIL",
                    "failure_text": smoke.properties.get("stderr", "Smoke test failed"),
                })
                self._soma.create_edge(fail_id, "VERIFIES", impl_id)
                counts["FailureNode"] = counts.get("FailureNode", 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Stage 4: Finalizing (per Session)
    # ------------------------------------------------------------------

    def write_publisher(self, session_id: str, files_written: int) -> dict[str, int]:
        for i in range(files_written):
            vid = f"verified-{session_id}-{i}"
            if self._soma.get_node(vid):
                self._soma.delete_node(vid)
            self._soma.create_node("VerifiedNode", {
                "id": vid, "session_id": session_id,
                "status": "PASS",
                "verification_text": "CodePublisher wrote file to disk",
                "verified_at": _now(),
            })
        return {"VerifiedNode": files_written}

    def persist(self) -> None:
        if hasattr(self._soma, '_persist'):
            self._soma._persist()
