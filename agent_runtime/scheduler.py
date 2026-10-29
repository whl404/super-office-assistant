"""Durable agent/tool loop with scoped artifacts, approval waits and fenced recovery."""

import asyncio
import inspect
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from agent_runtime.executor import Grant, TaskContext, ToolFault, fingerprint


def snapshot(value):
    """Workflow payloads are private business artifacts, never a credential store."""
    forbidden = {"access_token", "accesstoken", "refresh_token", "refreshtoken", "client_secret",
                 "clientsecret", "authorization", "password", "api_key", "apikey"}
    def check(item):
        if isinstance(item, dict):
            if any(str(key).lower() in forbidden for key in item):
                raise ValueError("Credential fields cannot be stored in workflow artifacts")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if len(raw.encode("utf-8")) > 1024 * 1024:
        raise ValueError("Workflow payload exceeds 1 MiB")
    check(value)
    return raw


@dataclass(frozen=True)
class ToolRequest:
    call_id: str
    tool_name: str
    arguments: dict


@dataclass(frozen=True)
class Complete:
    output: object


@dataclass(frozen=True)
class NeedInput:
    keys: tuple[str, ...]
    question: str


@dataclass(frozen=True)
class Validation:
    accepted: bool
    code: str = "ACCEPTED"
    rework_step: str | None = None


@dataclass(frozen=True)
class Job:
    context: TaskContext
    step_id: str
    inputs: dict
    dependencies: dict
    history: tuple[dict, ...]
    instructions: str
    candidate_tools: tuple[dict, ...]


class Scheduler:
    """Handlers and grant issuers are trusted adapters, not JSON-supplied code."""

    def __init__(self, config, executor, *, grant_issuer=None, ai_reviewer=None,
                 handler_timeout=120, lease_seconds=150):
        if handler_timeout <= 0 or lease_seconds <= handler_timeout:
            raise ValueError("Step lease must exceed handler timeout")
        if grant_issuer is not None and not inspect.iscoroutinefunction(grant_issuer):
            raise ValueError("Grant issuer must be asynchronous")
        self.config, self.executor, self.ledger = config, executor, executor.ledger
        self.db = self.ledger.db
        self.grant_issuer, self.ai_reviewer = grant_issuer, ai_reviewer
        self.handler_timeout, self.lease_seconds = handler_timeout, lease_seconds
        self.handlers, self.validators, self.active = {}, {}, {}
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS wf_runs (
                run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, template_json TEXT NOT NULL,
                state TEXT NOT NULL, code TEXT NOT NULL, inputs_json TEXT NOT NULL,
                input_version INTEGER NOT NULL DEFAULT 1, max_agent_calls INTEGER NOT NULL,
                agent_calls INTEGER NOT NULL DEFAULT 0, max_rework INTEGER NOT NULL,
                rework_used INTEGER NOT NULL DEFAULT 0, final_artifact TEXT,
                input_keys_json TEXT NOT NULL DEFAULT '[]', question TEXT,
                validation_token TEXT, validation_until REAL
            );
            CREATE TABLE IF NOT EXISTS wf_steps (
                run_id TEXT NOT NULL, plan_version INTEGER NOT NULL, step_id TEXT NOT NULL,
                definition_json TEXT NOT NULL, state TEXT NOT NULL, code TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 1, lease_token TEXT, lease_until REAL,
                approval_id TEXT, artifact_id TEXT, PRIMARY KEY(run_id,plan_version,step_id)
            );
            CREATE TABLE IF NOT EXISTS wf_actions (
                run_id TEXT NOT NULL, plan_version INTEGER NOT NULL, step_id TEXT NOT NULL,
                generation INTEGER NOT NULL, call_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL, logical_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, operation_id TEXT, artifact_id TEXT,
                PRIMARY KEY(run_id,plan_version,step_id,generation,call_id)
            );
            CREATE TABLE IF NOT EXISTS wf_artifacts (
                artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, plan_version INTEGER NOT NULL,
                step_id TEXT NOT NULL, generation INTEGER NOT NULL, payload_json TEXT NOT NULL,
                digest TEXT NOT NULL, kind TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wf_receipts (
                operation_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS wf_inbox (
                organization_id TEXT NOT NULL, source TEXT NOT NULL, event_id TEXT NOT NULL,
                principal_id TEXT NOT NULL, payload_digest TEXT NOT NULL, run_id TEXT NOT NULL,
                PRIMARY KEY(organization_id,source,event_id)
            );
            CREATE TABLE IF NOT EXISTS wf_outbox (
                event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, plan_version INTEGER NOT NULL,
                artifact_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                UNIQUE(run_id,plan_version)
            );
        """)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def register_agent(self, agent_id, handler):
        if agent_id not in self.config.agents or not self.config.agents[agent_id]["enabled"]:
            raise ValueError("Agent unavailable")
        if not inspect.iscoroutinefunction(handler):
            raise ValueError("Agent adapter must be asynchronous")
        self.handlers[agent_id] = handler

    def register_validator(self, workflow_id, validator):
        if workflow_id not in self.config.workflows or not inspect.iscoroutinefunction(validator):
            raise ValueError("Known workflow and async validator required")
        self.validators[workflow_id] = validator

    def submit(self, workflow_id, inputs, *, principal_id, organization_id, source, event_id,
               max_agent_calls=30, max_tool_calls=30, max_rework=2, deadline=None):
        if workflow_id not in self.config.workflows:
            raise ValueError("Workflow unavailable")
        if not isinstance(inputs, dict) or not all(isinstance(x, str) and x for x in (source, event_id)):
            raise ValueError("Invalid workflow inputs or event identity")
        if type(max_agent_calls) is not int or max_agent_calls < 1 or type(max_rework) is not int or max_rework < 0:
            raise ValueError("Invalid workflow budget")
        template = self.config.workflows[workflow_id]
        raw, digest = snapshot(inputs), fingerprint({"workflow": workflow_id, "inputs": inputs})
        missing = [key for key in template["required_inputs"] if key not in inputs or inputs[key] is None]
        run_id = uuid.uuid4().hex
        with self.transaction():
            existing = self.db.execute("SELECT * FROM wf_inbox WHERE organization_id=? AND source=? AND event_id=?",
                                       (organization_id, source, event_id)).fetchone()
            if existing:
                if existing["principal_id"] != principal_id or existing["payload_digest"] != digest:
                    raise ValueError("EVENT_CONFLICT")
                return existing["run_id"]
            self.ledger.create_run(run_id, principal_id, organization_id, max_calls=max_tool_calls, deadline=deadline)
            self.db.execute("""INSERT INTO wf_runs(run_id,workflow_id,template_json,state,code,inputs_json,max_agent_calls,max_rework,input_keys_json)
                VALUES(?,?,?,?,?,?,?,?,?)""", (run_id, workflow_id, snapshot(template), "waiting_input" if missing else "executing",
                "MISSING_INPUT" if missing else "READY", raw, max_agent_calls, max_rework, snapshot(missing)))
            self._insert_steps(run_id, 1, template)
            self._put(run_id, 1, "$inputs", 1, inputs, "input_snapshot")
            self.db.execute("INSERT INTO wf_inbox VALUES(?,?,?,?,?,?)", (organization_id, source, event_id, principal_id, digest, run_id))
        return run_id

    def _insert_steps(self, run_id, plan, template):
        for definition in template["steps"]:
            self.config.select(definition["agent"], definition["skill"], definition["effect"])
            self.db.execute("INSERT INTO wf_steps(run_id,plan_version,step_id,definition_json,state,code) VALUES(?,?,?,?,?,?)",
                            (run_id, plan, definition["id"], snapshot(definition), "pending", "READY"))

    def _owner(self, run_id, principal_id, organization_id):
        row = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row or (row["principal_id"], row["organization_id"]) != (principal_id, organization_id):
            raise PermissionError("Workflow identity mismatch")
        return dict(row)

    def status(self, run_id, *, principal_id, organization_id):
        owner = self._owner(run_id, principal_id, organization_id)
        row = dict(self.db.execute("SELECT * FROM wf_runs WHERE run_id=?", (run_id,)).fetchone())
        steps = self.db.execute("SELECT step_id,state,code,approval_id,artifact_id,generation FROM wf_steps WHERE run_id=? AND plan_version=? ORDER BY rowid",
                                (run_id, owner["plan_version"])).fetchall()
        return {"run_id": run_id, "plan_version": owner["plan_version"], "state": row["state"], "code": row["code"],
                "input_version": row["input_version"], "input_keys": json.loads(row["input_keys_json"]),
                "question": row["question"], "agent_calls": row["agent_calls"], "final_artifact": row["final_artifact"],
                "steps": [dict(step) for step in steps]}

    def artifact(self, artifact_id, *, principal_id, organization_id):
        row = self.db.execute("SELECT * FROM wf_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if not row:
            raise ValueError("Artifact not found")
        self._owner(row["run_id"], principal_id, organization_id)
        return json.loads(row["payload_json"])

    def _put(self, run_id, plan, step_id, generation, value, kind):
        raw = snapshot(value)
        artifact_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO wf_artifacts VALUES(?,?,?,?,?,?,?,?)",
                        (artifact_id, run_id, plan, step_id, generation, raw, fingerprint(value), kind))
        return artifact_id

    def _receipt_sink(self, ctx, step):
        def sink(operation_id, payload):
            artifact_id = self._put(ctx.run_id, ctx.plan_version, step["step_id"], step["generation"], payload, "tool_receipt")
            self.db.execute("INSERT INTO wf_receipts VALUES(?,?)", (operation_id, artifact_id))
        return sink

    def provide_inputs(self, run_id, values, *, principal_id, organization_id, expected_input_version):
        with self.transaction():
            owner = self._owner(run_id, principal_id, organization_id)
            run = dict(self.db.execute("SELECT * FROM wf_runs WHERE run_id=?", (run_id,)).fetchone())
            if owner["cancelled"] or owner["deadline"] <= time.time() or run["state"] != "waiting_input" or run["input_version"] != expected_input_version:
                raise ValueError("STALE_INPUT_REPLY")
            allowed = set(json.loads(run["input_keys_json"]))
            if not values or not set(values) <= allowed:
                raise ValueError("Input reply may only fill requested missing fields")
            inputs = json.loads(run["inputs_json"])
            if any(key in inputs and inputs[key] != value for key, value in values.items() if inputs.get(key) is not None):
                raise ValueError("Existing input changes require plan revision")
            inputs.update(values)
            missing = [key for key in allowed if key not in inputs or inputs[key] is None]
            self.db.execute("UPDATE wf_runs SET inputs_json=?,input_version=input_version+1,input_keys_json=?,state=?,code=? WHERE run_id=?",
                (snapshot(inputs), snapshot(missing), "waiting_input" if missing else "executing", "MISSING_INPUT" if missing else "READY", run_id))
            if not missing:
                self.db.execute("UPDATE wf_steps SET state='pending',code='READY' WHERE run_id=? AND plan_version=? AND state='waiting_input'",
                                (run_id, owner["plan_version"]))

    def cancel(self, run_id, *, principal_id, organization_id):
        with self.transaction():
            self._owner(run_id, principal_id, organization_id)
            state = self.db.execute("SELECT state FROM wf_runs WHERE run_id=?", (run_id,)).fetchone()[0]
            if state == "delivered":
                raise ValueError("Delivered workflow cannot be cancelled")
            self.executor.cancel_run(run_id)
            self.db.execute("UPDATE wf_runs SET state='cancelled',code='CANCELLED' WHERE run_id=?", (run_id,))
            self.db.execute("UPDATE wf_steps SET state='cancelled',code='CANCELLED',lease_token=NULL WHERE run_id=? AND state!='succeeded'", (run_id,))
            self.db.execute("UPDATE wf_outbox SET state='cancelled' WHERE run_id=? AND state='pending'", (run_id,))
        for active_run, task in list(self.active.values()):
            if active_run == run_id:
                task.cancel()

    def _steps(self, run_id, plan):
        return [dict(row) for row in self.db.execute("SELECT * FROM wf_steps WHERE run_id=? AND plan_version=? ORDER BY rowid", (run_id, plan))]

    def _alive(self, ctx, step, token):
        if self.ledger.guard(ctx):
            return False
        row = self.db.execute("SELECT * FROM wf_steps WHERE run_id=? AND plan_version=? AND step_id=?",
                              (ctx.run_id, ctx.plan_version, step["step_id"])).fetchone()
        return bool(row and row["state"] == "running" and row["generation"] == step["generation"]
                    and row["lease_token"] == token and row["lease_until"] > time.time())

    def _set_step(self, ctx, step, token, state, code, **fields):
        if not self._alive(ctx, step, token):
            return False
        if set(fields) - {"approval_id", "artifact_id"}:
            raise ValueError("Invalid step mutation")
        columns = {"state": state, "code": code, "lease_token": None, **fields}
        self.db.execute("UPDATE wf_steps SET " + ",".join(key + "=?" for key in columns)
                        + " WHERE run_id=? AND plan_version=? AND step_id=? AND lease_token=?",
                        (*columns.values(), ctx.run_id, ctx.plan_version, step["step_id"], token))
        return True

    async def _bounded(self, key, run_id, callback, timeout):
        task = asyncio.create_task(callback())
        self.active[key] = (run_id, task)
        try:
            deadline = self.db.execute("SELECT deadline FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
            done, _ = await asyncio.wait({task}, timeout=min(timeout, max(0, deadline - time.time())))
            if not done:
                raise TimeoutError()
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            self.active.pop(key, None)

    async def _heartbeat(self, run_id, plan, step_id, token):
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            changed = self.db.execute("""UPDATE wf_steps SET lease_until=?
                WHERE run_id=? AND plan_version=? AND step_id=? AND lease_token=? AND state='running'
                AND EXISTS(SELECT 1 FROM runs WHERE run_id=? AND cancelled=0 AND plan_version=? AND deadline>?)""",
                (time.time() + self.lease_seconds, run_id, plan, step_id, token, run_id, plan, time.time())).rowcount
            if not changed:
                return

    def _reserve_agent(self, run_id):
        if not self.db.execute("UPDATE wf_runs SET agent_calls=agent_calls+1 WHERE run_id=? AND agent_calls<max_agent_calls", (run_id,)).rowcount:
            raise ValueError("AGENT_BUDGET_EXCEEDED")

    def _refresh(self, run_id, owner):
        for step in self._steps(run_id, owner["plan_version"]):
            if step["state"] == "waiting_authorization":
                approval = self.executor.approvals._row(step["approval_id"])
                if approval["status"] == "approved":
                    self.db.execute("UPDATE wf_steps SET state='pending',code='READY' WHERE run_id=? AND plan_version=? AND step_id=?",
                                    (run_id, owner["plan_version"], step["step_id"]))
                elif approval["status"] in {"expired", "rejected", "revoked"}:
                    self.db.execute("UPDATE wf_steps SET state='blocked',code=? WHERE run_id=? AND plan_version=? AND step_id=?",
                                    (approval["code"], run_id, owner["plan_version"], step["step_id"]))
            elif step["state"] == "running" and step["lease_until"] <= time.time():
                self.db.execute("UPDATE wf_steps SET state='pending',code='LEASE_RECOVERED',lease_token=NULL WHERE run_id=? AND plan_version=? AND step_id=?",
                                (run_id, owner["plan_version"], step["step_id"]))

    async def drive(self, run_id, *, principal_id, organization_id, max_ticks=100):
        """Run ready work until delivered, waiting or blocked; never sleep while awaiting users."""
        if type(max_ticks) is not int or max_ticks < 1:
            raise ValueError("Invalid tick limit")
        for _ in range(max_ticks):
            owner = self._owner(run_id, principal_id, organization_id)
            run = dict(self.db.execute("SELECT * FROM wf_runs WHERE run_id=?", (run_id,)).fetchone())
            if owner["cancelled"] or owner["deadline"] <= time.time():
                self.cancel(run_id, principal_id=principal_id, organization_id=organization_id)
                return self.status(run_id, principal_id=principal_id, organization_id=organization_id)
            if run["state"] in {"delivered", "syncing", "cancelled", "waiting_input", "blocked"}:
                return self.status(run_id, principal_id=principal_id, organization_id=organization_id)
            with self.transaction():
                self._refresh(run_id, owner)
                steps = self._steps(run_id, owner["plan_version"])
                succeeded = {s["step_id"] for s in steps if s["state"] == "succeeded"}
                ready = next((s for s in steps if s["state"] == "pending"
                              and set(json.loads(s["definition_json"])["depends_on"]) <= succeeded), None)
                if ready:
                    token = uuid.uuid4().hex
                    self.db.execute("UPDATE wf_steps SET state='running',lease_token=?,lease_until=? WHERE run_id=? AND plan_version=? AND step_id=?",
                                    (token, time.time() + self.lease_seconds, run_id, owner["plan_version"], ready["step_id"]))
                    self.db.execute("UPDATE wf_runs SET state='executing',code='RUNNING' WHERE run_id=?", (run_id,))
            if ready:
                heartbeat = asyncio.create_task(self._heartbeat(run_id, owner["plan_version"], ready["step_id"], token))
                try:
                    await self._run_step(run, owner, ready, token)
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                continue
            if len(succeeded) == len(steps):
                if await self._finalize(run, owner) is False:
                    return self.status(run_id, principal_id=principal_id, organization_id=organization_id)
                continue
            blocked = next((s for s in steps if s["state"] == "blocked"), None)
            state = "blocked" if blocked else "waiting_authorization" if any(s["state"] == "waiting_authorization" for s in steps) else "executing"
            self.db.execute("UPDATE wf_runs SET state=?,code=? WHERE run_id=?", (state, blocked["code"] if blocked else "WAITING", run_id))
            return self.status(run_id, principal_id=principal_id, organization_id=organization_id)
        return self.status(run_id, principal_id=principal_id, organization_id=organization_id)

    async def _run_step(self, run, owner, step, token):
        definition = json.loads(step["definition_json"])
        ctx = TaskContext(owner["run_id"], f"{step['step_id']}:g{step['generation']}", token,
                          owner["plan_version"], owner["principal_id"], owner["organization_id"],
                          definition["agent"], definition["skill"], definition["effect"])
        try:
            actions = [dict(row) for row in self.db.execute("SELECT * FROM wf_actions WHERE run_id=? AND plan_version=? AND step_id=? AND generation=? ORDER BY rowid",
                       (ctx.run_id, ctx.plan_version, step["step_id"], step["generation"]))]
            unfinished = next((a for a in actions if a["state"] != "succeeded"), None)
            if unfinished:
                await self._run_action(ctx, step, token, unfinished)
                return
            handler = self.handlers.get(ctx.agent_id)
            if handler is None:
                self._set_step(ctx, step, token, "blocked", "AGENT_ADAPTER_UNAVAILABLE")
                return
            self._reserve_agent(ctx.run_id)
            assembled = self.config.assemble(ctx.agent_id, ctx.skill_id, ctx.effect)
            artifacts = {s["step_id"]: s["artifact_id"] for s in self._steps(ctx.run_id, ctx.plan_version)}
            deps = {key: {"artifact_id": artifacts[key], "output": self.artifact(artifacts[key], principal_id=ctx.principal_id, organization_id=ctx.organization_id)}
                    for key in definition["depends_on"]}
            history = tuple({"call_id": a["call_id"], "artifact_id": a["artifact_id"],
                             "output": self.artifact(a["artifact_id"], principal_id=ctx.principal_id, organization_id=ctx.organization_id)} for a in actions)
            job = Job(ctx, step["step_id"], json.loads(run["inputs_json"]), deps, history,
                      assembled["instructions"], tuple(assembled["candidate_tools"]))
            reply = await self._bounded(token, ctx.run_id, lambda: handler(job), min(self.handler_timeout, max(0, owner["deadline"] - time.time())))
            with self.transaction():
                if not self._alive(ctx, step, token):
                    return
                if isinstance(reply, ToolRequest):
                    if not isinstance(reply.call_id, str) or not reply.call_id or not isinstance(reply.arguments, dict):
                        raise ValueError("INVALID_AGENT_REPLY")
                    if reply.tool_name not in {tool["name"] for tool in assembled["candidate_tools"]}:
                        raise ValueError("TOOL_DENIED")
                    key = f"{ctx.run_id}:{ctx.plan_version}:{step['step_id']}:{step['generation']}:{reply.call_id}"
                    if any(a["call_id"] == reply.call_id for a in actions):
                        raise ValueError("DUPLICATE_AGENT_CALL_ID")
                    self.db.execute("INSERT INTO wf_actions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (ctx.run_id, ctx.plan_version, step["step_id"], step["generation"], reply.call_id,
                         reply.tool_name, snapshot(reply.arguments), key, "pending", None, None))
                    self._set_step(ctx, step, token, "pending", "TOOL_PLANNED")
                elif isinstance(reply, Complete):
                    if reply.output in (None, {}, [], ""):
                        raise ValueError("EMPTY_AGENT_OUTPUT")
                    artifact_id = self._put(ctx.run_id, ctx.plan_version, step["step_id"], step["generation"], reply.output, "step_output")
                    self._set_step(ctx, step, token, "succeeded", "OK", artifact_id=artifact_id)
                elif isinstance(reply, NeedInput):
                    inputs = json.loads(run["inputs_json"])
                    if (not isinstance(reply.keys, tuple) or not reply.keys
                            or any(not isinstance(key, str) or not key for key in reply.keys)
                            or not isinstance(reply.question, str) or not reply.question
                            or any(key in inputs and inputs[key] is not None for key in reply.keys)):
                        raise ValueError("INVALID_INPUT_REQUEST")
                    self._set_step(ctx, step, token, "waiting_input", "MISSING_INPUT")
                    self.db.execute("UPDATE wf_runs SET state='waiting_input',code='MISSING_INPUT',input_keys_json=?,question=? WHERE run_id=?",
                                    (snapshot(list(reply.keys)), reply.question, ctx.run_id))
                else:
                    raise ValueError("INVALID_AGENT_REPLY")
        except asyncio.CancelledError:
            self._set_step(ctx, step, token, "pending", "INTERRUPTED")
            if not self.ledger.guard(ctx):
                raise
        except ToolFault as exc:
            self._set_step(ctx, step, token, "blocked", exc.code)
        except TimeoutError:
            self._set_step(ctx, step, token, "blocked", "STEP_TIMEOUT")
        except Exception as exc:
            codes = {"AGENT_BUDGET_EXCEEDED", "INVALID_AGENT_REPLY", "TOOL_DENIED", "DUPLICATE_AGENT_CALL_ID", "EMPTY_AGENT_OUTPUT", "INVALID_INPUT_REQUEST"}
            self._set_step(ctx, step, token, "blocked", str(exc) if str(exc) in codes else "STEP_ADAPTER_ERROR")

    async def _run_action(self, ctx, step, token, action):
        if self.grant_issuer is None:
            self._set_step(ctx, step, token, "blocked", "AUTH_ISSUER_UNAVAILABLE")
            return
        arguments = json.loads(action["arguments_json"])
        arguments_digest = fingerprint(arguments)
        grant = await self._bounded(token, ctx.run_id,
            lambda: self.grant_issuer(ctx, action["tool_name"], arguments, action["logical_key"]), self.handler_timeout)
        if not self._alive(ctx, step, token):
            return
        if not isinstance(grant, Grant):
            raise ValueError("Invalid grant issuer result")
        if fingerprint(arguments) != arguments_digest:
            raise ToolFault("AUTH_ARGUMENTS_CHANGED")
        tool, binding, _ = self.executor._authorize(ctx, action["tool_name"], arguments, grant)
        sink = self._receipt_sink(ctx, step)
        operation = self.ledger.get(action["operation_id"]) if action["operation_id"] else None
        if operation is None:
            row = self.db.execute("SELECT operation_id FROM operations WHERE principal_id=? AND organization_id=? AND logical_key=?",
                (ctx.principal_id, ctx.organization_id, action["logical_key"])).fetchone()
            operation = self.ledger.get(row["operation_id"]) if row else None
        receipt = self.db.execute("SELECT artifact_id FROM wf_receipts WHERE operation_id=?", (operation["operation_id"],)).fetchone() if operation else None
        if receipt and operation["state"] == "confirmed":
            # Results already confirmed before a scheduler crash are reused without a second write.
            linked = self.db.execute("SELECT approval_id FROM operation_approvals WHERE operation_id=?", (operation["operation_id"],)).fetchone()
            if linked is None:
                self._set_step(ctx, step, token, "blocked", "APPROVAL_RECORD_MISSING")
                return
            approved = self.executor.approvals.check(linked["approval_id"], ctx, tool, arguments, grant,
                action["logical_key"], self.executor._approval_flags(binding, arguments))
            if not approved.allowed:
                self._set_step(ctx, step, token, "blocked", approved.code)
                return
            result_state, code, op_id, artifact_id = "succeeded", "RECOVERED_RECEIPT", operation["operation_id"], receipt["artifact_id"]
        else:
            if operation and operation["state"] == "unknown":
                result = await self.executor.reconcile(ctx, operation["operation_id"], arguments, grant, receipt_sink=sink)
            else:
                result = await self.executor.execute(ctx, action["tool_name"], arguments, grant, logical_key=action["logical_key"], receipt_sink=sink)
            if result.code == "AI_REVIEW_REQUIRED":
                preview = self.executor.approval_preview(result.approval_id, ctx, action["tool_name"], arguments, grant, logical_key=action["logical_key"])
                if self.ai_reviewer is not None:
                    self._reserve_agent(ctx.run_id)
                reviewed = await self.executor.approvals.review_with_ai(result.approval_id, preview, self.ai_reviewer)
                self._set_step(ctx, step, token, "pending" if reviewed.allowed else "waiting_authorization" if reviewed.status == "pending" else "blocked",
                               reviewed.code, approval_id=reviewed.approval_id)
                return
            if result.code == "USER_APPROVAL_REQUIRED":
                self._set_step(ctx, step, token, "waiting_authorization", result.code, approval_id=result.approval_id)
                return
            receipt = self.db.execute("SELECT artifact_id FROM wf_receipts WHERE operation_id=?", (result.operation_id,)).fetchone()
            result_state, code, op_id = result.status, result.code, result.operation_id
            artifact_id = receipt["artifact_id"] if receipt else None
            if result_state == "succeeded" and artifact_id is None:
                result_state, code = "blocked", "ARTIFACT_RECOVERY_REQUIRED"
        with self.transaction():
            if not self._alive(ctx, step, token):
                return
            self.db.execute("UPDATE wf_actions SET state=?,operation_id=?,artifact_id=? WHERE logical_key=?",
                            (result_state, op_id, artifact_id, action["logical_key"]))
            self._set_step(ctx, step, token, "pending" if result_state == "succeeded" else "blocked", code)

    async def _finalize(self, run, owner):
        validation_token = uuid.uuid4().hex
        with self.transaction():
            changed = self.db.execute("""UPDATE wf_runs SET state='verifying',validation_token=?,validation_until=?
                WHERE run_id=? AND state NOT IN ('delivered','cancelled','blocked')
                AND (validation_token IS NULL OR validation_until<=?)""",
                (validation_token, time.time() + self.lease_seconds, owner["run_id"], time.time())).rowcount
        if not changed:
            return False
        validator = self.validators.get(run["workflow_id"])
        if validator is None:
            self.db.execute("UPDATE wf_runs SET state='blocked',code='VALIDATOR_UNAVAILABLE',validation_token=NULL WHERE run_id=? AND validation_token=?", (owner["run_id"], validation_token))
            return
        try:
            self._verify_evidence(run, owner)
            self._reserve_agent(owner["run_id"])
            outputs = {s["step_id"]: self.artifact(s["artifact_id"], principal_id=owner["principal_id"], organization_id=owner["organization_id"])
                       for s in self._steps(owner["run_id"], owner["plan_version"])}
            verdict = await self._bounded("validate:" + owner["run_id"], owner["run_id"],
                lambda: validator(json.loads(run["inputs_json"]), outputs), min(self.handler_timeout, max(0, owner["deadline"] - time.time())))
            if not isinstance(verdict, Validation) or type(verdict.accepted) is not bool:
                raise ValueError("Invalid validator result")
            with self.transaction():
                current = self.db.execute("SELECT * FROM runs WHERE run_id=?", (owner["run_id"],)).fetchone()
                validation = self.db.execute("SELECT validation_token,validation_until FROM wf_runs WHERE run_id=?", (owner["run_id"],)).fetchone()
                if (current["cancelled"] or current["deadline"] <= time.time() or current["plan_version"] != owner["plan_version"]
                        or validation["validation_token"] != validation_token or validation["validation_until"] <= time.time()):
                    return
                if verdict.accepted:
                    final_step = self._steps(owner["run_id"], owner["plan_version"])[-1]
                    self.db.execute("UPDATE wf_runs SET state='syncing',code='ACCEPTED',final_artifact=? WHERE run_id=?",
                                    (final_step["artifact_id"], owner["run_id"]))
                    self.db.execute("INSERT OR IGNORE INTO wf_outbox(event_id,run_id,plan_version,artifact_id) VALUES(?,?,?,?)",
                                    (uuid.uuid4().hex, owner["run_id"], owner["plan_version"], final_step["artifact_id"]))
                elif verdict.rework_step:
                    self._rework(run, owner, verdict.rework_step)
                else:
                    self.db.execute("UPDATE wf_runs SET state='blocked',code='ACCEPTANCE_FAILED' WHERE run_id=?", (owner["run_id"],))
        except asyncio.CancelledError:
            if not self.db.execute("SELECT cancelled FROM runs WHERE run_id=?", (owner["run_id"],)).fetchone()[0]:
                raise
        except Exception as exc:
            code = str(exc) if str(exc) in {"EVIDENCE_MISSING", "AGENT_BUDGET_EXCEEDED"} else "VALIDATION_ADAPTER_ERROR"
            self.db.execute("UPDATE wf_runs SET state='blocked',code=? WHERE run_id=? AND state!='cancelled' AND validation_token=?",
                            (code, owner["run_id"], validation_token))
        finally:
            self.db.execute("UPDATE wf_runs SET validation_token=NULL WHERE run_id=? AND validation_token=?", (owner["run_id"], validation_token))

    def _verify_evidence(self, run, owner):
        effects = {tool["name"]: tool["effect"] for service in self.config.services.values() for tool in service["tools"]}
        steps = {s["step_id"]: s for s in self._steps(owner["run_id"], owner["plan_version"])}
        for definition in json.loads(run["template_json"])["steps"]:
            effect = definition.get("requires_tool_effect")
            if effect is None:
                continue
            step = steps[definition["id"]]
            rows = self.db.execute("""SELECT a.tool_name FROM wf_actions a JOIN operations o ON a.operation_id=o.operation_id
                JOIN wf_receipts r ON o.operation_id=r.operation_id JOIN wf_artifacts f ON r.artifact_id=f.artifact_id
                WHERE a.run_id=? AND a.plan_version=? AND a.step_id=? AND a.generation=? AND a.state='succeeded'
                AND o.state='confirmed' AND o.tool_name=a.tool_name AND o.logical_key=a.logical_key
                AND f.run_id=a.run_id AND f.plan_version=a.plan_version AND f.step_id=a.step_id AND f.generation=a.generation""",
                (owner["run_id"], owner["plan_version"], definition["id"], step["generation"])).fetchall()
            if not any(effects.get(row["tool_name"]) == effect for row in rows):
                raise ValueError("EVIDENCE_MISSING")

    def _rework(self, run, owner, step_id):
        definitions = {s["id"]: s for s in json.loads(run["template_json"])["steps"]}
        if step_id not in definitions:
            raise ValueError("Unknown rework step")
        affected = {step_id}
        while True:
            expanded = affected | {key for key, value in definitions.items() if set(value["depends_on"]) & affected}
            if expanded == affected:
                break
            affected = expanded
        writes = self.db.execute("SELECT state FROM operations WHERE run_id=? AND effect='write'", (owner["run_id"],)).fetchall()
        if any(row["state"] in {"pending", "unknown", "confirmed"} for row in writes):
            self.db.execute("UPDATE wf_runs SET state='blocked',code='WRITE_REWORK_REQUIRES_NEW_PLAN' WHERE run_id=?", (owner["run_id"],))
            return
        changed = self.db.execute("UPDATE wf_runs SET rework_used=rework_used+1,state='executing',code='REWORK' WHERE run_id=? AND rework_used<max_rework", (owner["run_id"],)).rowcount
        if not changed:
            self.db.execute("UPDATE wf_runs SET state='blocked',code='REWORK_LIMIT' WHERE run_id=?", (owner["run_id"],))
            return
        for key in affected:
            self.db.execute("UPDATE wf_steps SET state='pending',code='REWORK',generation=generation+1,artifact_id=NULL,approval_id=NULL,lease_token=NULL WHERE run_id=? AND plan_version=? AND step_id=?",
                            (owner["run_id"], owner["plan_version"], key))

    def resume(self, run_id, *, principal_id, organization_id):
        owner = self._owner(run_id, principal_id, organization_id)
        if owner["cancelled"] or owner["deadline"] <= time.time():
            raise ValueError("Run is cancelled or expired")
        for step in self._steps(run_id, owner["plan_version"]):
            if step["state"] != "blocked":
                continue
            definition = json.loads(step["definition_json"])
            generation = step["generation"] + 1 if definition["effect"] == "read" else step["generation"]
            self.db.execute("UPDATE wf_steps SET state='pending',code='RESUMED',generation=?,lease_token=NULL WHERE run_id=? AND plan_version=? AND step_id=?",
                            (generation, run_id, owner["plan_version"], step["step_id"]))
        self.db.execute("UPDATE wf_runs SET state='executing',code='RESUMED' WHERE run_id=? AND state='blocked'", (run_id,))

    def revise(self, run_id, inputs, *, principal_id, organization_id, expected_plan_version):
        if not isinstance(inputs, dict):
            raise ValueError("Inputs must be an object")
        with self.transaction():
            owner = self._owner(run_id, principal_id, organization_id)
            if owner["cancelled"] or owner["plan_version"] != expected_plan_version:
                raise ValueError("STALE_PLAN")
            writes = self.db.execute("SELECT 1 FROM operations WHERE run_id=? AND effect='write' AND state IN ('pending','unknown','confirmed') LIMIT 1", (run_id,)).fetchone()
            if writes:
                raise ValueError("WRITE_REVISION_REQUIRES_NEW_PLAN")
            run = dict(self.db.execute("SELECT * FROM wf_runs WHERE run_id=?", (run_id,)).fetchone())
            template = json.loads(run["template_json"])
            missing = [key for key in template["required_inputs"] if key not in inputs or inputs[key] is None]
            self.ledger.advance_plan(run_id, expected_plan_version)
            self.db.execute("UPDATE wf_steps SET state='cancelled',code='SUPERSEDED',lease_token=NULL WHERE run_id=? AND plan_version=? AND state!='succeeded'",
                            (run_id, expected_plan_version))
            plan = expected_plan_version + 1
            self._insert_steps(run_id, plan, template)
            self._put(run_id, plan, "$inputs", 1, inputs, "input_snapshot")
            self.db.execute("UPDATE wf_runs SET inputs_json=?,input_version=input_version+1,state=?,code='REVISED',input_keys_json=?,final_artifact=NULL,validation_token=NULL WHERE run_id=?",
                            (snapshot(inputs), "waiting_input" if missing else "executing", snapshot(missing), run_id))
            self.db.execute("UPDATE wf_outbox SET state='cancelled' WHERE run_id=? AND state='pending'", (run_id,))
        return plan

    def pending_deliveries(self, *, principal_id, organization_id):
        return [dict(row) for row in self.db.execute("""SELECT o.* FROM wf_outbox o JOIN runs r ON o.run_id=r.run_id
            WHERE r.principal_id=? AND r.organization_id=? AND r.cancelled=0 AND o.plan_version=r.plan_version AND o.state='pending'""",
            (principal_id, organization_id))]

    def acknowledge_delivery(self, event_id, *, principal_id, organization_id):
        row = self.db.execute("SELECT run_id FROM wf_outbox WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise ValueError("Delivery event not found")
        with self.transaction():
            owner = self._owner(row["run_id"], principal_id, organization_id)
            changed = self.db.execute("UPDATE wf_outbox SET state='acknowledged' WHERE event_id=? AND state='pending' AND plan_version=? AND ?=0",
                                      (event_id, owner["plan_version"], owner["cancelled"])).rowcount
            if changed:
                self.db.execute("UPDATE wf_runs SET state='delivered',code='DELIVERED' WHERE run_id=? AND state='syncing'", (row["run_id"],))

    async def serve(self, stop: asyncio.Event, *, poll_interval=1):
        """Wake runnable/approval/input waits; stop without cancelling the business run."""
        if poll_interval <= 0:
            raise ValueError("Invalid poll interval")
        while not stop.is_set():
            rows = self.db.execute("""SELECT w.run_id,r.principal_id,r.organization_id FROM wf_runs w
                JOIN runs r ON w.run_id=r.run_id
                WHERE w.state IN ('executing','verifying','waiting_authorization','waiting_input')
                ORDER BY w.rowid""").fetchall()
            for row in rows:
                if stop.is_set():
                    break
                work = asyncio.create_task(self.drive(row["run_id"], principal_id=row["principal_id"], organization_id=row["organization_id"]))
                stopping = asyncio.create_task(stop.wait())
                try:
                    done, _ = await asyncio.wait({work, stopping}, return_when=asyncio.FIRST_COMPLETED)
                    if work in done:
                        work.result()
                finally:
                    if not work.done():
                        work.cancel()
                    stopping.cancel()
                    await asyncio.gather(work, stopping, return_exceptions=True)
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_interval)
                except TimeoutError:
                    pass
