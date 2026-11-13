# Complete Pipeline Action Map — Every Stage, Every Action

## PRE-PIPELINE: Session Seeding
**File:** `session_seeder.py`
**Purpose:** Load raw chat data into Soma graph foundation

| Line | Action |
|------|--------|
| 13 | `seed_session(soma, chat_sets, session_id, session_type)` — receives Soma + list of ChatSetRecord objects |
| 30 | `soma.create_node("SessionAnchor", {...})` — creates master anchor node with session_id, session_type, chat_set_count, created_at |
| 42 | `soma.create_node("ChatSet", {...})` — for EACH chat_set record: creates ChatSet node with chat_set_id, session_id, set_number, user_text, assistant_text |
| 54 | `soma.create_edge(session_id, "HAS_CHATSET", cs_id)` — links SessionAnchor → each ChatSet |

**Input:** Raw JSON chat files → ChatSetRecord list
**Output:** 1 SessionAnchor + N ChatSet nodes
**Edges:** `SessionAnchor →HAS_CHATSET→ ChatSet`

---

## STAGE 1: CLASSIFIER
**File:** `classifier_dendrite.py`
**Purpose:** Extract signals from each ChatSet's user_text → classify into typed nodes

### Constructor
- Stores `soma`, `llm_client`, `model_rotator`

### `run(chat_set_id)`
| Line | Action |
|------|--------|
| 178 | `model_id = self._rotator.next_model("classifier")` — picks model from deck |
| 180 | `cs = self._soma.get_node(chat_set_id)` — fetches ChatSet node |
| 184-185 | Extracts `session_id` and `user_text` from ChatSet properties |
| 206-210 | `get_receptor_config(self._soma, "classifier")` — loads LLM instruction template |
| 213-215 | **LLM CALL #1:** `complete_stage(self._llm, model_id, instruction_body, user_text)` — sends user_text with classifier prompt to TurnKey engine |
| 217-221 | If LLM call fails → falls back to regex-based heuristic classification |

### Fallback (`_run_regex_fallback`)
| Line | Action |
|------|--------|
| 272 | `signals = classify(user_text)` — regex patterns check for: Question, Correction, Directive/Command, Constraint, Preference, Noise |
| 241 | `soma.create_node(sig_type, {...})` — creates one node per signal found |

### Node Types Created
`IntentNode`, `DirectiveNode`, `ConstraintNode`, `QuestionNode`, `PreferenceNode`, `CorrectionNode`, `NoiseNode`

| Line | Action |
|------|--------|
| 234 | `node_id = f"sig-{chat_set_id}-{sig_type}-{i}"` — deterministic node ID |
| 249 | `soma.create_edge(chat_set_id, "EXTRACTED_FROM", node_id)` — links ChatSet → signal node |

**Input:** ChatSet nodes (from seeding)
**Output:** Signal nodes (one or more per ChatSet)
**LLM Calls:** 1 per ChatSet
**Edges:** `ChatSet →EXTRACTED_FROM→ SignalNode`

---

## STAGE 2: NEXUS
**File:** `nexus_dendrite.py`
**Purpose:** Synthesize a strategic blueprint from all DirectiveNode + ConstraintNode signals

### Constructor
- Stores `soma`, `llm_client`, `model_rotator`

### `run(session_id)`
| Line | Action |
|------|--------|
| 36 | `model_id = self._rotator.next_model("nexus")` — picks model for this stage |
| 38-41 | `self._soma.query_by_type("DirectiveNode")` — queries ALL DirectiveNode for this session |
| 42-45 | `self._soma.query_by_type("ConstraintNode")` — queries ALL ConstraintNode for this session |
| 48-53 | Builds payload: concatenates `source_text` from all directives and constraints |
| 56-60 | `get_receptor_config(self._soma, "nexus")` — loads LLM prompt template |
| 63-65 | **LLM CALL #2:** `complete_stage(self._llm, model_id, instruction_body, payload)` — asks LLM to synthesize blueprint from all directives+constraints |
| 80 | `blueprint_id = f"blueprint-{session_id}"` — deterministic ID |
| 85-91 | `soma.create_node("StrategicBlueprintNode", {...})` — creates blueprint node with blueprint_text, directive_count, constraint_count |
| 93-96 | `soma.create_edge(blueprint_id, "STRATEGIC_PLAN", d.id)` — links blueprint → each DirectiveNode |
| 96 | `soma.create_edge(blueprint_id, "CONSTRAINED_BY", c.id)` — links blueprint → each ConstraintNode |

**Input:** All DirectiveNode + ConstraintNode (from Classifier)
**Output:** 1 StrategicBlueprintNode
**LLM Calls:** 1
**Edges:** `Blueprint →STRATEGIC_PLAN→ DirectiveNode`, `Blueprint →CONSTRAINED_BY→ ConstraintNode`

---

## STAGE 3: SPARK
**File:** `spark_dendrite.py`
**Purpose:** Derive concrete requirements from the StrategicBlueprintNode

### Constructor
- Stores `soma`, `llm_client`, `model_rotator`

### `run(session_id)`
| Line | Action |
|------|--------|
| 35 | `model_id = self._rotator.next_model("spark")` — picks model |
| 37-40 | `self._soma.query_by_type("StrategicBlueprintNode")` — queries blueprint nodes |
| 55-56 | Extracts `blueprint_text` from the blueprint node |
| 59-63 | `get_receptor_config(self._soma, "spark")` — loads prompt template |
| 66-68 | **LLM CALL #3:** `complete_stage(self._llm, model_id, instruction_body, payload)` — asks to derive concrete requirements from blueprint |
| 83 | `requirements_id = f"req-{session_id}"` — deterministic ID |
| 88-93 | `soma.create_node("RequirementsNode", {...})` — creates requirements node with requirement_text, source_blueprint_id |
| 95 | `soma.create_edge(requirements_id, "DERIVED_FROM", blueprint.id)` — links Requirements → Blueprint |

**Input:** StrategicBlueprintNode (from Nexus)
**Output:** 1 RequirementsNode
**LLM Calls:** 1
**Edges:** `Requirements →DERIVED_FROM→ StrategicBlueprintNode`

---

## STAGE 4: FALCON
**File:** `falcon_dendrite.py`
**Purpose:** Derive architecture from requirements, then build ArchitectureContractNode from ConstraintNode signals

### Constructor
- Stores `soma`, `llm_client`, `model_rotator`
- `_CHOSEN_KEYS = frozenset(("store", "navigation", "backend"))`

### `run(session_id)`
| Line | Action |
|------|--------|
| 54 | `model_id = self._rotator.next_model("falcon")` — picks model |
| 56-59 | `self._soma.query_by_type("RequirementsNode")` — queries requirement nodes |
| 74-75 | Extracts `requirement_text` from RequirementsNode |
| 78-82 | `get_receptor_config(self._soma, "falcon")` — loads prompt template |
| 85-87 | **LLM CALL #4:** `complete_stage(self._llm, model_id, instruction_body, payload)` — asks to derive system architecture from requirements |
| 102 | `architecture_id = f"arch-{session_id}"` — deterministic ID |
| 104-106 | Deletes existing architecture node if present (overwrite idempotent) |
| 107-112 | `soma.create_node("ArchitectureNode", {...})` — creates architecture node with architecture_text, source_requirements_id |
| 115-118 | Removes old DERIVED_FROM edges, then `soma.create_edge(architecture_id, "DERIVED_FROM", req.id)` — links Architecture → Requirements |
| 122 | `contract_result = self._build_contract_from_constraints(session_id)` — builds contract NON-LLM |

### `_build_contract_from_constraints(session_id)`
| Line | Action |
|------|--------|
| 165-168 | Queries all ConstraintNode for session |
| 178-212 | Parses constraint `source_text` for `CHOSEN:` and `FORBIDDEN:` lines — extracts key=value pairs (store, navigation, backend) and forbidden tokens |
| 216-221 | Builds contract_text: `CHOSEN: store=<value>`, `CHOSEN: navigation=<value>`, `CHOSEN: backend=<value>`, optionally `FORBIDDEN: <tokens>` |
| 135 | `contract_id = f"arch-contract-{session_id}"` — deterministic ID |
| 138-140 | Deletes existing contract node if present |
| 141-145 | `soma.create_node("ArchitectureContractNode", {...})` — creates contract node with contract_text |
| 146-149 | Removes old LOCKS edges, then `soma.create_edge(architecture_id, "LOCKS", contract_id)` — links Architecture → Contract |

**Input:** RequirementsNode (from Spark), all ConstraintNode (from Classifier)
**Output:** 1 ArchitectureNode, 1 ArchitectureContractNode
**LLM Calls:** 1
**Edges:** `Architecture →DERIVED_FROM→ RequirementsNode`, `Architecture →LOCKS→ ArchitectureContractNode`

---

## STAGE 5: DRAFTER
**File:** `drafter_dendrite.py`
**Purpose:** Generate actual code via LLM, write to disk, create ReferenceArtifactNode + FileNode

### Infrastructure
- **ToolRegistry** (L79-214): Exposes callable tools to LLM:
  - `get_receptor_config` — fetch ReceptorConfig by agent_id
  - `get_chatset` — retrieve ChatSet by node_id
  - `list_chatsets_for_session` — list all ChatSet IDs for a session
  - `create_soma_node` — create typed node
  - `create_soma_edge` — create directed edge

### Constructor
- Stores `soma`, `llm_client`, `model_rotator`
- Creates `ToolRegistry(soma)`
- `_code_fence_re = re.compile(r"(?:^|\n)(```|~~~)(?:\w+)?\n(.*?)\1(?:\n|$)", re.DOTALL)` — extracts fenced code blocks

### `run(session_id)` — Phase 1: Architecture Planning
| Line | Action |
|------|--------|
| 537 | `self._tools.call("list_chatsets_for_session", {"session_id": session_id})` — gets all ChatSet nodes via ToolRegistry |
| 538 | `self._build_task_plan(session_id, chatsets)` — concatenates user_text from ChatSets (first 500 chars each) |
| 541 | `self._phase1_map_files(session_id, task_plan)` — makes planning call to LLM |

#### `_phase1_map_files(session_id, task_plan)`
| Line | Action |
|------|--------|
| 386 | Calls `self._llm.chat(messages, model=model_id)` — **LLM CALL #5 (1st planning call)** — asks LLM to output JSON array of file paths |
| 415-421 | Parses JSON array response (looks for `[` and `]`) |
| 428 | Fallback: if no files found, defaults to `["main.py"]` |
| 429-432 | Prints mapped file list |

### `run(session_id)` — FileNode Creation
| Line | Action |
|------|--------|
| 544 | `self._get_last_chatset_id(session_id)` — fetches last ChatSet node ID |
| 547-562 | For each file_path: creates FileNode |
| 554-561 | `soma.create_node("FileNode", {...})` — with session_id, chat_set_id, path, file_path, source_text="" |

### `run(session_id)` — Phase 2: Code Generation
| Line | Action |
|------|--------|
| 564-572 | For each (path, file_node_id): calls `self._phase2_generate_file(session_id, path, file_node_id, task_plan, index=i, total=len(file_paths))` |

#### `_phase2_generate_file(session_id, file_path, file_node_id, task_plan, index, total)`
| Line | Action |
|------|--------|
| 444 | `model_id = self._rotator.next()` — rotates to next model in deck |
| 445-462 | Calls `self._llm.chat(messages, model=model_id)` — **LLM CALL #6-N (1 per file)** — asks LLM to generate code for the specific file |
| 474-475 | `soma.create_node("ReferenceArtifactNode", {...})` — creates node with session_id, code_payload, file_path |
| 475 | `soma.create_edge(ref_node.id, "PRODUCES", file_node_id)` — links ReferenceArtifact → FileNode |
| 477-481 | **TB-DEND-022:** Looks up ArchitectureContractNode, creates `soma.create_edge(ref_node.id, "ARCHITECTS", contract_id)` — ReferenceArtifact → ArchitectureContract |

### `run(session_id)` — File Writing (TB-DRAFT-009)
| Line | Action |
|------|--------|
| 574-584 | For each reference artifact: reads `code_payload`, calls `self._write_draft_file()` |
| 500-505 | `_get_workspace_path(session_id)` — returns `Path.home() / "soma" / "chats" / safe_id / "workspace"` |
| 507-525 | `_write_draft_file(session_id, file_path, code_payload)`: normalizes path via `normalize_draft_relpath()`, resolves path, verifies it stays under workspace (security gate), creates parent dirs, writes code_payload as UTF-8 text |

### Metrics Aggregation
| Line | Action |
|------|--------|
| 587-593 | `self._parse_code_structure(total_code)` — counts functions, classes, imports, code chars |
| 598-620 | Aggregates: prompt_tokens, completion_tokens, total_tokens, llm_latency_ms across all LLM calls |

**Input:** All ChatSet nodes (from seeding), ArchitectureContractNode (from Falcon)
**Output:** ReferenceArtifactNode (1 per file), FileNode (1 per file), written code files to workspace
**LLM Calls:** 1 planning + N generation (1 per file)
**Edges:** `ReferenceArtifact →PRODUCES→ FileNode`, `ReferenceArtifact →ARCHITECTS→ ArchitectureContractNode`

---

## STAGE 6: SCOUT
**File:** `scout_dendrite.py`
**Purpose:** Map invariant rules to FileNodes — creates InvariantQueryNode for each file pattern match

### InvariantsDB (mock database, lines 26-67)
| Pattern | Rules |
|---------|-------|
| `*.py` | "All functions must have type hints", "No mutable module-level variables", "All public functions must have docstrings" |
| `*.json` | "Must be valid JSON", "Must have a root object or array" |
| `*.md` | "Must have a top-level heading" |
| `*test*.py` | "Must use pytest fixtures for Soma setup", "Must not write to production paths" |
| `default` | "Must not contain hardcoded secrets", "Must handle errors explicitly" |

### Constructor
- Stores `soma`, `llm_client`, `model_rotator`, `repo`, `invariants_client` (defaults to InvariantsDB)

### `run(session_id)`
| Line | Action |
|------|--------|
| 91 | `model_id = self._rotator.next_model("scout")` — picks model (not actually used for LLM call — this is rule-based) |
| 93-97 | `self._soma.query_by_type("FileNode")` — queries all FileNode for session |
| 100-103 | `self._soma.query_by_type("RequirementsNode")` — queries requirements nodes |
| 106-113 | Resolves repo from SessionAnchor if not provided |
| 116-129 | For each FileNode: |
| 118 | `file_path = fn.properties.get("file_path", fn.id)` |
| 119 | `rules = self._db.query(file_path, repo)` — matches file extension against InvariantsDB patterns via `fnmatch.fnmatch()` |
| 121 | `node_id = f"inv-{fn.id}-{uuid.uuid4().hex[:8]}"` — random ID for each invariant |
| 122-127 | `soma.create_node("InvariantQueryNode", {...})` — creates invariant node with invariant_text, target_file_node_id |
| 128 | `soma.create_edge(fn.id, "GOVERNS", node_id)` — links FileNode → InvariantQueryNode |

**Input:** FileNode (from Drafter)
**Output:** 1 InvariantQueryNode per invariant rule per file (typically 3-5 per file)
**LLM Calls:** 0 (pure rule-based)
**Edges:** `FileNode →GOVERNS→ InvariantQueryNode`

---

## STAGE 7: AXONHILLOCK
**File:** `axon_hillock_oc.py`
**Purpose:** Slice code files into FunctionSlot nodes + link evidence via TRACES_TO edges

### Constructor
- Stores `soma`, `llm_client`, `model_rotator`

### `run(session_id)`
| Line | Action |
|------|--------|
| 117 | `model_id = self._rotator.next_model("axon_hillock")` — picks model (not used for LLM, tracked for metrics) |
| 119 | `workspace = Path("/tmp") / f"soma_draft_{_sanitize_session_id(session_id)}"` — builds workspace path |
| 120-121 | If workspace doesn't exist, returns error with empty slot_ids |
| 124-129 | Pre-run cleanup: queries existing FunctionSlot nodes for session, deletes them (idempotent overwrite) |
| 134-147 | Walks workspace directory recursively: |
| 137 | `rel_path = str(file_path.relative_to(workspace))` |
| 139 | `content = file_path.read_text(encoding="utf-8")` — reads file content |
| 145 | `parent_file_id = self._find_or_create_file_node(session_id, rel_path)` — finds or creates FileNode |
| 146 | `self.process_file(session_id, rel_path, content, parent_file_id)` — slices file into FunctionSlots |

### `_find_or_create_file_node(session_id, rel_path)`
| Line | Action |
|------|--------|
| 181-185 | Queries existing FileNode by path — if found, returns its id |
| 188-201 | If not found: creates FileNode with session_id, chat_set_id (last ChatSet), path, file_path, source_text="" |

### `process_file(session_id, rel_path, content, parent_file_id)` — the actual slicing
| Line | Action |
|------|--------|
| 288 | `language = _detect_language(rel_path)` — detects Python/Shell/JavaScript from file extension |
| 289 | `spans = _extract_slot_spans(content, language)` — finds top-level function/class definitions using regex: |
| 27-40 | **Patterns:** Python `def/class`, Shell `function name()`, JavaScript async function/arrow/class |
| 295-315 | For each span (name, start, end): |
| 297 | `slot_id = f"slot-{session_id}-{file_hash}-{i}"` — deterministic ID |
| 298-311 | `soma.create_node("FunctionSlot", {...})` — with session_id, parent_file_node_id, file_path, name, mandate, terminal_id, status="draft", pin_hash="pending", code_block=raw code span, inputs=[], outputs=[], evidence_ids=[] |
| 314 | `soma.create_edge(slot_id, "SLICED_FROM", parent_file_id)` — links FunctionSlot → FileNode |

### `link_evidence_to_slots(session_id)` — TB-DEND-022
| Line | Action |
|------|--------|
| 328-331 | Queries all FunctionSlot nodes for this session |
| 333-339 | Queries DirectiveNode, ConstraintNode, TaskNode — collects their lowercased source_text/task_text |
| 342-354 | For each slot: |
| 343-346 | Extracts slot name and file_stem (basename without extension), lowercased |
| 349-351 | For each evidence node: checks if any needle (slot name, file stem) appears in evidence text |
| 353 | `slot.properties["evidence_ids"] = matched` — stores matched IDs as property |
| 351-352 | `self._soma.create_edge(slot.id, "TRACES_TO", node.id)` — **actual edge** from FunctionSlot → matched evidence node (Directive/Constraint/Task) |

**Input:** Code files in workspace (from Drafter)
**Output:** FunctionSlot nodes (one per function/class per file)
**LLM Calls:** 0 (pure heuristic, regex-based)
**Edges:** `FunctionSlot →SLICED_FROM→ FileNode`, `FunctionSlot →TRACES_TO→ DirectiveNode/ConstraintNode/TaskNode`

---

## STAGE 8: MYELINATOR
**File:** `myelinator_olc.py`
**Purpose:** Harden FunctionSlots with invariant rules → create OptimizedSlot + generate cryptographically secure PINs

### Constructor
- Stores `soma`, `llm_client`, `model_rotator`, `ToolRegistry`

### `run(session_id)`
| Line | Action |
|------|--------|
| 58 | `model_id = self._rotator.next_model("myelinator")` — picks model |
| 61-64 | Queries FunctionSlot nodes for session |
| 65-68 | Queries InvariantQueryNode for session |
| 89-113 | If invariants exist: calls LLM to match invariants to slots via tool-calling |
| 93 | `self._call_match_llm(slots, invariants, model_id)` — builds prompt listing all slots and invariants, calls `self._llm.chat()` with `create_soma_edge` tool schema |
| 103-113 | Executes tool calls through ToolRegistry — counts GOVERNS edges created |
| 120-128 | For each FunctionSlot: calls `self.harden_slot(slot.id, session_id, model_id)` |
| 132 | `self._write_manifest(session_id, pin_map)` — writes master SessionManifestNode with all PIN mappings |

### `harden_slot(slot_id, session_id, model_id)`
| Line | Action |
|------|--------|
| 217 | Gets the FunctionSlot node |
| 221-224 | Queries all InvariantQueryNode for session |
| 226-230 | Gets existing GOVERNS edges to avoid duplicates |
| 234 | Preserves GOVERNS edges already in the tree |
| 237-241 | Collects slot needles: (name, mandate, file_path) — lowercased |
| 244-250 | For each invariant: if invariant text contains any slot needle → `soma.create_edge(inv.id, "GOVERNS", slot_id)` → adds to matched list |
| 252 | `opt_id = f"opt-{slot_id}"` — deterministic ID |
| 253-255 | Deletes existing OptimizedSlot if present (overwrite) |
| 257-260 | Extracts mandate, terminal_id, laws (list of invariant texts) |
| 262 | `pin = self._generate_pin()` — cryptographically secure 4-digit PIN via `secrets.randbelow(10000)` |
| 263 | `pin_hash = self._hash_pin(pin)` — SHA-256 hash of PIN |
| 265-276 | `soma.create_node("OptimizedSlot", {...})` — with mandate (original + HARDENED RULES), terminal_id, status="optimized", pin_hash (SHA-256), parent_slot_id, invariant_count, laws |
| 276 | `soma.create_edge(opt_id, "LINKED_TO", slot_id)` — links OptimizedSlot → FunctionSlot |

### `_write_manifest(session_id, pin_map)`
| Line | Action |
|------|--------|
| 156-165 | `manifest_id = f"manifest-{session_id}"`, writes SessionManifestNode with manifest_payload = JSON dump of all PIN mappings |

**Input:** FunctionSlot (from AxonHillock), InvariantQueryNode (from Scout)
**Output:** OptimizedSlot nodes (one per FunctionSlot), SessionManifestNode (1)
**LLM Calls:** 1 (optional invariant matching call)
**Edges:** `OptimizedSlot →LINKED_TO→ FunctionSlot`, `InvariantQueryNode →GOVERNS→ FunctionSlot`

---

## STAGE 9: SALTATORYGATE
**File:** `pipeline/saltatory_gate.py`
**Purpose:** Run tests against generated code → create VerifiedNode or FailureNode

### Constructor
- Stores `soma`, `workspace_root`

### `run(session_id)`
| Line | Action |
|------|--------|
| 39 | Queries FunctionSlot nodes for session |
| 50-83 | For each FunctionSlot: |
| 55 | Prints slot name and file_path |
| 58 | `test_result = self._run_slot_tests(session_id, file_path)` — runs tests |
| 60-63 | If skipped (no test files): adds to skipped list |
| 65-73 | If passed: creates VerifiedNode + VERIFIED edge |
| 74-83 | If failed: creates FailureNode + VERIFIED edge |

### `_run_slot_tests(session_id, file_path)`
| Line | Action |
|------|--------|
| 107 | Gets workspace path for session |
| 112 | `test_files = list(workspace.rglob("test_*"))` — finds test files |
| 119 | Checks if target file exists |
| 123-128 | `subprocess.run([_PYTHON_BIN, "-m", "pytest", "-x", "--tb=short", ...test_files...], cwd=workspace, timeout=60)` — executes pytest |
| 130 | If returncode == 0: returns `{"passed": True}` |
| 133 | Else: returns `{"passed": False, "error": stderr[:500]}` |
| 135 | Timeout → `{"passed": False, "error": "test timeout"}` |
| 137 | FileNotFoundError → `{"passed": False, "error": "pytest not available", "skipped": True}` |

### `_create_verified_node(session_id, slot_id, slot_name, file_path)`
| Line | Action |
|------|--------|
| 153-166 | Creates VerifiedNode with slot_id, slot_name, file_path, status="verified", verification_method="test_suite" |

### `_create_failure_node(session_id, slot_id, slot_name, file_path, error)`
| Line | Action |
|------|--------|
| 174-188 | Creates FailureNode with slot_id, slot_name, file_path, status="failed", failure_reason (from test error) |

### Edge Creation
| Line | Action |
|------|--------|
| 71 | `soma.create_edge(slot_id, "VERIFIED", verified_id)` — if tests pass |
| 81 | `soma.create_edge(slot_id, "VERIFIED", failure_id)` — if tests fail |

**Input:** FunctionSlot (from AxonHillock)
**Output:** VerifiedNode (if tests pass) or FailureNode (if tests fail)
**LLM Calls:** 0 (test execution only)
**Edges:** `FunctionSlot →VERIFIED→ VerifiedNode/FailureNode`

---

## STAGE 10: CODEPUBLISHER
**File:** `pipeline/code_publisher.py`
**Purpose:** Write verified code to disk — only publishes from VerifiedNodes, aborts if any FailureNode exists

### Constructor
- Stores `soma`

### `run(session_id)`
| Line | Action |
|------|--------|
| 41-45 | Queries VerifiedNode nodes for session |
| 47-92 | For each VerifiedNode: |
| 48-49 | Gets slot_id, slot_name, file_path from VerifiedNode properties |
| 51-58 | Checks for FailureNode: `failure_node = self._soma.get_node(f"failure-{slot_id}")` — if FailureNode exists AND is type FailureNode, adds to skipped (reason="failure_node_exists"), prints SKIPPED message |
| 60-68 | Gets FunctionSlot node — if not found, adds to skipped (reason="slot_not_found") |
| 70-72 | Gets code_block from FunctionSlot — if empty, adds to skipped (reason="no_code_block") |
| 74-76 | `self._resolve_file_path(session_id, slot_id, file_path)` — resolves file path via SLICED_FROM chain to FileNode |
| 78-80 | `self._write_code(resolved_path, code_block, session_id)` — writes code to disk |

### `_resolve_file_path(session_id, slot_id, fallback_path)`
| Line | Action |
|------|--------|
| 123-147 | Queries SLICED_FROM edges from FunctionSlot |
| 125-127 | For each SLICED_FROM edge: gets target FileNode, extracts path from FileNode.properties["path"] or FileNode.properties["file_path"] |
| 128 | Falls back to FunctionSlot.properties["file_path"] |

### `_write_code(file_path, code, session_id)`
| Line | Action |
|------|--------|
| 149-167 | Gets workspace path for session |
| 153-154 | Constructs target = workspace / file_path |
| 155 | Creates parent directories |
| 156 | Writes code as UTF-8 text |

### Integrity Check (in orchestrator, run_pipeline.py L171-183)
| Line | Action |
|------|--------|
| 171 | If failure_count > 0: aborts publisher, creates abort report with failure reasons |
| 173-183 | Else: creates CodePublisher, runs with session_id |

**Input:** VerifiedNode (from SaltatoryGate)
**Output:** Written code files to workspace
**Precondition:** No FailureNode exists for the session (publisher aborts with report showing all failure reasons)
**Edge Traversal:** VerifiedNode → (reverse VERIFIED) → FunctionSlot → (reverse SLICED_FROM) → FileNode → (read properties["path"])

---

## COMPLETE NODE TYPE INVENTORY

| Type | Created By | Quantity (perms session) |
|------|-----------|------------------------|
| SessionAnchor | Seeding | 1 |
| ChatSet | Seeding | 22 |
| IntentNode | Classifier | 6 |
| DirectiveNode | Classifier | 5 |
| ConstraintNode | Classifier | 5 |
| CorrectionNode | Classifier | 1 |
| NoiseNode | Classifier | 7 |
| PreferenceNode | Classifier | 4 |
| StrategicBlueprintNode | Nexus | 1 |
| RequirementsNode | Spark | 1 |
| ArchitectureNode | Falcon | 1 |
| ArchitectureContractNode | Falcon | 1 |
| FileNode | Drafter + AxonHillock | 10 |
| ReferenceArtifactNode | Drafter | 5 |
| InvariantQueryNode | Scout | 15 |
| FunctionSlot | AxonHillock | 9 |
| OptimizedSlot | Myelinator | 9 |
| SessionManifestNode | Myelinator | 1 |
| VerifiedNode | SaltatoryGate | 0 |
| FailureNode | SaltatoryGate | 9 |

## COMPLETE EDGE TYPE INVENTORY

| Edge Type | Created By | Quantity (perms session) |
|-----------|-----------|------------------------|
| HAS_CHATSET | Seeding | 22 |
| EXTRACTED_FROM | Classifier | 28 |
| STRATEGIC_PLAN | Nexus | 3 |
| CONSTRAINED_BY | Nexus | 4 |
| DERIVED_FROM | Spark + Falcon | 2 |
| LOCKS | Falcon | 1 |
| PRODUCES | Drafter | 5 |
| ARCHITECTS | Drafter (TB-DEND-022) | 5 |
| GOVERNS | Scout | 15 |
| SLICED_FROM | AxonHillock | 9 |
| TRACES_TO | AxonHillock (TB-DEND-022) | 8 |
| LINKED_TO | Myelinator | 9 |
| VERIFIED | SaltatoryGate | 9 |

## LLM CALL SUMMARY

| Stage | Calls | Purpose |
|-------|-------|---------|
| Classifier | N per ChatSet | Extract signals from user_text |
| Nexus | 1 | Synthesize blueprint from directives+constraints |
| Spark | 1 | Derive requirements from blueprint |
| Falcon | 1 | Derive architecture from requirements |
| Drafter (Phase 1) | 1 | Plan file paths from task plan |
| Drafter (Phase 2) | N per file | Generate code for each file |
| Scout | 0 | Rule-based invariant lookup |
| AxonHillock | 0 | Heuristic regex slicing |
| Myelinator | 0-1 | Optional invariant matching via LLM |
| SaltatoryGate | 0 | Test execution (subprocess) |
| CodePublisher | 0 | File I/O only |

## LINEAGE CHAIN (Full Traceability)

```
ChatSet →EXTRACTED_FROM→ DirectiveNode →STRATEGIC_PLAN→ StrategicBlueprintNode
                                              →CONSTRAINED_BY→ ConstraintNode
StrategicBlueprintNode ←DERIVED_FROM── RequirementsNode
RequirementsNode ←DERIVED_FROM─────── ArchitectureNode
ArchitectureNode →LOCKS────────────→ ArchitectureContractNode
ReferenceArtifactNode →PRODUCES────→ FileNode
ReferenceArtifactNode →ARCHITECTS──→ ArchitectureContractNode
FileNode →SLICED_FROM──────────────→ FunctionSlot
FunctionSlot →TRACES_TO────────────→ DirectiveNode/ConstraintNode/TaskNode
FileNode →GOVERNS──────────────────→ InvariantQueryNode
InvariantQueryNode →GOVERNS────────→ FunctionSlot
FunctionSlot →LINKED_TO────────────→ OptimizedSlot
FunctionSlot →VERIFIED────────────→ VerifiedNode/FailureNode
```

---

*Generated: 2026-10-08. Source: /home/flintx/soma/*