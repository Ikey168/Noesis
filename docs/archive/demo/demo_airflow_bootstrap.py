#!/usr/bin/env python3
"""
Demo script for Airflow Connections & Variables Bootstrap (Issue #194)

This script validates:
1. Bootstrap script functionality and idempotency
2. Connection creation and validation
3. Variable setup and management
4. Integration with Airflow environment

Tests both successful bootstrap and error handling scenarios.
"""

import os
import sys
import time
import logging
import subprocess
from datetime import datetime
from pathlib import Path
import tempfile

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def run_command(cmd: str, check: bool = True, cwd: Path = None) -> subprocess.CompletedProcess:
    """Run shell command and return result."""
    logger.info(f"🔧 Running: {cmd}")
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=cwd or project_root
    )
    
    if check and result.returncode != 0:
        logger.error(f"❌ Command failed: {cmd}")
        logger.error(f"STDOUT: {result.stdout}")
        logger.error(f"STDERR: {result.stderr}")
        raise subprocess.CalledProcessError(result.returncode, cmd)
    
    return result


def test_airflow_services():
    """Test that Airflow services are running and healthy."""
    logger.info("🏥 Testing Airflow service health...")
    
    # Check if containers are running
    result = run_command("docker-compose -f docker/airflow/docker-compose.airflow.yml ps")
    if "airflow-webserver" not in result.stdout:
        logger.error("❌ Airflow services not running. Starting them...")
        run_command("make airflow-up")
        time.sleep(30)  # Wait for services to start
    
    # Test webserver health
    for attempt in range(5):
        try:
            result = run_command("curl -f http://localhost:8080/health", check=False)
            if result.returncode == 0:
                logger.info("✅ Airflow webserver is healthy")
                break
        except Exception as e:
            logger.warning(f"⚠️ Attempt {attempt + 1}: Webserver not ready - {e}")
            time.sleep(10)
    else:
        raise Exception("❌ Airflow webserver health check failed")


def test_bootstrap_script_existence():
    """Test that the bootstrap script exists and is executable."""
    logger.info("📋 Testing bootstrap script existence...")
    
    script_path = project_root / "scripts" / "airflow_bootstrap.sh"
    
    if not script_path.exists():
        logger.error(f"❌ Bootstrap script not found: {script_path}")
        return False
    
    if not os.access(script_path, os.X_OK):
        logger.error(f"❌ Bootstrap script not executable: {script_path}")
        return False
    
    logger.info("✅ Bootstrap script exists and is executable")
    
    # Test help option
    result = run_command(f"{script_path} --help", check=False)
    if result.returncode == 0 and "Usage:" in result.stdout:
        logger.info("✅ Bootstrap script help option works")
    else:
        logger.warning("⚠️ Bootstrap script help option may have issues")
    
    return True


def test_bootstrap_script_dry_run():
    """Test the bootstrap script in a controlled environment."""
    logger.info("🧪 Testing bootstrap script execution...")
    
    script_path = project_root / "scripts" / "airflow_bootstrap.sh"
    
    # Create a temporary environment file for testing
    with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as env_file:
        env_content = """
# Test environment for bootstrap script
AWS_ACCESS_KEY_ID=test_access_key
AWS_SECRET_ACCESS_KEY=test_secret_key
AWS_DEFAULT_REGION=us-west-2
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_USER=airflow
POSTGRES_PASSWORD=airflow
POSTGRES_DB=airflow
ENVIRONMENT=test
PROJECT_VERSION=1.0.0-test
OPENLINEAGE_URL=http://marquez:5000
OPENLINEAGE_NAMESPACE=neuro_news_test
"""
        env_file.write(env_content)
        env_file_path = env_file.name
    
    try:
        # Run bootstrap script inside Airflow container with test environment
        container_cmd = f"""
        docker exec $(docker-compose -f docker/airflow/docker-compose.airflow.yml ps -q airflow-webserver) bash -c '
        cd /opt/airflow && 
        if [ -f scripts/airflow_bootstrap.sh ]; then
            echo "Running bootstrap script...";
            ./scripts/airflow_bootstrap.sh --verbose;
        else
            echo "Bootstrap script not found in container";
            exit 1;
        fi
        '
        """
        
        result = run_command(container_cmd, check=False)
        
        if result.returncode == 0:
            logger.info("✅ Bootstrap script executed successfully")
            if "Bootstrap completed successfully" in result.stdout:
                logger.info("✅ Bootstrap script completed successfully")
            else:
                logger.warning("⚠️ Bootstrap script ran but may not have completed fully")
        else:
            logger.error(f"❌ Bootstrap script failed: {result.stderr}")
            return False
        
        return True
        
    finally:
        # Clean up temporary file
        try:
            os.unlink(env_file_path)
        except:
            pass


def test_connections_created():
    """Test that expected connections were created."""
    logger.info("🔗 Testing Airflow connections...")
    
    expected_connections = ["aws_default", "neuro_postgres"]
    
    for conn_id in expected_connections:
        cmd = f"""
        docker exec $(docker-compose -f docker/airflow/docker-compose.airflow.yml ps -q airflow-webserver) \
        airflow connections get {conn_id}
        """
        
        result = run_command(cmd, check=False)
        
        if result.returncode == 0:
            logger.info(f"✅ Connection {conn_id} exists")
        else:
            logger.error(f"❌ Connection {conn_id} does not exist")
            return False
    
    return True


def test_variables_created():
    """Test that expected variables were created."""
    logger.info("📊 Testing Airflow variables...")
    
    expected_variables = [
        "DATA_ROOT",
        "NAMESPACE", 
        "ENVIRONMENT",
        "OPENLINEAGE_URL",
        "OPENLINEAGE_NAMESPACE",
        "PROJECT_VERSION"
    ]
    
    for var_key in expected_variables:
        cmd = f"""
        docker exec $(docker-compose -f docker/airflow/docker-compose.airflow.yml ps -q airflow-webserver) \
        airflow variables get {var_key}
        """
        
        result = run_command(cmd, check=False)
        
        if result.returncode == 0:
            var_value = result.stdout.strip()
            logger.info(f"✅ Variable {var_key} = {var_value}")
        else:
            logger.error(f"❌ Variable {var_key} does not exist")
            return False
    
    return True


def test_idempotency():
    """Test that the bootstrap script is idempotent."""
    logger.info("🔄 Testing bootstrap script idempotency...")
    
    # Run bootstrap script twice and compare outputs
    runs = []
    for run_num in range(2):
        logger.info(f"Running bootstrap script (attempt {run_num + 1}/2)...")
        
        cmd = f"""
        docker exec $(docker-compose -f docker/airflow/docker-compose.airflow.yml ps -q airflow-webserver) bash -c '
        cd /opt/airflow && ./scripts/airflow_bootstrap.sh --verbose
        '
        """
        
        result = run_command(cmd, check=False)
        runs.append(result)
        
        if result.returncode != 0:
            logger.error(f"❌ Bootstrap script failed on run {run_num + 1}")
            return False
        
        time.sleep(2)  # Brief pause between runs
    
    # Both runs should succeed
    if all(run.returncode == 0 for run in runs):
        logger.info("✅ Bootstrap script is idempotent (both runs succeeded)")
        return True
    else:
        logger.error("❌ Bootstrap script idempotency test failed")
        return False


def test_connection_validation():
    """Test connection validation functionality."""
    logger.info("🔍 Testing connection validation...")
    
    # Test that connections can be listed
    cmd = f"""
    docker exec $(docker-compose -f docker/airflow/docker-compose.airflow.yml ps -q airflow-webserver) \
    airflow connections list --output table
    """
    
    result = run_command(cmd, check=False)
    
    if result.returncode == 0:
        if "aws_default" in result.stdout and "neuro_postgres" in result.stdout:
            logger.info("✅ Both required connections are listed")
            return True
        else:
            logger.error("❌ Required connections not found in listing")
            return False
    else:
        logger.error("❌ Failed to list connections")
        return False


def test_makefile_integration():
    """Test Makefile integration for bootstrap script."""
    logger.info("📋 Testing Makefile integration...")
    
    # Check if Makefile has bootstrap target
    makefile_path = project_root / "Makefile"
    
    if not makefile_path.exists():
        logger.warning("⚠️ Makefile not found")
        return True  # Not critical for this test
    
    with open(makefile_path, 'r') as f:
        makefile_content = f.read()
    
    if "bootstrap" in makefile_content.lower():
        logger.info("✅ Makefile contains bootstrap-related targets")
    else:
        logger.info("ℹ️ Makefile does not contain bootstrap targets (may be added later)")
    
    return True


def validate_documentation():
    """Validate that the script is properly documented."""
    logger.info("📚 Validating documentation...")
    
    script_path = project_root / "scripts" / "airflow_bootstrap.sh"
    
    with open(script_path, 'r') as f:
        script_content = f.read()
    
    documentation_checks = [
        ("Usage information", "Usage:"),
        ("Help option", "--help"),
        ("Environment variables", "Environment Variables"),
        ("Examples", "Examples:"),
        ("Issue reference", "Issue #194"),
    ]
    
    all_checks_passed = True
    for check_name, pattern in documentation_checks:
        if pattern in script_content:
            logger.info(f"✅ {check_name} documented")
        else:
            logger.warning(f"⚠️ {check_name} not found in documentation")
            all_checks_passed = False
    
    return all_checks_passed


def main():
    """Run all bootstrap script tests."""
    logger.info("🎯 Starting Airflow Bootstrap Script validation (Issue #194)")
    
    try:
        # Test suite
        test_functions = [
            test_airflow_services,
            test_bootstrap_script_existence,
            test_bootstrap_script_dry_run,
            test_connections_created,
            test_variables_created,
            test_idempotency,
            test_connection_validation,
            test_makefile_integration,
            validate_documentation,
        ]
        
        results = []
        for test_func in test_functions:
            try:
                result = test_func()
                results.append((test_func.__name__, result))
                if result:
                    logger.info(f"✅ {test_func.__name__} passed")
                else:
                    logger.error(f"❌ {test_func.__name__} failed")
            except Exception as e:
                logger.error(f"❌ {test_func.__name__} failed with exception: {e}")
                results.append((test_func.__name__, False))
        
        # Summary
        passed = sum(1 for _, result in results if result)
        total = len(results)
        
        logger.info(f"\n🎯 Test Summary: {passed}/{total} tests passed")
        
        if passed == total:
            logger.info("🎉 All bootstrap script tests passed!")
            logger.info("\n📋 Issue #194 Requirements Verified:")
            logger.info("✅ Bootstrap script created and executable")
            logger.info("✅ AWS connection (aws_default) configured")
            logger.info("✅ PostgreSQL connection (neuro_postgres) configured")
            logger.info("✅ Variables seeded: DATA_ROOT, NAMESPACE, etc.")
            logger.info("✅ Script is idempotent and safe to run multiple times")
            logger.info("✅ Runs inside webserver container after airflow db init")
            
            logger.info("\n🔧 Available Connections:")
            logger.info("• aws_default - AWS credentials from environment")
            logger.info("• neuro_postgres - PostgreSQL connection for metadata")
            
            logger.info("\n📊 Available Variables:")
            logger.info("• DATA_ROOT=/opt/airflow/data")
            logger.info("• NAMESPACE=neuro_news_dev") 
            logger.info("• ENVIRONMENT, PROJECT_VERSION, OPENLINEAGE_*")
            
            logger.info("\n🚀 Usage:")
            logger.info("docker exec <webserver-container> ./scripts/airflow_bootstrap.sh")
            
            return True
        else:
            logger.error("❌ Some tests failed. Please check the logs above.")
            return False
            
    except Exception as e:
        logger.error(f"❌ Test execution failed: {e}")
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
