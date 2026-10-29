"""End-to-end durable workflow tests with fake agents, tools and authenticated actors."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from agent_runtime.approvals import AIReview
from agent_runtime.executor import Binding, Executor, Grant, Ledger, Reconciliation, fingerprint
from agent_runtime.scheduler import Complete, NeedInput, Scheduler, ToolRequest, Validation
from scripts.agents import AgentConfig

READ = "mcp__dingtalk_contact__get_dept_info_by_dept_id"
SEND = "mcp__dingtalk_mail__send_email"
DOC = "mcp__dingtalk_doc__create_document"
OWNER = {"principal_id": "user", "organization_id": "org"}


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "workflow.sqlite3"
        self.config = AgentConfig()
        self.reads, self.writes, self.agent_steps = 0, 0, []
        self.remote = {}
        self.build()

    def build(self, *, lose_write_response=False):
        self.ledger = Ledger(self.path)
        self.executor = Executor(self.config, self.ledger, timeout=.1, read_retries=0)

        async def read(args):
            self.reads += 1
            return {"success": True, "result": {"memberCount": 12}}

        async def write(args):
            self.writes += 1
            self.remote["id"] = "fixture-object"
            if lose_write_response:
                raise ConnectionError()
            return {"success": True, "id": self.remote["id"]}

        async def reconcile(operation_id, digest):
            return Reconciliation("applied", {"success": True, "id": self.remote["id"]})

        def read_schema(args):
            if set(args) != {"deptId"} or type(args["deptId"]) is not int:
                raise ValueError("Invalid department")

        def write_schema(args):
            if set(args) != {"value"}:
                raise ValueError("Invalid simulated schema")

        self.executor.register(READ, Binding(read, read_schema))
        for name in (SEND, DOC):
            self.executor.register(name, Binding(write, write_schema, reconcile=reconcile, resource_key=lambda _: "fixture-target"))

        async def issuer(ctx, tool, args, key):
            # Test-only issuer. Production must resolve task scope from authenticated policy.
            deadline = self.ledger.db.execute("SELECT deadline FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()[0]
            return Grant("grant:" + key, ctx.run_id, ctx.task_id, ctx.plan_version, ctx.principal_id,
                         ctx.organization_id, tool, fingerprint(args), deadline, resource_key="fixture-target")

        async def reviewer(packet):
            return AIReview(packet["approval_id"], packet["action_digest"], packet["policy_digest"], "approve", "low", .99, ("LOW_RISK",))

        self.scheduler = Scheduler(self.config, self.executor, grant_issuer=issuer, ai_reviewer=reviewer,
                                   handler_timeout=.5, lease_seconds=1)
        self.scheduler.register_agent("orchestrator", self.orchestrator)
        self.scheduler.register_agent("dingtalk", self.dingtalk)
        self.scheduler.register_validator("office-query", self.validate_query)
        self.scheduler.register_validator("office-action", self.validate_action)

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        self.ledger.close()
        self.folder.cleanup()

    async def orchestrator(self, job):
        self.agent_steps.append(job.step_id)
        if job.step_id == "deliver":
            return Complete({"answer": next(iter(job.dependencies.values()))["output"], "query": job.inputs.get("query")})
        return Complete({"goal": job.inputs.get("query", job.inputs.get("action"))})

    async def dingtalk(self, job):
        self.agent_steps.append(job.step_id)
        if not job.history:
            if job.step_id == "query":
                return ToolRequest("lookup", READ, {"deptId": job.inputs.get("department_id", 1)})
            return ToolRequest("perform", DOC if job.inputs["action"] == "document" else SEND, {"value": "fixture"})
        return Complete({"data": job.history[-1]["output"], "evidence": job.history[-1]["artifact_id"]})

    async def validate_query(self, inputs, outputs):
        return Validation(outputs["query"]["data"]["result"]["memberCount"] == 12)

    async def validate_action(self, inputs, outputs):
        return Validation(outputs["act"]["data"]["id"] == "fixture-object")

    def submit(self, workflow="office-query", inputs=None, **limits):
        if inputs is None:
            inputs = {"query": "人数"} if workflow == "office-query" else {"action": "send", "target_refs": ["fixture-target"], "task_authorization": "test-reference-only"}
        return self.scheduler.submit(workflow, inputs, **OWNER, source="test", event_id="event-1", **limits)

    async def drive(self, run_id, **kwargs):
        return await self.scheduler.drive(run_id, **OWNER, **kwargs)

    def approve(self, status):
        approval_id = next(s["approval_id"] for s in status["steps"] if s["state"] == "waiting_authorization")
        row = self.executor.approvals._row(approval_id)
        self.executor.approvals.user_decide(approval_id, actor_user_id="user", actor_organization_id="org",
                                          expected_action_digest=row["action_digest"], approve=True)

    async def test_full_query_chain_validates_and_delivers_once(self):
        run_id = self.submit()
        result = await self.drive(run_id)
        self.assertEqual(result["state"], "syncing")
        self.assertTrue(all(step["state"] == "succeeded" for step in result["steps"]))
        self.assertEqual(self.reads, 1)
        events = self.scheduler.pending_deliveries(**OWNER)
        self.assertEqual(len(events), 1)
        self.assertIn("answer", self.scheduler.artifact(result["final_artifact"], **OWNER))
        self.scheduler.acknowledge_delivery(events[0]["event_id"], **OWNER)
        self.scheduler.acknowledge_delivery(events[0]["event_id"], **OWNER)
        self.assertEqual((await self.drive(run_id))["state"], "delivered")
        self.assertEqual(self.reads, 1)
        self.assertEqual(self.scheduler.pending_deliveries(**OWNER), [])

    async def test_user_approval_survives_restart_and_resumes_same_action(self):
        run_id = self.submit("office-action")
        waiting = await self.drive(run_id)
        self.assertEqual(waiting["state"], "waiting_authorization")
        self.assertEqual(self.writes, 0)
        self.approve(waiting)
        self.ledger.close()
        self.build()
        result = await self.drive(run_id)
        self.assertEqual(result["state"], "syncing")
        self.assertEqual(self.writes, 1)

    async def test_ai_review_and_write_loop_automatically_complete(self):
        run_id = self.submit("office-action", {"action": "document", "target_refs": ["fixture-target"], "task_authorization": "test-reference-only"})
        result = await self.drive(run_id)
        self.assertEqual(result["state"], "syncing")
        self.assertEqual(self.writes, 1)
        self.assertEqual(self.ledger.db.execute("SELECT code FROM approvals WHERE mode='ai'").fetchone()[0], "AI_APPROVED")

    async def test_duplicate_event_is_idempotent_and_conflicting_event_rejected(self):
        first = self.submit()
        self.assertEqual(first, self.submit())
        with self.assertRaisesRegex(ValueError, "EVENT_CONFLICT"):
            self.submit(inputs={"query": "different"})

    async def test_missing_inputs_wait_and_only_requested_fields_can_be_supplied(self):
        run_id = self.submit(inputs={})
        status = await self.drive(run_id)
        self.assertEqual(status["state"], "waiting_input")
        self.assertEqual(self.agent_steps, [])
        with self.assertRaises(ValueError):
            self.scheduler.provide_inputs(run_id, {"other": 1}, **OWNER, expected_input_version=1)
        self.scheduler.provide_inputs(run_id, {"query": "人数"}, **OWNER, expected_input_version=1)
        self.assertEqual((await self.drive(run_id))["state"], "syncing")
        with self.assertRaises(ValueError):
            self.scheduler.provide_inputs(run_id, {"query": "changed"}, **OWNER, expected_input_version=1)

    async def test_agent_can_request_missing_business_information(self):
        async def planner(job):
            if job.step_id == "plan" and "department_id" not in job.inputs:
                return NeedInput(("department_id",), "哪个部门？")
            return await self.orchestrator(job)
        self.scheduler.register_agent("orchestrator", planner)
        run_id = self.submit()
        self.assertEqual((await self.drive(run_id))["state"], "waiting_input")
        self.scheduler.provide_inputs(run_id, {"department_id": 1}, **OWNER, expected_input_version=1)
        self.assertEqual((await self.drive(run_id))["state"], "syncing")

    async def test_crash_after_remote_confirmation_uses_persisted_receipt(self):
        run_id = self.submit("office-action")
        self.approve(await self.drive(run_id))
        await self.drive(run_id, max_ticks=1)
        self.assertEqual(self.writes, 1)
        # Simulate loss of the scheduler bookmark after the atomic tool confirmation/receipt commit.
        self.ledger.db.execute("UPDATE wf_actions SET state='pending',operation_id=NULL,artifact_id=NULL WHERE run_id=?", (run_id,))
        self.ledger.close()
        self.build()
        self.assertEqual((await self.drive(run_id))["state"], "syncing")
        self.assertEqual(self.writes, 1)

    async def test_unknown_write_is_reconciled_without_repeating_it(self):
        self.ledger.close()
        self.build(lose_write_response=True)
        run_id = self.submit("office-action")
        self.approve(await self.drive(run_id))
        result = await self.drive(run_id)
        self.assertEqual(result["code"], "REMOTE_STATE_UNKNOWN")
        self.scheduler.resume(run_id, **OWNER)
        result = await self.drive(run_id)
        self.assertEqual(result["state"], "syncing")
        self.assertEqual(self.writes, 1)

    async def test_fake_success_without_tool_evidence_cannot_deliver(self):
        async def fake(job):
            return Complete({"data": {"result": {"memberCount": 12}}})
        self.scheduler.register_agent("dingtalk", fake)
        result = await self.drive(self.submit())
        self.assertEqual(result["code"], "EVIDENCE_MISSING")
        self.assertEqual(self.scheduler.pending_deliveries(**OWNER), [])

    async def test_read_only_rework_is_bounded_and_requeries(self):
        validations = 0

        async def validator(inputs, outputs):
            nonlocal validations
            validations += 1
            return Validation(validations > 1, rework_step="query")
        self.scheduler.register_validator("office-query", validator)
        result = await self.drive(self.submit())
        self.assertEqual(result["state"], "syncing")
        self.assertEqual(self.reads, 2)
        self.assertEqual(next(s["generation"] for s in result["steps"] if s["step_id"] == "query"), 2)

    async def test_rework_limit_stops_loop(self):
        async def validator(inputs, outputs):
            return Validation(False, rework_step="query")
        self.scheduler.register_validator("office-query", validator)
        result = await self.drive(self.submit(max_rework=1))
        self.assertEqual(result["code"], "REWORK_LIMIT")
        self.assertEqual(self.reads, 2)

    async def test_cancel_during_agent_call_cannot_publish_late_output(self):
        entered = asyncio.Event()

        async def planner(job):
            entered.set()
            await asyncio.sleep(10)
            return Complete({"late": True})
        self.scheduler.register_agent("orchestrator", planner)
        run_id = self.submit()
        pending = asyncio.create_task(self.drive(run_id))
        await entered.wait()
        self.scheduler.cancel(run_id, **OWNER)
        self.assertEqual((await pending)["state"], "cancelled")
        self.assertEqual(self.scheduler.pending_deliveries(**OWNER), [])

    async def test_agent_budget_is_not_reset_by_restart(self):
        run_id = self.submit(max_agent_calls=1)
        result = await self.drive(run_id)
        self.assertEqual(result["code"], "AGENT_BUDGET_EXCEEDED")
        self.ledger.close()
        self.build()
        self.assertEqual(self.scheduler.status(run_id, **OWNER)["agent_calls"], 1)

    async def test_cross_user_cannot_read_artifacts_or_resume_workflow(self):
        run_id = self.submit()
        result = await self.drive(run_id)
        other = {"principal_id": "other", "organization_id": "org"}
        with self.assertRaises(PermissionError):
            self.scheduler.artifact(result["final_artifact"], **other)
        with self.assertRaises(PermissionError):
            await self.scheduler.drive(run_id, **other)

    async def test_plan_revision_invalidates_old_steps_and_preserves_budget(self):
        run_id = self.submit()
        await self.drive(run_id, max_ticks=1)
        before = self.scheduler.status(run_id, **OWNER)["agent_calls"]
        self.scheduler.revise(run_id, {"query": "new query"}, **OWNER, expected_plan_version=1)
        result = await self.drive(run_id)
        self.assertEqual(result["plan_version"], 2)
        self.assertGreater(result["agent_calls"], before)
        output = self.scheduler.artifact(result["final_artifact"], **OWNER)
        self.assertEqual(output["query"], "new query")

    async def test_missing_validator_blocks_delivery(self):
        self.scheduler.validators.clear()
        result = await self.drive(self.submit())
        self.assertEqual(result["code"], "VALIDATOR_UNAVAILABLE")
        self.assertEqual(self.scheduler.pending_deliveries(**OWNER), [])

    async def test_long_ai_wait_renews_step_lease_and_excludes_other_driver(self):
        entered, release = asyncio.Event(), asyncio.Event()
        self.scheduler.handler_timeout, self.scheduler.lease_seconds = .1, .3

        async def reviewer(packet):
            entered.set()
            await release.wait()
            return AIReview(packet["approval_id"], packet["action_digest"], packet["policy_digest"], "approve", "low", .99, ("LOW_RISK",))
        self.scheduler.ai_reviewer = reviewer
        run_id = self.submit("office-action", {"action": "document", "target_refs": ["fixture-target"], "task_authorization": "reference"})
        pending = asyncio.create_task(self.drive(run_id))
        await entered.wait()
        await asyncio.sleep(.4)
        second = await self.drive(run_id, max_ticks=1)
        self.assertEqual(second["state"], "executing")
        self.assertEqual(self.writes, 0)
        release.set()
        self.assertEqual((await pending)["state"], "syncing")
        self.assertEqual(self.writes, 1)

    async def test_final_validation_is_exclusive_between_drivers(self):
        entered, release = asyncio.Event(), asyncio.Event()
        validations = 0

        async def validator(inputs, outputs):
            nonlocal validations
            validations += 1
            entered.set()
            await release.wait()
            return Validation(True)
        self.scheduler.register_validator("office-query", validator)
        run_id = self.submit()
        pending = asyncio.create_task(self.drive(run_id))
        await entered.wait()
        second = await self.drive(run_id, max_ticks=1)
        self.assertEqual(second["state"], "verifying")
        self.assertEqual(validations, 1)
        release.set()
        self.assertEqual((await pending)["state"], "syncing")
        self.assertEqual(len(self.scheduler.pending_deliveries(**OWNER)), 1)

    async def test_background_worker_resumes_approved_task_without_manual_drive(self):
        run_id = self.submit("office-action")
        stop = asyncio.Event()
        worker = asyncio.create_task(self.scheduler.serve(stop, poll_interval=.01))
        try:
            async def wait_state(expected):
                async with asyncio.timeout(3):
                    while self.scheduler.status(run_id, **OWNER)["state"] != expected:
                        await asyncio.sleep(.01)
            await wait_state("waiting_authorization")
            self.approve(self.scheduler.status(run_id, **OWNER))
            await wait_state("syncing")
            self.assertEqual(self.writes, 1)
        finally:
            stop.set()
            await worker

    async def test_delivered_run_cannot_be_relabelled_cancelled(self):
        run_id = self.submit()
        await self.drive(run_id)
        event = self.scheduler.pending_deliveries(**OWNER)[0]
        self.scheduler.acknowledge_delivery(event["event_id"], **OWNER)
        with self.assertRaisesRegex(ValueError, "cannot be cancelled"):
            self.scheduler.cancel(run_id, **OWNER)
        self.assertEqual(self.scheduler.status(run_id, **OWNER)["state"], "delivered")
