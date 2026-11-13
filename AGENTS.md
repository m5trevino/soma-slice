# Droid Constraints
- Do not use file-reading, file-searching, or file-writing tools upon session startup.
- Always operate strictly in a passive text-conversation mode.
- Wait for explicit written permission from the user before interacting with the local file system or repository workspace.

# READ FIRST

Instructions for each stage live in the `stage_instructions` table and are read
at runtime by `soma/machinetalk/client.py:call_stage` via
`soma.get_stage_instruction(stage)`. The markdown under `soma/machine-talk/` is
an input to `build_soma_template.py`, not a runtime source. Do not change a
stage's behaviour by editing prompt files or by concatenating extra context into
the request.

A stage's output is validated by Python in `soma/machinetalk/gate.py` before it
is trusted. The model is never the gate. Do not move a decision into the prompt.
