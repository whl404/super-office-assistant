"""Install/update this offline package without touching workspace or account settings."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_path(root, relative):
    part = PurePosixPath(relative)
    if part.is_absolute() or '..' in part.parts or '\\' in relative or ':' in relative:
        raise ValueError('Unsafe path: ' + relative)
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Path escapes destination: ' + relative)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['install', 'update', 'check'])
    parser.add_argument('--codex-home', type=Path, default=Path.home()/'.codex')
    parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parent,
                        help='Read package data using this local installer; never run remote code.')
    args = parser.parse_args()
    package = args.package.resolve()
    manifest = json.loads((package/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('formatVersion') != 1:
        raise ValueError('Unsupported package format; upgrade installer manually.')
    home = args.codex_home.resolve()
    record_path = home/'office-agents/install.json'
    previous = json.loads(record_path.read_text(encoding='utf-8')) if record_path.exists() else None
    if args.mode == 'update' and not previous:
        raise ValueError('No previous installation. Use install first.')
    if args.mode == 'install' and previous:
        raise ValueError('Already installed. Use update or check.')
    old_files = previous['files'] if previous else {}
    allowed_roles = {'office-' + x for x in ('orchestrator','developer','reviewer','teambition','gitlab','dingtalk')}
    allowed_skills = {'development-handoff','create-test-mr','create-prod-mr'}
    def allowed(relative):
        parts = PurePosixPath(relative).parts
        return ((len(parts) == 2 and parts[0] == 'agents' and Path(parts[1]).stem in allowed_roles and parts[1].endswith('.toml'))
                or (len(parts) == 3 and parts[0] == 'skills' and parts[1] in allowed_skills and parts[2] == 'SKILL.md'))
    new_files = {}
    conflicts = []
    for relative, expected in manifest['files'].items():
        source = safe_path(package, relative)
        if digest(source) != expected:
            raise ValueError('Package checksum mismatch: ' + relative)
        if not (relative.startswith('agents/') or relative.startswith('skills/')):
            continue
        if not allowed(relative):
            raise ValueError('Unexpected managed file; upgrade installer manually: ' + relative)
        destination = safe_path(home, relative)
        new_files[relative] = expected
        if destination.exists() and digest(destination) != expected:
            if relative not in old_files or digest(destination) != old_files[relative]:
                conflicts.append(relative)
    retired = set(old_files) - set(new_files)
    for relative in retired:
        if not allowed(relative):
            raise ValueError('Invalid installed-file record: ' + relative)
        path = safe_path(home, relative)
        if path.exists() and digest(path) != old_files[relative]:
            conflicts.append(relative)
    if conflicts:
        print(json.dumps({'status': 'conflict', 'files': sorted(conflicts),
                          'action': 'Manually merge or restore these files, then retry. No files changed.'}, ensure_ascii=False))
        return 2
    if args.mode == 'check':
        print(json.dumps({'status': 'ready', 'installedVersion': previous['version'] if previous else None,
                          'packageVersion': manifest['version'], 'managedFiles': len(new_files)}))
        return 0
    if previous and previous['version'] == manifest['version']:
        if old_files != new_files:
            raise ValueError('Same version has different content; publisher must increase version.')
        print(json.dumps({'status': 'up_to_date', 'version': manifest['version']}))
        return 0
    if previous and tuple(map(int, manifest['version'].split('.'))) < tuple(map(int, previous['version'].split('.'))):
        raise ValueError('Downgrade refused. Restore a backup explicitly instead.')
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup = home/'office-agents/backups'/stamp
    backup.mkdir(parents=True)
    changed = []
    try:
        for relative in sorted(set(new_files) | retired):
            destination = safe_path(home, relative)
            if relative in new_files and destination.exists() and digest(destination) == new_files[relative]:
                continue
            existed = destination.exists()
            if existed:
                saved = safe_path(backup, relative)
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, saved)
            changed.append((relative, existed))
            if relative in retired:
                if existed:
                    destination.unlink()
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                temp = destination.with_name(destination.name + '.office-install.tmp')
                shutil.copy2(safe_path(package, relative), temp)
                os.replace(temp, destination)
        if previous:
            shutil.copy2(record_path, backup/'previous-install.json')
        record = {'version': manifest['version'], 'files': new_files, 'backup': str(backup)}
        record_path.parent.mkdir(parents=True, exist_ok=True)
        temp_record = record_path.with_suffix('.tmp')
        temp_record.write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        os.replace(temp_record, record_path)
    except Exception:
        for relative, existed in reversed(changed):
            destination = safe_path(home, relative)
            if existed:
                shutil.copy2(safe_path(backup, relative), destination)
            elif destination.exists():
                destination.unlink()
        raise
    (backup/'changes.json').write_text(json.dumps(changed, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'installed', 'version': manifest['version'], 'backup': str(backup),
                      'monitorStarted': False, 'workspaceSettingsChanged': False}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
