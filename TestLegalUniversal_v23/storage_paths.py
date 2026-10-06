"""One writable root; conservative, restartable migration of diagnostic storage."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import tempfile


def app_dir():
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA', str(Path.home() / 'AppData' / 'Roaming'))) / 'Legalyze'
    return Path.home() / '.local' / 'share' / 'Legalyze'


def legacy_roots():
    if os.name != 'nt':
        return []
    return [('roaming', Path(os.environ.get('APPDATA', str(Path.home() / 'AppData' / 'Roaming'))) / 'LegalyzeWin11'),
            ('local', Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))) / 'LegalyzeWin11'),
            ('temp', Path(tempfile.gettempdir()) / 'LegalyzeWin11')]


def busy_windows_paths(paths):
    """Return matching PID only; command lines never leave this function or enter logs."""
    if os.name != 'nt':
        return []
    command = ('[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); '
               '$ErrorActionPreference="Stop"; '
               '@(Get-CimInstance Win32_Process -Filter "Name = \'chrome.exe\' OR Name = \'chromium.exe\'" | '
               'Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, timeout=25, creationflags=0x08000000)
    if result.returncode:
        raise RuntimeError('Cannot verify that old browser profiles are closed')
    processes = json.loads(result.stdout.decode('utf-8-sig').strip() or '[]')
    if isinstance(processes, dict):
        processes = [processes]
    needles = [str(p).replace('/', '\\').casefold() for p in paths]
    busy = []
    for process in processes:
        line = process.get('CommandLine')
        if not line:  # Unknown ownership: do not risk moving a live profile.
            raise RuntimeError('Browser command line is inaccessible; close browsers before migration')
        line = line.replace('/', '\\').casefold()
        if any(path in line for path in needles):
            busy.append(process['ProcessId'])
    return busy


def _redirected(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def _copy_missing(source, target):
    """Never overwrite an existing canonical file. Keep originals in the archive."""
    if _redirected(source) or (target.exists() and _redirected(target)):
        raise RuntimeError('Refusing a symlink in migration data')
    if source.is_dir():
        if target.exists() and not target.is_dir():
            return
        target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            _copy_missing(child, target / child.name)
    elif not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        # A partial copy is never published as a usable config/cache.
        temp = target.with_name(target.name + '.migration.tmp')
        shutil.copy2(source, temp)
        temp.replace(target)


def migrate_legacy(root, sources, busy_check=busy_windows_paths):
    """Rename old roots into archives, promote absent data, and keep conflicts intact.

    Call under the new application's QLockFile. Caller also locks legacy client
    directories. No rmtree, profile merge, stale-lock deletion, or process killing.
    Same-volume rename only: on a cross-volume/locked-directory failure leave the
    source intact and report the failure instead of performing destructive cleanup.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if _redirected(root):
        raise RuntimeError("Refusing a redirected application directory")
    backup = root / 'migration-backups'
    remaining = []
    seen = set()
    for label, path in sources:
        path = Path(path)
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen and path.exists():
            remaining.append((label, path))
            seen.add(key)
    archives = sorted(p for p in backup.glob('*-LegalyzeWin11-*') if not (p / '.migration-complete').exists()) if backup.exists() else []
    if not remaining and not archives:
        return []
    busy = busy_check([path for _, path in remaining] + [root / 'chromium-profile'] + archives)
    if busy:
        raise RuntimeError('Close browser processes before migration: ' + ', '.join(map(str, busy)))
    report = []
    for label, path in remaining:
        if _redirected(path):
            raise RuntimeError('Refusing a redirected legacy directory')
        backup.mkdir(parents=True, exist_ok=True)
        archive = backup / f'{label}-LegalyzeWin11-{time.time_ns()}'
        path.rename(archive)
        archives.append(archive)
        report.append({'source': str(path), 'archive': str(archive)})
    for archive in archives:
        marker = archive / '.migration-complete'
        if marker.exists():
            continue
        if archive.name.startswith('roaming-'):
            for name in ('config.json', 'Шаблон.txt', 'data'):
                source = archive / name
                if source.exists():
                    _copy_missing(source, root / name)
        elif archive.name.startswith('local-'):
            profile = archive / 'Profile'
            if profile.exists() and not (root / 'chromium-profile').exists():
                profile.rename(root / 'chromium-profile')
        logs = archive / 'logs'
        if logs.exists():
            (root / 'logs').mkdir(parents=True, exist_ok=True)
            dest = root / 'logs' / ('imported-' + archive.name)
            logs.rename(dest)
        marker.touch()
    return report
