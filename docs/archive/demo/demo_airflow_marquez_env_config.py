#!/usr/bin/env python3
"""
Demo: Airflow → Marquez OpenLineage Environment Configuration (Issue #188)

This demo verifies that OpenLineage environment variables are properly
configured to send lineage events from Airflow to Marquez with the
correct namespace.

Requirements:
- Docker and docker-compose
- Airflow + Marquez services running
- Environment variables properly set
"""

import os
import time
import requests
import subprocess
import sys
from datetime import datetime


class AirflowMarquezDemo:
    def __init__(self):
        self.airflow_url = "http://localhost:8080"
        self.marquez_url = "http://localhost:3000"
        self.marquez_api_url = "http://localhost:5000"
        self.expected_namespace = os.getenv('OPENLINEAGE_NAMESPACE', 'neuro_news_dev')
        
    def check_services(self):
        """Check if required services are running."""
        print("🔍 Checking services...")
        
        services = {
            "Airflow": self.airflow_url,
            "Marquez UI": self.marquez_url,
            "Marquez API": self.marquez_api_url
        }
        
        for name, url in services.items():
            try:
                response = requests.get(f"{url}/health" if "api" in url else url, timeout=5)
                if response.status_code < 400:
                    print(f"✅ {name} is running at {url}")
                else:
                    print(f"⚠️  {name} responded with status {response.status_code}")
            except requests.exceptions.RequestException:
                print(f"❌ {name} is not accessible at {url}")
                return False
        
        return True
    
    def check_environment_variables(self):
        """Check OpenLineage environment variables in Airflow containers."""
        print("\n🔧 Checking environment variables...")
        
        try:
            # Check environment variables
            result = subprocess.run([
                "docker-compose", "-f", "docker/airflow/docker-compose.airflow.yml",
                "exec", "-T", "airflow-webserver", "env"
            ], capture_output=True, text=True, cwd="/workspaces/NeuroNews")
            
            env_output = result.stdout
            
            # Check for required variables
            required_vars = [
                "OPENLINEAGE_URL",
                "OPENLINEAGE_NAMESPACE", 
                "OPENLINEAGE_DISABLED"
            ]
            
            found_vars = {}
            for var in required_vars:
                for line in env_output.split('\n'):
                    if line.startswith(f"{var}="):
                        found_vars[var] = line.split('=', 1)[1]
                        break
            
            print(f"Found environment variables:")
            for var, value in found_vars.items():
                print(f"  {var}={value}")
            
            # Verify Airflow OpenLineage configuration
            config_result = subprocess.run([
                "docker-compose", "-f", "docker/airflow/docker-compose.airflow.yml",
                "exec", "-T", "airflow-webserver", "airflow", "config", "get-value", "openlineage", "namespace"
            ], capture_output=True, text=True, cwd="/workspaces/NeuroNews")
            
            if config_result.returncode == 0:
                airflow_namespace = config_result.stdout.strip()
                print(f"✅ Airflow OpenLineage namespace: {airflow_namespace}")
                
                if airflow_namespace == self.expected_namespace:
                    print(f"✅ Namespace matches expected: {self.expected_namespace}")
                else:
                    print(f"⚠️  Namespace mismatch. Expected: {self.expected_namespace}, Got: {airflow_namespace}")
            else:
                print(f"❌ Could not get Airflow OpenLineage configuration")
                
            return len(found_vars) == len(required_vars)
            
        except subprocess.CalledProcessError as e:
            print(f"❌ Error checking environment: {e}")
            return False
    
    def trigger_test_dag(self):
        """Trigger the test DAG to generate lineage events."""
        print("\n🚀 Triggering test DAG...")
        
        try:
            result = subprocess.run([
                "docker-compose", "-f", "docker/airflow/docker-compose.airflow.yml",
                "exec", "-T", "airflow-webserver", "airflow", "dags", "trigger", "test_openlineage_integration"
            ], capture_output=True, text=True, cwd="/workspaces/NeuroNews")
            
            if result.returncode == 0:
                print("✅ Test DAG triggered successfully")
                print("⏱️  Waiting for DAG execution...")
                time.sleep(10)  # Wait for execution
                return True
            else:
                print(f"❌ Failed to trigger DAG: {result.stderr}")
                return False
                
        except subprocess.CalledProcessError as e:
            print(f"❌ Error triggering DAG: {e}")
            return False
    
    def check_marquez_lineage(self):
        """Check if lineage events appear in Marquez with correct namespace."""
        print("\n📊 Checking Marquez lineage events...")
        
        try:
            # Get namespaces from Marquez API
            response = requests.get(f"{self.marquez_api_url}/api/v1/namespaces", timeout=10)
            
            if response.status_code == 200:
                namespaces = response.json().get('namespaces', [])
                namespace_names = [ns.get('name') for ns in namespaces]
                
                print(f"Available namespaces in Marquez: {namespace_names}")
                
                if self.expected_namespace in namespace_names:
                    print(f"✅ Found expected namespace: {self.expected_namespace}")
                    
                    # Get jobs in the namespace
                    jobs_response = requests.get(
                        f"{self.marquez_api_url}/api/v1/namespaces/{self.expected_namespace}/jobs",
                        timeout=10
                    )
                    
                    if jobs_response.status_code == 200:
                        jobs = jobs_response.json().get('jobs', [])
                        print(f"📋 Found {len(jobs)} jobs in namespace {self.expected_namespace}")
                        
                        for job in jobs[:3]:  # Show first 3 jobs
                            print(f"  - {job.get('name', 'Unknown')}")
                        
                        if jobs:
                            print("✅ Lineage events successfully sent to Marquez!")
                            return True
                        else:
                            print("⚠️  No jobs found in namespace yet. May need more time.")
                            return False
                    else:
                        print(f"❌ Could not get jobs from namespace: {jobs_response.status_code}")
                        return False
                else:
                    print(f"❌ Expected namespace '{self.expected_namespace}' not found in Marquez")
                    return False
            else:
                print(f"❌ Could not get namespaces from Marquez API: {response.status_code}")
                return False
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Error checking Marquez: {e}")
            return False
    
    def run_demo(self):
        """Run the complete demo."""
        print("🎯 Airflow → Marquez OpenLineage Environment Configuration Demo")
        print("=" * 60)
        print(f"Expected namespace: {self.expected_namespace}")
        print(f"Timestamp: {datetime.now().isoformat()}")
        print()
        
        # Step 1: Check services
        if not self.check_services():
            print("\n❌ Demo failed: Required services not running")
            print("💡 Run: make airflow-up")
            return False
        
        # Step 2: Check environment variables
        if not self.check_environment_variables():
            print("\n❌ Demo failed: Environment variables not properly configured")
            return False
        
        # Step 3: Trigger test DAG
        if not self.trigger_test_dag():
            print("\n❌ Demo failed: Could not trigger test DAG")
            return False
        
        # Step 4: Check Marquez lineage
        if not self.check_marquez_lineage():
            print("\n⚠️  Demo partially successful: Lineage events may still be processing")
            print("💡 Check Marquez UI manually: http://localhost:3000")
            return False
        
        print("\n🎉 Demo completed successfully!")
        print("✅ All requirements for Issue #188 verified:")
        print("  - Environment variables properly configured")
        print("  - DAG triggered successfully")
        print(f"  - Lineage events appear in Marquez under '{self.expected_namespace}' namespace")
        print()
        print("🔗 Access points:")
        print(f"  - Airflow UI: {self.airflow_url}")
        print(f"  - Marquez UI: {self.marquez_url}")
        
        return True


def main():
    """Main demo function."""
    demo = AirflowMarquezDemo()
    
    try:
        success = demo.run_demo()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⏹️  Demo interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Demo failed with error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
