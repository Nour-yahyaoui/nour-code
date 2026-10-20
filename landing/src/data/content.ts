export type Feature = {
  id: string;
  title: string;
  body: string;
  meta: string;
};

export const FEATURES: Feature[] = [
  {
    id: "loop",
    title: "An agentic loop you can bound",
    body: "Message → model → tool call → result → answer, up to 15 iterations. Every turn ends with a finish_reason, so “it stopped” is never a mystery.",
    meta: "NOUR_CODE_MAX_ITERATIONS",
  },
  {
    id: "tools",
    title: "Seven tools, not a shell taped to a model",
    body: "read_file, write_file, list_directory, search_files, run_shell, apply_patch, delete_file. Each one is Pydantic-validated, permission-checked, output-capped and audited.",
    meta: "tools/",
  },
  {
    id: "permissions",
    title: "Permissions instead of vibes",
    body: "Allowed roots, shell allow/deny patterns and per-tool switches live in one YAML file. Paths outside the roots are refused before a single byte is read.",
    meta: "permissions.yaml",
  },
  {
    id: "approvals",
    title: "A human in the loop when it matters",
    body: "Ask for approval: \"mutating\" and every write, patch, delete or shell call parks until you approve or reject it over the API — with a risk label and a timeout.",
    meta: "POST /sessions/{id}/approve",
  },
  {
    id: "skills",
    title: "Skills that hot-reload",
    body: "Drop a .py file in the skills directory and POST /skills/reload: its tools appear in the model's tool list. A broken skill is logged and skipped, never fatal.",
    meta: "POST /skills/reload",
  },
  {
    id: "observability",
    title: "An audit trail by default",
    body: "Every tool call is written to JSONL with a SHA-256 fingerprint of its arguments, next to rotating JSON logs and Prometheus metrics.",
    meta: "GET /audit",
  },
];

export type Endpoint = {
  method: "GET" | "POST" | "DELETE";
  path: string;
  summary: string;
  group: "chat" | "model" | "skills" | "sessions" | "files" | "ops";
};

export const ENDPOINTS: Endpoint[] = [
  { method: "POST", path: "/chat", summary: "Run a turn and stream it back as SSE", group: "chat" },
  { method: "POST", path: "/chat/sync", summary: "Same turn, one JSON response", group: "chat" },
  { method: "GET", path: "/health", summary: "Ollama reachability, pulled vs resident model", group: "model" },
  { method: "POST", path: "/ollama/warmup", summary: "Preload the model; honours keep_alive", group: "model" },
  { method: "POST", path: "/ollama/unload", summary: "Free the model from memory now", group: "model" },
  { method: "GET", path: "/skills", summary: "List loaded skills and their tools", group: "skills" },
  { method: "POST", path: "/skills/reload", summary: "Hot-reload skill files", group: "skills" },
  { method: "GET", path: "/sessions", summary: "List active sessions", group: "sessions" },
  { method: "DELETE", path: "/sessions/{id}", summary: "Forget one session", group: "sessions" },
  { method: "GET", path: "/sessions/{id}/approvals", summary: "Tool calls waiting for a human", group: "sessions" },
  { method: "POST", path: "/sessions/{id}/approve", summary: "Approve, reject, approve_all, reject_all", group: "sessions" },
  { method: "GET", path: "/files", summary: "Read-only workspace listing", group: "files" },
  { method: "GET", path: "/files/content", summary: "Read one file inside the allowed roots", group: "files" },
  { method: "GET", path: "/audit", summary: "Tail the JSONL audit trail", group: "ops" },
  { method: "GET", path: "/metrics", summary: "Prometheus exposition", group: "ops" },
];

export type Step = {
  n: string;
  title: string;
  body: string;
  command: string;
};

export const STEPS: Step[] = [
  {
    n: "01",
    title: "Pull the model",
    body: "One download, about 5 GB. Ollama keeps it on your disk and serves it on localhost:11434.",
    command: "ollama pull qwen3:8b",
  },
  {
    n: "02",
    title: "Start the server",
    body: "Python 3.11+, pinned dependencies, one process. Check wiring with the built-in doctor before you serve.",
    command: "python -m nour_code --doctor && python -m nour_code --port 8000",
  },
  {
    n: "03",
    title: "Talk to it",
    body: "Use the browser console, the terminal chat, or curl the API. Same agent, same permissions, same audit trail.",
    command: 'curl -s localhost:8000/chat/sync -H "content-type: application/json" -d \'{"message":"what files are here?"}\'',
  },
];

export type FaqItem = { q: string; a: string };

export const FAQ: FaqItem[] = [
  {
    q: "Why is `ollama ps` empty while I can chat in the terminal?",
    a: "Those are two different questions. `ollama list` (and GET /api/tags) says the model is pulled; `ollama ps` (and GET /api/ps) says it is in memory right now. Ollama loads on demand and unloads after keep_alive — five minutes by default. GET /health now reports both, as model_loaded and model_running, and POST /ollama/warmup preloads the model.",
  },
  {
    q: "Does it need a GPU?",
    a: "No. qwen3:8b runs on CPU — `ollama ps` will simply show 100% CPU and a lower token rate. A GPU makes it faster; nothing in nour-code assumes one.",
  },
  {
    q: "Does any of my code leave the machine?",
    a: "No. The only network call the server makes is to your Ollama daemon at NOUR_CODE_OLLAMA_BASE_URL (localhost by default). There is no account, no API key and no telemetry.",
  },
  {
    q: "Can I use a different model?",
    a: "Yes: NOUR_CODE_MODEL=qwen3:14b (or any Ollama model with tool calling). Per-turn overrides are available via the model field on /chat.",
  },
  {
    q: "Can I keep the model in memory all day?",
    a: "POST /ollama/warmup?keep_alive=-1 pins it until you unload it, or start the daemon with OLLAMA_KEEP_ALIVE=-1. Warm-up goes through Ollama's native /api/generate because /v1 ignores keep_alive.",
  },
  {
    q: "Long prompts seem cut off around 4k tokens. Why?",
    a: "Ollama's OpenAI-compatible /v1 endpoint silently drops num_ctx, so the daemon falls back to its own default (4096 unless configured). Set OLLAMA_CONTEXT_LENGTH=16384 for the Ollama service; `python -m nour_code --doctor` flags the mismatch for you.",
  },
  {
    q: "Does it work on Windows?",
    a: "Yes. start.cmd creates the venv, installs the pinned requirements, runs the doctor and serves on :8000. Paths inside permissions.yaml are validated per platform; Windows-style roots are ignored elsewhere and vice versa.",
  },
];

export const GUARANTEES = [
  { title: "Stays on localhost", body: "The server binds 0.0.0.0 only if you ask it to; the model endpoint is local." },
  { title: "Path jail", body: "Every path is resolved and checked against allowed roots before a tool runs." },
  { title: "Shell allowlist", body: "Commands are matched against allow/deny patterns, with a timeout and a killed process group." },
  { title: "Hashed arguments", body: "The audit trail stores a SHA-256 fingerprint of arguments, not file contents." },
];
