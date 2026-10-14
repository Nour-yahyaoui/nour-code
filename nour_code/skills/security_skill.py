"""Security skill — static review of a folder, secret detection, dependency CVEs.

Exposed tools
-------------
``security_scan(path)``
    Walks every source file and flags dangerous patterns (shell injection,
    ``eval``, disabled TLS verification, SQL built by concatenation, XSS sinks,
    weak hashes, debug flags left on, ...).
``check_secrets(path)``
    Looks for committed credentials (AWS keys, GitHub/PyPI/npm tokens, private
    key blocks, JWTs, connection strings with passwords, high-entropy literals).
    Evidence is redacted so findings can be logged safely.
``check_dependency_cves(package_json_path)``
    Runs ``npm audit --json`` in the project directory and summarises the
    advisories by severity.  When npm/network is unavailable the tool says so
    explicitly instead of pretending the project is clean.

None of these tools modify files: the skill is read-only.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import anyio

SKILL_NAME = "security_skill"
SKILL_DESCRIPTION = (
    "Read-only security review: scans source files for dangerous patterns, detects committed "
    "secrets, and audits Node dependencies for known CVEs (npm audit)."
)
SKILL_TRIGGERS = [
    "when the user asks for a security review or security audit",
    "when the user asks to check for secrets or leaked credentials",
    "before deploying to production",
    "when the user asks about vulnerabilities or CVEs in dependencies",
]

SKILL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "security_scan",
            "description": (
                "Static security review of a directory. Flags shell injection, eval/exec, unsafe "
                "deserialisation, disabled TLS verification, SQL built by concatenation, XSS sinks, "
                "weak hashes and debug flags. Read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory or file to scan."},
                    "max_files": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 400},
                    "severity_threshold": {
                        "type": "string",
                        "enum": ["info", "low", "medium", "high", "critical"],
                        "default": "low",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_secrets",
            "description": (
                "Scan a directory for committed credentials and high-entropy secrets. Evidence is "
                "redacted. Read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory or file to scan."},
                    "max_files": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 400},
                    "entropy_threshold": {"type": "number", "minimum": 2.0, "maximum": 6.0, "default": 4.2},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_dependency_cves",
            "description": (
                "Audit Node dependencies for known CVEs by running 'npm audit --json' in the project "
                "directory. Returns advisories grouped by severity, or an explicit status when npm "
                "or the network is unavailable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package_json_path": {
                        "type": "string",
                        "description": "Path to package.json, or the directory that contains it.",
                    },
                    "timeout_seconds": {"type": "integer", "minimum": 5, "maximum": 600, "default": 180},
                    "include_dev": {"type": "boolean", "default": True},
                },
                "required": ["package_json_path"],
                "additionalProperties": False,
            },
        },
    },
]

SKILL_VERSION = "1.1.0"

# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

SOURCE_SUFFIXES = frozenset(
    {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".rs", ".go", ".java",
        ".rb", ".php", ".cs", ".sh", ".bash", ".yml", ".yaml", ".toml", ".json",
        ".env", ".tf", ".sql", ".dockerfile", ".ps1",
    }
)

# (rule id, severity, regex, advice)
RISK_RULES: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    (
        "shell-injection",
        "high",
        re.compile(r"(?i)\b(os\.system|subprocess\.(?:call|run|Popen)\s*\(\s*[^)]*shell\s*=\s*True|child_process\.exec(?:Sync)?\s*\()"),
        "Command built from untrusted input can execute arbitrary code. Use argv lists "
        "(subprocess without shell=True, child_process.execFile) and validate inputs.",
    ),
    (
        "code-execution",
        "critical",
        re.compile(r"(?i)\b(eval\s*\(|exec\s*\(|new\s+Function\s*\(|setTimeout\s*\(\s*['\"`]|pickle\.loads|yaml\.load\s*\((?![^)]*SafeLoader)|marshal\.loads)"),
        "Dynamic code execution is a remote-code-execution risk and breaks static analysis. "
        "Replace with an explicit parser or a safe loader (yaml.safe_load).",
    ),
    (
        "insecure-deserialization",
        "high",
        re.compile(r"(?i)\b(pickle\.load|torch\.load|jsonpickle\.decode|cPickle\.loads)"),
        "Deserialising untrusted data executes arbitrary code. Use a data-only format (JSON) "
        "and validate the schema.",
    ),
    (
        "disabled-tls-verification",
        "high",
        re.compile(r"(?i)(verify\s*=\s*False|rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*['\"]?0|ssl\._create_unverified_context|CURLOPT_SSL_VERIFYPEER,\s*false|InsecureSkipVerify\s*:\s*true)"),
        "TLS verification is disabled, so traffic can be intercepted. Keep verification on and "
        "install the CA properly instead.",
    ),
    (
        "sql-concatenation",
        "high",
        re.compile(r"(?i)(execute\s*\(\s*['\"`][^'\"`]*(?:SELECT|INSERT|UPDATE|DELETE)[^'\"`]*['\"`]\s*(?:\+|%|f['\"])|f?['\"`]?\s*(?:SELECT|INSERT|UPDATE|DELETE)\s[^'\"`]*['\"]?\s*\+\s*\w|query\s*\(\s*f?['\"`].*\{)"),
        "SQL assembled by string interpolation is injectable. Use parameterised queries / "
        "prepared statements (sqlx::query!, $1 placeholders, %s with parameters).",
    ),
    (
        "xss-sink",
        "medium",
        re.compile(r"(?i)(innerHTML\s*=|dangerouslySetInnerHTML|document\.write\s*\(|bypassSecurityTrustHtml|\[innerHTML\])"),
        "Raw HTML injection enables XSS. Render text nodes or sanitise the HTML first.",
    ),
    (
        "weak-crypto",
        "medium",
        re.compile(r"(?i)\b(hashlib\.(md5|sha1)|createHash\s*\(\s*['\"](md5|sha1)['\"]|Math\.random\s*\(\s*\)|random\.random\s*\(\s*\))"),
        "MD5/SHA1 and non-CSPRNG randomness are not acceptable for security decisions. "
        "Use SHA-256+/bcrypt/argon2 and secrets.SystemRandom().",
    ),
    (
        "hardcoded-credentials",
        "high",
        re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*(?::|=|:=)\s*['\"][^'\"$\{]{6,}['\"]"),
        "Credential literal in source. Move it to the environment (validated config module) and "
        "rotate the leaked value.",
    ),
    (
        "permissive-cors",
        "medium",
        re.compile(r"(?i)(Access-Control-Allow-Origin['\"]?\s*[:,]\s*['\"]?\*|Cors::permissive|cors\s*\(\s*\{\s*origin\s*:\s*['\"]?\*)"),
        "Wildcard CORS with credentials exposes APIs to any origin. Allow-list the origins.",
    ),
    (
        "debug-mode",
        "medium",
        re.compile(r"(?i)(DEBUG\s*=\s*True|app\.debug\s*=\s*True|NODE_ENV\s*===?\s*['\"]development['\"]|RUST_LOG\s*=\s*['\"]?(trace|debug))"),
        "Debug settings must never ship: they leak stack traces and slow the process down.",
    ),
    (
        "insecure-transport",
        "low",
        re.compile(r"(?i)(http://(?!localhost|127\.0\.0\.1|schemas\.|www\.w3\.org|example\.com)[a-z0-9.-]+|ws://)"),
        "Plaintext transport. Use https:// / wss:// and enforce it with HSTS.",
    ),
    (
        "sensitive-logging",
        "medium",
        re.compile(r"(?i)(console\.log|print|logger\.(info|debug))\s*\([^)]*(password|secret|token|authorization|api[_-]?key)"),
        "Secrets in logs leak to log aggregation and backups. Redact before logging.",
    ),
    (
        "path-traversal",
        "high",
        re.compile(r"(?i)(open\s*\(\s*(?:request|req|params|query|body)[^)]*\)|readFile\s*\(\s*(?:req|request)\.|sendFile\s*\(\s*(?:req|request)\.|PathBuf::from\s*\(\s*&?\w*query)"),
        "User input reaching the filesystem allows ../ traversal. Resolve and validate the path "
        "against an allowed root.",
    ),
    (
        "missing-csrf",
        "low",
        re.compile(r"(?i)(csrf\s*[:=]\s*false|@csrf_exempt|csrf_protection\s*=\s*False)"),
        "CSRF protection disabled on a state-changing endpoint.",
    ),
)

# (rule id, severity, regex, kind)
SECRET_RULES: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    ("aws-access-key-id", "critical", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"), "AWS access key id"),
    ("aws-secret-access-key", "critical", re.compile(r"(?i)aws(.{0,20})?(secret|private)(.{0,20})?['\"][0-9a-zA-Z/+]{40}['\"]"), "AWS secret access key"),
    ("github-token", "critical", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{36}\b|\bgithub_pat_[0-9A-Za-z_]{22,}\b"), "GitHub token"),
    ("gitlab-token", "high", re.compile(r"\bglpat-[0-9A-Za-z_-]{20}\b"), "GitLab personal access token"),
    ("slack-token", "high", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b"), "Slack token"),
    ("openai-key", "high", re.compile(r"\bsk-(?:proj-)?[0-9A-Za-z_-]{20,}\b"), "OpenAI-style API key"),
    ("npm-token", "high", re.compile(r"\bnpm_[0-9A-Za-z]{36}\b"), "npm access token"),
    ("pypi-token", "high", re.compile(r"\bpypi-[0-9A-Za-z_-]{16,}\b"), "PyPI upload token"),
    ("google-api-key", "high", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    ("stripe-key", "critical", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{20,}\b"), "Stripe secret key"),
    ("private-key-block", "critical", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"), "Private key material"),
    ("jwt", "medium", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
    ("basic-auth-url", "high", re.compile(r"(?i)\b(?:https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:@/]+:[^\s@/]{3,}@"), "Connection string with inline password"),
    ("generic-password-assignment", "medium", re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key)\s*(?::|=|:=)\s*['\"][^'\"\s${}]{8,}['\"]"), "Hard-coded password / key literal"),
    ("sendgrid-key", "high", re.compile(r"\bSG\.[0-9A-Za-z_-]{16,}\.[0-9A-Za-z_-]{16,}\b"), "SendGrid API key"),
    ("twilio-sid", "medium", re.compile(r"\bAC[0-9a-fA-F]{32}\b"), "Twilio account SID"),
)

HIGH_ENTROPY_RE = re.compile(r"['\"]([A-Za-z0-9+/=_-]{24,})['\"]")
IGNORE_DIRS = frozenset(
    {
        ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
        "target", ".next", ".mypy_cache", ".pytest_cache", ".ruff_cache", "coverage",
        ".idea", ".vscode", "vendor", "third_party",
    }
)
MAX_FILE_BYTES = 1_000_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _redact(value: str, keep: int = 4) -> str:
    """Show just enough of a secret to recognise it, never the whole thing."""
    value = value.strip()
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * min(12, len(value) - keep)}{value[-keep:]}"


async def _iter_files(root: Path, max_files: int) -> AsyncIterator[Path]:
    """Yield candidate text files under ``root`` (bounded, ignores vendored dirs)."""
    anyio_root = anyio.Path(root)
    if await anyio_root.is_file():
        yield root
        return
    count = 0
    async for path in anyio_root.rglob("*"):
        if count >= max_files:
            return
        try:
            if not await path.is_file():
                continue
        except OSError:
            continue
        if any(part in IGNORE_DIRS for part in path.parts):
            continue
        if path.suffix and path.suffix not in SOURCE_SUFFIXES and path.name not in {"Dockerfile", ".env", "Makefile"}:
            continue
        try:
            if (await path.stat()).st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        count += 1
        yield Path(path)


async def _read_text(path: Path) -> str | None:
    try:
        raw = await anyio.Path(path).read_bytes()
    except (OSError, UnicodeDecodeError):
        return None
    if b"\x00" in raw[:4096]:
        return None
    return raw.decode("utf-8", errors="replace")


def _resolve(ctx: dict[str, Any], raw: str, *, kind: str = "any") -> Path:
    """Resolve a path that may be a file or a directory."""
    permissions = ctx.get("permissions")
    if permissions is None:  # pragma: no cover - wiring guarantee
        raise RuntimeError("permissions unavailable in skill context")
    return permissions.resolve_path(raw, must_exist=True, kind=kind)


def _finding(
    *, rule: str, severity: str, path: Path, line: int, snippet: str, advice: str, **extra: Any
) -> dict[str, Any]:
    return {
        "rule": rule,
        "severity": severity,
        "file": str(path),
        "line": line,
        "snippet": snippet.strip()[:220],
        "advice": advice,
        **extra,
    }


def _summarise(findings: list[dict[str, Any]]) -> dict[str, int]:
    summary = {level: 0 for level in SEVERITY_ORDER}
    for finding in findings:
        level = str(finding.get("severity", "info")).lower()
        summary[level] = summary.get(level, 0) + 1
    summary["total"] = len(findings)
    return summary


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "<!--", "--", ";")


def _is_comment(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(_COMMENT_PREFIXES)


async def _security_scan(
    ctx: dict[str, Any],
    path: str,
    max_files: int = 400,
    severity_threshold: str = "low",
) -> dict[str, Any]:
    """Flag dangerous code patterns under ``path``."""
    try:
        root = _resolve(ctx, path)
    except PermissionError as exc:
        return {"ok": False, "denied": True, "error": f"path rejected: {exc}"}

    threshold = SEVERITY_ORDER.get(str(severity_threshold).lower(), 1)
    findings: list[dict[str, Any]] = []
    files_scanned = 0
    skipped: list[str] = []

    async for file_path in _iter_files(root, max_files):
        text = await _read_text(file_path)
        if text is None:
            skipped.append(str(file_path))
            continue
        files_scanned += 1
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            comment = _is_comment(line)
            for rule, severity, pattern, advice in RISK_RULES:
                match = pattern.search(line)
                if not match:
                    continue
                if comment and SEVERITY_ORDER[severity] < SEVERITY_ORDER["high"]:
                    continue  # noisy comment matches are only relevant for high/critical
                findings.append(
                    _finding(
                        rule=rule,
                        severity=severity,
                        path=file_path,
                        line=lineno,
                        snippet=line,
                        advice=advice,
                        evidence=_redact(match.group(0)),
                        in_comment=comment,
                    )
                )

    kept = [f for f in findings if SEVERITY_ORDER.get(f["severity"], 0) >= threshold]
    kept.sort(key=lambda f: (-SEVERITY_ORDER.get(f["severity"], 0), f["file"], f["line"]))
    return {
        "ok": True,
        "tool": "security_scan",
        "path": str(root),
        "files_scanned": files_scanned,
        "files_skipped": skipped[:20],
        "rules_applied": len(RISK_RULES),
        "findings": kept[:300],
        "summary": _summarise(kept),
        "truncated": len(kept) > 300,
    }


async def _check_secrets(
    ctx: dict[str, Any],
    path: str,
    max_files: int = 400,
    entropy_threshold: float = 4.2,
) -> dict[str, Any]:
    """Detect committed credentials (evidence redacted)."""
    try:
        root = _resolve(ctx, path)
    except PermissionError as exc:
        return {"ok": False, "denied": True, "error": f"path rejected: {exc}"}

    findings: list[dict[str, Any]] = []
    files_scanned = 0
    noisy_names = ("lock", ".min.", "yarn", "pnpm-lock", "poetry.lock", "Cargo.lock")

    async for file_path in _iter_files(root, max_files):
        text = await _read_text(file_path)
        if text is None:
            continue
        files_scanned += 1
        low_name = file_path.name.lower()
        allow_entropy = not any(token in low_name for token in noisy_names)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            hits: list[tuple[str, str, str, re.Match[str]]] = []
            for rule, severity, pattern, kind in SECRET_RULES:
                for match in pattern.finditer(line):
                    hits.append((rule, severity, kind, match))

            # Never echo a secret back: redact every hit inside the snippet too.
            redacted_line = line
            for _rule, _severity, _kind, match in hits:
                redacted_line = redacted_line.replace(match.group(0), _redact(match.group(0)))

            for rule, severity, kind, match in hits:
                findings.append(
                    _finding(
                        rule=rule,
                        severity=severity,
                        path=file_path,
                        line=lineno,
                        snippet=redacted_line,
                        advice=f"Rotate this {kind} immediately, remove it from the repository, "
                        "and load it from the environment or a secret manager.",
                        evidence=_redact(match.group(0)),
                        secret_kind=kind,
                    )
                )
            if allow_entropy:
                for match in HIGH_ENTROPY_RE.finditer(line):
                    candidate = match.group(1)
                    if _shannon_entropy(candidate) >= entropy_threshold and not candidate.startswith(("http", "sha", "eyJ")):
                        redacted_line = redacted_line.replace(candidate, _redact(candidate))
                        findings.append(
                            _finding(
                                rule="high-entropy-literal",
                                severity="low",
                                path=file_path,
                                line=lineno,
                                snippet=redacted_line,
                                advice="High-entropy string; if it is a credential move it to the "
                                "environment and rotate it.",
                                evidence=_redact(candidate),
                                entropy=round(_shannon_entropy(candidate), 2),
                            )
                        )

    findings.sort(key=lambda f: (-SEVERITY_ORDER.get(f["severity"], 0), f["file"], f["line"]))
    return {
        "ok": True,
        "tool": "check_secrets",
        "path": str(root),
        "files_scanned": files_scanned,
        "findings": findings[:300],
        "summary": _summarise(findings),
        "note": "Evidence is redacted; open the file at the reported line to confirm.",
        "truncated": len(findings) > 300,
    }


async def _check_dependency_cves(
    ctx: dict[str, Any],
    package_json_path: str,
    timeout_seconds: int = 180,
    include_dev: bool = True,
) -> dict[str, Any]:
    """Run ``npm audit --json`` and summarise advisories by severity."""
    permissions = ctx.get("permissions")
    if permissions is None:  # pragma: no cover - wiring guarantee
        return {"ok": False, "error": "permissions unavailable"}
    try:
        target = permissions.resolve_path(package_json_path, must_exist=True)
        if await anyio.Path(target).is_dir():
            target = target / "package.json"
            if not await anyio.Path(target).is_file():
                return {
                    "ok": True,
                    "status": "no_package_json",
                    "message": f"no package.json found in {target.parent}",
                }
    except PermissionError as exc:
        return {"ok": False, "denied": True, "error": f"path rejected: {exc}"}

    project_dir = target.parent
    try:
        manifest = json.loads(await anyio.Path(target).read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"cannot read package.json: {exc}"}

    dependencies = dict(manifest.get("dependencies") or {})
    dev_dependencies = dict(manifest.get("devDependencies") or {})
    if include_dev:
        dependencies.update(dev_dependencies)
    lockfile = next(
        (name for name in ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml")
         if (project_dir / name).exists()),
        None,
    )

    report: dict[str, Any] = {
        "ok": True,
        "tool": "check_dependency_cves",
        "project": str(project_dir),
        "package": manifest.get("name"),
        "version": manifest.get("version"),
        "declared_dependencies": len(dependencies),
        "lockfile": lockfile,
        "status": "unknown",
    }

    tools = ctx.get("tools")
    if tools is None:  # pragma: no cover - wiring guarantee
        return {"ok": False, "error": "tool facade unavailable"}

    outcome = await tools.call(
        "run_shell",
        command="npm audit --json",
        cwd=str(project_dir),
        timeout_seconds=max(10, min(int(timeout_seconds), 600)),
    )
    payload = outcome.get("result") if isinstance(outcome.get("result"), dict) else {}
    stdout = str(payload.get("stdout") or "")
    stderr = str(payload.get("stderr") or "")
    exit_code = payload.get("exit_code")

    if not stdout.strip():
        reason = stderr.strip()[:400] or outcome.get("error") or "npm produced no output"
        report.update(
            status="npm_unavailable",
            npm_ran=False,
            reason=reason,
            recommendations=[
                "Install Node/npm and run `npm audit` manually, or enable network access "
                "(network.allow_outbound) for the audit.",
                "Keep dependencies pinned and enable Dependabot/Renovate as a fallback.",
            ],
        )
        return report

    try:
        audit = json.loads(stdout)
    except json.JSONDecodeError:
        report.update(
            status="unparsable_output",
            npm_ran=True,
            exit_code=exit_code,
            raw_head=stdout[:800],
            recommendations=["Run `npm audit` by hand to inspect the output."],
        )
        return report

    vulnerabilities = audit.get("vulnerabilities") or {}
    advisories: list[dict[str, Any]] = []
    severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for name, entry in vulnerabilities.items():
        if not isinstance(entry, dict):
            continue
        severity = str(entry.get("severity") or "info").lower()
        if severity == "moderate":
            severity = "medium"
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        via = entry.get("via") or []
        titles = [
            (v.get("title") if isinstance(v, dict) else str(v))
            for v in via[:3]
            if (isinstance(v, dict) and v.get("title")) or isinstance(v, str)
        ]
        advisories.append(
            {
                "package": name,
                "severity": severity,
                "range": entry.get("range"),
                "direct": bool(entry.get("isDirect")),
                "fix_available": entry.get("fixAvailable") if entry.get("fixAvailable") is not None else False,
                "via": titles,
                "url": next(
                    (v.get("url") for v in via if isinstance(v, dict) and v.get("url")),
                    None,
                ),
            }
        )

    advisories.sort(key=lambda a: (-SEVERITY_ORDER.get(a["severity"], 0), a["package"]))
    fixable = sorted({a["package"] for a in advisories if a["fix_available"]})
    report.update(
        status="ok",
        npm_ran=True,
        exit_code=exit_code,
        total_vulnerabilities=len(advisories),
        severity_counts=severity_counts,
        advisories=advisories[:60],
        truncated=len(advisories) > 60,
        fixable_packages=fixable[:40],
        recommendations=[
            "Run `npm audit fix` for non-breaking remediation, then re-run this check."
            if fixable else "No automatic fix is available: review the advisories and upgrade manually.",
            "Commit the updated lockfile so CI installs the patched versions.",
            "Enable `npm audit --audit-level=high` (or Dependabot) in CI to catch regressions.",
        ],
    )
    return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def handle(tool_name: str, args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Route a tool call to the right implementation."""
    logger = ctx.get("logger")
    try:
        if tool_name == "security_scan":
            return await _security_scan(
                ctx,
                path=str(args["path"]),
                max_files=int(args.get("max_files", 400)),
                severity_threshold=str(args.get("severity_threshold", "low")),
            )
        if tool_name == "check_secrets":
            return await _check_secrets(
                ctx,
                path=str(args["path"]),
                max_files=int(args.get("max_files", 400)),
                entropy_threshold=float(args.get("entropy_threshold", 4.2)),
            )
        if tool_name == "check_dependency_cves":
            return await _check_dependency_cves(
                ctx,
                package_json_path=str(args["package_json_path"]),
                timeout_seconds=int(args.get("timeout_seconds", 180)),
                include_dev=bool(args.get("include_dev", True)),
            )
    except KeyError as exc:
        return {"ok": False, "error": f"missing required argument: {exc.args[0]}"}
    except PermissionError as exc:
        return {"ok": False, "denied": True, "error": f"permission denied: {exc}"}
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"invalid arguments: {exc}"}
    except OSError as exc:
        if logger:
            logger.warning("io error in %s: %s", tool_name, exc)
        return {"ok": False, "error": f"io error: {exc}"}

    return {"ok": False, "error": f"security_skill has no tool named {tool_name!r}"}
