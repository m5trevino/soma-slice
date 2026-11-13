# TurnKey SDK — Comprehensive Technical Manual

**Project:** /home/flintx/turn-key  
**Version:** 0.2.0  
**Package:** turnkey-sdk (Python)  
**Built:** 2026-10-06

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Architecture Map](#2-architecture-map)
3. [Module Deep Dives](#3-module-deep-dives)
4. [Consumer Modules](#4-consumer-modules)
5. [Profile Builder](#5-profile-builder)
6. [Data Flow Traces](#6-data-flow-traces)
7. [Decision Archaeology](#7-decision-archaeology)
8. [API Reference](#8-api-reference)
9. [Known Issues](#9-known-issues)
10. [Handoff Notes](#10-handoff-notes)

---

## 1. Executive Summary

**TurnKey SDK** is a Python package (`turnkey-sdk` v0.2.0) that provides intelligent model rotation and stage execution for the Peacock AI engine. It sits between consumer applications and a large language model API backend, providing:

- **Model deck rotation** — round-robin cycling through a deck of AI models
- **Gateway-based rotation** — fair distribution across infrastructure providers (OpenRouter, Ollama, Novita, Groq)
- **Stage pinning** — per-stage model lock (e.g., always use Claude-3 for the "distiller" stage)
- **Automatic retry** — on failure, rotates to the next model, up to a configurable max
- **Comprehensive error handling** — `StageError` captures full retry history, failed models, root cause
- **Tool/function-calling support** — passes `tools` and `tool_choice` through to the engine
- **Performance metrics** — timing data attached to responses
- **Configurable logging** — console + file handlers
- **Migration path** — coexists with legacy PeacockClient

**What it does NOT do:** It does not implement the engine-side model serving. It is purely a client-side orchestration layer that POSTs to `/v1/chat/completions` on an engine URL.

### Key Architecture Principles

1. **Profile-driven** — behavior is defined by JSON profile files, not hardcoded
2. **Two schema flavors** — new TurnKey schema (`deck`/`stages`/`pipeline`) and legacy Peacock schema (`attempts`)
3. **Gateway-first rotation** — models are grouped by infrastructure provider; rotation cycles through gateways
4. **Zero external runtime dependencies** — uses only Python stdlib (`urllib`, `json`, `threading`, `dataclasses`)
5. **Thread-safe** — deck rotation uses `threading.Lock`

---

## 2. Architecture Map

### 2.1 Directory Structure

```
turn-key/
├── pyproject.toml          # Package metadata: name=turnkey-sdk, v0.2.0
├── manifest.yaml           # Active slice: TB-TK-009 (retry logic)
├── sdk_manifest.yaml       # Full 40-slice implementation plan
├── TURNKEY_SLICES.md       # Human-readable slice descriptions
│
├── turnkey/                 # MAIN SDK PACKAGE (public API)
│   ├── __init__.py          # Public exports: SDK, types, load_profile
│   ├── sdk.py               # CORE: SDK class + run_stage()
│   ├── types.py             # Data types: NormalizedResponse, StageError, Profile, DeckCard, StageConfig, SDKConfig, SDKError
│   ├── loader.py            # Profile loader + schema validation
│   ├── rotator.py           # DeckRotator (thread-safe round-robin)
│   ├── gateway_rotator.py   # GatewayDeckRotator (multi-provider rotation)
│   ├── machinetalk_client.py # MachineTalkClient (Dendritic integration)
│   ├── profile_integration.py # Profile loading integration helpers
│   ├── tool_handling.py     # EnhancedSDK (tool support)
│   ├── timeout_handling.py  # TimeoutEnhancedSDK (per-call timeout overrides)
│   ├── error_propagation.py # ErrorPropagatingSDK (full diagnostics)
│   ├── logging_integration.py # LoggedSDK (detailed logging)
│   ├── performance_metrics.py # PerformanceEnhancedSDK + PerformanceMonitor
│   ├── config_validation.py # ValidatedSDKConfig + ValidatedSDK
│   ├── dependency_manager.py # Dependency analysis
│   ├── package_builder.py   # Packaging utilities
│   ├── migration_support.py # MigratableSDK + MigrationHelper
│   ├── release_manager.py   # Release management
│   ├── installation_guide.py # Installation docs generator
│   ├── example_usage.py     # Documentation examples
│   ├── ci_setup.py          # CI integration
│   └── [test_*.py]          # SDK unit tests
│
├── consumer/                 # CONSUMER MODULES (lightweight, copy-paste friendly)
│   ├── python/               # Standalone Python consumer
│   │   ├── executor.py       # run_profile(path, prompt) — main entry point
│   │   ├── loader.py         # Minimal profile loader (JSON/YAML, no validation)
│   │   ├── resolver.py       # Path resolution: CLI arg > env > default
│   │   ├── watcher.py        # ProfileWatcher (hot-reload on file change)
│   │   ├── peacock_types.py  # TypedDict/Protocol type hints
│   │   └── [test_*.py]       # Consumer tests
│   ├── js/                   # TypeScript consumer (not yet implemented)
│   ├── examples/             # Zero-config examples
│   ├── samples/              # Sample profile files
│   └── docs/                 # Consumer documentation
│
├── profile_builder/          # UI BUILDER (Solid.js frontend + Python backend)
│   ├── web/                  # Vanilla JS/HTML prototype
│   ├── solid/                # Solid.js production build
│   └── src/                  # Python backend (store, models, discovery, export)
│
├── tests/                    # SDK integration tests
├── .specstory/               # Conversation history cache
└── [misc]                    # Scripts, PDFs, config files
```

### 2.2 Module Dependency Graph

```
turnkey/__init__.py
  ├── sdk.py ──────────────────────────┐
  │   ├── loader.py ──────────────┐    │
  │   ├── rotator.py ────────┐    │    │
  │   ├── gateway_rotator.py │    │    │
  │   └── types.py ──────────┼────┘    │
  │                            │        │
  ├── types.py ←───────────────┼────────┘
  ├── loader.py ───────────────┘
  ├── gateway_rotator.py
  ├── rotator.py
  ├── machinetalk_client.py ───┤
  │   └── sdk.py ←────────────┘
  ├── profile_integration.py ──┤
  │   ├── loader.py ───────┐   │
  │   └── sdk.py ←─────────┼───┘
  │                        │
  ├── tool_handling.py ────┼──┐
  │   └── sdk.py ←────────┘  │
  ├── timeout_handling.py ───┼──┤
  ├── error_propagation.py ──┼──┤
  ├── logging_integration.py─┼──┤
  ├── performance_metrics.py─┼──┤
  ├── config_validation.py ──┼──┤
  ├── migration_support.py ──┼──┤
  ├── dependency_manager.py  │  │
  ├── package_builder.py     │  │
  ├── release_manager.py     │  │
  ├── installation_guide.py  │  │
  └── example_usage.py ──────┴──┘

consumer/python/executor.py
  └── consumer/python/loader.py  (independent, copy-paste version)
```

### 2.3 Entry Points

| Entry Point | Location | Purpose |
|---|---|---|
| `SDK.run_stage()` | `turnkey/sdk.py` | **Primary API** — runs a named stage with messages, tools, timeout |
| `load_profile()` | `turnkey/loader.py` | Loads and validates a profile JSON from disk |
| `DeckRotator.next()` | `turnkey/rotator.py` | Gets next model for a stage |
| `GatewayDeckRotator.next_model()` | `turnkey/gateway_rotator.py` | Gets next gateway-grouped model |
| `MachineTalkClient.call_stage()` | `turnkey/machinetalk_client.py` | Dendritic integration wrapper |
| `run_profile()` | `consumer/python/executor.py` | **Consumer API** — runs a profile against a prompt |
| `turnkey` CLI | `pyproject.toml:21` | Console script entry (not yet implemented) |

### 2.4 External Dependencies

- **Engine URL** (default: `http://engine.save-aichats.com:3099`) — receives POST to `/v1/chat/completions`
- **No Python packages required** — stdlib only (`urllib`, `json`, `threading`, `dataclasses`, `pathlib`, `typing`, `datetime`)

### 2.5 Profile Schema

**New TurnKey schema (required fields):**
```json
{
  "deck": ["gpt-4", "claude-3", "gemini-1.5"],
  "stages": {
    "distiller": {"pinnedModels": ["claude-3"], "useDeck": false},
    "summary": {"useDeck": true}
  },
  "pipeline": {
    "timeout": 180,
    "maxRetries": 10,
    "engineUrl": "http://engine.save-aichats.com:3099"
  }
}
```

**Legacy Peacock schema (required fields):**
```json
{
  "attempts": [
    {"model": "gpt-4", "temperature": 0.7, "max_tokens": 4096},
    {"model": "claude-3", "temperature": 0.7}
  ]
}
```

---

## 3. Module Deep Dives

### 3.1 SDK Core (`turnkey/sdk.py`)

**Lines:** ~500 (truncated in reading, ~22KB)  
**Purpose:** The heart of the SDK. `SDK` class orchestrates everything.

**Class: `SDK`**

```python
class SDK:
    def __init__(self, config: SDKConfig = None, profile_path: str = None)
    def run_stage(self, stage_name, messages, timeout=None, tools=None, tool_choice=None, temperature=None)
    def _call_engine(self, payload, timeout)
    def _normalize_response(self, raw, duration_ms)
```

**Initialization flow:**
1. Accepts `SDKConfig` and optional `profile_path`
2. If `profile_path` provided, calls `loader.load_profile(path)` to parse and validate
3. Normalizes deck entries (handles both string cards and `{"modelId": ...}` objects)
4. Normalizes stages (handles both list and dict formats)
5. Loads `timeout`, `max_retries`, `engine_url` from profile's `pipeline` section
6. Initializes `GatewayDeckRotator` with gateway-structured models

**`run_stage()` flow:**
1. Calls `rotator.next(stage_name)` to get the next model
2. Builds a chat completion payload: `{model, messages, temperature, tools, tool_choice}`
3. POSTs to `{engine_url}/v1/chat/completions` using `urllib.request`
4. Retries on HTTP errors (connection, timeout, 5xx) with model rotation
5. Tracks retry history in a `StageError` if all retries exhausted
6. Returns raw JSON response from engine

**Hard gates from design:**
- Must use `urllib` (no external deps)
- Must not mutate the deck during rotation
- Must respect per-call timeout overrides
- Must pass tools/tool_choice through unchanged

### 3.2 Data Types (`turnkey/types.py`)

**Lines:** 212 | **Size:** 7.3KB

**Key types:**

| Type | Kind | Purpose |
|---|---|---|
| `NormalizedResponse` | `@dataclass` | Canonical response: content, model, usage, duration_ms |
| `StageError` | `Exception` | Stage failure with retry history, failed_models, root_cause, error_code, details. JSON-serializable via `to_dict()` |
| `ProfileValidationError` | `Exception` | Profile schema validation failure with path |
| `Profile` | `@dataclass` | User/system profile: id, name, email, role, metadata |
| `DeckCard` | `@dataclass` | Individual card in a deck: id, title, content, tags, metadata |
| `StageConfig` | `@dataclass` | Stage config: stage_name, enabled, timeout, retries, max_concurrent, config_data |
| `SDKConfig` | `class` | Config: api_key, base_url, engine_url, timeout, max_retries |
| `SDKError` | `Exception` | Base SDK error with message and error_code |

**`StageError.to_dict()`** produces JSON-serializable output:
```python
{
    "stage": "distiller",
    "message": "All retries exhausted",
    "error_code": "STAGE_FAILURE",
    "retry_history": [{"model": "gpt-4", "error": "HTTP_503"}],
    "failed_models": ["gpt-4"],
    "root_cause": "Connection error",
    "details": None
}
```

**Private function: `_normalize_gateway_models()`**  
Converts deck-based profiles into gateway structure. If profile has `gateways` field, uses it directly. Otherwise wraps the entire deck as a single `{"gateway": "default", "models": [...]}`.

### 3.3 Profile Loader (`turnkey/loader.py`)

**Lines:** 160 | **Size:** 5KB

**Function: `load_profile(path=None) -> dict`**

Resolution order:
1. If `path` is None → defaults to `"peacock_profile.json"` in CWD
2. Reads file as UTF-8
3. Parses as JSON (raises `ValueError` on invalid JSON)
4. Validates schema via `_validate()`

**Validation logic (`_validate()`):**
- Checks that profile has EITHER `deck` (TurnKey) OR `attempts` (Peacock)
- `has_deck and has_attempts` → both validated independently

**TurnKey validation (`_validate_turnkey()`):**
- Required fields: `deck`, `stages`, `pipeline`
- `deck` must be a non-empty list
- `stages` must be a list or object
- `pipeline` must be an object

**Peacock validation (`_validate_peacock()`):**
- `attempts` must be a non-empty list
- Each attempt must be a dict with non-empty string `model` field

### 3.4 Deck Rotator (`turnkey/rotator.py`)

**Lines:** 111 | **Size:** 3.8KB

**Class: `DeckRotator`**

```python
class DeckRotator:
    def __init__(self, deck: list[str], stages_config: dict[str, Any])
    def next(self, stage: str) -> str
    @property deck_size -> int
    @property current_position -> int
```

**Behavior:**
- **Pinned stages:** if `stages_config[stage].pinnedModels` is non-empty AND `useDeck` is `False` → always returns `pinnedModels[0]`
- **Unpinned stages:** draws sequentially from deck, wraps around with `(pos + 1) % len(deck)`
- **Thread-safe:** `threading.Lock` protects `_pos`

**Important:** The internal deck is shallow-copied on construction to prevent caller mutation.

**Example:**
```python
rotator = DeckRotator(
    deck=["gpt-4", "claude-3", "gemini-1.5"],
    stages_config={"distiller": {"pinnedModels": ["claude-3"], "useDeck": False}}
)
rotator.next("distiller")  # "claude-3" (pinned)
rotator.next("summary")    # "gpt-4" (first from deck)
rotator.next("summary")    # "claude-3" (second from deck)
```

### 3.5 Gateway Rotator (`turnkey/gateway_rotator.py`)

**Lines:** 268 | **Size:** 9.1KB

**Class: `GatewayDeckRotator`**

```python
class GatewayDeckRotator:
    def __init__(self, gateway_models: list[dict[str, Any]])
    def next_model(self, stage: str) -> dict
    def get_gateway_info() -> dict
    def reset_positions()
    @property total_models -> int
    @property gateway_count -> int
```

**Rotation algorithm:**
1. Groups models by gateway: `{"gateway": name, "models": [{id, name}, ...]}`
2. Tracks per-gateway position
3. On each `next_model()`:
   - Finds gateway with minimum current position (lowest = next to draw from)
   - Gets next model from that gateway: `models[pos % len(models)]`
   - Advances that gateway's position

**Fair distribution:** Gateways with fewer models reset to model 1 sooner, ensuring no single gateway dominates.

**Example (4 gateways: OpenRouter=4, Ollama=3, Novita=2, Groq=4):**
```
1. OpenRouter: gpt-4
2. Ollama: llama3
3. Novita: mixtral-8x7b
4. Groq: llama3-70b
5. OpenRouter: claude-3
6. Ollama: mistral
7. Novita: qwen-72b
8. Groq: llama3-8b
9. OpenRouter: gemini-1.5
10. Ollama: phi3
11. Novita: mixtral-8x7b (reset)
12. Groq: mixtral-8x7b
...
```

### 3.6 MachineTalkClient (`turnkey/machinetalk_client.py`)

**Lines:** 62 | **Size:** 2.2KB

**Class: `MachineTalkClient`**

Purpose: Bridge between Dendritic's old `PeacockClient` and the new SDK.

```python
class MachineTalkClient:
    def __init__(self, sdk: SDK)
    def call_stage(self, stage_name, messages, timeout=None, tools=None, tool_choice=None, temperature=None) -> dict
```

**How it works:** Delegates entirely to `sdk.run_stage()`. Just adds the `MachineTalkClient` wrapper layer for Dendritic compatibility.

### 3.7 Profile Integration (`turnkey/profile_integration.py`)

**Lines:** 81 | **Size:** 2.8KB

**Functions:**

```python
def load_turnkey_profile(profile_path=None) -> dict
def initialize_sdk_with_profile(profile_path=None) -> SDK
```

**`load_turnkey_profile()`:**
- Default path: `~/dendritic/turn-key-profiles/`
- Scans directory for first `.json` file
- Calls `loader.load_profile(path)` to validate

**`initialize_sdk_with_profile()`:**
- Loads profile, extracts `pipeline` config
- Creates `SDKConfig` from pipeline fields
- Returns initialized `SDK` instance

### 3.8 Enhanced SDK Subclasses

The project creates several subclasses that add cross-cutting concerns:

**`EnhancedSDK` (tool_handling.py):** Adds `run_stage_with_tools()` — just passes tools through to parent.

**`TimeoutEnhancedSDK` (timeout_handling.py):** Adds `run_stage_with_timeout_override()` and `run_stage_with_default_timeout()`. Validates timeout ≥ 0.

**`ErrorPropagatingSDK` (error_propagation.py):** Wraps `run_stage()` in try/except, converts unexpected errors to `StageError` with `UNEXPECTED_ERROR` code. Also provides `handle_sdk_error()` for formatting errors for Dendritic.

**`LoggedSDK` (logging_integration.py):** Subclasses `run_stage()` with pre/post logging. Sets up file + console handlers. Logs model selection, retries, failures.

**`PerformanceEnhancedSDK` (performance_metrics.py):** Adds `run_stage_with_metrics()` — measures `time.time()` before/after, attaches `{"performance": {"total_duration_ms": ..., "call_timestamp": ...}}` to response.

**`ValidatedSDKConfig` (config_validation.py):** Validates timeout ≥ 0, max_retries ≥ 0, engine_url non-empty before construction.

**`ValidatedSDK` (config_validation.py):** Validates config AND profile data before initialization.

**`MigratableSDK` (migration_support.py):** Adds `run_stage_with_migration_support(legacy_mode=False)` — in legacy mode, provides compatibility shim. Also includes `MigrationHelper` class with `migrate_profile_format()` (converts old `attempts` format to new `deck`/`stages`/`pipeline`).

### 3.9 Dependency Manager (`turnkey/dependency_manager.py`)

**Lines:** 372 | **Size:** 11.8KB

**Class: `DependencyManager`**

Purpose: Analyzes project dependencies by parsing `pyproject.toml`, `setup.py`, `requirements.txt`. Uses basic regex parsing (no AST). Notably, it imports `tomli` for TOML parsing but the actual dependency analysis isn't hooked into the SDK itself — it's a standalone utility.

### 3.10 Package Builder (`turnkey/package_builder.py`)

**Lines:** 360 | **Size:** 9.5KB

Purpose: Generates packaging artifacts (setup.py, MANIFEST.in, README.md, LICENSE). Can build sdist/wheel via `subprocess`. Not a replacement for `pyproject.toml` — it's documentation of how packaging works.

### 3.11 Release Manager (`turnkey/release_manager.py`)

**Lines:** ~200+ (large file, 21KB)

**Class: `ReleaseManager`**

Manages version bumps, changelog updates, git tagging. Version stored in `turnkey/version.py` as `__version__ = "0.1.0"`.

### 3.12 Installation Guide Generator (`turnkey/installation_guide.py`)

**Lines:** 528 | **Size:** 11.1KB

Purpose: Generates installation documentation files (INSTALLATION_GUIDE.md, DEVELOPMENT_SETUP.md, quickstart.py, INSTALLATION_CHECKLIST.md).

---

## 4. Consumer Modules

These are **lightweight, standalone modules** in `consumer/python/` designed for copy-paste into other projects. They don't depend on the SDK package.

### 4.1 Executor (`consumer/python/executor.py`)

**Lines:** 163 | **Size:** 5.5KB

**Public API:**
```python
def run_profile(path: str | None, prompt: str | list[dict[str, str]], *, engine_url: str | None = None) -> NormalizedResponse
```

**How it works:**
1. Loads profile via `consumer/python/loader.py:load_profile()`
2. If profile has no `attempts` key, builds attempts from TurnKey `deck` (`_build_attempts_from_turnkey()`)
3. For each attempt in order:
   - Extracts `model` and optional `temperature`/`max_tokens`/`tools`
   - POSTs `{model, messages, temperature}` to `{engine_url}/v1/chat/completions`
   - Header: `X-Peacock-Mode: {attempt.get("mode", "balanced")}`
   - Timeout: 300 seconds
   - On success → returns `NormalizedResponse` immediately
   - On HTTP error → records failure, continues to next attempt
4. If all attempts fail → raises `FinalFailure(attempts=[...], last_status, last_body)`

**`NormalizedResponse` (frozen dataclass):** content, model, usage, duration_ms

**`FinalFailure` (frozen dataclass):** all failed attempts, last HTTP status, last response body

### 4.2 Loader (`consumer/python/loader.py`)

**Lines:** 47 | **Size:** 1.4KB

**Function: `load_profile(path=None)`**

Minimal loader — no schema validation. Supports `.json` and `.yaml`/`.yml`. If `path` is None → defaults to `"peacock_profile.json"`.

### 4.3 Resolver (`consumer/python/resolver.py`)

**Lines:** 37 | **Size:** 930B

**Function: `resolve_profile_path(path=None)`**

Resolution order:
1. Explicit `path` argument
2. `PEACOCK_PROFILE` environment variable
3. Default: `"peacock_profile.json"`

Does NOT check if file exists.

### 4.4 Watcher (`consumer/python/watcher.py`)

**Lines:** 99 | **Size:** 2.9KB

**Class: `ProfileWatcher`**

Polls a profile file's mtime in a background thread. When file changes (with debouncing), calls `callback(new_profile_dict)`.

```python
def watch_profile(path, callback, poll_interval=1.0, debounce_seconds=0.25) -> ProfileWatcher
```

### 4.5 Type Hints (`consumer/python/peacock_types.py`)

**Lines:** 50 | **Size:** 1.1KB

TypedDict and Protocol definitions for static type checking:
- `Attempt` — individual model attempt
- `Profile` — full profile structure
- `NormalizedResponse` — response protocol
- `FinalFailure` — failure protocol

---

## 5. Profile Builder

A separate application (not part of the SDK package) for building and managing profile JSON files.

### 5.1 Vanilla JS Prototype (`profile_builder/web/`)

Single-page app with:
- `index.html` — main page
- `app.js` — application logic
- `components/` — UI components (ProfilePreview, AttemptForm, AttemptsList, ModelPicker, ExportButton, ValidationPanel, etc.)
- `styles.css` — styling
- `useShortcuts.js` — keyboard shortcuts
- `src_bridge.js` — bridge to backend

### 5.2 Solid.js Production App (`profile_builder/solid/`)

**Framework:** Solid.js + TypeScript + Vite

```
solid/
├── src/
│   ├── App.tsx              # Main app component
│   ├── index.tsx             # Entry point
│   ├── index.css             # Global styles
│   ├── components/
│   │   ├── StagePinner.tsx   # Stage pinning UI
│   │   ├── ModelPool.tsx     # Model pool selection
│   │   ├── PipelinePanel.tsx # Pipeline configuration
│   │   ├── ProfileIO.tsx     # Import/export profile
│   │   └── DeckManager.tsx   # Deck management
│   └── stores/
│       ├── deckStore.ts      # Deck state (Zustand-like)
│       ├── stageStore.ts     # Stage configuration
│       └── pipelineStore.ts  # Pipeline config
├── vite.config.ts
├── tsconfig.json
└── package.json
```

### 5.3 Python Backend (`profile_builder/src/`)

**Modules:**
- `store.py` — Profile storage (save/load/delete)
- `models.py` — Data models for profiles
- `loader.py` — Profile loading
- `discovery.py` — Model discovery (finds available models)
- `export.py` — Export profile to JSON
- `profile.py` — Profile manipulation utilities
- `dirty.py` — Change tracking (unsaved changes detection)

### 5.4 Tests (`profile_builder/tests/`)

Tests for: store, loader, export, discovery, models, dirty tracking.

---

## 6. Data Flow Traces

### 6.1 Basic Stage Execution (SDK)

```
User calls: sdk.run_stage("distiller", [{"role": "user", "content": "Hello"}])
  │
  ├─► sdk.py:SDK.run_stage()
  │   │
  │   ├─► rotator.next("distiller")
  │   │   └─► Check pinnedModels → "claude-3" (pinned)
  │   │
  │   ├─► _call_engine({model: "claude-3", messages: [...], temperature: 0.7})
  │   │   ├─► urllib.request.POST to http://engine.save-aichats.com:3099/v1/chat/completions
  │   │   ├─► Timeout: 300s (from pipeline.timeout)
  │   │   └─► Returns: {"choices": [{"message": {"content": "..."}}, ...]}
  │   │
  │   └─► Returns: raw JSON response
```

### 6.2 Retry on Failure

```
User calls: sdk.run_stage("summary", messages)
  │
  ├─► rotator.next("summary") → "gpt-4"
  │   │
  │   ├─► POST to engine → HTTP 503
  │   │   └─► Retry 1: rotator.next("summary") → "claude-3"
  │   │       POST → HTTP 500
  │   │       └─► Retry 2: rotator.next("summary") → "gemini-1.5"
  │   │           POST → HTTP 502
  │   │           └─► Retry 3 (max reached)
  │   │
  │   └─► Raise: StageError(stage="summary", retry_history=[...], failed_models={"gpt-4","claude-3","gemini-1.5"})
```

### 6.3 Consumer `run_profile()`

```
User calls: run_profile("my_profile.json", "Hello, world!")
  │
  ├─► loader.load_profile("my_profile.json")
  │   └─► Returns: {"deck": [...], "stages": {...}, "pipeline": {...}}
  │
  ├─► _build_attempts_from_turnkey(profile)
  │   └─► [{"model": "gpt-4"}, {"model": "claude-3"}, ...]
  │
  ├─► For each attempt:
  │   ├─► POST {model, messages, temperature} to engine
  │   ├─► On success → return NormalizedResponse
  │   └─► On failure → record, continue to next
  │
  └─► If all failed → raise FinalFailure(attempts=[...])
```

### 6.4 Dendritic Integration (MachineTalkClient)

```
Dendritic calls: client.call_stage("distiller", messages)
  │
  ├─► MachineTalkClient.call_stage()
  │   └─► sdk.run_stage("distiller", messages)
  │       ├─► rotator.next("distiller") → pinned model
  │       ├─► POST to engine
  │       └─► Returns raw response
  │
  └─► Returns: raw response dict to Dendritic
```

### 6.5 Profile Hot-Reload

```
Profile file changes on disk
  │
  ├─► ProfileWatcher._check() detects mtime change
  │   └─► debounces (0.25s)
  │
  ├─► Reads and parses new JSON
  │
  └─► Calls callback(new_profile_dict)
```

### 6.6 Gateway-Based Rotation Sequence

```
Gateways: OpenRouter(4 models), Ollama(3), Novita(2), Groq(4)
  │
  ├─► Call 1:  OpenRouter / gpt-4         (pos: OR=1, Ol=0, Nv=0, Gr=0)
  ├─► Call 2:  Ollama / llama3             (pos: OR=1, Ol=1, Nv=0, Gr=0)
  ├─► Call 3:  Novita / mixtral-8x7b       (pos: OR=1, Ol=1, Nv=1, Gr=0)
  ├─► Call 4:  Groq / llama3-70b           (pos: OR=1, Ol=1, Nv=1, Gr=1)
  ├─► Call 5:  OpenRouter / claude-3       (pos: OR=2, Ol=1, Nv=1, Gr=1)
  ├─► Call 6:  Ollama / mistral            (pos: OR=2, Ol=2, Nv=1, Gr=1)
  ├─► Call 7:  Novita / qwen-72b           (pos: OR=2, Ol=2, Nv=2, Gr=1)
  ├─► Call 8:  Groq / llama3-8b            (pos: OR=2, Ol=2, Nv=2, Gr=2)
  │   ... cycle continues ...
  └─► Call 13: OpenRouter / gpt-4 (reset)  (all gateways reset)
```

---

## 7. Decision Archaeology

### Decision 1: Profile Schema — TurnKey vs Peacock

**What was decided:** Support both TurnKey (`deck`/`stages`/`pipeline`) and Peacock (`attempts`) schemas.

**When:** TB-TK-003 / TB-TK-017

**Why:** Backward compatibility with existing Peacock profiles that use `attempts`. New profiles use the more structured TurnKey schema with stage pinning and pipeline config.

**Rejected alternatives:** 
- Drop Peacock support entirely (breaking change)
- Force all profiles to one schema (migration pain)

**Consequences:** Validation logic is more complex; both schemas must be handled in loader, executor, and migration helper.

### Decision 2: Gateway-Based Rotation Over Simple Deck Rotation

**What was decided:** Replace `DeckRotator` with `GatewayDeckRotator` for fair distribution across infrastructure providers.

**When:** Gateway integration (post-TB-TK-007)

**Why:** Different gateways (OpenRouter, Ollama, Novita, Groq) provide different numbers of models. Simple round-robin would bias toward gateways with more models.

**Consequences:** More complex rotation logic but fairer model distribution and cost optimization across providers.

### Decision 3: Zero External Dependencies

**What was decided:** Use only Python stdlib (`urllib`, `json`, `threading`). No `requests`, no `aiohttp`, no third-party HTTP client.

**When:** TB-TK-006

**Why:** Keep the SDK lightweight and installable without dependency resolution. The `dependency_manager.py` and `package_builder.py` modules list dependencies like `urllib3`, `psutil`, `tomli` but these are NOT actually required at runtime — they're documentation of what COULD be used.

**Consequences:** No connection pooling, no async support, manual retry logic instead of using a battle-tested HTTP library.

### Decision 4: Subclass Pattern for Cross-Cutting Concerns

**What was decided:** Instead of mixins or decorators, create concrete subclasses of `SDK` for each concern (timeout, logging, error propagation, performance, validation, migration).

**Why:** Simple inheritance is easier to understand than dynamic composition. Each subclass can be used independently.

**Consequences:** Potential for method resolution order issues if someone tries to combine multiple enhanced SDKs. No multiple inheritance in practice.

### Decision 5: Two Separate Loader Implementations

**What was decided:** `turnkey/loader.py` (validated, SDK-focused) and `consumer/python/loader.py` (minimal, no validation).

**Why:** The SDK needs validation and schema enforcement. The consumer module is designed for copy-paste into other projects where validation overhead is unnecessary.

**Consequences:** Duplication of basic file-reading logic. A future refactoring could extract a common base.

### Decision 6: No Engine-Side Implementation

**What was decided:** The SDK only implements the CLIENT side. The engine (model serving, routing, etc.) is a separate system at `engine.save-aichats.com:3099`.

**Why:** Separation of concerns — the SDK is a reusable client library; the engine is the actual model orchestration backend.

**Consequences:** SDK is useless without a running engine. No local/demo mode built in.

---

## 8. API Reference

### 8.1 SDK Public API (from `turnkey/__init__.py`)

```python
from turnkey import SDK, SDKConfig, SDKError, StageError, ProfileValidationError
from turnkey import NormalizedResponse, Profile, DeckCard, StageConfig
from turnkey import load_profile

# === SDK ===
sdk = SDK(config=SDKConfig(engine_url="http://localhost:3099", timeout=180, max_retries=10))
result = sdk.run_stage(
    stage_name="distiller",
    messages=[{"role": "user", "content": "Summarize this text."}],
    timeout=60,           # Optional override
    tools=[{"type": "function", ...}],
    tool_choice="auto",
    temperature=0.7
)
# result is raw dict from engine: {"choices": [...], "model": "...", "usage": {...}}

# === StageError ===
try:
    sdk.run_stage("distiller", messages)
except StageError as e:
    print(e.to_dict())  # JSON-serializable
    print(e.failed_models)  # set of model IDs
    print(e.retry_history)  # list of {model, error}
    print(str(e))  # Human-readable: "message | [N attempts: model1: err; model2: err]"

# === Profile Loading ===
profile = load_profile("/path/to/profile.json")
# Returns validated dict with deck/stages/pipeline

# === MachineTalkClient ===
from turnkey import MachineTalkClient
client = MachineTalkClient(sdk)
result = client.call_stage("distiller", messages)

# === Profile Integration ===
from turnkey import initialize_sdk_with_profile
sdk = initialize_sdk_with_profile("/path/to/profile.json")
```

### 8.2 Consumer API (from `consumer/python/executor.py`)

```python
from executor import run_profile, NormalizedResponse, FinalFailure

# Simple one-shot profile execution
result = run_profile("my_profile.json", "Hello, world!")
print(result.content)      # Response text
print(result.model)        # Model that succeeded
print(result.duration_ms)  # Time taken

# With engine override
result = run_profile("my_profile.json", "Hello", engine_url="http://localhost:3099")

# Error handling
try:
    result = run_profile("my_profile.json", "Hello")
except FinalFailure as e:
    print(f"All {len(e.attempts)} attempts failed")
    print(f"Last HTTP status: {e.last_status}")

# NormalizedResponse is a frozen dataclass:
result.content    # str | None
result.model      # str | None
result.usage      # dict | None
result.duration_ms # float | None
```

### 8.3 Profile Watcher API

```python
from watcher import watch_profile

def on_profile_changed(new_profile):
    print(f"Profile updated: {new_profile}")

watcher = watch_profile("my_profile.json", on_profile_changed)
# watcher.stop() to stop watching
```

### 8.4 Resolver API

```python
from resolver import resolve_profile_path

path = resolve_profile_path()               # Uses env or default
path = resolve_profile_path("custom.json")  # Explicit path
```

### 8.5 Type Definitions (from `consumer/python/peacock_types.py`)

```python
from peacock_types import Attempt, Profile, NormalizedResponse, FinalFailure

# Attempt TypedDict
attempt: Attempt = {"model": "gpt-4", "temperature": 0.7, "max_tokens": 4096}

# Profile TypedDict
profile: Profile = {"name": "my-profile", "attempts": [...]}
```

### 8.6 Enhanced SDK APIs

```python
from tool_handling import EnhancedSDK
sdk = EnhancedSDK()
sdk.run_stage_with_tools("stage", messages, tools=[...], tool_choice="auto")

from timeout_handling import TimeoutEnhancedSDK
sdk = TimeoutEnhancedSDK()
sdk.run_stage_with_timeout_override("stage", messages, timeout=30)

from error_propagation import ErrorPropagatingSDK, handle_sdk_error
sdk = ErrorPropagatingSDK()
try:
    sdk.run_stage_with_error_propagation("stage", messages)
except StageError as e:
    info = handle_sdk_error(e, "stage")  # Dict for Dendritic

from logging_integration import LoggedSDK, setup_sdk_logging
setup_sdk_logging(level=logging.DEBUG, file_path="sdk_debug.log")
sdk = LoggedSDK()

from performance_metrics import PerformanceEnhancedSDK, PerformanceMonitor
sdk = PerformanceEnhancedSDK()
result = sdk.run_stage_with_metrics("stage", messages)
# result["performance"]["total_duration_ms"]

monitor = PerformanceMonitor()
monitor.record_metrics("stage", metrics, exec_time)
report = monitor.get_performance_report()

from config_validation import ValidatedSDKConfig, ValidatedSDK
config = ValidatedSDKConfig(engine_url="...", timeout=180, max_retries=10)
sdk = ValidatedSDK(config=config, profile_path="profile.json")

from migration_support import MigratableSDK, MigrationHelper
sdk = MigratableSDK()
sdk.run_stage_with_migration_support("stage", messages, legacy_mode=False)

new_profile = MigrationHelper.migrate_profile_format(old_profile)
ready = MigrationHelper.check_migration_readiness(profile)
guide = MigrationHelper.generate_migration_guide()
check_and_migrate_profile("old.json", "new.json")
```

---

## 9. Known Issues

### 9.1 Missing Version Module

`pyproject.toml` declares `version = "0.2.0"` but `turnkey/version.py` (created by `package_builder.py`) contains `__version__ = "0.1.0"` — the versions are out of sync. The `release_manager.py` expects `version.py` to exist but it's not part of the repository.

### 9.2 No `setup.py`

`pyproject.toml` is present but there's no `setup.py` file. The `package_builder.py` module can generate one, but it hasn't been run. The project builds via `setuptools.build_meta` backend, so `pip install .` should work.

### 9.3 Unused/Orphaned Modules

Several modules exist as **illustrations** rather than fully integrated code:
- `dependency_manager.py` — standalone utility, not used by SDK
- `package_builder.py` — generates packaging artifacts, not integrated
- `release_manager.py` — standalone release management
- `installation_guide.py` — generates docs, not part of runtime
- `test_*.py` files within `turnkey/` — SDK unit tests (unrelated to `tests/` directory)

### 9.4 Engine Dependency

The SDK has no way to verify that the engine is reachable. `_call_engine()` will raise `urllib` exceptions on connection failure, which are caught by retry logic. But there's no health check or startup validation.

### 9.5 No Async Support

All HTTP calls are synchronous via `urllib.request`. No `asyncio` or `aiohttp`. This is by design (zero dependencies) but limits concurrency.

### 9.6 Consumer/SDK Loader Duplication

`consumer/python/loader.py` and `turnkey/loader.py` both implement `load_profile()` with different behavior. The consumer version has no validation and supports YAML. The SDK version validates schema and only supports JSON.

### 9.7 `DeckCard.tags` Default

`DeckCard.__post_init__()` sets `self.tags = []` if `None`, but `tags: List[str] = None` in the dataclass creates a mutable default at class definition time (though `__post_init__` catches it). The `created_at` and `updated_at` defaults use `datetime.now()` at instance creation time, which is correct.

### 9.8 `StageError` with `root_cause` Not Pickled

The `StageError.to_dict()` converts `root_cause` to `str()` to avoid pickling issues, but the original `Exception` reference is lost if the error is serialized/deserialized across process boundaries.

### 9.9 `GatewayDeckRotator.next_model(stage)` Ignores Stage

The `stage` parameter is accepted but **not used** in the current implementation. It's noted in the code as "can be used for future stage-specific behavior" (line 94-95).

### 9.10 Profile Builder Solid App Has Not Been Built

`profile_builder/solid/dist/` contains compiled assets but no clear build script or documentation for how to rebuild. The Vite config exists but there's no `build` script documented.

---

## 10. Handoff Notes

### 10.1 How to Use This SDK From Another App

**Option A: Direct SDK import (recommended for Python apps)**

```python
# pip install -e /home/flintx/turn-key  (or just add to PYTHONPATH)
from turnkey.sdk import SDK
from turnkey.types import SDKConfig, StageError

sdk = SDK(config=SDKConfig(engine_url="http://engine.save-aichats.com:3099"))

try:
    result = sdk.run_stage("summary", [{"role": "user", "content": "Hello"}])
    print(result["choices"][0]["message"]["content"])
except StageError as e:
    print(f"All retries failed: {e}")
```

**Option B: Consumer `run_profile()` for simple profile-based usage**

```python
# Copy consumer/python/*.py into your project
from executor import run_profile

result = run_profile("my_profile.json", "Hello, world!")
print(result.content)
```

**Option C: MachineTalkClient for Dendritic integration**

```python
from turnkey.sdk import SDK
from turnkey.machinetalk_client import MachineTalkClient

sdk = SDK(config=SDKConfig(engine_url="..."))
client = MachineTalkClient(sdk)
result = client.call_stage("distiller", messages)
```

### 10.2 Profile File Format

Save as `peacock_profile.json` (or any path):

```json
{
  "deck": [
    {"modelId": "gpt-4", "modelName": "GPT-4"},
    {"modelId": "claude-3", "modelName": "Claude 3"},
    {"modelId": "gemini-1.5", "modelName": "Gemini 1.5"}
  ],
  "stages": {
    "distiller": {"pinnedModels": ["claude-3"], "useDeck": false},
    "summary": {"useDeck": true},
    "analyst": {"useDeck": true}
  },
  "pipeline": {
    "timeout": 180,
    "maxRetries": 10,
    "engineUrl": "http://engine.save-aichats.com:3099"
  }
}
```

### 10.3 Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `PEACOCK_ENGINE_URL` | Consumer module engine URL | `https://engine.save-aichats.com` |
| `PEACOCK_PROFILE` | Consumer module profile path | `peacock_profile.json` |

The SDK itself uses `SDKConfig` for configuration, not environment variables.

### 10.4 Package Installation

```bash
# From source (development mode)
cd /home/flintx/turn-key
pip install -e .

# Verify
python -c "from turnkey import SDK; print('OK')"
```

### 10.5 Testing

```bash
cd /home/flintx/turn-key
python -m pytest tests/  # SDK integration tests
python -m pytest turnkey/test_*.py  # SDK unit tests
python -m pytest consumer/python/test_*.py  # Consumer tests
python -m pytest profile_builder/tests/  # Profile builder tests
```

### 10.6 Quick Start Checklist

1. [ ] Install: `pip install -e /home/flintx/turn-key`
2. [ ] Create profile: `peacock_profile.json` with deck/stages/pipeline
3. [ ] Run: `from turnkey import SDK; sdk = SDK(config=SDKConfig()); sdk.run_stage("stage", messages)`
4. [ ] Or use consumer API: copy `consumer/python/*.py` and call `run_profile(path, prompt)`
5. [ ] For Dendritic: wrap with `MachineTalkClient(sdk)`
6. [ ] For hot-reload: use `ProfileWatcher` from consumer module

### 10.7 Slice Implementation Status

The `sdk_manifest.yaml` documents 40 slices (TB-TK-001 through TB-TK-040). Based on code review:

| Slice | Name | Status |
|---|---|---|
| TB-TK-001 | SDK Package Structure | DONE |
| TB-TK-002 | SDK Core Types | DONE |
| TB-TK-003 | Profile Loader | DONE |
| TB-TK-004 | Deck Rotator | DONE |
| TB-TK-005 | Stage Pinning | DONE (integrated into DeckRotator) |
| TB-TK-006 | Engine HTTP Client | DONE (in SDK._call_engine) |
| TB-TK-007 | SDK Main Class | DONE |
| TB-TK-008 | Response Normalizer | DONE (in SDK) |
| TB-TK-009 | Retry Logic | DONE (in SDK.run_stage) |
| TB-TK-010 | Error Handling | DONE (StageError class) |
| TB-TK-011 | Configuration Loading | DONE |
| TB-TK-012 | SDK Public Interface | DONE (__init__.py) |
| TB-TK-013 | SDK Documentation | DONE (docstrings) |
| TB-TK-014 | SDK Tests Framework | DONE (test files present) |
| TB-TK-015 | SDK Integration Testing | DONE (tests/ directory) |
| TB-TK-016 | MachineTalkClient | DONE |
| TB-TK-017 | Profile Loading Integration | DONE |
| TB-TK-018 | SDK Tool Handling | DONE |
| TB-TK-019 | Timeout Override | DONE |
| TB-TK-020 | Error Propagation | DONE |
| TB-TK-021 | Performance Metrics | DONE |
| TB-TK-022 | Logging Integration | DONE |
| TB-TK-023 | Concurrent Access | DONE (thread-safe rotator) |
| TB-TK-024 | Configuration Validation | DONE |
| TB-TK-025 | Migration Path | DONE |
| TB-TK-026 to TB-TK-040 | Testing/Deployment | PARTIAL (infrastructure code exists but not all tests run) |

### 10.8 Key Files for Any Developer

If you only read 5 files to understand this codebase:

1. **`turnkey/sdk.py`** — The main SDK class. Every call goes through `run_stage()`.
2. **`turnkey/types.py`** — All data types and exceptions.
3. **`turnkey/loader.py`** — How profiles are loaded and validated.
4. **`turnkey/rotator.py`** + `turnkey/gateway_rotator.py`** — How models are selected.
5. **`consumer/python/executor.py`** — The simplest possible way to use the system.

### 10.9 Profile Builder

The Solid.js profile builder (`profile_builder/solid/`) is a separate application. To use it:

```bash
cd /home/flintx/turn-key/profile_builder/solid
npm install
npm run dev      # Development server
npm run build    # Production build
```

It generates JSON profiles that the SDK can consume directly.

---

*End of TurnKey SDK Technical Manual.*