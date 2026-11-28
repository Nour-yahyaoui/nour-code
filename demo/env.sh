# Shell environment for the two-role demo. Source it, do not execute it:
#   . demo/env.sh
#
# nour-code shells out to whatever `pytest` is on PATH, so the venv has to come
# first — otherwise the agent reports "pytest: not found" and, correctly, refuses
# to guess about code it could not test.

export NOUR_CODE_PORT=8001
export NOUR_CODE_OLLAMA_BASE_URL=http://127.0.0.1:11934
export NOUR_CODE_MODEL=qwen3:8b
export NOUR_CODE_WORKING_DIR=/home/user/nour-code/demo/workspaces/shop
export NOUR_CODE_PERMISSIONS_FILE=/home/user/nour-code/demo/permissions.yaml
export NOUR_CODE_SESSIONS_FILE=/home/user/nour-code/demo/sessions.json
export PATH=/home/user/nour-code/.venv/bin:$PATH
