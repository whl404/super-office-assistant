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

    def test_paused_gitlab_cannot_be_assembled(self):
        with self.assertRaises(MODULE.ConfigError):
            self.config.assemble("gitlab", "gitlab-mr-delivery")
        with self.assertRaises(MODULE.ConfigError):
            self.config.plan("gitlab-mr-delivery")

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
        service["tools"].append({"name": "mcp__dingtalk_contact__*", "effect": "read"})
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
