#!/bin/bash
# NeuroNews Container Build and Push Script
# For Issue #71 - Containerize NeuroNews Services

set -e

# Configuration
REGISTRY=${REGISTRY:-"neuronews"}
VERSION=${VERSION:-"latest"}
DOCKER_REPO=${DOCKER_REPO:-"docker.io"}

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo -e "${BLUE}[HEADER]${NC} $1"
}

# Check if Docker is running
check_docker() {
    if ! docker info > /dev/null 2>&1; then
        print_error "Docker is not running. Please start Docker first."
        exit 1
    fi
    print_status "Docker is running"
}

# Build Docker image
build_image() {
    local service=$1
    local dockerfile=$2
    local context=${3:-"."}
    
    print_header "Building $service image..."
    
    # Build development image
    docker build \
        -f "$dockerfile" \
        --target development \
        -t "${REGISTRY}/${service}:${VERSION}-dev" \
        -t "${REGISTRY}/${service}:dev" \
        "$context"
    
    # Build production image
    docker build \
        -f "$dockerfile" \
        --target production \
        -t "${REGISTRY}/${service}:${VERSION}" \
        -t "${REGISTRY}/${service}:latest" \
        "$context"
    
    print_status "Successfully built $service images"
}

# Push image to registry
push_image() {
    local service=$1
    
    print_header "Pushing $service images to registry..."
    
    # Push development image
    docker push "${REGISTRY}/${service}:${VERSION}-dev"
    docker push "${REGISTRY}/${service}:dev"
    
    # Push production image
    docker push "${REGISTRY}/${service}:${VERSION}"
    docker push "${REGISTRY}/${service}:latest"
    
    print_status "Successfully pushed $service images"
}

# Login to Docker registry
docker_login() {
    if [ -n "$DOCKER_USERNAME" ] && [ -n "$DOCKER_PASSWORD" ]; then
        print_status "Logging in to Docker registry..."
        echo "$DOCKER_PASSWORD" | docker login "$DOCKER_REPO" -u "$DOCKER_USERNAME" --password-stdin
    else
        print_warning "Docker credentials not provided. Skipping login."
        print_warning "Set DOCKER_USERNAME and DOCKER_PASSWORD environment variables to push images."
    fi
}

# Optimize images
optimize_images() {
    print_header "Optimizing Docker images..."
    
    # Remove dangling images
    docker image prune -f
    
    # Show image sizes
    echo
    print_status "Image sizes:"
    docker images "${REGISTRY}/*" --format "table {{.Repository}}:{{.Tag}}\t{{.Size}}"
}

# Main build process
main() {
    print_header "🐳 NeuroNews Containerization - Issue #71"
    print_status "Building and optimizing Docker images for all services"
    echo
    
    # Check prerequisites
    check_docker
    
    # Login to registry if credentials provided
    docker_login
    
    # Build all service images
    print_header "Building all service images..."
    
    # Build FastAPI service
    build_image "fastapi" "docker/fastapi.Dockerfile"
    
    # Build Scraper service
    build_image "scraper" "docker/scraper.Dockerfile"
    
    # Build NLP service
    build_image "nlp" "docker/nlp.Dockerfile"

    # Optimize images
    optimize_images
    
    # Push images if credentials are available
    if [ -n "$DOCKER_USERNAME" ] && [ -n "$DOCKER_PASSWORD" ]; then
        print_header "Pushing all images to registry..."
        
        push_image "fastapi"
        push_image "scraper"
        push_image "nlp"
        push_image "dashboard"
        
        print_status "All images successfully pushed to registry!"
    else
        print_warning "Skipping push - no Docker credentials provided"
        print_status "To push images, set DOCKER_USERNAME and DOCKER_PASSWORD environment variables"
    fi
    
    echo
    print_header "✅ Containerization Complete!"
    print_status "All NeuroNews services have been successfully containerized"
    print_status "Images are ready for local development and production deployment"
    
    echo
    print_status "To start development environment:"
    echo "  docker-compose -f docker-compose.dev.yml up -d"
    echo
    print_status "To start production environment:"
    echo "  docker-compose -f docker-compose.prod.yml up -d"
    echo
}

# Parse command line arguments
case "${1:-build}" in
    "build")
        main
        ;;
    "push")
        check_docker
        docker_login
        push_image "fastapi"
        push_image "scraper"
        push_image "nlp"
        push_image "dashboard"
        ;;
    "dev")
        print_status "Starting development environment..."
        docker-compose -f docker-compose.dev.yml up -d
        ;;
    "prod")
        print_status "Starting production environment..."
        docker-compose -f docker-compose.prod.yml up -d
        ;;
    "stop")
        print_status "Stopping all environments..."
        docker-compose -f docker-compose.dev.yml down
        docker-compose -f docker-compose.prod.yml down
        ;;
    "clean")
        print_status "Cleaning up Docker resources..."
        docker-compose -f docker-compose.dev.yml down -v
        docker-compose -f docker-compose.prod.yml down -v
        docker system prune -af
        ;;
    *)
        echo "Usage: $0 {build|push|dev|prod|stop|clean}"
        echo "  build - Build all Docker images"
        echo "  push  - Push images to registry"
        echo "  dev   - Start development environment"
        echo "  prod  - Start production environment"
        echo "  stop  - Stop all environments"
        echo "  clean - Clean up all Docker resources"
        exit 1
        ;;
esac
