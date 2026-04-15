#!/usr/bin/env -S uv run
"""
transfermaker Launcher
Cross-platform launcher for the transfermaker application.
"""
import subprocess
import sys
import os
import platform
import re
import time
from pathlib import Path
import requests

class Colors:
    MAGENTA = '\033[95m'
    BLUE    = '\033[94m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    RED     = '\033[91m'
    NC      = '\033[0m'

kobold_process = None

def cleanup():
    global kobold_process
    if kobold_process and kobold_process.poll() is None:
        print(f"\n{Colors.YELLOW}Stopping KoboldCpp...{Colors.NC}")
        kobold_process.terminate()
        try:
            kobold_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kobold_process.kill()

def detect_cuda():
    """Return (cuda_available: bool, cuda_version: str | None)."""
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            match = re.search(r'CUDA Version:\s*(\d+\.\d+)', result.stdout)
            if match:
                return True, match.group(1)
            return True, "12.0"   # nvidia-smi present but version unparseable
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False, None

cuda_available, cuda_version = detect_cuda()

def download_file(url, destination):
    """
    Downloads a file from the specified URL to the destination path.

    Returns True if successful
    """
    try:
        print(f"Downloading from {url}...")
        response = requests.get(url, stream=True)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        block_size = 1024
        downloaded = 0

        with open(destination, 'wb') as file:
            for data in response.iter_content(block_size):
                downloaded += len(data)
                file.write(data)

                if total_size > 0:
                    progress = int(50 * downloaded / total_size)
                    sys.stdout.write(f"\r[{'=' * progress}{' ' * (50 - progress)}] {downloaded}/{total_size} bytes")
                    sys.stdout.flush()

        if total_size > 0:
            sys.stdout.write('\n')

        print(f"Download completed: {destination}")
        return True
    except Exception as e:
        print(f"Error downloading file: {e}")
        if os.path.exists(destination):
            os.remove(destination)
        return False

def determine_kobold_filename():
    """
    Determines which KoboldCPP executable to download based on platform.

    Returns:
        str: Filename to download
    """
    system = platform.system()

    if system == "Windows":

        return "koboldcpp.exe"

    elif system == "Darwin":  # macOS
        if platform.machine() == "arm64":
            return "koboldcpp-mac-arm64"
        else:
            # Intel Macs are not supported by KoboldCpp pre-built binaries
            print("Error: Intel Macs are not supported.")
            print("KoboldCpp only provides pre-built binaries for Apple Silicon (ARM64) Macs.")
            print("Please use an Apple Silicon Mac or build KoboldCpp from source.")
            return None

    elif system == "Linux":
        if cuda_available:
            major_version = float(cuda_version.split('.')[0])
            if major_version >= 12:
                return "koboldcpp-linux-x64"
            else:
                return "koboldcpp-linux-x64-oldpc"
        else:
            return "koboldcpp-linux-x64-nocuda"

    else:
        raise ValueError(f"Unsupported operating system: {system}")

def get_resources_dir():
    return Path(__file__).parent / "resources"

def get_kobold_executable():
    """Return path to koboldcpp executable if it exists, else None."""
    filename = determine_kobold_filename()
    if filename is None:
        return None
    exe_path = get_resources_dir() / filename
    return exe_path if exe_path.exists() else None


def get_latest_kobold_version():
    """Query GitHub for the latest KoboldCpp release tag. Returns None on failure."""
    try:
        resp = requests.get(
            "https://api.github.com/repos/LostRuins/koboldcpp/releases/latest",
            timeout=10,
            headers={"Accept": "application/vnd.github.v3+json"},
        )
        resp.raise_for_status()
        return resp.json().get("tag_name")
    except Exception:
        return None

def download_kobold():
    print("Checking for KoboldCpp update...")
    resources = get_resources_dir()
    resources.mkdir(exist_ok=True)

    base_url = "https://github.com/LostRuins/koboldcpp/releases/latest/download/"
    download_filename = determine_kobold_filename()

    if download_filename is None:
        existing_executable = get_kobold_executable()
        if existing_executable:
            print(f"Using existing executable: {existing_executable}")
            return existing_executable
        else:
            raise FileNotFoundError("No compatible KoboldCpp executable available for your system")

    dest_path = resources / download_filename
    download_url = base_url + download_filename

    # Update check
    version_file = resources / ".kobold_version"
    current_version = version_file.read_text().strip() if version_file.exists() else None

    if dest_path.exists():
        latest_version = get_latest_kobold_version()
        if latest_version is None:
            print("Could not reach GitHub — using existing executable.")
            return dest_path
        if current_version == latest_version:
            print(f"KoboldCpp is up to date ({current_version}).")
            return dest_path
        print(f"Updating KoboldCpp: {current_version or 'unknown'} → {latest_version}")
    else:
        latest_version = get_latest_kobold_version()

    if not download_file(download_url, str(dest_path)):
        if dest_path.exists():
            return dest_path   # download failed but old copy still usable
        raise RuntimeError(f"Failed to download KoboldCpp from {download_url}")

    if platform.system() != "Windows":
        dest_path.chmod(dest_path.stat().st_mode | 0o755)

    if latest_version:
        version_file.write_text(latest_version)

    return dest_path

def launch_kobold(exe_path):
    """Launch KoboldCpp with the config file and --onready to open the GUI."""
    global kobold_process

    resources = get_resources_dir()
    config_path = resources / "config.kcpps"

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    gui_script = Path(__file__).parent / "src" / "gui.py"
    onready_cmd = f'"{sys.executable}" "{gui_script}"'

    cmd = [
        str(exe_path),
        "--config", str(config_path),
        "--onready", onready_cmd,
    ]

    print(f"{Colors.GREEN}Starting KoboldCpp...{Colors.NC}")
    print(f"Config: {config_path}")
    print("Model will load, then the GUI will open automatically.")
    print("Press Ctrl+C to exit.\n")

    kobold_process = subprocess.Popen(cmd, cwd=str(resources))
    return kobold_process

def main():
    exe_path = get_kobold_executable()

    if exe_path is None:
        print("KoboldCpp not found in resources/. Downloading...")
        exe_path = download_kobold()
    else:
        try:
            exe_path = download_kobold()
        except Exception as e:
            print(f"{Colors.YELLOW}Update check failed ({e}). Using existing executable.{Colors.NC}")

    proc = launch_kobold(exe_path)

    try:
        proc.wait()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Colors.MAGENTA}Interrupted. Exiting...{Colors.NC}")
        cleanup()
        sys.exit(0)
