#!/usr/bin/env python3
"""Install the bundled local skill. No network, API keys or background tasks."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime, timezone

NAME = 'customer-development'
ROOT = Path(__file__).resolve().parent
IGNORED = {'__pycache__', '.DS_Store', '.git'}


def inventory(directory):
    result = {}
    for path in sorted(directory.rglob('*')):
        rel = path.relative_to(directory)
        if any(p in IGNORED for p in rel.parts) or path.suffix == '.pyc':
            continue
        if path.is_symlink():
            raise ValueError('Refusing symlink: ' + str(rel))
        if path.is_file():
            result[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def verify_package():
    manifest = ROOT / 'manifest.json'
    if not manifest.is_file():
        raise ValueError('Package manifest is missing; use the complete release package.')
    data = json.loads(manifest.read_text(encoding='utf-8'))
    actual = inventory(ROOT)
    actual.pop('manifest.json', None)
    if actual != data.get('files'):
        raise ValueError('Package files differ from manifest; obtain a clean release package.')


def main():
    parser = argparse.ArgumentParser(description='Install customer-development for Codex (Python 3.9+).')
    parser.add_argument('--destination', type=Path, default=Path.home() / '.agents' / 'skills',
                        help='Parent skills directory; default ~/.agents/skills')
    parser.add_argument('--replace', action='store_true', help='Back up and replace an existing different version')
    parser.add_argument('--verify-only', action='store_true', help='Check package without installing')
    args = parser.parse_args()
    verify_package()
    if args.verify_only:
        print(json.dumps({'status': 'verified', 'skill': NAME}, ensure_ascii=False))
        return
    source = ROOT / 'skills' / NAME
    if not (source / 'SKILL.md').is_file():
        raise ValueError('SKILL.md is missing')
    parent = args.destination.expanduser().absolute()
    target = parent / NAME
    if target.is_symlink():
        raise ValueError('Refusing to replace a symlink installation')
    if target.exists():
        if not target.is_dir():
            raise ValueError('Installation destination is not a directory')
        if inventory(source) == inventory(target):
            print(json.dumps({'status': 'already_installed', 'path': str(target)}, ensure_ascii=False))
            return
        if not args.replace:
            raise ValueError('Different version exists. Use --replace to back it up before updating.')
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.customer-development-install-', dir=parent))
    payload = staging / NAME
    backup = None
    try:
        shutil.copytree(source, payload, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
        if inventory(payload) != inventory(source):
            raise ValueError('Copied files failed verification')
        if target.exists():
            backup_root = parent.parent / 'skill-backups'
            backup_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = backup_root / (NAME + '-' + stamp)
            os.rename(target, backup)
        try:
            os.rename(payload, target)
        except Exception:
            if backup is not None and not target.exists():
                os.rename(backup, target)
            raise
        print(json.dumps({'status': 'installed', 'path': str(target),
                          'backup': str(backup) if backup else None}, ensure_ascii=False))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print('Installation failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
