"""Exercise role isolation, task effects and workflow activation without network calls."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("agent_config", Path(__file__).resolve().parents[1] / "scripts/agents.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AgentConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = MODULE.AgentConfig()

    def test_script_cli_imports_runtime_from_outside_project(self):
        script = Path(__file__).resolve().parents[1] / "scripts/agents.py"
        with tempfile.TemporaryDirectory() as folder:
            completed = subprocess.run([sys.executable, str(script), "validate"], cwd=folder,
                                       capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["valid"])

    def test_read_query_never_receives_write_tools(self):
        result = self.config.assemble("dingtalk", "dingtalk-office-query")
        self.assertTrue(result["candidate_tools"])
        self.assertEqual({t["effect"] for t in result["candidate_tools"]}, {"read"})
        self.assertFalse(any("teambition" in t["name"] for t in result["candidate_tools"]))

    def test_write_skill_is_read_only_until_write_effect_selected(self):
        result = self.config.assemble("dingtalk", "dingtalk-scoped-write")
        self.assertTrue(all(t["effect"] == "read" for t in result["candidate_tools"]))
        write_result = self.config.assemble("dingtalk", "dingtalk-scoped-write", "write")
        self.assertTrue(any(t["effect"] == "write" for t in write_result["candidate_tools"]))
        with self.assertRaises(MODULE.ConfigError):
            self.config.assemble("dingtalk", "dingtalk-office-query", "write")

    def test_gitlab_is_enabled_and_writes_require_user_approval(self):
        read = self.config.assemble("gitlab", "gitlab-mr-delivery")
        self.assertTrue(read["candidate_tools"])
        self.assertTrue(all(t["effect"] == "read" for t in read["candidate_tools"]))
        write = self.config.assemble("gitlab", "gitlab-mr-delivery", "write")
        writes = [t for t in write["candidate_tools"] if t["effect"] == "write"]
        self.assertEqual({t["name"] for t in writes}, {
            "mcp__gitlab__create_merge_request", "mcp__gitlab__update_merge_request"})
        self.assertTrue(all(t["approval_mode"] == "user" for t in writes))
        plan = self.config.plan("gitlab-mr-delivery")
        steps = {s["id"]: s for s in plan["steps"]}
        self.assertIn("review", steps["mr"]["depends_on"])
        self.assertEqual(steps["mr"]["requires_tool_effect"], "write")
        self.assertEqual(steps["verify"]["requires_tool_effect"], "read")
        self.assertFalse(plan["executed"])

    def test_gitlab_branch_and_mr_skills_have_separate_write_tools(self):
        for skill, expected in (
            ("gitlab-create-branch", {"mcp__gitlab__create_branch"}),
            ("gitlab-submit-mr", {"mcp__gitlab__create_merge_request", "mcp__gitlab__update_merge_request"}),
        ):
            read = self.config.assemble("gitlab", skill)
            self.assertTrue(all(t["effect"] == "read" for t in read["candidate_tools"]))
            write = self.config.assemble("gitlab", skill, "write")
            writes = [t for t in write["candidate_tools"] if t["effect"] == "write"]
            self.assertEqual({t["name"] for t in writes}, expected)
            self.assertTrue(all(t["approval_mode"] == "user" for t in writes))

    def test_skill_allowlist_cannot_borrow_another_roles_tool(self):
        self.config.agents["gitlab"]["skills"][0]["tools"].append(
            "mcp__teambition__teambition_task_create")
        with self.assertRaises(MODULE.ConfigError):
            self.config.validate()

    def test_disabled_gitlab_still_cannot_be_assembled(self):
        self.config.agents["gitlab"]["enabled"] = False
        with self.assertRaises(MODULE.ConfigError):
            self.config.assemble("gitlab", "gitlab-mr-delivery")

    def test_teambition_generic_update_cannot_inherit_ai_approval(self):
        assembled = self.config.assemble("teambition", "teambition-task-sync", "write")
        tools = {t["name"]: t for t in assembled["candidate_tools"]}
        update = "mcp__teambition__teambition_task_update"
        self.assertEqual(tools[update]["approval_mode"], "user")
        self.assertIn("mcp__teambition__teambition_docs_get", tools)
        self.config.approval_policy.data["tool_modes"][update] = "ai"
        self.assertEqual(self.config.approval_policy.mode(update, "write", ()), "user")
        self.assertTrue(all(n.startswith("mcp__teambition__") for n in tools))

    def test_teambition_cannot_load_dingtalk_services(self):
        skill = self.config.agents["teambition"]["skills"][0]
        skill["services"].append("dingtalk-contact")
        with self.assertRaises(MODULE.ConfigError):
            self.config.validate()

    def test_dependency_cycle_or_missing_node_is_rejected(self):
        workflow = self.config.workflows["office-query"]
        workflow["steps"][0]["depends_on"] = [workflow["steps"][-1]["id"]]
        with self.assertRaises(MODULE.ConfigError):
            self.config.validate()

    def test_unlisted_wildcard_tools_are_rejected(self):
        service = self.config.services["dingtalk-contact"]
        service["tools"].append({"name": "mcp__dingtalk_contacts__*", "effect": "read"})
        with self.assertRaises(MODULE.ConfigError):
            self.config.validate()

    def test_local_code_assembly_has_no_business_tools(self):
        result = self.config.assemble("developer", "implement-change", "local_write")
        self.assertEqual(result["candidate_tools"], [])
        self.assertEqual(result["mode"], "assembly_only")

    def test_query_workflow_has_no_external_write_steps(self):
        for name in ("office-query", "project-query"):
            result = self.config.plan(name)
            self.assertFalse(result["executed"])
            self.assertTrue(all(s["effect"] == "read" for s in result["steps"]))


if __name__ == "__main__":
    unittest.main()
