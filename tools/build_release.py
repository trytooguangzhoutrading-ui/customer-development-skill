#!/usr/bin/env python3
"""Rebuild the package manifest and optionally a ZIP containing only release files."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_FILES = {'README.md', 'INSTALL.md', 'LICENSE', 'VERSION', 'install.py',
                 '.gitignore', '.github/workflows/validate.yml', 'tools/build_release.py'}
IGNORED = {'.git', '__pycache__', '.DS_Store'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    output = args.output.expanduser().resolve() if args.output else None
    if output and (output == ROOT or ROOT in output.parents):
        parser.error('Release ZIP must be outside the source package')
    if output and output.exists():
        parser.error('Output already exists; choose a new path')
    files = {}
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if any(part in IGNORED for part in relative.parts) or path.suffix == '.pyc':
            continue
        if path.is_symlink():
            parser.error('Refusing symlink: ' + str(relative))
        if not path.is_file() or relative.as_posix() == 'manifest.json':
            continue
        name = relative.as_posix()
        if name not in ALLOWED_FILES and not name.startswith('skills/customer-development/'):
            parser.error('Unexpected file in release: ' + name)
        if path.suffix not in {'.md', '.py', '.json', '.yaml', '.yml', ''} and name != '.gitignore':
            parser.error('Unexpected file type: ' + name)
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {'schema_version': 1, 'name': 'customer-development-kit',
                'version': (ROOT / 'VERSION').read_text().strip(), 'files': files}
    (ROOT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
            for name in sorted([*files, 'manifest.json']):
                archive.write(ROOT / name, 'customer-development-kit/' + name)
    print(json.dumps({'files': len(files), 'manifest': str(ROOT / 'manifest.json'),
                      'archive': str(output) if output else None}, ensure_ascii=False))


if __name__ == '__main__':
    main()
