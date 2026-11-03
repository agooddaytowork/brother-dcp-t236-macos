#!/usr/bin/env python3
"""Per-user installation. Never changes unrelated CUPS queues."""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
BASE = Path.home() / 'Library/Application Support/Brother DCP-T236 Mac'
AGENTS = Path.home() / 'Library/LaunchAgents'
LOGS = Path.home() / 'Library/Logs/Brother DCP-T236 Mac'
LABELS = ['com.local.brother-t236-mac', 'com.local.brother-t236-airscan']
QUEUE = 'Brother_DCP_T236'
URI = 'ipp://localhost:8631/ipp/print'

def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)

def device_uri(explicit=None):
    if explicit:
        candidates = [explicit]
    else:
        result = run(['/usr/libexec/cups/backend/usb'], capture_output=True, text=True)
        candidates = re.findall(r'^direct (usb://Brother/DCP-T236\?\S+)', result.stdout, re.M)
    if len(candidates) != 1:
        raise RuntimeError('Connect exactly one DCP-T236 by USB, or select it with --device-uri.')
    uri = candidates[0]
    parsed = urllib.parse.urlsplit(uri)
    serial = urllib.parse.parse_qs(parsed.query).get('serial', [''])[0]
    if parsed.scheme != 'usb' or parsed.netloc != 'Brother' or parsed.path != '/DCP-T236' or not serial:
        raise RuntimeError('Expected usb://Brother/DCP-T236?serial=...')
    return uri, serial

def service_plists(python, usb_uri):
    env = {'LANG': 'en_US.UTF-8', 'BROTHER_IO_LOCK': str(BASE / 'io.lock'),
           'BROTHER_BASE_DIR': str(BASE), 'TMPDIR': str(BASE / 'tmp')}
    common = dict(RunAtLoad=True, KeepAlive=True, ThrottleInterval=10,
                  ProcessType='Background', Umask=63, WorkingDirectory=str(BASE))
    printer = dict(common, Label=LABELS[0], EnvironmentVariables=dict(env, BROTHER_DEVICE_URI=usb_uri),
        ProgramArguments=['/usr/bin/ippeveprinter', '-n', 'localhost', '-p', '8631', '-r', 'off', '--no-web-forms',
            '-f', 'application/pdf', '-M', 'Brother', '-m', 'DCP-T236 Mac USB', '-s', '10,10', '-D', '/dev/null',
            '-c', str(BASE / 'brother-t236-print'), '-d', str(BASE / 'spool'), 'Brother DCP-T236 Mac USB'],
        StandardOutPath=str(LOGS / 'service.log'), StandardErrorPath=str(LOGS / 'service.log'))
    scanner = dict(common, Label=LABELS[1], EnvironmentVariables=env,
        ProgramArguments=[str(python), '-u', str(BASE / 'scanner/airscan_bridge.py')],
        StandardOutPath=str(LOGS / 'scan-service.log'), StandardErrorPath=str(LOGS / 'scan-service.log'))
    return dict(zip(LABELS, [printer, scanner]))

def queue_uri():
    result = subprocess.run(['/usr/bin/lpstat', '-v', QUEUE], capture_output=True, text=True)
    return result.stdout.strip().split(': ', 1)[1] if result.returncode == 0 else None

def stop_services():
    for label in LABELS:
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def lpadmin(args):
    cmd = ['/usr/sbin/lpadmin', *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode:
        print('macOS requires administrator authorization to update this print queue.')
        run(['/usr/bin/sudo', *cmd])

def install(args):
    usb_uri, serial = device_uri(args.device_uri)
    binary = ROOT / 'bin/brother-t236-print'
    if not binary.exists():
        binary = ROOT / 'build/brother-t236-print'
    if not binary.exists():
        raise RuntimeError('Build the print helper first: make all')
    if args.dry_run:
        print('DRY RUN — no changes made.\nInstall per-user print + scan services, select the connected USB device,\ncreate an A4 CUPS queue, advertise the scanner to this Mac only.\nLaunchAgents and private runtime go into your user Library.')
        return
    if platform.system() != 'Darwin' or os.getuid() == 0:
        raise RuntimeError('Run on macOS as your normal user, without sudo.')
    for program in ['/usr/bin/ippeveprinter', '/usr/libexec/cups/backend/usb']:
        if not os.access(program, os.X_OK):
            raise RuntimeError(f'Required macOS component is missing: {program}')
    run(['/usr/bin/lipo', '-verify_arch', platform.machine(), binary], capture_output=True)
    existing = queue_uri()
    if existing and existing != URI:
        raise RuntimeError(f'{QUEUE} belongs to another driver. Rename/remove that queue in System Settings first. No changes made.')
    pending = subprocess.run(['/usr/bin/lpstat', '-W', 'not-completed', '-o', QUEUE], capture_output=True, text=True)
    if pending.stdout.strip():
        raise RuntimeError('Finish or cancel queued DCP-T236 print jobs before installing.')
    # Refuse unexpected services on these ports. Recognized installed services
    # may be replaced only at their established per-user paths.
    for port, label in zip([8631, 8632], LABELS):
        with socket.socket() as probe:
            busy = probe.connect_ex(('127.0.0.1', port)) == 0
        registered = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if busy and not registered:
            raise RuntimeError(f'Port {port} is in use by another service. No changes made.')
    for directory in [BASE, AGENTS, LOGS, BASE/'scanner', BASE/'spool', BASE/'tmp']:
        directory.mkdir(parents=True, exist_ok=True)
    os.chmod(BASE, 0o700)
    for directory in [BASE/'spool', BASE/'tmp']:
        os.chmod(directory, 0o700)
    old_manifest = json.loads((BASE/'install-manifest.json').read_text()) if (BASE/'install-manifest.json').exists() else {}
    backup = BASE/'backups'/time.strftime('%Y%m%d-%H%M%S')
    backup.mkdir(parents=True, exist_ok=True)
    for filename in ['config.json', 'install-manifest.json', 'brother-t236-print']:
        if (BASE/filename).exists():
            shutil.copy2(BASE/filename, backup/filename)
    for label in LABELS:
        if (AGENTS/(label+'.plist')).exists():
            shutil.copy2(AGENTS/(label+'.plist'), backup/(label+'.plist'))
    print('Preparing scanner runtime (first installation downloads Python packages)…', flush=True)
    venv = BASE/'scanner/venv'
    if not (venv/'bin/python3').exists():
        run([sys.executable, '-m', 'venv', venv])
    run([venv/'bin/python3', '-m', 'pip', 'install', '--disable-pip-version-check', '-r', ROOT/'requirements.txt'])
    stop_services()
    for filename in ['airscan_bridge.py', 't236scan.py']:
        shutil.copy2(ROOT/'src/scanner'/filename, BASE/'scanner'/filename)
    shutil.copy2(binary, BASE/'brother-t236-print.new')
    os.replace(BASE/'brother-t236-print.new', BASE/'brother-t236-print')
    for src, dest in [('src/print/brother-t236-print.c', 'brother-t236-print.c'), ('ppd/Brother-DCP-T236-Mac.ppd', 'Brother-DCP-T236-Mac.ppd'), ('LICENSE', 'scanner/COPYING'), ('NOTICE', 'scanner/NOTICE')]:
        shutil.copy2(ROOT/src, BASE/dest)
    (BASE/'config.json').write_text(json.dumps({'serial':serial, 'device_uri':usb_uri}, indent=2)+'\n')
    for label, data in service_plists(venv/'bin/python3', usb_uri).items():
        agent = AGENTS/(label+'.plist')
        agent.write_bytes(plistlib.dumps(data))
        run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', agent])
    try:
        for _ in range(40):
            try:
                with urllib.request.urlopen('http://127.0.0.1:8632/eSCL/ScannerCapabilities', timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(.25)
        else:
            raise RuntimeError('Scanner service did not start. See the logs in your Library/Logs/Brother DCP-T236 Mac folder.')
        lpadmin(['-p', QUEUE, '-E', '-v', URI, '-P', str(BASE/'Brother-DCP-T236-Mac.ppd'), '-D', 'Brother DCP-T236 Mac USB', '-o', 'printer-is-shared=false'])
    except Exception:
        print('Installation needs attention. Run install.command again after resolving the error; backups are in the runtime backups folder.', file=sys.stderr)
        raise
    (BASE/'install-manifest.json').write_text(json.dumps({'queue':QUEUE,'queue_created':old_manifest.get('queue_created', not bool(existing)),'version':'0.1.0'}, indent=2)+'\n')
    print('\nInstalled. No test page was printed.\nPrint: choose Brother DCP-T236 Mac USB in Word or Preview.\nScan: open Image Capture > Shared > Brother DCP-T236 Mac USB.\nFor a Settings shortcut, add the Bonjour Scanner as Brother DCP-T236 Scanner USB.\nServices start automatically when you log in to this Mac account.')

def uninstall(args):
    manifest_path = BASE/'install-manifest.json'
    if not manifest_path.exists():
        raise RuntimeError('No installation manifest found. Refusing to remove an unmanaged installation.')
    manifest = json.loads(manifest_path.read_text())
    if args.dry_run:
        print('DRY RUN: unload these two per-user services; remove only a queue created by this installer; retain runtime, backups and logs.')
        return
    if platform.system() != 'Darwin' or os.getuid() == 0:
        raise RuntimeError('Run as your normal Mac user, without sudo.')
    pending = subprocess.run(['/usr/bin/lpstat','-W','not-completed','-o',QUEUE],capture_output=True,text=True)
    if pending.stdout.strip():
        raise RuntimeError('Finish or cancel queued DCP-T236 print jobs before uninstalling.')
    if manifest.get('queue_created') and queue_uri() == URI:
        lpadmin(['-x', QUEUE])
    stop_services()
    for label in LABELS:
        (AGENTS/(label+'.plist')).unlink(missing_ok=True)
    print('Services removed. Runtime, backups and logs retained in your Library.\nRemove the optional Scanner entry yourself in System Settings.\nA pre-existing print queue is preserved; remove it in Settings if you no longer need it.')

def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ['install', 'uninstall']:
        p = sub.add_parser(command)
        p.add_argument('--dry-run', action='store_true')
        if command == 'install':
            p.add_argument('--device-uri', help='Select one DCP-T236 USB URI if multiple are connected')
    args = parser.parse_args()
    try:
        (install if args.command == 'install' else uninstall)(args)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
