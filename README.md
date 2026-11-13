# Soma 10-14

Clean package of the Soma engine and TurnKey SDK.

## Layout

```
soma-10-14/
  run.sh              launcher
  soma/               the engine, dendrites, prompts, profiles
  turn-key/           the TurnKey SDK
```

## Setup

```bash
unzip soma-10-14.zip
cd soma-10-14
pip install -e turn-key
```

## Run a session

A session directory must contain `session.json` and one or more numbered chatset files (`000001.json`, etc.).

```bash
./run.sh --source /path/to/session --profile soma/turn-key-profiles/soma-current.json
```

Options:

- `--out /tmp/soma_out` — where artifacts and the checkpoint go
- `--debug /tmp/soma_debug` — where the dashboard events go
- `--workers 2` — parallel chatset workers
- `--port 9876` — dashboard port

The dashboard prints a URL. Open it in a browser on the same machine.

## Force a re-run

The engine resumes from the checkpoint. To force stage 1 again, delete the output directory or pass the force flag through the engine directly.
