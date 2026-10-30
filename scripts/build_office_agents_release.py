"""Build a portable allowlisted release. No account, monitor or history directories copied."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--version', default='1.0.0')
parser.add_argument('--refresh-skills', action='store_true', help='Import and sanitize personal skills once.')
args = parser.parse_args()
if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
    raise SystemExit('Version must be major.minor.patch')
package = ROOT/'distribution/office-agents'
common = (ROOT/'config/global-agents/common.md').read_text(encoding='utf-8')
roles = json.loads((ROOT/'config/global-agents/roles.json').read_text(encoding='utf-8'))
for name, data in roles.items():
    destination = package/'agents'/f'{name}.toml'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text('\n'.join(f'{k} = {json.dumps(v, ensure_ascii=False)}' for k, v in {
        'name':name, 'description':data['description'],
        'developer_instructions':common+'\n\n# 当前职责\n'+data['instructions'],
    }.items())+'\n', encoding='utf-8')
skills = ('development-handoff','create-test-mr','create-prod-mr')
for skill in skills:
    destination = package/'skills'/skill/'SKILL.md'
    if args.refresh_skills:
        source = Path.home()/'.codex/skills'/skill/'SKILL.md'
        text = source.read_text(encoding='utf-8')
        text = re.sub(r'CPQ 已确认的项目为 .*?不复用 CPQ 的 ID。',
                      '从当前工作区绑定或用户指定范围确认项目 ID、编号前缀与访问权限，不复用其他项目的 ID。', text)
        text = text.replace('CPQ-728', 'TASK-123').replace('uniqueId = 728','uniqueId = 123')
        text = re.sub(r'该项目目前使用 `https://gitlab\.[^`]+`，实际执行仍以仓库 remote 为准。',
                      '以实际仓库 remote 和工作区绑定核验目标实例。', text)
        text = re.sub(r'当前机器的 glab 已安装在 `[^`]+`；PATH 尚未刷新时可直接使用该路径，其他机器应重新查找。',
                      '从 PATH 查找 glab，不使用其他机器的绝对安装路径。', text)
        text = text.replace('使用 `attach_artifact` 将 MR URL 附加到当前聊天。',
                            '若当前环境支持 attach_artifact，可将 MR URL 附加到聊天；否则返回普通可点击链接。')
        text = text.replace('用户已要求任务交接时发给对应测试，即授权在该任务范围内发送测试交接。',
                            '只有当前用户已明确要求向对应测试发送交接，才具有该任务范围内的发送授权；安装本技能不提供授权。')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding='utf-8')
    if not destination.is_file():
        raise SystemExit('Missing shareable skill; first use --refresh-skills')
files = []
for path in sorted(package.rglob('*')):
    if path.is_file() and path.name != 'manifest.json' and '__pycache__' not in path.parts:
        files.append(path)
for path in files:
    path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
    text = path.read_text(encoding='utf-8-sig')
    if re.search(r'HMG-BA110|hmglog\.com|66b0353b1f1e659482e27da7|CPQ|刘恳|甄德衡|吴会龙|冯亚杰|张友明|D:[/\\]HMG|super-office-assistant|cid\w+==', text, re.I):
        raise SystemExit('Private identifier in release: '+str(path))
manifest = {'formatVersion':1,'version':args.version,'files':{
    path.relative_to(package).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
(package/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
release = ROOT/'dist'
release.mkdir(exist_ok=True)
archive = release/f'office-agents-{args.version}.zip'
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as bundle:
    for path in files+[package/'manifest.json']:
        bundle.write(path, Path('office-agents')/path.relative_to(package))
checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
archive.with_suffix('.zip.sha256').write_text(checksum+'  '+archive.name+'\n', encoding='ascii')
print(json.dumps({'archive':str(archive),'sha256':checksum,'files':len(files)+1},ensure_ascii=False))
