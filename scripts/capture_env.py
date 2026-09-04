#!/usr/bin/env python3
"""
Environment Capture Script for MLflow Reproducibility

Issue #222: Captures environment information including pip freeze and GPU/CPU info
as artifacts for MLflow runs to ensure reproducibility.

This script captures:
- Python package dependencies (pip freeze)
- System information (CPU, GPU, OS, etc.)
- Hardware specifications
- Python environment details

Usage:
    python scripts/capture_env.py [--output-dir OUTPUT_DIR] [--mlflow-run]
    
    --output-dir: Directory to save artifacts (default: ./env_artifacts)
    --mlflow-run: Log artifacts to active MLflow run
"""

import os
import sys
import json
import platform
import subprocess
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

import mlflow


def get_pip_packages() -> Dict[str, str]:
    """
    Get installed pip packages and their versions.
    
    Returns:
        Dictionary mapping package names to versions
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            check=True
        )
        
        packages = {}
        for line in result.stdout.strip().split('\n'):
            if line and '==' in line:
                name, version = line.split('==', 1)
                packages[name] = version
            elif line and not line.startswith('#'):
                # Handle packages without version (e.g., from git)
                packages[line] = "unknown"
                
        return packages
    except subprocess.CalledProcessError as e:
        print(f"Warning: Failed to get pip packages: {e}")
        return {}


def get_conda_packages() -> Dict[str, str]:
    """
    Get conda packages if conda is available.
    
    Returns:
        Dictionary mapping package names to versions
    """
    try:
        result = subprocess.run(
            ["conda", "list", "--export"],
            capture_output=True,
            text=True,
            check=True
        )
        
        packages = {}
        for line in result.stdout.strip().split('\n'):
            if line and '=' in line and not line.startswith('#'):
                parts = line.split('=')
                if len(parts) >= 2:
                    packages[parts[0]] = parts[1]
                    
        return packages
    except (subprocess.CalledProcessError, FileNotFoundError):
        # conda not available
        return {}


def get_gpu_info() -> Dict[str, Any]:
    """
    Get GPU information using nvidia-smi if available.
    
    Returns:
        Dictionary with GPU information
    """
    gpu_info = {"gpus": [], "cuda_available": False}
    
    try:
        # Check if CUDA is available through PyTorch
        try:
            import torch
            gpu_info["cuda_available"] = torch.cuda.is_available()
            if torch.cuda.is_available():
                gpu_info["torch_cuda_version"] = torch.version.cuda
                gpu_info["gpu_count"] = torch.cuda.device_count()
                for i in range(torch.cuda.device_count()):
                    gpu_info["gpus"].append({
                        "id": i,
                        "name": torch.cuda.get_device_name(i),
                        "memory_total": torch.cuda.get_device_properties(i).total_memory,
                        "capability": f"{torch.cuda.get_device_properties(i).major}.{torch.cuda.get_device_properties(i).minor}"
                    })
        except ImportError:
            pass
            
        # Try nvidia-smi for additional info
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,temperature.gpu", 
                 "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                check=True
            )
            
            nvidia_gpus = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    parts = [p.strip() for p in line.split(',')]
                    if len(parts) >= 4:
                        nvidia_gpus.append({
                            "name": parts[0],
                            "memory_total_mb": parts[1],
                            "memory_used_mb": parts[2],
                            "temperature_c": parts[3]
                        })
            
            if nvidia_gpus:
                gpu_info["nvidia_smi_gpus"] = nvidia_gpus
                
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
            
    except Exception as e:
        print(f"Warning: Failed to get GPU info: {e}")
        
    return gpu_info


def get_cpu_info() -> Dict[str, Any]:
    """
    Get CPU information.
    
    Returns:
        Dictionary with CPU information
    """
    cpu_info = {
        "processor": platform.processor(),
        "machine": platform.machine(),
        "architecture": platform.architecture(),
        "cpu_count": os.cpu_count()
    }
    
    # Try to get more detailed CPU info on Linux
    try:
        if platform.system() == "Linux":
            with open("/proc/cpuinfo", "r") as f:
                cpuinfo = f.read()
                for line in cpuinfo.split('\n'):
                    if "model name" in line:
                        cpu_info["model_name"] = line.split(':')[1].strip()
                        break
    except Exception:
        pass
        
    return cpu_info


def get_memory_info() -> Dict[str, Any]:
    """
    Get system memory information.
    
    Returns:
        Dictionary with memory information
    """
    memory_info = {}
    
    try:
        if platform.system() == "Linux":
            with open("/proc/meminfo", "r") as f:
                meminfo = f.read()
                for line in meminfo.split('\n'):
                    if "MemTotal" in line:
                        memory_info["total_kb"] = int(line.split()[1])
                    elif "MemAvailable" in line:
                        memory_info["available_kb"] = int(line.split()[1])
        else:
            # Use psutil if available for cross-platform memory info
            try:
                import psutil
                mem = psutil.virtual_memory()
                memory_info["total_bytes"] = mem.total
                memory_info["available_bytes"] = mem.available
                memory_info["percent_used"] = mem.percent
            except ImportError:
                pass
                
    except Exception as e:
        print(f"Warning: Failed to get memory info: {e}")
        
    return memory_info


def get_python_info() -> Dict[str, Any]:
    """
    Get Python environment information.
    
    Returns:
        Dictionary with Python information
    """
    return {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "compiler": platform.python_compiler(),
        "executable": sys.executable,
        "prefix": sys.prefix,
        "path": sys.path[:5]  # First 5 entries to avoid too much data
    }


def get_git_info() -> Dict[str, Any]:
    """
    Get git repository information.
    
    Returns:
        Dictionary with git information
    """
    git_info = {}
    
    try:
        # Get current commit hash
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        )
        git_info["commit_sha"] = result.stdout.strip()
        
        # Get current branch
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        )
        git_info["branch"] = result.stdout.strip()
        
        # Check if there are uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True
        )
        git_info["has_uncommitted_changes"] = bool(result.stdout.strip())
        git_info["uncommitted_files"] = len(result.stdout.strip().split('\n')) if result.stdout.strip() else 0
        
        # Get remote URL
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True
            )
            git_info["remote_url"] = result.stdout.strip()
        except subprocess.CalledProcessError:
            pass
            
    except subprocess.CalledProcessError:
        git_info["error"] = "Not a git repository or git not available"
        
    return git_info


def capture_environment(output_dir: Optional[str] = None, log_to_mlflow: bool = False) -> Dict[str, str]:
    """
    Capture complete environment information and save as artifacts.
    
    Args:
        output_dir: Directory to save artifacts (default: ./env_artifacts)
        log_to_mlflow: Whether to log artifacts to active MLflow run
        
    Returns:
        Dictionary mapping artifact names to file paths
    """
    if output_dir is None:
        output_dir = "./env_artifacts"
        
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().isoformat()
    artifacts = {}
    
    print(f"🔍 Capturing environment information...")
    print(f"📁 Output directory: {output_path.absolute()}")
    
    # 1. Capture pip packages (requirements.txt format)
    print("  📦 Capturing pip packages...")
    pip_packages = get_pip_packages()
    requirements_file = output_path / "requirements.txt"
    
    with open(requirements_file, "w") as f:
        f.write(f"# Generated by capture_env.py on {timestamp}\n")
        f.write(f"# Python {platform.python_version()}\n\n")
        for package, version in sorted(pip_packages.items()):
            if version != "unknown":
                f.write(f"{package}=={version}\n")
            else:
                f.write(f"{package}\n")
    
    artifacts["requirements.txt"] = str(requirements_file)
    print(f"    ✅ Saved {len(pip_packages)} packages to requirements.txt")
    
    # 2. Capture conda packages if available
    conda_packages = get_conda_packages()
    if conda_packages:
        print("  🐍 Capturing conda packages...")
        conda_file = output_path / "conda_packages.txt"
        
        with open(conda_file, "w") as f:
            f.write(f"# Generated by capture_env.py on {timestamp}\n")
            f.write(f"# Conda packages\n\n")
            for package, version in sorted(conda_packages.items()):
                f.write(f"{package}={version}\n")
        
        artifacts["conda_packages.txt"] = str(conda_file)
        print(f"    ✅ Saved {len(conda_packages)} conda packages")
    
    # 3. Capture system information
    print("  💻 Capturing system information...")
    system_info = {
        "timestamp": timestamp,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "node": platform.node()
        },
        "python": get_python_info(),
        "cpu": get_cpu_info(),
        "memory": get_memory_info(),
        "gpu": get_gpu_info(),
        "git": get_git_info(),
        "environment_variables": {
            key: (
                "<redacted>"
                if any(marker in key for marker in ("KEY", "TOKEN", "PASSWORD", "SECRET"))
                else value
            )
            for key, value in os.environ.items()
            if key.startswith(
                ('CUDA_', 'PYTHONPATH', 'PATH', 'MLFLOW_', 'NOESIS_', 'NEURONEWS_')
            )
        }
    }
    
    system_info_file = output_path / "system_info.json"
    with open(system_info_file, "w") as f:
        json.dump(system_info, f, indent=2, default=str)
    
    artifacts["system_info.json"] = str(system_info_file)
    print(f"    ✅ Saved system information")
    
    # 4. Log to MLflow if requested
    if log_to_mlflow:
        try:
            print("  📊 Logging artifacts to MLflow...")
            for artifact_name, file_path in artifacts.items():
                mlflow.log_artifact(file_path, "environment")
            
            # Log some key metrics as MLflow params/metrics
            mlflow.log_param("python_version", system_info["python"]["version"])
            mlflow.log_param("platform_system", system_info["platform"]["system"])
            mlflow.log_param("cpu_count", system_info["cpu"]["cpu_count"])
            mlflow.log_param("gpu_count", len(system_info["gpu"]["gpus"]))
            mlflow.log_param("git_commit", system_info["git"].get("commit_sha", "unknown"))
            mlflow.log_param("git_branch", system_info["git"].get("branch", "unknown"))
            
            if system_info["git"].get("has_uncommitted_changes"):
                mlflow.log_param("git_uncommitted_changes", True)
                
            print(f"    ✅ Logged {len(artifacts)} artifacts to MLflow")
            
        except Exception as e:
            print(f"    ❌ Failed to log to MLflow: {e}")
    
    print(f"✅ Environment capture complete!")
    print(f"📊 Summary:")
    print(f"   • {len(pip_packages)} pip packages")
    if conda_packages:
        print(f"   • {len(conda_packages)} conda packages")
    print(f"   • {system_info['cpu']['cpu_count']} CPU cores")
    print(f"   • {len(system_info['gpu']['gpus'])} GPUs")
    print(f"   • Git commit: {system_info['git'].get('commit_sha', 'unknown')[:8]}")
    
    return artifacts


def main():
    """Main function to run environment capture."""
    parser = argparse.ArgumentParser(
        description="Capture environment information for MLflow reproducibility"
    )
    parser.add_argument(
        "--output-dir",
        default="./env_artifacts",
        help="Directory to save environment artifacts"
    )
    parser.add_argument(
        "--mlflow-run",
        action="store_true",
        help="Log artifacts to active MLflow run"
    )
    
    args = parser.parse_args()
    
    try:
        artifacts = capture_environment(
            output_dir=args.output_dir,
            log_to_mlflow=args.mlflow_run
        )
        
        print(f"\n📁 Artifacts saved to:")
        for name, path in artifacts.items():
            print(f"   • {name}: {path}")
            
    except Exception as e:
        print(f"❌ Error capturing environment: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
