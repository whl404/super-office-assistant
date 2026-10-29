"""Behavioral fault-injection tests; all adapters are local fakes."""

import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from agent_runtime.executor import (Binding, Executor, Grant, Ledger, Reconciliation,
                                    TaskContext, ToolFault, classify_response, fingerprint)
from scripts.agents import AgentConfig
from agent_runtime.approvals import ApprovalPolicy

READ = "mcp__dingtalk_contact__get_dept_info_by_dept_id"
WRITE = "mcp__dingtalk_doc__create_document"


def validate_read(args):
    if set(args) != {"deptId"} or type(args["deptId"]) is not int:
        raise ValueError("deptId must be an integer")


def validate_write(args):
    if set(args) != {"name"} or not isinstance(args["name"], str) or not args["name"]:
        raise ValueError("name is required")


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path)
        self.ledger.create_run("run-1", "user-1", "org-1")
        self.ctx = TaskContext("run-1", "task-1", "attempt-1", 1, "user-1", "org-1",
                               "dingtalk", "dingtalk-office-query", "read")
        self.config = AgentConfig()
        # These tests isolate execution faults; approval-mode tests use production defaults.
        policy_data = ApprovalPolicy.load(self.config.root).data
        policy_data["default_modes"] = {"read": "auto", "write": "auto"}
        policy_data["tool_modes"] = {}
        policy_data["mandatory_user_tools"] = []
        self.auto_policy = ApprovalPolicy(policy_data)
        self.delays = []

        async def sleep(delay):
            self.delays.append(delay)
        self.executor = Executor(self.config, self.ledger, timeout=.05, sleep=sleep, jitter=lambda _: 0,
                                 approval_policy=self.auto_policy)

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        self.ledger.close()
        self.temp.cleanup()

    def grant(self, tool=READ, args=None, ctx=None):
        ctx = ctx or self.ctx
        return Grant("auth-1", ctx.run_id, ctx.task_id, ctx.plan_version, ctx.principal_id,
                     ctx.organization_id, tool, fingerprint(args if args is not None else {"deptId": 1}),
                     time.time() + 60, resource_key="fixture-resource")

    def write_context(self):
        return replace(self.ctx, skill_id="dingtalk-scoped-write", effect="write")

    async def execute_read(self, callback, *, args=None, grant=None, logical_key="read-1"):
        self.executor.register(READ, Binding(callback, validate_read))
        return await self.executor.execute(self.ctx, READ, args if args is not None else {"deptId": 1},
                                           grant or self.grant(), logical_key=logical_key)

    async def test_success_and_duplicate_never_dispatch_twice(self):
        called = 0

        async def tool(args):
            nonlocal called
            called += 1
            return {"isError": False, "structuredContent": {"success": True, "result": {"memberCount": 12}}}
        first = await self.execute_read(tool)
        again = await self.execute_read(tool)
        self.assertEqual(first.payload["result"]["memberCount"], 12)
        self.assertTrue(again.cached)
        self.assertIsNone(again.payload)
        self.assertEqual(called, 1)

    async def test_nested_permission_error_is_not_success_or_retried(self):
        async def tool(args):
            return {"isError": False, "structuredContent": {"result": {"success": False, "errorCode": "12063"}}}
        result = await self.execute_read(tool)
        self.assertEqual(result.code, "PERMISSION_DENIED")
        self.assertEqual(result.calls, 1)
        self.assertEqual(self.delays, [])

    async def test_read_retries_and_honors_rate_limit(self):
        called = 0

        async def tool(args):
            nonlocal called
            called += 1
            if called == 1:
                return {"success": False, "errorCode": "429", "retryAfter": 4}
            if called == 2:
                raise ConnectionError("transport secret must not be logged")
            return {"success": True}
        result = await self.execute_read(tool)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.calls, 3)
        self.assertEqual(self.delays, [4, 5])

    async def test_retry_limit_stops(self):
        async def tool(args):
            raise ConnectionError()
        result = await self.execute_read(tool)
        self.assertEqual(result.code, "TRANSPORT_ERROR")
        self.assertEqual(result.calls, 4)

    async def test_budget_is_durable_and_prevents_dispatch(self):
        self.ledger.db.execute("UPDATE runs SET max_calls=1")
        called = 0

        async def tool(args):
            nonlocal called
            called += 1
            raise ConnectionError()
        result = await self.execute_read(tool)
        self.assertEqual(result.code, "BUDGET_EXCEEDED")
        self.assertEqual(called, 1)
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.assertEqual(self.ledger.db.execute("SELECT calls_used FROM runs").fetchone()[0], 1)

    async def test_changed_arguments_rejected_before_dispatch(self):
        async def tool(args):
            self.fail("must not execute")
        result = await self.execute_read(tool, args={"deptId": 2})
        self.assertEqual(result.code, "AUTH_ARGUMENTS_CHANGED")

    async def test_invalid_schema_rejected_before_dispatch(self):
        args = {"deptId": "1"}

        async def tool(args):
            self.fail("must not execute")
        result = await self.execute_read(tool, args=args, grant=self.grant(args=args))
        self.assertEqual(result.code, "SCHEMA_VALIDATION_FAILED")
        self.assertEqual(result.calls, 0)

    async def test_identity_and_expiry_rejected(self):
        async def tool(args):
            self.fail("must not execute")
        wrong = replace(self.grant(), organization_id="other")
        self.assertEqual((await self.execute_read(tool, grant=wrong)).code, "AUTH_SCOPE_MISMATCH")
        expired = replace(self.grant(), expires_at=time.time() - 1)
        self.assertEqual((await self.execute_read(tool, grant=expired)).code, "AUTH_SCOPE_MISMATCH")

    async def test_read_skill_cannot_call_write_tool(self):
        async def tool(args):
            self.fail("must not execute")
        self.executor.register(WRITE, Binding(tool, validate_write, resource_key=lambda _: "fixture-resource"))
        result = await self.executor.execute(self.ctx, WRITE, {"name": "fixture"},
                                             self.grant(WRITE, {"name": "fixture"}), logical_key="w")
        self.assertEqual(result.code, "TOOL_DENIED")

    async def test_unknown_write_survives_restart_and_is_not_replayed(self):
        called = 0
        args = {"name": "fixture"}
        ctx = self.write_context()

        async def tool(args):
            nonlocal called
            called += 1
            await asyncio.sleep(10)
        binding = Binding(tool, validate_write, resource_key=lambda _: "fixture-resource")
        self.executor.register(WRITE, binding)
        first = await self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="create-1")
        self.assertEqual(first.code, "REMOTE_STATE_UNKNOWN")
        self.assertEqual(self.ledger.get(first.operation_id)["state"], "unknown")
        await asyncio.sleep(0)
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.executor = Executor(self.config, self.ledger, timeout=.05, approval_policy=self.auto_policy)
        self.executor.register(WRITE, binding)
        second = await self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="create-1")
        self.assertEqual(second.code, "REMOTE_STATE_UNKNOWN")
        self.assertEqual(called, 1)

    async def test_reconciliation_checks_existing_write_without_replaying(self):
        args = {"name": "fixture"}
        ctx = self.write_context()
        called = 0

        async def tool(args):
            nonlocal called
            called += 1
            raise ConnectionError()

        async def reconcile(op_id, digest):
            return Reconciliation("applied", {"documentId": "fixture-document"})
        self.executor.register(WRITE, Binding(tool, validate_write, reconcile=reconcile, resource_key=lambda _: "fixture-resource"))
        first = await self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="create-1")
        result = await self.executor.reconcile(ctx, first.operation_id, args, self.grant(WRITE, args, ctx))
        self.assertEqual(result.verification_level, "readback_verified")
        self.assertEqual(self.ledger.get(first.operation_id)["state"], "confirmed")
        self.assertEqual(called, 1)
        cached = await self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="create-1")
        self.assertEqual(cached.verification_level, "readback_verified")

    async def test_reconciliation_absence_does_not_auto_repeat_write(self):
        args = {"name": "fixture"}
        ctx = self.write_context()

        async def tool(args):
            raise ConnectionError()

        async def reconcile(op_id, digest):
            return Reconciliation("not_applied")
        self.executor.register(WRITE, Binding(tool, validate_write, reconcile=reconcile, resource_key=lambda _: "fixture-resource"))
        grant = self.grant(WRITE, args, ctx)
        first = await self.executor.execute(ctx, WRITE, args, grant, logical_key="create-1")
        result = await self.executor.reconcile(ctx, first.operation_id, args, grant)
        self.assertEqual(result.code, "VERIFIED_NOT_APPLIED")
        self.assertEqual((await self.executor.execute(ctx, WRITE, args, grant, logical_key="create-1")).code,
                         "VERIFIED_NOT_APPLIED")

    async def test_cancellation_preserves_uncertain_write(self):
        entered = asyncio.Event()
        args = {"name": "fixture"}
        ctx = self.write_context()

        async def tool(args):
            entered.set()
            await asyncio.sleep(10)
        self.executor.register(WRITE, Binding(tool, validate_write, resource_key=lambda _: "fixture-resource"))
        pending = asyncio.create_task(self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="w"))
        await entered.wait()
        self.executor.cancel_run(ctx.run_id)
        result = await pending
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.ledger.get(result.operation_id)["state"], "unknown")

    async def test_old_plan_late_result_cannot_advance_task(self):
        async def tool(args):
            self.ledger.advance_plan("run-1", 1)
            return {"success": True}
        result = await self.execute_read(tool)
        self.assertEqual(result.code, "STALE_PLAN")
        self.assertEqual(self.ledger.get(result.operation_id)["state"], "confirmed")

    async def test_duplicate_inflight_operation_is_excluded(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def tool(args):
            entered.set()
            await release.wait()
            return {"success": True}
        self.executor.register(READ, Binding(tool, validate_read))
        first = asyncio.create_task(self.executor.execute(self.ctx, READ, {"deptId": 1}, self.grant(), logical_key="r"))
        await entered.wait()
        second = await self.executor.execute(self.ctx, READ, {"deptId": 1}, self.grant(), logical_key="r")
        self.assertEqual(second.code, "OPERATION_IN_PROGRESS")
        release.set()
        self.assertEqual((await first).status, "succeeded")

    async def test_same_key_changed_payload_does_not_create_second_operation(self):
        async def tool(args):
            return {"success": True}
        await self.execute_read(tool)
        args = {"deptId": 2}
        result = await self.execute_read(tool, args=args, grant=self.grant(args=args))
        self.assertEqual(result.code, "IDEMPOTENCY_CONFLICT")

    async def test_writes_to_same_target_are_serialized_across_actions(self):
        entered, release = asyncio.Event(), asyncio.Event()
        ctx = self.write_context()
        calls = 0

        async def tool(args):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            return {"success": True}
        self.executor.register(WRITE, Binding(tool, validate_write, resource_key=lambda _: "fixture-resource"))
        args = {"name": "first"}
        first = asyncio.create_task(self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="w-1"))
        await entered.wait()
        other_args = {"name": "second"}
        busy = await self.executor.execute(ctx, WRITE, other_args, self.grant(WRITE, other_args, ctx), logical_key="w-2")
        self.assertEqual(busy.code, "RESOURCE_BUSY")
        self.assertEqual(calls, 1)
        release.set()
        await first
        retried = await self.executor.execute(ctx, WRITE, other_args, self.grant(WRITE, other_args, ctx), logical_key="w-2")
        self.assertEqual(retried.status, "succeeded")
        self.assertEqual(calls, 2)

    async def test_unknown_write_keeps_target_locked_for_other_actions(self):
        ctx, args = self.write_context(), {"name": "fixture"}

        async def tool(args):
            raise ConnectionError()
        self.executor.register(WRITE, Binding(tool, validate_write, resource_key=lambda _: "fixture-resource"))
        first = await self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="w-1")
        self.assertEqual(first.code, "REMOTE_STATE_UNKNOWN")
        other = await self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="w-2")
        self.assertEqual(other.code, "RESOURCE_BUSY")

    async def test_write_budget_denial_is_not_unknown_remote_state(self):
        self.ledger.db.execute("UPDATE runs SET calls_used=max_calls")
        ctx, args = self.write_context(), {"name": "fixture"}

        async def tool(args):
            self.fail("budget denial must happen before dispatch")
        self.executor.register(WRITE, Binding(tool, validate_write, resource_key=lambda _: "fixture-resource"))
        result = await self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="w")
        self.assertEqual(result.code, "BUDGET_EXCEEDED")
        self.assertEqual(self.ledger.get(result.operation_id)["state"], "rejected")

    async def test_wrong_target_grant_cannot_acquire_resource(self):
        ctx, args = self.write_context(), {"name": "fixture"}

        async def tool(args):
            self.fail("target grant mismatch must not execute")
        self.executor.register(WRITE, Binding(tool, validate_write, resource_key=lambda _: "real-target"))
        result = await self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="w")
        self.assertEqual(result.code, "RESOURCE_SCOPE_MISMATCH")

    async def test_reconciliation_lease_fences_old_worker(self):
        ctx, args = self.write_context(), {"name": "fixture"}

        async def tool(args):
            raise ConnectionError()

        async def reconcile(op_id, digest):
            return Reconciliation("applied", {"id": "fixture"})
        self.executor.register(WRITE, Binding(tool, validate_write, reconcile=reconcile, resource_key=lambda _: "fixture-resource"))
        result = await self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="w")
        old_token = self.ledger.get(result.operation_id)["lease_token"]
        await self.executor.reconcile(ctx, result.operation_id, args, self.grant(WRITE, args, ctx))
        self.assertFalse(self.ledger.finish(result.operation_id, old_token, "rejected", "STALE_RESULT"))
        self.assertEqual(self.ledger.get(result.operation_id)["state"], "confirmed")

    async def test_expired_run_never_dispatches(self):
        self.ledger.db.execute("UPDATE runs SET deadline=?", (time.time() - 1,))

        async def tool(args):
            self.fail("expired run must not execute")
        result = await self.execute_read(tool)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.code, "DEADLINE_EXCEEDED")

    async def test_cancelled_write_can_be_reconciled_by_explicit_maintenance_grant(self):
        ctx, args = self.write_context(), {"name": "fixture"}

        async def tool(args):
            raise ConnectionError()

        async def reconcile(op_id, digest):
            return Reconciliation("applied", {"id": "fixture"})
        self.executor.register(WRITE, Binding(tool, validate_write, reconcile=reconcile, resource_key=lambda _: "fixture-resource"))
        original = await self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="w")
        self.executor.cancel_run(ctx.run_id)
        self.ledger.create_run("maintenance-1", "user-1", "org-1")
        maintenance_ctx = replace(ctx, run_id="maintenance-1", task_id="reconcile-1")
        grant = self.grant(WRITE, args, maintenance_ctx)
        denied = await self.executor.reconcile(maintenance_ctx, original.operation_id, args, grant, maintenance=True)
        self.assertEqual(denied.code, "AUTH_SCOPE_MISMATCH")
        grant = replace(grant, reconciliation_operation_id=original.operation_id)
        result = await self.executor.reconcile(maintenance_ctx, original.operation_id, args, grant, maintenance=True)
        self.assertEqual(result.code, "RECONCILED")
        self.assertEqual(self.ledger.guard(ctx), "CANCELLED")

    async def test_cancel_during_backoff_does_not_leave_pending_read(self):
        entered = asyncio.Event()

        async def sleep(delay):
            entered.set()
            await asyncio.sleep(10)

        async def tool(args):
            raise ConnectionError()
        self.executor.sleep = sleep
        self.executor.register(READ, Binding(tool, validate_read))
        pending = asyncio.create_task(self.executor.execute(self.ctx, READ, {"deptId": 1}, self.grant(), logical_key="r"))
        await entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.ledger.db.execute("SELECT state FROM operations").fetchone()[0], "rejected")

    async def test_deadline_during_call_cancels_read(self):
        self.ledger.db.execute("UPDATE runs SET deadline=?", (time.time() + .02,))

        async def tool(args):
            await asyncio.sleep(10)
        result = await self.execute_read(tool)
        self.assertEqual(result.code, "DEADLINE_EXCEEDED")
        self.assertEqual(result.status, "cancelled")

    async def test_reconciliation_cannot_bypass_budget(self):
        ctx, args = self.write_context(), {"name": "fixture"}
        reconciled = 0

        async def tool(args):
            raise ConnectionError()

        async def reconcile(op_id, digest):
            nonlocal reconciled
            reconciled += 1
            return Reconciliation("applied", {"id": "fixture"})
        self.executor.register(WRITE, Binding(tool, validate_write, reconcile=reconcile, resource_key=lambda _: "fixture-resource"))
        result = await self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="w")
        self.ledger.db.execute("UPDATE runs SET calls_used=max_calls")
        checked = await self.executor.reconcile(ctx, result.operation_id, args, self.grant(WRITE, args, ctx))
        self.assertEqual(checked.code, "BUDGET_EXCEEDED")
        self.assertEqual(checked.calls, 0)
        self.assertEqual(reconciled, 0)
        self.assertEqual(self.ledger.get(result.operation_id)["state"], "unknown")

    async def test_receipt_failure_rolls_back_confirmation(self):
        self.ledger.db.execute("CREATE TABLE receipt_fixture(value TEXT)")
        ctx, args = self.write_context(), {"name": "fixture"}

        async def tool(args):
            return {"success": True}

        def sink(operation_id, payload):
            self.ledger.db.execute("INSERT INTO receipt_fixture VALUES('partial')")
            raise ValueError("Simulated artifact failure")
        self.executor.register(WRITE, Binding(tool, validate_write, resource_key=lambda _: "fixture-resource"))
        result = await self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="w", receipt_sink=sink)
        self.assertEqual(result.code, "REMOTE_STATE_UNKNOWN")
        self.assertEqual(self.ledger.get(result.operation_id)["state"], "unknown")
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM receipt_fixture").fetchone()[0], 0)

    async def test_database_does_not_contain_arguments_or_provider_secrets(self):
        ctx, args = self.write_context(), {"name": "secret-content-98765"}

        async def tool(args):
            raise RuntimeError("access_token=private-token-12345")
        self.executor.register(WRITE, Binding(tool, validate_write, resource_key=lambda _: "fixture-resource"))
        result = await self.executor.execute(ctx, WRITE, args, self.grant(WRITE, args, ctx), logical_key="w")
        dump = "\n".join(self.ledger.db.iterdump())
        self.assertNotIn(args["name"], dump)
        self.assertNotIn("private-token-12345", dump)
        self.assertEqual(result.code, "REMOTE_STATE_UNKNOWN")


class ResponseTests(unittest.TestCase):
    def test_text_mcp_business_error_is_detected(self):
        response = {"isError": False, "content": [{"type": "text", "text": json.dumps({"result": {"success": False}})}]}
        with self.assertRaises(ToolFault):
            classify_response(response)

    def test_business_record_success_field_is_not_treated_as_envelope(self):
        response = {"success": True, "data": {"records": [{"success": False}]}}
        self.assertEqual(classify_response(response), response)

    def test_unstructured_success_text_is_not_accepted(self):
        with self.assertRaises(ToolFault):
            classify_response({"content": [{"type": "text", "text": "looks successful"}]})


if __name__ == "__main__":
    unittest.main()
