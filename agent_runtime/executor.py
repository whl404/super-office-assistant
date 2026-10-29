"""Task-scoped async tool execution with a durable, payload-free SQLite ledger."""

import asyncio
import hashlib
import inspect
import json
import math
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from scripts.agents import AgentConfig, ConfigError


def fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TaskContext:
    run_id: str
    task_id: str
    attempt_id: str
    plan_version: int
    principal_id: str
    organization_id: str
    agent_id: str
    skill_id: str
    effect: str

    def __post_init__(self):
        if type(self.plan_version) is not int or self.plan_version < 1:
            raise ValueError("Invalid task plan version")
        if any(not isinstance(value, str) or not value for key, value in vars(self).items() if key != "plan_version"):
            raise ValueError("Task identity and role fields must be nonempty strings")


@dataclass(frozen=True)
class Grant:
    """Constructed by a trusted control plane, never deserialized from model output."""

    authorization_id: str
    run_id: str
    task_id: str
    plan_version: int
    principal_id: str
    organization_id: str
    tool_name: str
    arguments_digest: str
    expires_at: float
    resource_key: str = ""
    policy_version: str = "1"
    reconciliation_operation_id: str = ""


@dataclass(frozen=True)
class Reconciliation:
    outcome: str  # applied / not_applied / unknown; established by adapter-specific reads
    payload: Any = None


@dataclass
class Outcome:
    status: str  # succeeded / failed / blocked / cancelled
    code: str
    operation_id: str | None = None
    verification_level: str = "unknown"
    payload: Any = None
    cached: bool = False
    calls: int = 0
    approval_id: str | None = None


class ToolFault(Exception):
    def __init__(self, code: str, *, retryable=False, retry_after=None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


def classify_response(response: Any) -> Any:
    """Inspect known response envelopes, not arbitrary business rows or record fields."""
    if isinstance(response, dict) and response.get("isError") is True:
        raise ToolFault("MCP_ERROR")
    data = response
    if isinstance(response, dict) and "structuredContent" in response:
        data = response["structuredContent"]
    elif isinstance(response, dict) and "content" in response:
        texts = [x.get("text", "") for x in response["content"]
                 if isinstance(x, dict) and x.get("type") == "text"]
        if len(texts) != 1:
            raise ToolFault("RESPONSE_UNCLASSIFIED")
        try:
            data = json.loads(texts[0])
        except (ValueError, TypeError):
            raise ToolFault("RESPONSE_UNCLASSIFIED") from None
    if not isinstance(data, (dict, list)):
        raise ToolFault("RESPONSE_UNCLASSIFIED")
    queue = [(data, 0)]
    while queue:
        item, depth = queue.pop()
        if not isinstance(item, dict):
            continue
        error_code = item.get("errorCode")
        failed = (item.get("success") is False
                  or item.get("status") in ("error", "failed", "failure")
                  or error_code not in (None, "", 0, "0")
                  or bool(item.get("error")))
        http_code = item.get("code")
        if isinstance(http_code, int) and not isinstance(http_code, bool) and 400 <= http_code <= 599:
            failed = True
        if failed:
            code = str(error_code or http_code or "BUSINESS_ERROR")
            # Persist only normalized categories, never untrusted error text or tokens.
            if code in {"401", "403", "12063", "PERMISSION_DENIED", "UNAUTHORIZED"}:
                raise ToolFault("PERMISSION_DENIED")
            if code in {"429", "RATE_LIMIT_ERROR", "rate_limit"}:
                delay = item.get("retryAfter", item.get("retry_after"))
                if not isinstance(delay, (int, float)) or isinstance(delay, bool) or not math.isfinite(delay) or delay < 0:
                    delay = None
                raise ToolFault("RATE_LIMITED", retryable=True, retry_after=delay)
            if code in {"500", "502", "503", "504", "TEMPORARILY_UNAVAILABLE"}:
                raise ToolFault("REMOTE_UNAVAILABLE", retryable=True)
            raise ToolFault("BUSINESS_ERROR")
        if depth < 5:
            for key in ("result", "data"):
                if isinstance(item.get(key), dict):
                    queue.append((item[key], depth + 1))
    return data


@dataclass(frozen=True)
class Binding:
    invoke: Callable[[dict], Awaitable[Any]]
    validate: Callable[[dict], None]
    classify: Callable[[Any], Any] = classify_response
    reconcile: Callable[[str, str], Awaitable[Reconciliation]] | None = None
    resource_key: Callable[[dict], str] | None = None
    approval_flags: Callable[[dict], tuple[str, ...]] | None = None


class Ledger:
    """No raw arguments, responses, credentials or provider error text are persisted."""

    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, organization_id TEXT NOT NULL,
                plan_version INTEGER NOT NULL, deadline REAL NOT NULL, max_calls INTEGER NOT NULL,
                calls_used INTEGER NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                principal_id TEXT NOT NULL, organization_id TEXT NOT NULL, logical_key TEXT NOT NULL,
                tool_name TEXT NOT NULL, arguments_digest TEXT NOT NULL, plan_version INTEGER NOT NULL,
                agent_id TEXT NOT NULL, skill_id TEXT NOT NULL, effect TEXT NOT NULL,
                state TEXT NOT NULL, code TEXT NOT NULL, authorization_id TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                lease_token TEXT NOT NULL, lease_until REAL NOT NULL, result_digest TEXT,
                UNIQUE (principal_id, organization_id, logical_key)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL,
                event_type TEXT NOT NULL, code TEXT NOT NULL, created_at REAL NOT NULL,
                run_id TEXT, attempt_id TEXT, authorization_id TEXT
            );
            CREATE TABLE IF NOT EXISTS resource_locks (
                organization_id TEXT NOT NULL, resource_digest TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                PRIMARY KEY (organization_id,resource_digest)
            );
        """)

    def close(self):
        self.db.close()

    def create_run(self, run_id, principal_id, organization_id, *, plan_version=1,
                   max_calls=30, deadline=None):
        if not all(isinstance(v, str) and v for v in (run_id, principal_id, organization_id)):
            raise ValueError("Run identity must be nonempty")
        if type(plan_version) is not int or plan_version < 1 or type(max_calls) is not int or max_calls < 1:
            raise ValueError("Invalid run version or budget")
        deadline = time.time() + 86400 if deadline is None else deadline
        if not math.isfinite(deadline) or deadline <= time.time():
            raise ValueError("Run deadline must be in the future")
        self.db.execute("INSERT INTO runs(run_id,principal_id,organization_id,plan_version,deadline,max_calls) VALUES(?,?,?,?,?,?)",
                        (run_id, principal_id, organization_id, plan_version, deadline, max_calls))

    def guard(self, ctx):
        run = self.db.execute("SELECT * FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()
        if run is None or (run["principal_id"], run["organization_id"]) != (ctx.principal_id, ctx.organization_id):
            return "IDENTITY_MISMATCH"
        if run["cancelled"]:
            return "CANCELLED"
        if run["deadline"] <= time.time():
            return "DEADLINE_EXCEEDED"
        if run["plan_version"] != ctx.plan_version:
            return "STALE_PLAN"
        return None

    def reserve_call(self, ctx):
        code = self.guard(ctx)
        if code:
            raise ToolFault(code)
        changed = self.db.execute("UPDATE runs SET calls_used=calls_used+1 WHERE run_id=? AND calls_used<max_calls AND cancelled=0 AND plan_version=? AND deadline>?",
                                  (ctx.run_id, ctx.plan_version, time.time())).rowcount
        if not changed:
            raise ToolFault(self.guard(ctx) or "BUDGET_EXCEEDED")

    def cancel_run(self, run_id):
        self.db.execute("UPDATE runs SET cancelled=1 WHERE run_id=?", (run_id,))

    def advance_plan(self, run_id, expected_version):
        if not self.db.execute("UPDATE runs SET plan_version=plan_version+1 WHERE run_id=? AND plan_version=? AND cancelled=0",
                               (run_id, expected_version)).rowcount:
            raise ValueError("Plan changed or run is cancelled")

    def get(self, operation_id):
        row = self.db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        return dict(row) if row else None

    def claim(self, ctx, tool, digest, logical_key, authorization_id, policy_version, timeout):
        op_id, token = uuid.uuid4().hex, uuid.uuid4().hex
        try:
            self.db.execute("""INSERT INTO operations
                (operation_id,run_id,task_id,attempt_id,principal_id,organization_id,logical_key,
                 tool_name,arguments_digest,plan_version,agent_id,skill_id,effect,state,code,
                 authorization_id,policy_version,lease_token,lease_until,result_digest)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (op_id, ctx.run_id, ctx.task_id, ctx.attempt_id, ctx.principal_id, ctx.organization_id,
                 logical_key, tool, digest, ctx.plan_version, ctx.agent_id, ctx.skill_id,
                 ctx.effect, "pending", "IN_PROGRESS", authorization_id, policy_version, token,
                 time.time() + timeout + 5, None))
        except sqlite3.IntegrityError:
            row = self.db.execute("SELECT * FROM operations WHERE principal_id=? AND organization_id=? AND logical_key=?",
                                  (ctx.principal_id, ctx.organization_id, logical_key)).fetchone()
            if row is None:
                raise
            return dict(row), False
        return self.get(op_id), True

    def finish(self, operation_id, token, state, code, *, payload=None):
        digest = fingerprint(payload) if payload is not None else None
        return bool(self.db.execute("""UPDATE operations SET state=?,code=?,result_digest=?
            WHERE operation_id=? AND lease_token=? AND state IN ('pending','unknown')""",
                                    (state, code, digest, operation_id, token)).rowcount)

    def confirm(self, operation_id, token, code, payload, receipt_sink=None):
        """Commit confirmation and an optional trusted same-database receipt atomically."""
        savepoint = "receipt_" + uuid.uuid4().hex
        self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            changed = self.finish(operation_id, token, "confirmed", code, payload=payload)
            if changed and receipt_sink is not None:
                receipt_sink(operation_id, payload)
            self.db.execute(f"RELEASE {savepoint}")
            return changed
        except BaseException:
            self.db.execute(f"ROLLBACK TO {savepoint}")
            self.db.execute(f"RELEASE {savepoint}")
            raise

    def event(self, op_id, event_type, code, *, context=None, authorization_id=None):
        self.db.execute("""INSERT INTO events(operation_id,event_type,code,created_at,run_id,attempt_id,authorization_id)
            VALUES(?,?,?,?,?,?,?)""",
            (op_id, event_type, code, time.time(), context.run_id if context else None,
             context.attempt_id if context else None, authorization_id))

    def lease_reconciliation(self, operation_id, token, timeout):
        new_token = uuid.uuid4().hex
        changed = self.db.execute("""UPDATE operations SET lease_token=?,lease_until=?,state='unknown',code='RECONCILING'
            WHERE operation_id=? AND lease_token=?
            AND ((state='unknown' AND (code!='RECONCILING' OR lease_until<=?))
                 OR (state='pending' AND lease_until<=?))""",
            (new_token, time.time() + timeout + 5, operation_id, token, time.time(), time.time())).rowcount
        return new_token if changed else None

    def acquire_resource(self, organization_id, resource_key, operation_id):
        digest = fingerprint(resource_key)
        # Also recover a crash after confirmation but before resource-lock release.
        self.db.execute("""DELETE FROM resource_locks WHERE organization_id=? AND resource_digest=?
            AND operation_id IN (SELECT operation_id FROM operations WHERE state IN ('confirmed','rejected'))""",
            (organization_id, digest))
        try:
            self.db.execute("INSERT INTO resource_locks VALUES(?,?,?)", (organization_id, digest, operation_id))
            return True
        except sqlite3.IntegrityError:
            return False

    def release_resource(self, operation_id):
        self.db.execute("DELETE FROM resource_locks WHERE operation_id=?", (operation_id,))

    def reopen_resource_wait(self, row, timeout, ctx, grant):
        token = uuid.uuid4().hex
        changed = self.db.execute("""UPDATE operations SET state='pending',code='IN_PROGRESS',lease_token=?,lease_until=?,
            attempt_id=?,authorization_id=?,policy_version=?
            WHERE operation_id=? AND lease_token=? AND state='rejected' AND code='RESOURCE_BUSY'""",
            (token, time.time() + timeout + 5, ctx.attempt_id, grant.authorization_id, grant.policy_version,
             row["operation_id"], row["lease_token"])).rowcount
        return self.get(row["operation_id"]) if changed else None


class Executor:
    def __init__(self, config: AgentConfig, ledger: Ledger, *, timeout=30, read_retries=3,
                 backoff=(2, 5, 15), sleep=asyncio.sleep, jitter=None, approval_policy=None):
        if timeout <= 0 or not math.isfinite(timeout) or type(read_retries) is not int or read_retries < 0:
            raise ValueError("Invalid execution limits")
        if read_retries > len(backoff) or any(not math.isfinite(v) or v < 0 for v in backoff):
            raise ValueError("Invalid retry backoff")
        self.config, self.ledger = config, ledger
        self.timeout, self.read_retries, self.backoff, self.sleep = timeout, read_retries, backoff, sleep
        self.jitter = jitter or (lambda delay: random.uniform(0, min(1, delay * .1)))
        self.bindings = {}
        self.active = {}
        from agent_runtime.approvals import ApprovalEngine
        self.approvals = ApprovalEngine(ledger, approval_policy or config.approval_policy)

    def register(self, tool_name, binding: Binding):
        known = {t["name"]: t for s in self.config.services.values() for t in s["tools"]}
        if tool_name not in known or not inspect.iscoroutinefunction(binding.invoke):
            raise ValueError("Only known tools with async adapters can be registered")
        if not callable(binding.validate) or not callable(binding.classify):
            raise ValueError("Schema validator and classifier are required")
        if binding.reconcile and not inspect.iscoroutinefunction(binding.reconcile):
            raise ValueError("Reconciler must be async")
        if known[tool_name]["effect"] == "write" and not callable(binding.resource_key):
            raise ValueError("Write adapters must derive a canonical target resource key")
        self.bindings[tool_name] = binding

    def cancel_run(self, run_id):
        self.ledger.cancel_run(run_id)
        for active_run, task in list(self.active.values()):
            if active_run == run_id:
                task.cancel()

    def _authorize(self, ctx, tool_name, arguments, grant):
        if not isinstance(arguments, dict):
            raise ToolFault("INVALID_ARGUMENTS")
        try:
            candidate = self.config.assemble(ctx.agent_id, ctx.skill_id, ctx.effect)["candidate_tools"]
        except (ConfigError, KeyError):
            raise ToolFault("ROLE_OR_SKILL_DENIED") from None
        tool = next((t for t in candidate if t["name"] == tool_name), None)
        if tool is None:
            raise ToolFault("TOOL_DENIED")
        code = self.ledger.guard(ctx)
        if code:
            raise ToolFault(code)
        if not isinstance(grant, Grant) or not grant.authorization_id:
            raise ToolFault("AUTH_REQUIRED")
        expected = (ctx.run_id, ctx.task_id, ctx.plan_version, ctx.principal_id, ctx.organization_id, tool_name)
        actual = (grant.run_id, grant.task_id, grant.plan_version, grant.principal_id, grant.organization_id, grant.tool_name)
        if (expected != actual or not isinstance(grant.expires_at, (int, float))
                or not math.isfinite(grant.expires_at) or grant.expires_at <= time.time()
                or not isinstance(grant.policy_version, str) or not grant.policy_version):
            raise ToolFault("AUTH_SCOPE_MISMATCH")
        try:
            digest = fingerprint(arguments)
        except (ValueError, TypeError):
            raise ToolFault("INVALID_ARGUMENTS") from None
        if digest != grant.arguments_digest:
            raise ToolFault("AUTH_ARGUMENTS_CHANGED")
        binding = self.bindings.get(tool_name)
        if binding is None:
            raise ToolFault("CAPABILITY_UNAVAILABLE")
        try:
            binding.validate(arguments)
        except Exception:
            raise ToolFault("SCHEMA_VALIDATION_FAILED") from None
        if fingerprint(arguments) != digest:
            raise ToolFault("SCHEMA_MUTATED_ARGUMENTS")
        if tool["effect"] == "write":
            try:
                resource_key = binding.resource_key(arguments)
            except Exception:
                raise ToolFault("RESOURCE_SCOPE_MISMATCH") from None
            if not isinstance(resource_key, str) or not resource_key or resource_key != grant.resource_key:
                raise ToolFault("RESOURCE_SCOPE_MISMATCH")
        return tool, binding, digest

    async def _invoke(self, ctx, op_id, callback):
        self.ledger.reserve_call(ctx)
        task = asyncio.create_task(callback())
        self.active[op_id] = (ctx.run_id, task)
        try:
            run = self.ledger.db.execute("SELECT deadline FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()
            remaining = max(0, run["deadline"] - time.time())
            deadline_limited = remaining < self.timeout
            done, _ = await asyncio.wait({task}, timeout=min(self.timeout, remaining))
            if not done:
                task.cancel()
                # A cancelled request may already have reached a remote server.
                raise ToolFault("DEADLINE_EXCEEDED" if deadline_limited else "TIMEOUT", retryable=not deadline_limited)
            try:
                return task.result()
            except (TimeoutError, ConnectionError):
                raise ToolFault("TRANSPORT_ERROR", retryable=True) from None
        finally:
            if not task.done():
                task.cancel()
            # Retrieve late adapter exceptions without trusting them as business outcomes.
            def cleanup(completed):
                if not completed.cancelled():
                    completed.exception()
                if self.active.get(op_id, (None, None))[1] is completed:
                    self.active.pop(op_id, None)
            task.add_done_callback(cleanup)

    def _failure(self, code, op_id=None, calls=0):
        status = "cancelled" if code in {"CANCELLED", "DEADLINE_EXCEEDED"} else "blocked"
        return Outcome(status, code, op_id, calls=calls)

    @staticmethod
    def _approval_flags(binding, arguments):
        try:
            flags = binding.approval_flags(arguments) if binding.approval_flags else ()
            if not isinstance(flags, (tuple, list)) or any(not isinstance(flag, str) or not flag for flag in flags):
                raise ValueError("Invalid flags")
            return tuple(sorted(set(flags)))
        except Exception:
            raise ToolFault("RISK_ASSESSMENT_UNAVAILABLE") from None

    def prepare_approval(self, ctx, tool_name, arguments, grant, *, logical_key):
        if not isinstance(logical_key, str) or not logical_key:
            raise ToolFault("INVALID_OPERATION_KEY")
        arguments = json.loads(json.dumps(arguments, allow_nan=False))
        tool, binding, _ = self._authorize(ctx, tool_name, arguments, grant)
        flags = self._approval_flags(binding, arguments)
        return self.approvals.require(ctx, tool, arguments, grant, logical_key, flags)

    def approval_preview(self, approval_id, ctx, tool_name, arguments, grant, *, logical_key):
        arguments = json.loads(json.dumps(arguments, allow_nan=False))
        tool, binding, _ = self._authorize(ctx, tool_name, arguments, grant)
        return self.approvals.preview(approval_id, ctx, tool, arguments, grant, logical_key,
                                      self._approval_flags(binding, arguments))

    async def execute(self, ctx: TaskContext, tool_name: str, arguments: dict,
                      grant: Grant, *, logical_key: str, receipt_sink=None) -> Outcome:
        """logical_key identifies a business action and remains stable across retries."""
        calls = 0
        try:
            if not isinstance(logical_key, str) or not logical_key:
                raise ToolFault("INVALID_OPERATION_KEY")
            # Freeze the request before any await; caller mutations cannot change approved args.
            arguments = json.loads(json.dumps(arguments, allow_nan=False))
            tool, binding, digest = self._authorize(ctx, tool_name, arguments, grant)
            flags = self._approval_flags(binding, arguments)
        except (TypeError, ValueError) as exc:
            return self._failure(exc.code if isinstance(exc, ToolFault) else "INVALID_ARGUMENTS")
        except ToolFault as exc:
            return self._failure(exc.code)
        approval = self.approvals.require(ctx, tool, arguments, grant, logical_key, flags)
        if not approval.allowed:
            return Outcome("blocked", approval.code, approval_id=approval.approval_id)
        row, created = self.ledger.claim(ctx, tool_name, digest, logical_key, grant.authorization_id,
                                        grant.policy_version, self.timeout)
        op_id, token = row["operation_id"], row["lease_token"]
        if not created:
            expected = (ctx.run_id, ctx.task_id, tool_name, digest, ctx.plan_version, ctx.agent_id, ctx.skill_id, ctx.effect)
            actual = tuple(row[k] for k in ("run_id", "task_id", "tool_name", "arguments_digest", "plan_version", "agent_id", "skill_id", "effect"))
            if expected != actual:
                return self._failure("IDEMPOTENCY_CONFLICT", op_id)
            if row["state"] == "confirmed":
                return Outcome("succeeded", "ALREADY_CONFIRMED", op_id,
                               "readback_verified" if row["code"] == "RECONCILED" else
                               "write_acknowledged" if tool["effect"] == "write" else "read_probe_passed", cached=True)
            if row["state"] == "pending" and row["lease_until"] > time.time():
                return self._failure("OPERATION_IN_PROGRESS", op_id)
            if row["state"] == "pending":
                self.ledger.finish(op_id, token, "unknown", "LEASE_EXPIRED")
            if row["state"] == "rejected" and row["code"] == "RESOURCE_BUSY":
                row = self.ledger.reopen_resource_wait(row, self.timeout, ctx, grant)
                if row is None:
                    return self._failure("OPERATION_IN_PROGRESS", op_id)
                token = row["lease_token"]
            else:
                return self._failure("REMOTE_STATE_UNKNOWN" if row["state"] != "rejected" else row["code"], op_id)
        self.approvals.attach(op_id, approval.approval_id)
        if tool["effect"] == "write" and not self.ledger.acquire_resource(ctx.organization_id, grant.resource_key, op_id):
            self.ledger.finish(op_id, token, "rejected", "RESOURCE_BUSY")
            return self._failure("RESOURCE_BUSY", op_id)
        try:
            result = await self._execute_claimed(ctx, tool_name, arguments, grant, tool, binding, op_id, token,
                                                approval.approval_id, logical_key, flags, receipt_sink)
            result.approval_id = approval.approval_id
            return result
        finally:
            if self.ledger.get(op_id)["state"] in {"confirmed", "rejected"}:
                self.ledger.release_resource(op_id)

    async def _execute_claimed(self, ctx, tool_name, arguments, grant, tool, binding, op_id, token,
                               approval_id, logical_key, flags, receipt_sink):
        calls = 0
        for attempt in range(self.read_retries + 1):
            try:
                # Recheck identity, plan, grant expiry and arguments before every retry.
                self._authorize(ctx, tool_name, arguments, grant)
                approval = self.approvals.check(approval_id, ctx, tool, arguments, grant, logical_key,
                                                self._approval_flags(binding, arguments))
                if not approval.allowed:
                    raise ToolFault(approval.code)
                async def dispatch():
                    nonlocal calls
                    calls += 1
                    return await binding.invoke(arguments)
                response = await self._invoke(ctx, op_id, dispatch)
                payload = binding.classify(response)
                if not self.ledger.confirm(op_id, token, "OK", payload, receipt_sink):
                    return self._failure("STALE_LEASE", op_id, calls)
                self.ledger.event(op_id, "confirmed", "OK", context=ctx, authorization_id=grant.authorization_id)
                guard = self.ledger.guard(ctx)
                if guard:
                    return self._failure(guard, op_id, calls)
                approval = self.approvals.check(approval_id, ctx, tool, arguments, grant, logical_key, flags)
                if not approval.allowed:
                    return self._failure(approval.code, op_id, calls)
                return Outcome("succeeded", "OK", op_id,
                               "write_acknowledged" if tool["effect"] == "write" else "read_probe_passed",
                               payload=payload, calls=calls)
            except asyncio.CancelledError:
                self.ledger.finish(op_id, token, "unknown" if tool["effect"] == "write" else "rejected", "CANCELLED")
                if self.ledger.guard(ctx) == "CANCELLED":
                    return self._failure("CANCELLED", op_id, calls)
                raise
            except ToolFault as exc:
                guard = self.ledger.guard(ctx) or (exc.code if exc.code == "DEADLINE_EXCEEDED" else None)
                if guard:
                    self.ledger.finish(op_id, token, "unknown" if tool["effect"] == "write" and calls else "rejected", guard)
                    return self._failure(guard, op_id, calls)
                if tool["effect"] == "write" and calls:
                    # Even an explicit remote failure may follow a partial write.
                    self.ledger.finish(op_id, token, "unknown", "REMOTE_STATE_UNKNOWN")
                    self.ledger.event(op_id, "unknown", exc.code)
                    return self._failure("REMOTE_STATE_UNKNOWN", op_id, calls)
                if tool["effect"] == "read" and exc.retryable and attempt < self.read_retries:
                    delay = max(self.backoff[attempt] + self.jitter(self.backoff[attempt]), exc.retry_after or 0)
                    run = self.ledger.db.execute("SELECT deadline FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()
                    if time.time() + delay >= min(run["deadline"], grant.expires_at):
                        self.ledger.finish(op_id, token, "rejected", "RETRY_WINDOW_EXCEEDED")
                        return self._failure("RETRY_WINDOW_EXCEEDED", op_id, calls)
                    self.ledger.event(op_id, "retry", exc.code)
                    # Read retries retain the operation lease to exclude a second worker.
                    self.ledger.db.execute("UPDATE operations SET lease_until=? WHERE operation_id=? AND lease_token=?",
                                           (time.time() + delay + self.timeout + 5, op_id, token))
                    try:
                        await self.sleep(delay)
                    except asyncio.CancelledError:
                        self.ledger.finish(op_id, token, "rejected", "CANCELLED")
                        raise
                    continue
                self.ledger.finish(op_id, token, "rejected", exc.code)
                self.ledger.event(op_id, "rejected", exc.code)
                return self._failure(exc.code, op_id, calls)
            except Exception:
                state = "unknown" if tool["effect"] == "write" and calls else "rejected"
                code = "REMOTE_STATE_UNKNOWN" if state == "unknown" else "ADAPTER_ERROR"
                self.ledger.finish(op_id, token, state, code)
                self.ledger.event(op_id, state, code)
                return self._failure(code, op_id, calls)
        raise AssertionError("Retry loop exhausted without outcome")

    async def reconcile(self, ctx, operation_id, arguments, grant, *, maintenance=False, receipt_sink=None):
        row = self.ledger.get(operation_id)
        if row is None:
            return self._failure("OPERATION_NOT_FOUND")
        try:
            tool, binding, digest = self._authorize(ctx, row["tool_name"], arguments, grant)
        except ToolFault as exc:
            return self._failure(exc.code, operation_id)
        expected = (ctx.run_id, ctx.task_id, ctx.plan_version, ctx.agent_id, ctx.skill_id, ctx.effect, digest)
        actual = tuple(row[k] for k in ("run_id", "task_id", "plan_version", "agent_id", "skill_id", "effect", "arguments_digest"))
        same_identity = (row["principal_id"], row["organization_id"]) == (ctx.principal_id, ctx.organization_id)
        maintenance_allowed = (
            maintenance and same_identity and ctx.run_id != row["run_id"]
            and grant.reconciliation_operation_id == operation_id
            and digest == row["arguments_digest"] and ctx.agent_id == row["agent_id"]
        )
        if not same_identity or (expected != actual and not maintenance_allowed) or tool["effect"] != "write":
            return self._failure("AUTH_SCOPE_MISMATCH", operation_id)
        if row["state"] == "confirmed":
            return Outcome("succeeded", "ALREADY_CONFIRMED", operation_id,
                           "readback_verified" if row["code"] == "RECONCILED" else "write_acknowledged", cached=True)
        if row["state"] == "pending" and row["lease_until"] > time.time():
            return self._failure("OPERATION_IN_PROGRESS", operation_id)
        if row["state"] not in {"unknown", "pending"} or binding.reconcile is None:
            return self._failure("RECONCILIATION_UNAVAILABLE", operation_id)
        if operation_id in self.active and not self.active[operation_id][1].done():
            return self._failure("OPERATION_IN_PROGRESS", operation_id)
        token = self.ledger.lease_reconciliation(operation_id, row["lease_token"], self.timeout)
        if token is None:
            return self._failure("OPERATION_IN_PROGRESS", operation_id)
        calls = 0
        try:
            async def dispatch():
                nonlocal calls
                calls += 1
                return await binding.reconcile(operation_id, digest)
            result = await self._invoke(ctx, operation_id, dispatch)
            if not isinstance(result, Reconciliation) or result.outcome not in {"applied", "not_applied", "unknown"}:
                raise ToolFault("INVALID_RECONCILIATION")
            if result.outcome == "applied":
                if not self.ledger.confirm(operation_id, token, "RECONCILED", result.payload, receipt_sink):
                    return self._failure("STALE_LEASE", operation_id, 1)
                self.ledger.event(operation_id, "reconciled", "APPLIED", context=ctx, authorization_id=grant.authorization_id)
                guard = self.ledger.guard(ctx)
                if guard:
                    return self._failure(guard, operation_id, 1)
                return Outcome("succeeded", "RECONCILED", operation_id, "readback_verified", result.payload, calls=1)
            if result.outcome == "not_applied":
                self.ledger.finish(operation_id, token, "rejected", "VERIFIED_NOT_APPLIED")
                return self._failure("VERIFIED_NOT_APPLIED", operation_id, 1)
            return self._failure("REMOTE_STATE_UNKNOWN", operation_id, 1)
        except asyncio.CancelledError:
            if self.ledger.guard(ctx) == "CANCELLED":
                return self._failure("CANCELLED", operation_id, 1)
            raise
        except ToolFault as exc:
            return self._failure(self.ledger.guard(ctx) or (exc.code if not calls or exc.code == "DEADLINE_EXCEEDED" else "REMOTE_STATE_UNKNOWN"),
                                 operation_id, calls)
        except Exception:
            return self._failure("REMOTE_STATE_UNKNOWN", operation_id, calls)
        finally:
            self.ledger.db.execute("""UPDATE operations SET code='REMOTE_STATE_UNKNOWN'
                WHERE operation_id=? AND lease_token=? AND state='unknown' AND code='RECONCILING'""",
                (operation_id, token))
            if self.ledger.get(operation_id)["state"] in {"confirmed", "rejected"}:
                self.ledger.release_resource(operation_id)
