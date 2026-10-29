"""Persistent auto / AI / user approval decisions bound to an exact operation."""

import asyncio
import inspect
import json
import math
import time
import uuid
from dataclasses import dataclass

from agent_runtime.executor import fingerprint
from scripts.agents import project_file, read_json

MODES = {"auto", "ai", "user"}
REASONS = {"LOW_RISK", "SCOPE_UNCLEAR", "EXTERNAL_EFFECT", "DESTRUCTIVE_EFFECT",
           "COST_UNCLEAR", "POLICY_CONFLICT", "INSUFFICIENT_CONTEXT"}


@dataclass(frozen=True)
class Approval:
    approval_id: str
    action_digest: str
    mode: str
    status: str
    code: str

    @property
    def allowed(self):
        return self.status == "approved"


@dataclass(frozen=True)
class AIReview:
    approval_id: str
    action_digest: str
    policy_digest: str
    decision: str
    risk: str
    confidence: float
    reason_codes: tuple[str, ...]


class ApprovalPolicy:
    def __init__(self, data, *, instructions=""):
        # Keep an independent snapshot; external mutation must not silently change policy.
        self.data = json.loads(json.dumps(data, allow_nan=False))
        if self.data.get("schema_version") != "1.0":
            raise ValueError("Unsupported approval policy version")
        if set(self.data.get("default_modes", {})) != {"read", "write"}:
            raise ValueError("Read and write defaults required")
        if any(mode not in MODES for mode in self.data["default_modes"].values()):
            raise ValueError("Invalid approval default")
        if not isinstance(self.data.get("tool_modes", {}), dict):
            raise ValueError("Tool approval rules must be an object")
        if any(mode not in MODES for mode in self.data.get("tool_modes", {}).values()):
            raise ValueError("Invalid tool approval mode")
        for name in ("approval_ttl_seconds", "ai_review_timeout_seconds"):
            value = self.data.get(name)
            if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid {name}")
        confidence = self.data.get("ai_min_confidence")
        if type(confidence) not in (float, int) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("Invalid AI confidence threshold")
        if not isinstance(self.data.get("policy_version"), str) or not self.data["policy_version"]:
            raise ValueError("Policy version required")
        if not isinstance(self.data.get("ai_reviewer_id"), str) or not self.data["ai_reviewer_id"]:
            raise ValueError("AI reviewer identity required")
        for name in ("mandatory_user_tools", "user_required_flags"):
            values = self.data.get(name, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not v or "*" in v for v in values):
                raise ValueError(f"Invalid {name}")
        if any("*" in name for name in self.data.get("tool_modes", {})):
            raise ValueError("Approval tool rules must use exact names")
        self.instructions = instructions
        self.digest = fingerprint({"policy": self.data, "instructions": instructions})

    @classmethod
    def load(cls, root, relative="config/approvals.json"):
        data = read_json(project_file(root, relative))
        instructions = project_file(root, data["ai_reviewer_instructions"]).read_text(encoding="utf-8")
        return cls(data, instructions=instructions)

    def mode(self, tool_name, effect, flags):
        if effect not in {"read", "write"}:
            raise ValueError("No approval policy for this effect")
        if tool_name in self.data.get("mandatory_user_tools", []) or flags:
            return "user"
        return self.data.get("tool_modes", {}).get(tool_name, self.data["default_modes"][effect])


class ApprovalEngine:
    def __init__(self, ledger, policy):
        self.ledger, self.policy = ledger, policy
        ledger.db.executescript("""
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY, action_digest TEXT NOT NULL UNIQUE,
                policy_digest TEXT NOT NULL, policy_version TEXT NOT NULL,
                plan_version INTEGER NOT NULL, preview_digest TEXT NOT NULL,
                run_id TEXT NOT NULL, task_id TEXT NOT NULL, principal_id TEXT NOT NULL,
                organization_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL, code TEXT NOT NULL,
                expires_at REAL NOT NULL, reviewer_id TEXT, reason_codes TEXT NOT NULL DEFAULT '[]',
                review_token TEXT, review_until REAL
            );
            CREATE TABLE IF NOT EXISTS approval_events (
                id INTEGER PRIMARY KEY, approval_id TEXT NOT NULL, event_type TEXT NOT NULL,
                actor_id TEXT NOT NULL, code TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operation_approvals (
                operation_id TEXT PRIMARY KEY, approval_id TEXT NOT NULL
            );
        """)

    def _action(self, ctx, tool, arguments, grant, logical_key, flags):
        return fingerprint({"run": ctx.run_id, "task": ctx.task_id, "plan": ctx.plan_version,
                            "principal": ctx.principal_id, "organization": ctx.organization_id,
                            "agent": ctx.agent_id, "skill": ctx.skill_id, "task_effect": ctx.effect,
                            "tool": tool["name"], "tool_effect": tool["effect"],
                            "arguments": fingerprint(arguments), "target": fingerprint(grant.resource_key),
                            "logical_key": logical_key, "authorization": grant.authorization_id,
                            "authorization_policy": grant.policy_version,
                            "policy": self.policy.digest, "flags": sorted(set(flags))})

    def _event(self, approval_id, event_type, actor_id, code):
        self.ledger.db.execute("INSERT INTO approval_events(approval_id,event_type,actor_id,code,created_at) VALUES(?,?,?,?,?)",
                               (approval_id, event_type, actor_id, code, time.time()))

    def _row(self, approval_id):
        row = self.ledger.db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            raise ValueError("Approval not found")
        if row["expires_at"] <= time.time() and row["status"] in {"pending", "approved"}:
            self.ledger.db.execute("UPDATE approvals SET status='expired',code='APPROVAL_EXPIRED' WHERE approval_id=? AND status IN ('pending','approved')",
                                   (approval_id,))
            row = self.ledger.db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return dict(row)

    @staticmethod
    def _view(row):
        return Approval(row["approval_id"], row["action_digest"], row["mode"], row["status"], row["code"])

    def _live(self, row):
        if row["policy_digest"] != self.policy.digest:
            return "APPROVAL_POLICY_CHANGED"
        run = self.ledger.db.execute("SELECT * FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
        if not run or run["cancelled"]:
            return "CANCELLED"
        if run["deadline"] <= time.time():
            return "DEADLINE_EXCEEDED"
        if run["plan_version"] != row["plan_version"]:
            return "STALE_PLAN"
        if (run["principal_id"], run["organization_id"]) != (row["principal_id"], row["organization_id"]):
            return "IDENTITY_MISMATCH"
        return None

    @staticmethod
    def _preview_digest(tool_name, effect, target, arguments, flags):
        return fingerprint({"tool": tool_name, "effect": effect, "target": target,
                            "arguments": arguments, "risk_flags": sorted(set(flags))})

    def require(self, ctx, tool, arguments, grant, logical_key, flags=()):
        digest = self._action(ctx, tool, arguments, grant, logical_key, flags)
        row = self.ledger.db.execute("SELECT * FROM approvals WHERE action_digest=?", (digest,)).fetchone()
        if row:
            return self.check(row["approval_id"], ctx, tool, arguments, grant, logical_key, flags)
        mode = self.policy.mode(tool["name"], tool["effect"], flags)
        approval_id = uuid.uuid4().hex
        run = self.ledger.db.execute("SELECT deadline FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()
        expires = min(time.time() + self.policy.data["approval_ttl_seconds"], grant.expires_at, run["deadline"])
        status = "approved" if mode == "auto" else "pending"
        code = "AUTO_APPROVED" if mode == "auto" else "AI_REVIEW_REQUIRED" if mode == "ai" else "USER_APPROVAL_REQUIRED"
        self.ledger.db.execute("""INSERT OR IGNORE INTO approvals
            (approval_id,action_digest,policy_digest,policy_version,plan_version,preview_digest,
             run_id,task_id,principal_id,organization_id,tool_name,mode,status,code,expires_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (approval_id, digest, self.policy.digest, self.policy.data["policy_version"], ctx.plan_version,
             self._preview_digest(tool["name"], tool["effect"], grant.resource_key, arguments, flags), ctx.run_id, ctx.task_id,
             ctx.principal_id, ctx.organization_id, tool["name"], mode, status, code, expires))
        row = self.ledger.db.execute("SELECT * FROM approvals WHERE action_digest=?", (digest,)).fetchone()
        self._event(row["approval_id"], "requested", "policy", code)
        return self.check(row["approval_id"], ctx, tool, arguments, grant, logical_key, flags)

    def check(self, approval_id, ctx, tool, arguments, grant, logical_key, flags=()):
        row = self._row(approval_id)
        expected = self._action(ctx, tool, arguments, grant, logical_key, flags)
        code = self.ledger.guard(ctx) or self._live(row)
        if code or expected != row["action_digest"]:
            return Approval(approval_id, row["action_digest"], row["mode"], "invalid", code or "APPROVAL_SCOPE_MISMATCH")
        return self._view(row)

    def preview(self, approval_id, ctx, tool, arguments, grant, logical_key, flags=()):
        decision = self.check(approval_id, ctx, tool, arguments, grant, logical_key, flags)
        if decision.status not in {"pending", "approved"}:
            raise ValueError(decision.code)
        return {"approval_id": approval_id, "action_digest": decision.action_digest,
                "policy_digest": self.policy.digest, "mode": decision.mode, "tool": tool["name"],
                "effect": tool["effect"], "target": grant.resource_key,
                "arguments": json.loads(json.dumps(arguments)), "risk_flags": sorted(set(flags))}

    def user_decide(self, approval_id, *, actor_user_id, actor_organization_id, expected_action_digest, approve):
        row = self._row(approval_id)
        if type(approve) is not bool:
            raise ValueError("User decision must be a boolean")
        if (actor_user_id, actor_organization_id) != (row["principal_id"], row["organization_id"]):
            raise PermissionError("Reviewer identity mismatch")
        code = self._live(row)
        if code or expected_action_digest != row["action_digest"]:
            raise ValueError(code or "Approval preview changed")
        if row["mode"] != "user" or row["status"] != "pending":
            raise ValueError("Approval is not awaiting user review")
        status, code = ("approved", "USER_APPROVED") if approve else ("rejected", "APPROVAL_REJECTED")
        changed = self.ledger.db.execute("""UPDATE approvals SET status=?,code=?,reviewer_id=?
            WHERE approval_id=? AND mode='user' AND status='pending' AND expires_at>?""",
            (status, code, actor_user_id, approval_id, time.time())).rowcount
        if not changed:
            raise ValueError("Approval already changed or expired")
        self._event(approval_id, status, actor_user_id, code)
        return self._view(self._row(approval_id))

    def pending_for(self, *, actor_user_id, actor_organization_id):
        """Actor identity must come from an authenticated UI/session adapter."""
        self.ledger.db.execute("""UPDATE approvals SET status='expired',code='APPROVAL_EXPIRED'
            WHERE principal_id=? AND organization_id=? AND status IN ('pending','approved') AND expires_at<=?""",
            (actor_user_id, actor_organization_id, time.time()))
        rows = self.ledger.db.execute("""SELECT a.* FROM approvals a JOIN runs r ON a.run_id=r.run_id
            WHERE a.principal_id=? AND a.organization_id=? AND a.mode='user' AND a.status='pending'
            AND a.policy_digest=? AND a.plan_version=r.plan_version AND r.cancelled=0 AND r.deadline>?
            ORDER BY a.rowid""", (actor_user_id, actor_organization_id, self.policy.digest, time.time())).fetchall()
        return [self._view(dict(row)) for row in rows]

    def revoke(self, approval_id, *, actor_user_id, actor_organization_id):
        row = self._row(approval_id)
        if (actor_user_id, actor_organization_id) != (row["principal_id"], row["organization_id"]):
            raise PermissionError("Reviewer identity mismatch")
        self.ledger.db.execute("UPDATE approvals SET status='revoked',code='APPROVAL_REVOKED' WHERE approval_id=?",
                               (approval_id,))
        self._event(approval_id, "revoked", actor_user_id, "APPROVAL_REVOKED")
        return self._view(self._row(approval_id))

    async def review_with_ai(self, approval_id, packet, reviewer=None):
        """The trusted caller supplies the frozen preview and a separate review adapter."""
        row = self._row(approval_id)
        if row["mode"] != "ai" or row["status"] != "pending":
            raise ValueError("Approval is not awaiting AI review")
        packet_fields = {"approval_id", "action_digest", "policy_digest", "mode", "tool", "effect", "target", "arguments", "risk_flags"}
        if (not isinstance(packet, dict) or set(packet) != packet_fields or self._live(row)
                or packet["mode"] != "ai" or packet["action_digest"] != row["action_digest"]
                or packet["approval_id"] != approval_id or packet["policy_digest"] != self.policy.digest
                or self._preview_digest(packet["tool"], packet["effect"], packet["target"], packet["arguments"], packet["risk_flags"]) != row["preview_digest"]):
            raise ValueError("Stale AI review packet")
        packet = json.loads(json.dumps(packet, allow_nan=False))
        token = uuid.uuid4().hex
        changed = self.ledger.db.execute("""UPDATE approvals SET review_token=?,review_until=?
            WHERE approval_id=? AND mode='ai' AND status='pending' AND (review_until IS NULL OR review_until<=?)""",
            (token, time.time() + self.policy.data["ai_review_timeout_seconds"], approval_id, time.time())).rowcount
        if not changed:
            raise ValueError("AI review is already running")
        mode, status, code, reasons = "user", "pending", "USER_APPROVAL_REQUIRED", []
        try:
            if reviewer is not None:
                if not inspect.iscoroutinefunction(reviewer):
                    raise ValueError("AI review adapter must be asynchronous")
                review = await self._call_reviewer(reviewer, {**packet, "instructions": self.policy.instructions},
                                                  min(self.policy.data["ai_review_timeout_seconds"], max(0, row["expires_at"] - time.time())))
                valid = (isinstance(review, AIReview) and review.approval_id == approval_id
                         and review.action_digest == row["action_digest"] and review.policy_digest == self.policy.digest
                         and review.decision in {"approve", "reject", "escalate"}
                         and review.risk in {"low", "medium", "high", "unknown"}
                         and type(review.confidence) in (int, float) and math.isfinite(review.confidence)
                         and 0 <= review.confidence <= 1 and isinstance(review.reason_codes, tuple)
                         and bool(review.reason_codes) and set(review.reason_codes) <= REASONS)
                if valid:
                    reasons = list(review.reason_codes)
                    confident = review.confidence >= self.policy.data["ai_min_confidence"]
                    if review.decision == "approve" and review.risk == "low" and confident and set(reasons) == {"LOW_RISK"}:
                        mode, status, code = "ai", "approved", "AI_APPROVED"
                    elif review.decision == "reject" and confident:
                        mode, status, code = "ai", "rejected", "APPROVAL_REJECTED"
        except asyncio.CancelledError:
            self.ledger.db.execute("UPDATE approvals SET review_until=0 WHERE approval_id=? AND review_token=?", (approval_id, token))
            raise
        except Exception:
            # Missing model, invalid output and timeout all require a user decision.
            pass
        current = self._row(approval_id)
        live_error = self._live(current)
        if live_error:
            return Approval(approval_id, current["action_digest"], current["mode"], "invalid", live_error)
        if current["status"] != "pending" or current["review_token"] != token:
            return self._view(current)
        changed = self.ledger.db.execute("""UPDATE approvals SET mode=?,status=?,code=?,reviewer_id=?,reason_codes=?,review_until=0
            WHERE approval_id=? AND review_token=? AND mode='ai' AND status='pending' AND expires_at>?""",
            (mode, status, code, self.policy.data["ai_reviewer_id"], json.dumps(reasons), approval_id, token, time.time())).rowcount
        if changed:
            self._event(approval_id, "ai_review", self.policy.data["ai_reviewer_id"], code)
        return self._view(self._row(approval_id))

    def attach(self, operation_id, approval_id):
        self.ledger.db.execute("INSERT OR REPLACE INTO operation_approvals VALUES(?,?)", (operation_id, approval_id))

    @staticmethod
    async def _call_reviewer(reviewer, packet, timeout):
        task = asyncio.create_task(reviewer(packet))
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                raise TimeoutError("AI review timed out")
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
