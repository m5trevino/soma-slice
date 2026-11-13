# Rules for Claude on the Soma project

Read AGENTS.md too. It still applies. This file adds what was learned the hard way.

## How I work with the user
- Session start is passive text conversation. No file reads, searches or writes until
  the user says so in writing. "approved", "yes please fix it" and "you got full access"
  are explicit permissions for the task they were given on. A new, different task
  (deleting, pushing, sudo, touching /usr/local) needs a fresh yes.
- "Don't change anything, give me a report" means read-only. Fetching with GET is fine.
  Writing project files is not. Scratch output goes in the session scratchpad.
- Answer what was asked, with evidence (`file:line`, request ids, counts). Say plainly
  what I did NOT check. Never claim "fixed" without a run that proves it.
- Short answers. A recommendation, not a menu. No pronoun guesses about people.
- Never commit, push or sudo unless asked. Commit messages end with the attribution
  line the harness gives.

## Secrets
- `ENGINE_KEY` lives only in `/home/flintx/soma-10-14/.env` (mode 600, gitignored).
  Never print it, echo it, paste it into a report, or put it in a tracked file or profile.
  Mask it when showing any env file.
- Using the key to call the user's own engine is fine. Sending it anywhere else is not.

## Architecture (do not fight it)
- All model calls: `run.sh` -> `run_sessions.py` -> `soma/run_engine.py` ->
  `soma/machinetalk/client.py` (`EngineClient`) -> TurnKey SDK (`/home/flintx/turn-key`)
  -> HTTP POST `{ENGINE_URL}/v1/chat/completions`. There is no anthropic/openai SDK.
- Engine URL order: profile `pipeline.engineUrl` > `ENGINE_URL` env > the SDK default
  `http://engine.save-aichats.com:3099`. `.env` sets the Tailscale one, `100.87.51.23:3099`.
- The model is never the gate. Python (`soma/machinetalk/gate.py`, `provenance.py`)
  validates every stage output. Do not move a decision into a prompt, do not loosen a gate
  to make a run go green, do not edit prompt files or concatenate context into requests.
  Stage instructions live in the `stage_instructions` table.
- Clients and dendrites do not pick models. TurnKey rotates over the profile's deck.
  To test one model, pin it in the profile (`pinnedModels`), never in code.

## Environment
- Run Python from `/home/flintx/soma-10-14/.venv` (editable install of turn-key).
  `run.sh` already prefers it. Use `.venv/bin/python`, not bare `python3`.
- `/usr/local/bin/soma` is root-owned. Never edit it. Board work goes in
  `/home/flintx/soma-copy/soma`. The user installs it with their own `sudo cp`.
- The shell is zsh: quote globs (`--include='*.py'`), and never `echo ====`
  unquoted ("= not found").

## Reading the logs correctly
- Engine log site: `https://engine.save-aichats.com/v1/soma/` (agent guide at
  `/v1/soma/agent-guide`). Needs `Authorization: Bearer $ENGINE_KEY`.
- Engine `SUCCESS` means the HTTP call worked, not that the output passed the gate.
  The local per-stage log in `soma/output/logs/<stage>/` and the pipeline's own table
  are the truth for gate results.
- IDs differ: the CLI's `SES-003` is a local counter. The engine's `SES-0xx` is global.
  Match them by timestamp, model and token counts.
- Failure kinds seen so far, so I check these first:
  `missing:chatset_hash` (stub reply, seen with `openrouter/auto-code`),
  `fabricated_quote` (directive text is not verbatim in the transcript),
  `json_parse` (broken JSON), engine `Request timed out`.

## Finding a model that works (the current job)
1. Read-only first: find from the output logs which models have ever passed the distiller gate.
2. Then test ONE model at a time, pinned via the profile, on the same chatset. Record
   model, gate result, tokens, seconds.
3. Judge by the gate verdict, never by engine SUCCESS and never by "it returned something".
4. Do not change the gate, prompts or provenance rules to make a model pass.
   If the gate itself looks wrong, say so with evidence and let the user decide.

## The Architect's imperatives (stated 2026-11-12, non-negotiable)
The user has re-explained this many times. The failure to prevent is drift back to the
"common way": prompt-dumping, raw HTTP, helpful-assistant chatter. Treat any of these
as a regression, not a style choice.
1. Soma is the interface. The model gets no system prompt carrying app logic or chat
   history. It fetches its own instructions (ReceptorConfig) and input (ChatSet) from the
   Soma database. Never write a prompt that contains instructions or historical context.
2. ATM protocol is physical. Smoke-test work happens only in the isolated directory
   `/home/flintx/temp-smoke/`, with no exploring of the rest of the repo.
3. No direct API calls. Only `sdk.run_stage()`. Never build requests or urllib payloads
   for `/v1/chat/completions` (that bypasses auth, the model rotator and provenance).
4. Invariant-first. Do not invent rules. If no rule exists for a FunctionSlot, flag it for
   human review instead of guessing.
5. The slice-eater tracer-bullet format is a binding contract.
6. Before touching any pipeline file, read the repo docs: `soma/PIPELINE_ACTION_MAP.md`,
   `soma/SESSION_HANDOFF.md`, `soma/TURNKEY_TECHNICAL_MANUAL.md`, AGENTS.md. Infra-only
   changes (.env, .venv, launcher) are the exception, and I say so when I make one.
7. No chatter. Produce the code, run the test, report green or red with output.

Honest limits: this session has no Soma DB tools (`query_nodes`, `get_chatset`,
`create_soma_node`). I can't fetch rules from the database, so when a rule is needed I
say so and ask instead of improvising.
