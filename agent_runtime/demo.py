"""Offline timeout-and-reconciliation walkthrough using simulated remote state."""

import asyncio
import json
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from agent_runtime.executor import Binding, Executor, Grant, Ledger, Reconciliation, TaskContext, fingerprint
from scripts.agents import AgentConfig
from agent_runtime.approvals import AIReview


async def main():
    with tempfile.TemporaryDirectory() as folder:
        ledger = Ledger(Path(folder) / "demo.sqlite3")
        try:
            ledger.create_run("demo-run", "demo-user", "demo-org", max_calls=5)
            ctx = TaskContext("demo-run", "demo-task", "demo-attempt", 1, "demo-user", "demo-org",
                              "dingtalk", "dingtalk-scoped-write", "write")
            name = "mcp__dingtalk_doc__create_document"
            arguments = {"name": "离线演示文档"}
            grant = Grant("demo-auth", ctx.run_id, ctx.task_id, 1, ctx.principal_id, ctx.organization_id,
                          name, fingerprint(arguments), time.time() + 60, resource_key="demo:document-folder")
            remote = {}
            calls = 0

            def validate(args):
                if set(args) != {"name"} or not isinstance(args["name"], str):
                    raise ValueError("invalid simulated schema")

            async def create(args):
                nonlocal calls
                calls += 1
                remote["documentId"] = "demo-document"
                raise TimeoutError("simulated response loss after remote creation")

            async def reconcile(operation_id, digest):
                return Reconciliation("applied", dict(remote)) if remote else Reconciliation("unknown")

            executor = Executor(AgentConfig(), ledger)
            executor.register(name, Binding(create, validate, reconcile=reconcile,
                                            resource_key=lambda _: "demo:document-folder"))
            requested = executor.prepare_approval(ctx, name, arguments, grant, logical_key="demo:create-document")
            preview = executor.approval_preview(requested.approval_id, ctx, name, arguments, grant,
                                                logical_key="demo:create-document")

            async def simulated_reviewer(packet):
                return AIReview(packet["approval_id"], packet["action_digest"], packet["policy_digest"],
                                "approve", "low", .99, ("LOW_RISK",))
            await executor.approvals.review_with_ai(requested.approval_id, preview, simulated_reviewer)
            first = await executor.execute(ctx, name, arguments, grant, logical_key="demo:create-document")
            repeated = await executor.execute(ctx, name, arguments, grant, logical_key="demo:create-document")
            checked = await executor.reconcile(ctx, first.operation_id, arguments, grant)
            cached = await executor.execute(ctx, name, arguments, grant, logical_key="demo:create-document")
            print(json.dumps({"mode": "offline_simulation", "real_business_calls": 0,
                              "ai_review": "simulated_not_real_model",
                              "simulated_create_calls": calls,
                              "outcomes": [asdict(x) for x in (first, repeated, checked, cached)]},
                             ensure_ascii=False, indent=2))
        finally:
            ledger.close()


if __name__ == "__main__":
    asyncio.run(main())
