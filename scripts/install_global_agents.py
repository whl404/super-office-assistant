"""Install user-scoped Codex roles; never start monitors or execute business tasks."""
import datetime
import json
import shutil
try:
    import tomllib
except ImportError:  # Python < 3.11, use the parser shipped with pip.
    from pip._vendor import tomli as tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home() / '.codex'
source = ROOT / 'config/global-agents'
roles = json.loads((source / 'roles.json').read_text(encoding='utf-8'))
common = (source / 'common.md').read_text(encoding='utf-8')
stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
backup = HOME / 'backups' / ('global-office-agents-' + stamp)
backup.mkdir(parents=True)

def save(path, text):
    if path.exists():
        relative = path.relative_to(HOME)
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')

for role, data in roles.items():
    instructions = common + '\n\n# 当前职责\n' + data['instructions']
    text = '\n'.join(f'{key} = {json.dumps(value, ensure_ascii=False)}' for key, value in {
        'name': role, 'description': data['description'],
        'developer_instructions': instructions,
    }.items()) + '\n'
    assert tomllib.loads(text)['name'] == role
    save(HOME / 'agents' / (role + '.toml'), text)

global_rules = HOME / 'AGENTS.md'
existing = global_rules.read_text(encoding='utf-8') if global_rules.exists() else ''
start, end = '<!-- office-agents:start -->', '<!-- office-agents:end -->'
block = f'''{start}
# 全局办公角色与项目绑定

办公交付可使用用户目录 .codex/agents/ 下的 office-orchestrator、office-developer、office-reviewer、office-teambition、office-gitlab、office-dingtalk。角色使用服从当前会话委派限制，注册本身不启动任务或扩大授权。
执行开发前读取目标仓库适用的 AGENTS.md（含修改目录规则）、工程文档和既有实现；不能将全局通用流程当成项目规范的替代。
按目标仓库绝对路径唯一匹配 .codex/workspace-monitors/*/config.json，读取该工作区的规则与state.json，不能复用其他项目的人员、群聊、任务或授权。无绑定时只执行明确指定范围。
全局角色可复用，监听单独开关。暂停的自动化不得因角色安装、加载或用户普通查询而恢复。
{end}'''
if start in existing:
    before, remainder = existing.split(start, 1)
    _, after = remainder.split(end, 1)
    updated = before + block + after
else:
    updated = existing.rstrip() + '\n\n' + block + '\n'
save(global_rules, updated)
for filename in ('config.toml', 'workspace-monitors/cpq/config.json', 'workspace-monitors/cpq/state.json', 'automations/cpq/automation.toml'):
    path = HOME / filename
    if path.exists():
        target = backup / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
print(json.dumps({'installed': list(roles), 'backup': str(backup), 'monitor_started': False}, ensure_ascii=False))
