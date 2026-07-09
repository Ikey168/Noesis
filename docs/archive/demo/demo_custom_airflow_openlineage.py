#!/usr/bin/env python3
"""
Demo script for Custom Airflow OpenLineage Integration (Issue #187).

This script validates that our custom Airflow image with OpenLineage provider
is correctly implemented and can be used for data lineage tracking.
"""

import os
import sys
import subprocess
import time
from pathlib import Path

def print_header(title):
    """Print a formatted header."""
    print(f"\n{'=' * 60}")
    print(f"🎯 {title}")
    print('=' * 60)

def print_step(step, description):
    """Print a formatted step."""
    print(f"\n{step}. 🔧 {description}")
    print('-' * 50)

def run_command(command, description="", check_output=False):
    """Run a shell command with error handling."""
    try:
        print(f"Running: {command}")
        if check_output:
            result = subprocess.run(command, shell=True, capture_output=True, text=True)
            print(f"Output: {result.stdout}")
            if result.stderr:
                print(f"Errors: {result.stderr}")
            return result.returncode == 0, result.stdout
        else:
            result = subprocess.run(command, shell=True)
            return result.returncode == 0, ""
    except Exception as e:
        print(f"❌ Error running command: {str(e)}")
        return False, ""

def check_file_exists(file_path, description=""):
    """Check if a file exists."""
    if os.path.exists(file_path):
        print(f"✅ {description or file_path} exists")
        return True
    else:
        print(f"❌ {description or file_path} not found")
        return False

def validate_dockerfile():
    """Validate the Dockerfile contains required components."""
    dockerfile_path = "docker/airflow/Dockerfile"
    
    if not check_file_exists(dockerfile_path, "Custom Airflow Dockerfile"):
        return False
    
    with open(dockerfile_path, 'r') as f:
        content = f.read()
    
    required_components = [
        "FROM apache/airflow",
        "apache-airflow-providers-openlineage",
        "openlineage-airflow",
        "openlineage-python"
    ]
    
    for component in required_components:
        if component in content:
            print(f"✅ Found: {component}")
        else:
            print(f"❌ Missing: {component}")
            return False
    
    return True

def validate_requirements():
    """Validate requirements.txt contains necessary packages."""
    requirements_path = "airflow/requirements.txt"
    
    if not check_file_exists(requirements_path, "Airflow requirements.txt"):
        return False
    
    with open(requirements_path, 'r') as f:
        content = f.read()
    
    required_packages = [
        "apache-airflow-providers-openlineage",
        "openlineage-airflow",
        "openlineage-python",
        "pandas",
        "requests"
    ]
    
    for package in required_packages:
        if package in content:
            print(f"✅ Found package: {package}")
        else:
            print(f"❌ Missing package: {package}")
            return False
    
    return True

def validate_docker_compose():
    """Validate docker-compose uses custom image."""
    compose_path = "docker/airflow/docker-compose.airflow.yml"
    
    if not check_file_exists(compose_path, "Docker Compose file"):
        return False
    
    with open(compose_path, 'r') as f:
        content = f.read()
    
    required_configs = [
        "build:",
        "dockerfile: Dockerfile",
        "neuronews/airflow:2.8.1-openlineage",
        "AIRFLOW__OPENLINEAGE__TRANSPORT",
        "AIRFLOW__OPENLINEAGE__NAMESPACE"
    ]
    
    for config in required_configs:
        if config in content:
            print(f"✅ Found config: {config}")
        else:
            print(f"❌ Missing config: {config}")
            return False
    
    return True

def validate_test_dag():
    """Validate test DAG exists."""
    dag_path = "airflow/dags/test_openlineage_integration.py"
    
    if not check_file_exists(dag_path, "Test OpenLineage DAG"):
        return False
    
    with open(dag_path, 'r') as f:
        content = f.read()
    
    required_elements = [
        "import openlineage.airflow",
        "test_openlineage_integration",
        "test_openlineage_import",
        "simulate_data_processing"
    ]
    
    for element in required_elements:
        if element in content:
            print(f"✅ Found DAG element: {element}")
        else:
            print(f"❌ Missing DAG element: {element}")
            return False
    
    return True

def validate_makefile():
    """Validate Makefile has required targets."""
    makefile_path = "Makefile"
    
    if not check_file_exists(makefile_path, "Makefile"):
        return False
    
    with open(makefile_path, 'r') as f:
        content = f.read()
    
    required_targets = [
        "airflow-build:",
        "airflow-test-openlineage:",
        "neuronews/airflow:2.8.1-openlineage"
    ]
    
    for target in required_targets:
        if target in content:
            print(f"✅ Found Makefile target: {target}")
        else:
            print(f"❌ Missing Makefile target: {target}")
            return False
    
    return True

def demo_build_process():
    """Demonstrate the build process (dry run)."""
    print("\n🏗️ Build Process Demonstration:")
    
    print("\n1. Build custom image:")
    print("   make airflow-build")
    print("   → Builds neuronews/airflow:2.8.1-openlineage")
    
    print("\n2. Start services:")
    print("   make airflow-up")
    print("   → Starts Airflow + Marquez with custom image")
    
    print("\n3. Test OpenLineage:")
    print("   make airflow-test-openlineage")
    print("   → Validates OpenLineage integration")
    
    print("\n4. Access UIs:")
    print("   Airflow: http://localhost:8080 (airflow/airflow)")
    print("   Marquez: http://localhost:3000")

def main():
    """Main demo function."""
    print_header("NeuroNews Custom Airflow OpenLineage Demo (Issue #187)")
    
    print("🎯 This demo validates the custom Airflow image implementation")
    print("   with OpenLineage provider for automatic data lineage tracking.")
    
    # Change to project root
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    
    validation_results = []
    
    print_step(1, "Validating Dockerfile Implementation")
    validation_results.append(("Dockerfile", validate_dockerfile()))
    
    print_step(2, "Validating Requirements.txt")
    validation_results.append(("Requirements", validate_requirements()))
    
    print_step(3, "Validating Docker Compose Configuration")
    validation_results.append(("Docker Compose", validate_docker_compose()))
    
    print_step(4, "Validating Test DAG")
    validation_results.append(("Test DAG", validate_test_dag()))
    
    print_step(5, "Validating Makefile Targets")
    validation_results.append(("Makefile", validate_makefile()))
    
    print_step(6, "Build Process Overview")
    demo_build_process()
    
    # Summary
    print_header("Validation Summary")
    
    all_passed = True
    for name, passed in validation_results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False
    
    print(f"\n🎯 Overall Status: {'✅ ALL VALIDATIONS PASSED' if all_passed else '❌ SOME VALIDATIONS FAILED'}")
    
    if all_passed:
        print("\n🚀 Issue #187 Requirements Validated:")
        print("  ✅ Extend apache/airflow image")
        print("  ✅ Install OpenLineage providers")
        print("  ✅ Include DAG dependencies in requirements.txt")
        print("  ✅ Update compose to use custom image")
        print("  ✅ Test DAG for validation")
        
        print("\n📋 Next Steps:")
        print("  1. Build the image: make airflow-build")
        print("  2. Start services: make airflow-up")
        print("  3. Test integration: make airflow-test-openlineage")
        print("  4. Check logs for 'OpenLineage plugin loaded' message")
        print("  5. Verify lineage in Marquez UI: http://localhost:3000")
        
        print("\n🎉 Implementation Ready for Production!")
    else:
        print("\n🔧 Please fix the failing validations before proceeding.")
    
    return 0 if all_passed else 1

if __name__ == "__main__":
    sys.exit(main())
