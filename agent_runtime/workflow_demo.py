"""Offline end-to-end query and user-approved action, including a process restart."""

import asyncio
import json
import tempfile
from pathlib import Path

from agent_runtime.executor import Binding, Executor, Grant, Ledger, fingerprint
from agent_runtime.scheduler import Complete, Scheduler, ToolRequest, Validation
from scripts.agents import AgentConfig

READ = "mcp__dingtalk_contacts__get_dept_info_by_dept_id"
SEND = "mcp__dingtalk_mail__send_email"
OWNER = {"principal_id": "demo-user", "organization_id": "demo-org"}


async def main():
    config = AgentConfig()
    calls = {"read": 0, "write": 0}

    def build(path):
        ledger = Ledger(path)
        executor = Executor(config, ledger)

        async def read(args):
            calls["read"] += 1
            return {"success": True, "memberCount": 12}

        async def send(args):
            calls["write"] += 1
            return {"success": True, "messageId": "simulated-message"}

        def validate(args):
            if not isinstance(args, dict):
                raise ValueError("Invalid simulated parameters")

        executor.register(READ, Binding(read, validate))
        executor.register(SEND, Binding(send, validate, resource_key=lambda _: "demo-mailbox"))

        async def issuer(ctx, tool, args, key):
            # Demo only: a real issuer must validate authenticated task/resource policy.
            deadline = ledger.db.execute("SELECT deadline FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()[0]
            return Grant("demo-grant:" + key, ctx.run_id, ctx.task_id, ctx.plan_version,
                         ctx.principal_id, ctx.organization_id, tool, fingerprint(args), deadline,
                         resource_key="demo-mailbox")

        async def orchestrator(job):
            if job.step_id == "deliver":
                return Complete({"result": next(iter(job.dependencies.values()))["output"]})
            return Complete({"goal": job.inputs.get("query", job.inputs.get("action"))})

        async def dingtalk(job):
            if not job.history:
                return ToolRequest("operation", READ if job.step_id == "query" else SEND,
                                   {"deptId": 1} if job.step_id == "query" else {"subject": "模拟发送"})
            return Complete({"data": job.history[-1]["output"], "source": job.history[-1]["artifact_id"]})

        async def query_validator(inputs, outputs):
            return Validation(outputs["query"]["data"]["memberCount"] == 12)

        async def action_validator(inputs, outputs):
            return Validation(outputs["act"]["data"]["messageId"] == "simulated-message")

        scheduler = Scheduler(config, executor, grant_issuer=issuer)
        scheduler.register_agent("orchestrator", orchestrator)
        scheduler.register_agent("dingtalk", dingtalk)
        scheduler.register_validator("office-query", query_validator)
        scheduler.register_validator("office-action", action_validator)
        return ledger, executor, scheduler

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "workflow.sqlite3"
        ledger, executor, scheduler = build(path)
        try:
            query = scheduler.submit("office-query", {"query": "通讯录人数"}, **OWNER, source="demo", event_id="query")
            result = await scheduler.drive(query, **OWNER)
            query_output = scheduler.artifact(result["final_artifact"], **OWNER)
            scheduler.acknowledge_delivery(scheduler.pending_deliveries(**OWNER)[0]["event_id"], **OWNER)
            action = scheduler.submit("office-action", {"action": "send", "target_refs": ["demo-mailbox"],
                "task_authorization": "simulation-only"}, **OWNER, source="demo", event_id="action")
            before = await scheduler.drive(action, **OWNER)
            approval_id = next(step["approval_id"] for step in before["steps"] if step["state"] == "waiting_authorization")
            ledger.close()
            ledger, executor, scheduler = build(path)
            approval = executor.approvals._row(approval_id)
            # Simulated authenticated user decision; no actual UI or external message.
            executor.approvals.user_decide(approval_id, actor_user_id="demo-user", actor_organization_id="demo-org",
                                          expected_action_digest=approval["action_digest"], approve=True)
            after = await scheduler.drive(action, **OWNER)
            scheduler.acknowledge_delivery(scheduler.pending_deliveries(**OWNER)[0]["event_id"], **OWNER)
            print(json.dumps({"mode": "offline_simulation", "real_business_calls": 0,
                "simulated_user_approval": True, "simulated_calls": calls,
                "query_result": query_output, "action_before_restart": before["state"],
                "action_after_resume": after["state"], "final_state": scheduler.status(action, **OWNER)["state"]},
                ensure_ascii=False, indent=2))
        finally:
            ledger.close()


if __name__ == "__main__":
    asyncio.run(main())
