"""Installs the local speech engine (Chatterbox) into a separate virtual environment: ~/.alice/tts-venv.

Why a separate environment: Chatterbox needs specific PyTorch/numpy versions and installs most smoothly on
Python 3.11; it must not break ALICE's own environment. Steps:
  1. Find Python 3.11 (on Windows `py -3.11`); if missing, try 3.12/3.10
  2. virtual environment + pip install chatterbox-tts
  3. If there is an NVIDIA card and the installed PyTorch has no CUDA, install the CUDA build of the same version
  4. import test, write tts_local = "chatterbox" and local_python to the config
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from alice.config import alice_home, set_config_value

CUDA_INDEXES = ["cu126", "cu124", "cu128", "cu121"]


def venv_dir() -> Path:
    return alice_home() / "tts-venv"


def venv_python(d: Path | None = None) -> Path:
    d = d or venv_dir()
    return d / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _out(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def find_python() -> tuple[list[str], str] | None:
    """(run command, version). 3.11 is preferred."""
    import os

    cands: list[list[str]] = []
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
        cands += [[str(base / f"Python{v}" / "python.exe")] for v in ("311", "312", "310")
                  if (base / f"Python{v}" / "python.exe").exists()]
    if sys.platform == "win32" and shutil.which("py"):
        cands += [["py", "-3.11"], ["py", "-3.12"], ["py", "-3.10"]]
    for name in ("python3.11", "python3.12", "python3.10"):
        if shutil.which(name):
            cands.append([name])
    cands.append([sys.executable])
    for cmd in cands:
        ver = _out(cmd + ["-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"])
        if ver in ("3.10", "3.11", "3.12"):
            return cmd, ver
    return None


def has_nvidia() -> bool:
    return shutil.which("nvidia-smi") is not None and _out(["nvidia-smi", "-L"]) is not None


def install(emit: Callable[[str], None] = print, run=None) -> Path:
    """Installs it and returns the server's Python path. On failure raises RuntimeError (Turkish message)."""
    def default_run(cmd: list[str]) -> int:
        emit("$ " + " ".join(cmd))
        return subprocess.call(cmd)

    run = run or default_run
    found = find_python()
    if found is None:
        raise RuntimeError("Python 3.10-3.12 bulunamadı. Önerilen: winget install Python.Python.3.11 "
                           "(sonra PowerShell'i kapatıp aç ve tekrar dene)")
    cmd, ver = found
    if ver != "3.11":
        emit(f"Not: Python {ver} kullanılacak; Chatterbox en sorunsuz 3.11'de kuruluyor. Sorun çıkarsa: "
             "winget install Python.Python.3.11")
    d = venv_dir()
    py = venv_python(d)
    if not py.exists():
        emit(f"sanal ortam oluşturuluyor: {d}")
        if run(cmd + ["-m", "venv", str(d)]) != 0:
            raise RuntimeError("sanal ortam oluşturulamadı")
    pip = [str(py), "-m", "pip"]
    run(pip + ["install", "--upgrade", "pip"])
    emit("Chatterbox kuruluyor (PyTorch dahil, birkaç GB sürebilir)")
    if run(pip + ["install", "chatterbox-tts"]) != 0:
        raise RuntimeError("chatterbox-tts kurulamadı. Yukarıdaki hatayı bana gönder; numpy hatasıysa çoğu zaman "
                           "Python 3.11 ile tekrar denemek çözüyor.")
    info = _out([str(py), "-c", "import torch; print(torch.__version__, torch.version.cuda or '', "
                                "torch.cuda.is_available())"])
    emit(f"PyTorch: {info}")
    if info and has_nvidia() and not info.endswith("True"):
        version = info.split()[0].split("+")[0]
        audio_ver = _out([str(py), "-c", "import torchaudio; print(torchaudio.__version__.split('+')[0])"])
        for idx in CUDA_INDEXES:
            emit(f"ekran kartı için CUDA'lı PyTorch {version} deneniyor ({idx})")
            pkgs = [f"torch=={version}"] + ([f"torchaudio=={audio_ver}"] if audio_ver else [])
            if run(pip + ["install", "--force-reinstall", "--no-deps", *pkgs, "--index-url",
                          f"https://download.pytorch.org/whl/{idx}"]) == 0:
                break
        else:
            emit("Uyarı: CUDA'lı PyTorch kurulamadı; ses işlemcide (yavaş) üretilecek.")
    # Local Whisper: checks the generated audio and trims hallucinated tail text. Leaves torch/numpy versions alone.
    emit("Whisper kuruluyor (sesi kontrol etmek için)")
    pins = _out([str(py), "-m", "pip", "freeze"]) or ""
    keep = [ln for ln in pins.splitlines() if ln.split("==")[0].lower() in ("torch", "torchaudio", "numpy")]
    cons = d / "constraints.txt"
    cons.write_text("\n".join(keep) + "\n", encoding="utf-8")
    if run(pip + ["install", "openai-whisper", "-c", str(cons)]) != 0:
        emit("Uyarı: Whisper kurulamadı; ses kontrolü Groq ile (ya da uzunluğa göre) yapılacak.")
    if _out([str(py), "-c", "from chatterbox.mtl_tts import ChatterboxMultilingualTTS; print('ok')"]) != "ok":
        raise RuntimeError("Chatterbox kuruldu ama içe aktarılamıyor. Şunu çalıştırıp çıktıyı gönder: "
                           f'"{py}" -c "from chatterbox.mtl_tts import ChatterboxMultilingualTTS"')
    set_config_value("voice", "local_python", str(py))
    set_config_value("voice", "tts_local", "chatterbox")
    return py
