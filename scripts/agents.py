"""Validate and assemble project agent definitions without executing models or tools."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))


class ConfigError(ValueError):
    pass


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def project_file(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ConfigError(f"Missing or outside-project file: {relative}")
    return path


def unique(items, key, label):
    values = [item[key] for item in items]
    if len(values) != len(set(values)):
        raise ConfigError(f"Duplicate {label}")
    return dict(zip(values, items))


class AgentConfig:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.registry = read_json(self.root / "config/agents.json")
        self.catalog = read_json(project_file(self.root, self.registry["catalog_path"]))
        self.workflows_doc = read_json(self.root / "config/workflows.json")
        for document in (self.registry, self.catalog, self.workflows_doc):
            if document.get("schema_version") != "1.0":
                raise ConfigError("Unsupported schema version")
        self.agents = unique(self.registry["agents"], "id", "agent")
        self.services = unique(self.catalog["services"], "id", "service")
        self.workflows = unique(self.workflows_doc["workflows"], "id", "workflow")
        self.validate()

    def validate(self):
        if self.catalog["default_tool_policy"] != "deny_unlisted":
            raise ConfigError("Unknown tools must be denied")
        if self.registry["entry_agent"] not in self.agents:
            raise ConfigError("Missing entry agent")
        if not self.agents[self.registry["entry_agent"]]["enabled"]:
            raise ConfigError("Entry agent must be enabled")
        tool_names = set()
        for service in self.services.values():
            if service["owner"] not in self.agents:
                raise ConfigError(f"Unknown owner: {service['id']}")
            for tool in service["tools"]:
                if tool["effect"] not in {"read", "write"}:
                    raise ConfigError(f"Invalid tool effect: {tool['name']}")
                if "*" in tool["name"] or tool["name"] in tool_names:
                    raise ConfigError(f"Wildcard or duplicate tool: {tool['name']}")
                tool_names.add(tool["name"])
        for agent in self.agents.values():
            project_file(self.root, agent["role_path"])
            unique(agent["skills"], "id", "skill")
            for skill in agent["skills"]:
                content = project_file(self.root, skill["path"]).read_text(encoding="utf-8")
                if f"\nname: {skill['id']}\n" not in content:
                    raise ConfigError(f"Skill name mismatch: {skill['id']}")
                if not skill["effects"] or not set(skill["effects"]) <= {"read", "write", "local_write"}:
                    raise ConfigError(f"Invalid skill effects: {skill['id']}")
                for service_id in skill["services"]:
                    if service_id not in self.services:
                        raise ConfigError(f"Unknown service: {service_id}")
                    if self.services[service_id]["owner"] != agent["id"]:
                        raise ConfigError(f"Cross-owner tool access: {agent['id']} / {service_id}")
                if "tools" in skill:
                    allowed = {t["name"] for sid in skill["services"] for t in self.services[sid]["tools"]}
                    selected = skill["tools"]
                    if (not isinstance(selected, list) or not selected
                            or any(not isinstance(n, str) for n in selected)
                            or len(selected) != len(set(selected)) or not set(selected) <= allowed):
                        raise ConfigError(f"Invalid skill tool allowlist: {skill['id']}")
        for workflow in self.workflows.values():
            steps = unique(workflow["steps"], "id", "workflow step")
            completed = set()
            for step in workflow["steps"]:
                if step.get("requires_tool_effect") not in (None, "read", "write"):
                    raise ConfigError("Invalid required tool evidence effect")
                if not set(step["depends_on"]) <= completed:
                    raise ConfigError(f"Missing, cyclic or unordered dependency: {workflow['id']}")
                self.select(step["agent"], step["skill"], step["effect"])
                completed.add(step["id"])
            if not steps:
                raise ConfigError("Workflow must contain steps")
        from agent_runtime.approvals import ApprovalPolicy
        self.approval_policy = ApprovalPolicy.load(self.root, self.registry.get("approval_policy_path", "config/approvals.json"))
        policy_tools = set(self.approval_policy.data.get("tool_modes", {})) | set(self.approval_policy.data.get("mandatory_user_tools", []))
        if not policy_tools <= tool_names:
            raise ConfigError("Approval policy references unlisted tools")
        return self

    def select(self, agent_id, skill_id, effect):
        if agent_id not in self.agents:
            raise ConfigError(f"Unknown agent: {agent_id}")
        agent = self.agents[agent_id]
        if not agent["enabled"]:
            raise ConfigError(f"Agent is paused: {agent_id}")
        skill = next((s for s in agent["skills"] if s["id"] == skill_id), None)
        if skill is None or effect not in skill["effects"]:
            raise ConfigError(f"Skill/effect unavailable: {agent_id} / {skill_id} / {effect}")
        return agent, skill

    def assemble(self, agent_id, skill_id, effect="read"):
        agent, skill = self.select(agent_id, skill_id, effect)
        # A write task needs reads for locating and verifying its target.
        effects = {"read", "write"} if effect == "write" else {"read"} if effect == "read" else set()
        tools = [tool for sid in skill["services"] for tool in self.services[sid]["tools"]
                 if tool["effect"] in effects and ("tools" not in skill or tool["name"] in skill["tools"])]
        tools = [{**tool, "approval_mode": self.approval_policy.mode(tool["name"], tool["effect"], ())}
                 for tool in tools]
        instructions = [project_file(self.root, path).read_text(encoding="utf-8")
                        for path in (agent["role_path"], skill["path"])]
        return {
            "agent_id": agent_id, "skill_id": skill_id, "effect": effect,
            "mode": "assembly_only", "model_config_ref": agent["model_config_ref"],
            "instructions": "\n\n".join(instructions), "candidate_tools": tools,
            "approval_policy_version": self.approval_policy.data["policy_version"],
            "authorization": "Requires trusted task/resource context before tool execution",
            "local_execution": agent["local_execution"],
            "runtime_missing": ["model_runner", "trusted_authorization_issuer",
                                "production_mcp_adapters", "authenticated_channel_adapters"],
        }

    def plan(self, workflow_id):
        if workflow_id not in self.workflows:
            raise ConfigError(f"Workflow unavailable: {workflow_id}")
        workflow = self.workflows[workflow_id]
        return {**workflow, "mode": "plan_only", "executed": False,
                "required_inputs_are_authorization": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    sub.add_parser("approvals")
    assemble = sub.add_parser("assemble")
    assemble.add_argument("agent")
    assemble.add_argument("skill")
    assemble.add_argument("--effect", choices=["read", "write", "local_write"], default="read")
    plan = sub.add_parser("plan")
    plan.add_argument("workflow")
    args = parser.parse_args()
    try:
        config = AgentConfig()
        if args.command == "validate":
            result = {"valid": True, "agents": len(config.agents),
                      "enabled_agents": sum(a["enabled"] for a in config.agents.values()),
                      "services": len(config.services), "workflows": len(config.workflows),
                      "runtime": "not_started"}
        elif args.command == "assemble":
            result = config.assemble(args.agent, args.skill, args.effect)
        elif args.command == "approvals":
            from collections import Counter
            counts = Counter(config.approval_policy.mode(tool["name"], tool["effect"], ())
                             for service in config.services.values() for tool in service["tools"])
            result = {"policy_version": config.approval_policy.data["policy_version"],
                      "default_modes": config.approval_policy.data["default_modes"],
                      "candidate_tool_modes": dict(counts),
                      "mandatory_user_tools": config.approval_policy.data["mandatory_user_tools"],
                      "ai_adapter": "not_connected", "user_review_interface": "python_api_only"}
        else:
            result = config.plan(args.workflow)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ConfigError, KeyError, ValueError, OSError) as exc:
        parser.exit(1, f"Configuration error: {exc}\n")


if __name__ == "__main__":
    main()
