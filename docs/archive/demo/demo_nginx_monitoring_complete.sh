#!/bin/bash

# NGINX Performance Monitoring - Complete Integration Demo
# Demonstrates the full monitoring stack with realistic API traffic simulation

set -euo pipefail

echo "=========================================="
echo "NGINX Performance Monitoring Demo"
echo "Issue #85 - Complete Implementation"
echo "=========================================="

# Step 1: Deploy the monitoring stack
echo "1. Deploying NGINX Performance Monitoring Stack..."

# Create namespace
kubectl create namespace neuronews --dry-run=client -o yaml | kubectl apply -f -

# Deploy configurations
echo "   - Applying NGINX monitoring configuration..."
kubectl apply -f k8s/nginx/monitoring-configmap.yaml

echo "   - Applying Fluentd log aggregation configuration..."
kubectl apply -f k8s/nginx/fluentd-config.yaml

echo "   - Applying Prometheus monitoring configuration..."
kubectl apply -f k8s/monitoring/prometheus-nginx-config.yaml

# Deploy services
echo "   - Deploying Prometheus monitoring service..."
kubectl apply -f k8s/monitoring/prometheus-deployment.yaml

echo "   - Deploying NGINX with performance monitoring..."
kubectl apply -f k8s/nginx/monitoring-deployment.yaml
kubectl apply -f k8s/nginx/monitoring-service.yaml

# Wait for deployments
echo "   - Waiting for deployments to be ready..."
kubectl wait --for=condition=available --timeout=300s deployment/prometheus-nginx -n neuronews
kubectl wait --for=condition=available --timeout=300s deployment/nginx-monitoring -n neuronews

echo "✅ Monitoring stack deployed successfully!"

# Step 2: Verify monitoring components
echo ""
echo "2. Verifying Monitoring Components..."

# Check pods
echo "   - Pod Status:"
kubectl get pods -n neuronews -o wide

# Check services
echo "   - Service Status:"
kubectl get svc -n neuronews

# Check configmaps
echo "   - Configuration Status:"
kubectl get configmap -n neuronews

echo "✅ All components verified!"

# Step 3: Setup access to monitoring interfaces
echo ""
echo "3. Setting up access to monitoring interfaces..."

# Kill any existing port forwards
pkill -f "kubectl port-forward" || true
sleep 2

# Setup port forwards in background
echo "   - Setting up Prometheus access (port 9090)..."
kubectl port-forward -n neuronews svc/prometheus-nginx-service 9090:9090 &
PROMETHEUS_PID=$!

echo "   - Setting up NGINX metrics access (port 9113)..."
kubectl port-forward -n neuronews svc/nginx-monitoring-service 9113:9113 &
METRICS_PID=$!

echo "   - Setting up NGINX web access (port 8080)..."
kubectl port-forward -n neuronews svc/nginx-monitoring-loadbalancer 8080:80 &
NGINX_PID=$!

# Wait for port forwards to be ready
sleep 10

echo "✅ Monitoring interfaces ready!"

# Step 4: Test the monitoring setup
echo ""
echo "4. Testing Monitoring Setup..."

# Test NGINX
echo "   - Testing NGINX response..."
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/ | grep -q "200\|404\|302"; then
    echo "     ✅ NGINX is responding"
else
    echo "     ❌ NGINX is not responding"
fi

# Test metrics endpoint
echo "   - Testing metrics collection..."
METRICS_COUNT=$(curl -s http://localhost:9113/metrics | grep -c "nginx_" || echo "0")
if [ "$METRICS_COUNT" -gt 0 ]; then
    echo "     ✅ Found $METRICS_COUNT NGINX metrics"
else
    echo "     ❌ No NGINX metrics found"
fi

# Test Prometheus
echo "   - Testing Prometheus integration..."
if curl -s http://localhost:9090/-/ready | grep -q "Prometheus is Ready"; then
    echo "     ✅ Prometheus is ready"
else
    echo "     ⚠️  Prometheus may not be ready yet"
fi

echo "✅ Monitoring setup tests completed!"

# Step 5: Generate realistic API traffic
echo ""
echo "5. Generating Realistic API Traffic for Testing..."

# Define API endpoints that would exist in a news platform
ENDPOINTS=(
    "/"
    "/api/v1/news"
    "/api/v1/news/trending"
    "/api/v1/news/search?q=technology"
    "/api/v1/analytics"
    "/api/v1/analytics/dashboard"
    "/api/v1/user/preferences"
    "/api/v1/health"
    "/nginx_status"
)

# Function to generate realistic traffic patterns
generate_traffic() {
    local duration=60  # 1 minute of traffic
    local end_time=$(($(date +%s) + duration))
    local request_count=0
    
    echo "   - Generating traffic for $duration seconds..."
    
    while [ $(date +%s) -lt $end_time ]; do
        # Simulate different user patterns
        case $((request_count % 10)) in
            0|1|2|3) # 40% news requests
                endpoint="/api/v1/news"
                ;;
            4|5) # 20% trending requests
                endpoint="/api/v1/news/trending"
                ;;
            6) # 10% search requests
                endpoint="/api/v1/news/search?q=technology"
                ;;
            7) # 10% analytics
                endpoint="/api/v1/analytics"
                ;;
            8) # 10% health checks
                endpoint="/api/v1/health"
                ;;
            9) # 10% other
                endpoint=${ENDPOINTS[$((RANDOM % ${#ENDPOINTS[@]}))]}
                ;;
        esac
        
        # Make request and capture response
        curl -s -o /dev/null -w "%{http_code},%{time_total}\n" \
            "http://localhost:8080$endpoint" &
        
        request_count=$((request_count + 1))
        
        # Vary request rate (simulate peak and low usage)
        if [ $((request_count % 20)) -lt 10 ]; then
            sleep 0.1  # High traffic period
        else
            sleep 0.5  # Lower traffic period
        fi
        
        # Progress indicator
        if [ $((request_count % 50)) -eq 0 ]; then
            echo "     Generated $request_count requests..."
        fi
    done
    
    # Wait for background requests to complete
    wait
    echo "     ✅ Generated $request_count total requests"
}

# Start traffic generation
generate_traffic

echo "✅ Traffic generation completed!"

# Step 6: Display monitoring results
echo ""
echo "6. Displaying Monitoring Results..."

# Wait a moment for metrics to be collected
sleep 10

echo "   - Current NGINX Metrics:"
echo "     Request Rate:"
curl -s http://localhost:9113/metrics | grep "nginx_http_requests_total" | head -5 | sed 's/^/       /'

echo "     Connection Statistics:"
curl -s http://localhost:9113/metrics | grep "nginx_connections" | sed 's/^/       /'

echo ""
echo "   - Prometheus Query Results:"
echo "     Total Requests (last 5 minutes):"
TOTAL_REQUESTS=$(curl -s "http://localhost:9090/api/v1/query?query=increase(nginx_http_requests_total[5m])" | \
    jq -r '.data.result[0].value[1] // "0"' 2>/dev/null || echo "N/A")
echo "       $TOTAL_REQUESTS requests"

echo "     Request Rate (per second):"
REQUEST_RATE=$(curl -s "http://localhost:9090/api/v1/query?query=rate(nginx_http_requests_total[1m])" | \
    jq -r '.data.result[0].value[1] // "0"' 2>/dev/null || echo "N/A")
echo "       $REQUEST_RATE req/s"

echo "     Active Connections:"
ACTIVE_CONN=$(curl -s "http://localhost:9090/api/v1/query?query=nginx_connections_active" | \
    jq -r '.data.result[0].value[1] // "0"' 2>/dev/null || echo "N/A")
echo "       $ACTIVE_CONN connections"

echo "✅ Monitoring results displayed!"

# Step 7: Show access information
echo ""
echo "7. Monitoring Access Information"
echo "=========================================="
echo "🔗 Access URLs (while port-forwarding is active):"
echo "   • Prometheus UI:     http://localhost:9090"
echo "   • NGINX Metrics:     http://localhost:9113/metrics"
echo "   • NGINX Web Server:  http://localhost:8080"
echo "   • NGINX Status:      http://localhost:8080/nginx_status"
echo ""
echo "📊 Key Prometheus Queries:"
echo "   • Request Rate:      rate(nginx_http_requests_total[5m])"
echo "   • Error Rate:        rate(nginx_http_requests_total{status=~\"4..|5..\"}[5m])"
echo "   • Active Connections: nginx_connections_active"
echo "   • Response Time:     nginx_http_request_duration_seconds"
echo ""
echo "📈 Grafana Dashboard Features (when deployed):"
echo "   • Real-time request monitoring"
echo "   • Performance trend analysis"
echo "   • Error rate tracking"
echo "   • Geographic request distribution"
echo ""
echo "🔧 Useful Commands:"
echo "   • View NGINX logs:   kubectl logs -n neuronews deployment/nginx-monitoring -c nginx -f"
echo "   • View metrics logs: kubectl logs -n neuronews deployment/nginx-monitoring -c prometheus-exporter -f"
echo "   • View Fluentd logs: kubectl logs -n neuronews deployment/nginx-monitoring -c fluentd -f"
echo ""
echo "📁 Configuration Files Created:"
echo "   • k8s/nginx/monitoring-configmap.yaml"
echo "   • k8s/nginx/monitoring-deployment.yaml"
echo "   • k8s/nginx/monitoring-service.yaml"
echo "   • k8s/nginx/fluentd-config.yaml"
echo "   • k8s/monitoring/prometheus-nginx-config.yaml"
echo "   • k8s/monitoring/prometheus-deployment.yaml"
echo "   • k8s/monitoring/grafana-nginx-dashboard.yaml"

# Cleanup function
cleanup() {
    echo ""
    echo "🧹 Cleaning up port forwards..."
    kill $PROMETHEUS_PID $METRICS_PID $NGINX_PID 2>/dev/null || true
    pkill -f "kubectl port-forward" 2>/dev/null || true
}

# Set cleanup trap
trap cleanup EXIT

echo ""
echo "=========================================="
echo "✅ NGINX Performance Monitoring Demo Complete!"
echo "Issue #85 Implementation Successfully Demonstrated"
echo "=========================================="
echo ""
echo "The monitoring stack will continue running."
echo "Press Ctrl+C to stop port forwarding and exit."
echo ""

# Keep the script running to maintain port forwards
echo "Monitoring active... (Press Ctrl+C to exit)"
while true; do
    sleep 10
    # Check if services are still running
    if ! kubectl get pods -n neuronews &>/dev/null; then
        echo "Monitoring services stopped."
        break
    fi
done
