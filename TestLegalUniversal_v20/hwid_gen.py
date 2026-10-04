import diagnostics as diag
from diagnostics import stage
import hashlib
import platform
import subprocess


def _run_hidden_subprocess(cmd: list[str]) -> subprocess.CompletedProcess:
    creationflags = 0
    startupinfo = None
    if platform.system() == "Windows":
        creationflags = 0x08000000  # CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
    diag.event("hwid.command.begin", executable=cmd[0])
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=5,
        creationflags=creationflags,
        startupinfo=startupinfo,
    )
    diag.event("hwid.command.end", executable=cmd[0], returncode=result.returncode, has_output=bool(result.stdout.strip()))
    return result


def _get_cpu_id() -> str:
    try:
        if platform.system() == "Windows":
            result = _run_hidden_subprocess(["wmic", "cpu", "get", "ProcessorId"])
            lines = result.stdout.strip().split("\n")
            for line in lines:
                line = line.strip()
                if line and line != "ProcessorId":
                    return line
    except Exception:
        diag.exception("hwid_gen.py:39")
        pass
    try:
        if platform.system() == "Windows":
            result = _run_hidden_subprocess(
                ["powershell", "-Command", "(Get-WmiObject Win32_Processor).ProcessorId"]
            )
            return result.stdout.strip()
    except Exception:
        diag.exception("hwid_gen.py:48")
        pass
    return "unknown_cpu"


def _get_gpu_id() -> str:
    try:
        if platform.system() == "Windows":
            result = _run_hidden_subprocess(
                ["wmic", "path", "win32_videocontroller", "get", "PNPDeviceID"]
            )
            lines = result.stdout.strip().split("\n")
            for line in lines:
                line = line.strip()
                if line and line != "PNPDeviceID":
                    return line
    except Exception:
        diag.exception("hwid_gen.py:65")
        pass
    try:
        if platform.system() == "Windows":
            result = _run_hidden_subprocess(
                ["powershell", "-Command", "(Get-WmiObject Win32_VideoController).PNPDeviceID"]
            )
            return result.stdout.strip()
    except Exception:
        diag.exception("hwid_gen.py:74")
        pass
    return "unknown_gpu"


@stage
def generate_hwid() -> str:
    cpu = _get_cpu_id()
    gpu = _get_gpu_id()
    raw = f"CPU:{cpu}|GPU:{gpu}"
    return hashlib.sha256(raw.encode()).hexdigest()
