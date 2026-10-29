"""Approval integration tests use real policy rules and simulated tool/review adapters."""

import asyncio
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from agent_runtime.approvals import AIReview, ApprovalPolicy
from agent_runtime.executor import Binding, Executor, Grant, Ledger, TaskContext, fingerprint
from scripts.agents import AgentConfig

READ = "mcp__dingtalk_contact__get_dept_info_by_dept_id"
WRITE = "mcp__dingtalk_doc__create_document"
SEND = "mcp__dingtalk_mail__send_email"


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.folder.name) / "state.sqlite3")
        self.ledger.create_run("run", "owner", "org")
        self.config = AgentConfig()
        self.ctx = TaskContext("run", "task", "attempt", 1, "owner", "org", "dingtalk", "dingtalk-scoped-write", "write")
        self.executor = Executor(self.config, self.ledger)
        self.calls = 0

        async def invoke(args):
            self.calls += 1
            return {"success": True, "id": "simulated"}

        def validate(args):
            if set(args) != {"value"} or not isinstance(args["value"], str):
                raise ValueError("Invalid fixture schema")
        self.binding = Binding(invoke, validate, resource_key=lambda _: "target")
        for tool in (READ, WRITE, SEND):
            self.executor.register(tool, self.binding)
        self.args = {"value": "fixture"}

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        self.ledger.close()
        self.folder.cleanup()

    def grant(self, tool=WRITE, args=None):
        return Grant("auth", "run", "task", 1, "owner", "org", tool,
                     fingerprint(args or self.args), time.time() + 60, resource_key="target")

    def request(self, tool=WRITE, args=None, key="action", grant=None):
        args = args or self.args
        grant = grant or self.grant(tool, args)
        decision = self.executor.prepare_approval(self.ctx, tool, args, grant, logical_key=key)
        preview = self.executor.approval_preview(decision.approval_id, self.ctx, tool, args, grant, logical_key=key)
        return decision, preview, grant

    @staticmethod
    async def approve_ai(packet):
        return AIReview(packet["approval_id"], packet["action_digest"], packet["policy_digest"],
                        "approve", "low", .99, ("LOW_RISK",))

    def approve_user(self, request, *, actor="owner", org="org", approve=True):
        return self.executor.approvals.user_decide(request.approval_id, actor_user_id=actor,
            actor_organization_id=org, expected_action_digest=request.action_digest, approve=approve)

    async def test_read_automatically_executes_and_records_approval(self):
        result = await self.executor.execute(self.ctx, READ, self.args, self.grant(READ), logical_key="read")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self.calls, 1)
        row = self.executor.approvals._row(result.approval_id)
        self.assertEqual((row["mode"], row["code"]), ("auto", "AUTO_APPROVED"))

    async def test_ai_pending_blocks_without_spending_tool_budget(self):
        result = await self.executor.execute(self.ctx, WRITE, self.args, self.grant(), logical_key="action")
        self.assertEqual(result.code, "AI_REVIEW_REQUIRED")
        self.assertIsNotNone(result.approval_id)
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.ledger.db.execute("SELECT calls_used FROM runs").fetchone()[0], 0)
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)

    async def test_ai_approved_operation_can_execute(self):
        request, preview, grant = self.request()
        approved = await self.executor.approvals.review_with_ai(request.approval_id, preview, self.approve_ai)
        self.assertEqual(approved.code, "AI_APPROVED")
        result = await self.executor.execute(self.ctx, WRITE, self.args, grant, logical_key="action")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self.calls, 1)

    async def test_user_required_cannot_be_approved_by_ai(self):
        request, preview, grant = self.request(SEND)
        self.assertEqual(request.mode, "user")
        with self.assertRaises(ValueError):
            await self.executor.approvals.review_with_ai(request.approval_id, preview, self.approve_ai)
        blocked = await self.executor.execute(self.ctx, SEND, self.args, grant, logical_key="action")
        self.assertEqual(blocked.code, "USER_APPROVAL_REQUIRED")
        self.assertEqual(self.calls, 0)

    async def test_user_approval_then_execution(self):
        request, _, grant = self.request(SEND)
        self.approve_user(request)
        result = await self.executor.execute(self.ctx, SEND, self.args, grant, logical_key="action")
        self.assertEqual(result.status, "succeeded")

    async def test_wrong_user_or_organization_cannot_approve(self):
        request, _, _ = self.request(SEND)
        for actor, org in (("other", "org"), ("owner", "other")):
            with self.assertRaises(PermissionError):
                self.approve_user(request, actor=actor, org=org)
        self.assertEqual(self.calls, 0)

    async def test_rejection_is_durable_and_not_recreated_on_retry(self):
        request, _, grant = self.request(SEND)
        self.approve_user(request, approve=False)
        await self.executor.execute(self.ctx, SEND, self.args, grant, logical_key="action")
        result = await self.executor.execute(self.ctx, SEND, self.args, grant, logical_key="action")
        self.assertEqual(result.code, "APPROVAL_REJECTED")
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 1)
        self.assertEqual(self.calls, 0)

    async def test_changed_parameters_require_fresh_review_even_with_fresh_grant(self):
        request, preview, _ = self.request()
        await self.executor.approvals.review_with_ai(request.approval_id, preview, self.approve_ai)
        args = {"value": "changed"}
        result = await self.executor.execute(self.ctx, WRITE, args, self.grant(WRITE, args), logical_key="action")
        self.assertEqual(result.code, "AI_REVIEW_REQUIRED")
        self.assertNotEqual(result.approval_id, request.approval_id)
        self.assertEqual(self.calls, 0)

    async def test_approval_is_bound_to_logical_action(self):
        request, _, grant = self.request(SEND)
        self.approve_user(request)
        result = await self.executor.execute(self.ctx, SEND, self.args, grant, logical_key="different-action")
        self.assertEqual(result.code, "USER_APPROVAL_REQUIRED")
        self.assertNotEqual(result.approval_id, request.approval_id)

    async def test_expired_or_revoked_approval_is_not_reused(self):
        request, _, grant = self.request(SEND)
        self.approve_user(request)
        self.ledger.db.execute("UPDATE approvals SET expires_at=? WHERE approval_id=?", (time.time() - 1, request.approval_id))
        result = await self.executor.execute(self.ctx, SEND, self.args, grant, logical_key="action")
        self.assertEqual(result.code, "APPROVAL_EXPIRED")
        self.executor.approvals.revoke(request.approval_id, actor_user_id="owner", actor_organization_id="org")
        result = await self.executor.execute(self.ctx, SEND, self.args, grant, logical_key="action")
        self.assertEqual(result.code, "APPROVAL_REVOKED")
        self.assertEqual(self.calls, 0)

    async def test_low_confidence_and_medium_risk_escalate_to_user(self):
        for index, (risk, confidence) in enumerate((("low", .5), ("medium", .99))):
            request, preview, grant = self.request(key=f"action-{index}")

            async def reviewer(packet):
                return AIReview(packet["approval_id"], packet["action_digest"], packet["policy_digest"],
                                "approve", risk, confidence, ("LOW_RISK",))
            result = await self.executor.approvals.review_with_ai(request.approval_id, preview, reviewer)
            self.assertEqual((result.mode, result.status), ("user", "pending"))
        self.assertEqual(self.calls, 0)

    async def test_unavailable_ai_escalates_to_user(self):
        request, preview, grant = self.request()
        result = await self.executor.approvals.review_with_ai(request.approval_id, preview)
        self.assertEqual(result.mode, "user")
        self.approve_user(result)
        self.assertEqual((await self.executor.execute(self.ctx, WRITE, self.args, grant, logical_key="action")).status, "succeeded")

    async def test_ai_timeout_escalates_to_user(self):
        data = ApprovalPolicy.load(self.config.root).data
        data["ai_review_timeout_seconds"] = .01
        self.executor.approvals.policy = ApprovalPolicy(data)
        request, preview, _ = self.request()

        async def reviewer(packet):
            await asyncio.sleep(10)
        result = await self.executor.approvals.review_with_ai(request.approval_id, preview, reviewer)
        self.assertEqual(result.code, "USER_APPROVAL_REQUIRED")

    async def test_modified_review_packet_cannot_approve_original(self):
        request, preview, _ = self.request()
        preview["arguments"]["value"] = "harmless fake content"
        with self.assertRaises(ValueError):
            await self.executor.approvals.review_with_ai(request.approval_id, preview, self.approve_ai)

    async def test_wrong_ai_fingerprint_escalates_instead_of_approving(self):
        request, preview, _ = self.request()

        async def reviewer(packet):
            return AIReview(packet["approval_id"], "wrong-digest", packet["policy_digest"], "approve", "low", .99, ("LOW_RISK",))
        result = await self.executor.approvals.review_with_ai(request.approval_id, preview, reviewer)
        self.assertEqual(result.mode, "user")

    async def test_mandatory_user_rule_overrides_auto_and_ai_rules(self):
        data = ApprovalPolicy.load(self.config.root).data
        data["tool_modes"][SEND] = "auto"
        policy = ApprovalPolicy(data)
        self.assertEqual(policy.mode(SEND, "write", ()), "user")
        self.assertEqual(policy.mode(WRITE, "write", ("external_recipients",)), "user")

    async def test_stale_plan_and_cancelled_run_cannot_be_approved(self):
        request, _, _ = self.request(SEND)
        self.ledger.advance_plan("run", 1)
        with self.assertRaisesRegex(ValueError, "STALE_PLAN"):
            self.approve_user(request)
        self.ledger.cancel_run("run")
        with self.assertRaisesRegex(ValueError, "CANCELLED"):
            self.approve_user(request)

    async def test_policy_change_requires_new_approval(self):
        request, _, grant = self.request(SEND)
        self.approve_user(request)
        data = ApprovalPolicy.load(self.config.root).data
        data["policy_version"] = "2"
        self.executor.approvals.policy = ApprovalPolicy(data)
        result = await self.executor.execute(self.ctx, SEND, self.args, grant, logical_key="action")
        self.assertEqual(result.code, "USER_APPROVAL_REQUIRED")
        self.assertNotEqual(result.approval_id, request.approval_id)

    async def test_restart_preserves_user_approval(self):
        request, _, grant = self.request(SEND)
        self.approve_user(request)
        self.ledger.close()
        self.ledger = Ledger(Path(self.folder.name) / "state.sqlite3")
        self.executor = Executor(self.config, self.ledger)
        self.executor.register(SEND, self.binding)
        result = await self.executor.execute(self.ctx, SEND, self.args, grant, logical_key="action")
        self.assertEqual(result.status, "succeeded")

    async def test_approval_storage_has_no_raw_arguments_or_review_prompt(self):
        args = {"value": "private-content-394827"}
        self.request(SEND, args=args)
        dump = "\n".join(self.ledger.db.iterdump())
        self.assertNotIn(args["value"], dump)
        self.assertNotIn("操作批准审查器", dump)

    async def test_trusted_risk_flags_escalate_ai_tool_to_user(self):
        binding = replace(self.binding, approval_flags=lambda args: ("permission_change",))
        self.executor.register(WRITE, binding)
        result = await self.executor.execute(self.ctx, WRITE, self.args, self.grant(), logical_key="action")
        self.assertEqual(result.code, "USER_APPROVAL_REQUIRED")
        self.assertEqual(self.calls, 0)

    async def test_ai_rejection_blocks_execution(self):
        request, preview, grant = self.request()

        async def reviewer(packet):
            return AIReview(packet["approval_id"], packet["action_digest"], packet["policy_digest"],
                            "reject", "high", .99, ("POLICY_CONFLICT",))
        await self.executor.approvals.review_with_ai(request.approval_id, preview, reviewer)
        result = await self.executor.execute(self.ctx, WRITE, self.args, grant, logical_key="action")
        self.assertEqual(result.code, "APPROVAL_REJECTED")
        self.assertEqual(self.calls, 0)

    async def test_conflicting_ai_reason_cannot_approve(self):
        request, preview, _ = self.request()

        async def reviewer(packet):
            return AIReview(packet["approval_id"], packet["action_digest"], packet["policy_digest"],
                            "approve", "low", .99, ("POLICY_CONFLICT",))
        result = await self.executor.approvals.review_with_ai(request.approval_id, preview, reviewer)
        self.assertEqual(result.mode, "user")

    async def test_expiry_during_ai_review_cannot_approve(self):
        request, preview, _ = self.request()

        async def reviewer(packet):
            self.ledger.db.execute("UPDATE approvals SET expires_at=? WHERE approval_id=?", (time.time() - 1, request.approval_id))
            return await self.approve_ai(packet)
        result = await self.executor.approvals.review_with_ai(request.approval_id, preview, reviewer)
        self.assertEqual(result.status, "expired")

    async def test_revocation_during_ai_review_cannot_be_overwritten(self):
        request, preview, _ = self.request()

        async def reviewer(packet):
            self.executor.approvals.revoke(request.approval_id, actor_user_id="owner", actor_organization_id="org")
            return await self.approve_ai(packet)
        result = await self.executor.approvals.review_with_ai(request.approval_id, preview, reviewer)
        self.assertEqual(result.status, "revoked")

    async def test_user_inbox_is_scoped_and_omits_stale_requests(self):
        request, _, _ = self.request(SEND)
        own = self.executor.approvals.pending_for(actor_user_id="owner", actor_organization_id="org")
        self.assertEqual([item.approval_id for item in own], [request.approval_id])
        other = self.executor.approvals.pending_for(actor_user_id="other", actor_organization_id="org")
        self.assertEqual(other, [])
        self.ledger.advance_plan("run", 1)
        self.assertEqual(self.executor.approvals.pending_for(actor_user_id="owner", actor_organization_id="org"), [])


if __name__ == "__main__":
    unittest.main()
