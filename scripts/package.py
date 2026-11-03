#!/usr/bin/env python3
"""Produce a source-complete, architecture-specific release archive."""
import argparse
import hashlib
from pathlib import Path
import shutil
import tarfile

ROOT = Path(__file__).resolve().parent.parent

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--arch', required=True, choices=['arm64', 'x86_64'])
    args = parser.parse_args()
    name = 'brother-dcp-t236-macos-' + args.arch
    stage = ROOT / 'build' / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for folder in ['src', 'scripts', 'ppd', 'tests', 'docs', 'third_party']:
        shutil.copytree(ROOT / folder, stage / folder, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for file in ['install.command', 'uninstall.command', 'Makefile', 'README.md', 'LICENSE', 'NOTICE', 'requirements.txt', 'CHANGELOG.md']:
        shutil.copy2(ROOT / file, stage / file)
    (stage / 'bin').mkdir()
    shutil.copy2(ROOT / 'build/brother-t236-print', stage / 'bin/brother-t236-print')
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    archive = dist / (name + '.tar.gz')
    def portable(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        return info
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(stage, arcname=name, filter=portable)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + '.sha256').write_text(f'{digest}  {archive.name}\n')
    print(archive)

if __name__ == '__main__':
    main()
