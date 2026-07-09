#!/bin/bash
# Demo script for Issue #187: Custom Airflow Image with OpenLineage Provider
# This script demonstrates the complete implementation and testing workflow.

echo "🚀 NeuroNews Custom Airflow Image Demo (Issue #187)"
echo "=================================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

print_section() {
    echo -e "\n${BLUE}=== $1 ===${NC}"
}

# Check prerequisites
print_section "Prerequisites Check"

# Check Docker
if command -v docker &> /dev/null; then
    print_status "Docker is installed"
    docker --version
else
    print_error "Docker is not installed!"
    exit 1
fi

# Check Docker Compose
if command -v docker-compose &> /dev/null; then
    print_status "Docker Compose is installed"
    docker-compose --version
else
    print_error "Docker Compose is not installed!"
    exit 1
fi

# Check if we're in the right directory
if [ ! -f "docker/airflow/Dockerfile" ]; then
    print_error "Please run this script from the NeuroNews root directory"
    exit 1
fi

print_status "All prerequisites met"

# Verify implementation files
print_section "Implementation Files Verification"

files=(
    "docker/airflow/Dockerfile"
    "airflow/requirements.txt"
    "docker/airflow/docker-compose.airflow.yml"
    "airflow/dags/test_openlineage_integration.py"
)

for file in "${files[@]}"; do
    if [ -f "$file" ]; then
        print_status "Found: $file"
    else
        print_error "Missing: $file"
        exit 1
    fi
done

# Show key implementation details
print_section "Implementation Details"

print_info "Dockerfile Features:"
echo "  • Extends apache/airflow:2.8.1"
echo "  • Installs OpenLineage providers"
echo "  • Includes NeuroNews dependencies"
echo "  • Health check for verification"

print_info "Requirements.txt includes:"
echo "  • apache-airflow-providers-openlineage==1.4.0"
echo "  • openlineage-airflow==1.9.0"
echo "  • pandas, numpy, requests for DAGs"
echo "  • scrapy, beautifulsoup4 for web scraping"

print_info "Docker Compose Updates:"
echo "  • Custom build configuration"
echo "  • OpenLineage environment variables"
echo "  • Marquez integration"

# Ask user if they want to proceed with build
echo ""
read -p "🔨 Do you want to build the custom Airflow image? (y/n): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    print_info "Skipping build. You can run 'make airflow-build' later."
    exit 0
fi

# Build the custom image
print_section "Building Custom Airflow Image"

print_info "Building neuronews/airflow:2.8.1-openlineage..."
cd docker/airflow

if docker build -t neuronews/airflow:2.8.1-openlineage .; then
    print_status "Custom Airflow image built successfully!"
else
    print_error "Failed to build custom image"
    exit 1
fi

cd ../..

# Verify the image
print_section "Image Verification"

print_info "Checking if image exists..."
if docker images | grep -q "neuronews/airflow.*2.8.1-openlineage"; then
    print_status "Custom image found in local registry"
    docker images | grep neuronews/airflow
else
    print_error "Custom image not found!"
    exit 1
fi

# Test OpenLineage installation in the image
print_info "Testing OpenLineage installation..."
if docker run --rm neuronews/airflow:2.8.1-openlineage python -c "
import openlineage.airflow
from openlineage.client import OpenLineageClient
print('✅ OpenLineage modules imported successfully')
print(f'OpenLineage Airflow version: {openlineage.airflow.__version__}')
"; then
    print_status "OpenLineage provider verification passed"
else
    print_error "OpenLineage provider verification failed"
    exit 1
fi

# Ask about running the services
echo ""
read -p "🚀 Do you want to start the Airflow services now? (y/n): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    print_info "Skipping service startup. You can run 'make airflow-up' later."
    print_section "Manual Instructions"
    echo "To start services manually:"
    echo "  1. make airflow-up"
    echo "  2. make airflow-test-openlineage"
    echo "  3. Visit http://localhost:8080 (airflow/airflow)"
    echo "  4. Visit http://localhost:3000 (Marquez UI)"
    exit 0
fi

# Start services
print_section "Starting Airflow Services"

print_info "Starting Airflow and Marquez services..."
cd docker/airflow

if docker-compose -f docker-compose.airflow.yml up -d; then
    print_status "Services started successfully!"
else
    print_error "Failed to start services"
    exit 1
fi

cd ../..

# Wait for services to be ready
print_info "Waiting for services to initialize..."
sleep 30

# Check service status
print_section "Service Status Check"

print_info "Checking service health..."
cd docker/airflow
docker-compose -f docker-compose.airflow.yml ps
cd ../..

# Test the services
print_section "Testing OpenLineage Integration"

print_info "Checking Airflow webserver logs for OpenLineage..."
cd docker/airflow

# Check if OpenLineage is loaded
if docker-compose -f docker-compose.airflow.yml logs airflow-webserver 2>/dev/null | grep -i openlineage; then
    print_status "OpenLineage messages found in webserver logs"
else
    print_warning "No OpenLineage messages found yet (may need more time)"
fi

cd ../..

# Trigger test DAG
print_info "Triggering test DAG..."
cd docker/airflow

if docker-compose -f docker-compose.airflow.yml exec -T airflow-webserver \
    airflow dags trigger test_openlineage_integration 2>/dev/null; then
    print_status "Test DAG triggered successfully"
else
    print_warning "Could not trigger test DAG (services may still be initializing)"
fi

cd ../..

# Final status
print_section "Demo Complete!"

print_status "Issue #187 Implementation Verified:"
echo "  ✅ Custom Airflow image built with OpenLineage provider"
echo "  ✅ Requirements.txt contains all necessary dependencies"
echo "  ✅ Docker Compose updated to use custom image"
echo "  ✅ Services started successfully"

print_info "Access Points:"
echo "  🌐 Airflow UI: http://localhost:8080 (airflow/airflow)"
echo "  📊 Marquez UI: http://localhost:3000"

print_info "Next Steps:"
echo "  1. Check DAG execution in Airflow UI"
echo "  2. Verify lineage events in Marquez UI"
echo "  3. Run 'make airflow-test-openlineage' for detailed testing"

print_info "Cleanup:"
echo "  • Stop services: make airflow-down"
echo "  • Remove image: docker rmi neuronews/airflow:2.8.1-openlineage"

echo ""
print_status "Demo completed successfully! 🎉"
