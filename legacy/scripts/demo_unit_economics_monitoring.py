#!/usr/bin/env python3

"""
Unit Economics Monitoring Demo
Issue #337: Unit economics: "€ per 1k articles" & "€ per RAG query"

This demo script demonstrates the complete unit economics monitoring system,
showing how to track business outcomes and calculate cost per outcome metrics.
"""

import json
import subprocess
import sys
import time
import requests
import random
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

def run_command(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Execute shell command and return result."""
    return subprocess.run(
        cmd, shell=True, capture_output=True, text=True, check=check
    )

def log(message: str, level: str = "INFO") -> None:
    """Log message with timestamp."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    colors = {
        "INFO": "\033[0;34m",    # Blue
        "SUCCESS": "\033[0;32m", # Green
        "WARNING": "\033[1;33m", # Yellow
        "ERROR": "\033[0;31m",   # Red
        "RESET": "\033[0m"       # Reset
    }
    color = colors.get(level, colors["INFO"])
    reset = colors["RESET"]
    print(f"{color}[{timestamp}] {level}: {message}{reset}")

class UnitEconomicsDemo:
    """Demo class for unit economics monitoring."""
    
    def __init__(self):
        self.namespace_monitoring = "monitoring"
        self.namespace_opencost = "opencost"
        self.prometheus_port = 9090
        self.grafana_port = 3000
        self.opencost_port = 9003
        self.port_forwards = {}
        
    def check_prerequisites(self) -> bool:
        """Check if required components are available."""
        log("Checking prerequisites...")
        
        # Check kubectl
        try:
            run_command("kubectl version --client")
            log("✓ kubectl is available")
        except subprocess.CalledProcessError:
            log("✗ kubectl is not available", "ERROR")
            return False
        
        # Check cluster connectivity
        try:
            run_command("kubectl cluster-info")
            log("✓ Cluster connection is working")
        except subprocess.CalledProcessError:
            log("✗ Cannot connect to cluster", "ERROR")
            return False
        
        return True
    
    def demonstrate_metrics_architecture(self) -> None:
        """Show the unit economics metrics architecture."""
        log("Demonstrating unit economics metrics architecture...")
        
        print("\n" + "="*60)
        print("UNIT ECONOMICS METRICS ARCHITECTURE")
        print("="*60)
        
        print("\n🏗️  Architecture Overview:")
        print("┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐")
        print("│   Application   │───▶│   Prometheus     │───▶│    Grafana      │")
        print("│   Counters      │    │  Recording Rules │    │   Dashboards    │")
        print("└─────────────────┘    └──────────────────┘    └─────────────────┘")
        print("         │                       │                       │")
        print("         ▼                       ▼                       ▼")
        print("┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐")
        print("│ Business Events │    │ Cost Calculations│    │ Unit Economics  │")
        print("│ • Articles      │    │ • €/hour cluster │    │ • € per 1k arts │")
        print("│ • RAG Queries   │    │ • €/hour compute │    │ • € per query   │")
        print("│ • Pipelines     │    │ • €/hour storage │    │ • Efficiency    │")
        print("└─────────────────┘    └──────────────────┘    └─────────────────┘")
        
        print("\n📊 Key Metrics:")
        metrics = {
            "Business Counters": [
                "neuro_articles_ingested_total - Articles processed",
                "neuro_rag_queries_total - RAG queries answered",
                "neuro_pipeline_operations_total - Pipeline operations"
            ],
            "Cost Metrics": [
                "cost:cluster_hourly:sum - Total infrastructure cost",
                "cost:compute_hourly:sum - Compute cost allocation",
                "cost:storage_hourly:sum - Storage cost allocation"
            ],
            "Rate Calculations": [
                "articles:rate_1h - Articles per hour rate",
                "ragq:rate_1h - RAG queries per hour rate"
            ],
            "Unit Economics": [
                "unit_economics:cost_per_1k_articles_hourly - € per 1000 articles",
                "unit_economics:cost_per_rag_query_hourly - € per RAG query",
                "efficiency:articles_per_euro_hourly - Articles per €",
                "efficiency:queries_per_euro_hourly - Queries per €"
            ]
        }
        
        for category, metric_list in metrics.items():
            print(f"\n📈 {category}:")
            for metric in metric_list:
                print(f"   • {metric}")
    
    def demonstrate_business_counters(self) -> None:
        """Show how business counters are implemented."""
        log("Demonstrating business counter implementation...")
        
        print("\n" + "="*60)
        print("BUSINESS COUNTER IMPLEMENTATION")
        print("="*60)
        
        print("\n🔧 Articles Ingested Counter:")
        print("Location: services/ingest/consumer.py")
        print("Code snippet:")
        print("""
from services.monitoring.unit_economics import increment_articles_ingested

def process_message(self, message, callback=None):
    if self._validate_payload(payload):
        # Process article successfully
        increment_articles_ingested(
            pipeline="ingest",
            source=payload.get('source_id', 'unknown'),
            status="success",
            count=1
        )
        """)
        
        print("\n🤖 RAG Queries Counter:")
        print("Location: legacy/services/api/routes/ask.py")
        print("Code snippet:")
        print("""
from services.monitoring.unit_economics import increment_rag_queries

@router.post("/ask")
async def ask_question(request: AskRequest):
    try:
        response = await rag_service.answer_question(...)
        
        # Track successful RAG query
        increment_rag_queries(
            endpoint="/ask",
            provider=request.provider or "openai",
            status="success",
            count=1
        )
        """)
        
        print("\n📝 Prometheus Metrics Generated:")
        print("• neuro_articles_ingested_total{pipeline=\"ingest\",source=\"rss\",status=\"success\"}")
        print("• neuro_rag_queries_total{endpoint=\"/ask\",provider=\"openai\",status=\"success\"}")
    
    def demonstrate_recording_rules(self) -> None:
        """Show Prometheus recording rules for unit economics."""
        log("Demonstrating Prometheus recording rules...")
        
        print("\n" + "="*60)
        print("PROMETHEUS RECORDING RULES")
        print("="*60)
        
        print("\n⚡ Cost Recording Rules:")
        cost_rules = {
            "Total Cluster Cost": "cost:cluster_hourly:sum = sum(opencost_node_cost_hourly)",
            "Compute Cost": "cost:compute_hourly:sum = sum(opencost_node_cost_hourly{cost_type=\"compute\"})",
            "Storage Cost": "cost:storage_hourly:sum = sum(opencost_pv_cost_hourly)"
        }
        
        for name, rule in cost_rules.items():
            print(f"   • {name}: {rule}")
        
        print("\n📈 Business Rate Rules:")
        rate_rules = {
            "Articles Rate": "articles:rate_1h = rate(neuro_articles_ingested_total[1h]) * 3600",
            "RAG Queries Rate": "ragq:rate_1h = rate(neuro_rag_queries_total[1h]) * 3600"
        }
        
        for name, rule in rate_rules.items():
            print(f"   • {name}: {rule}")
        
        print("\n💰 Unit Economics Rules:")
        unit_rules = {
            "Cost per 1k Articles": "unit_economics:cost_per_1k_articles_hourly = (cost:cluster_hourly:sum / (articles:rate_1h / 1000)) > 0",
            "Cost per RAG Query": "unit_economics:cost_per_rag_query_hourly = (cost:cluster_hourly:sum / ragq:rate_1h) > 0",
            "Articles per Euro": "efficiency:articles_per_euro_hourly = articles:rate_1h / cost:cluster_hourly:sum",
            "Queries per Euro": "efficiency:queries_per_euro_hourly = ragq:rate_1h / cost:cluster_hourly:sum"
        }
        
        for name, rule in unit_rules.items():
            print(f"   • {name}")
            print(f"     {rule}")
    
    def start_port_forwards(self) -> None:
        """Start port forwards for demo access."""
        log("Starting port forwards for demo...")
        
        services = {
            'prometheus': (f"svc/prometheus-server", f"{self.prometheus_port}:80", self.namespace_monitoring),
            'grafana': (f"svc/grafana", f"{self.grafana_port}:80", self.namespace_monitoring),
            'opencost': (f"svc/opencost", f"{self.opencost_port}:9003", self.namespace_opencost)
        }
        
        for service, (svc_name, port_mapping, namespace) in services.items():
            try:
                # Check if service exists
                check_cmd = f"kubectl get {svc_name} -n {namespace}"
                run_command(check_cmd, check=True)
                
                # Start port forward
                cmd = f"kubectl port-forward {svc_name} {port_mapping} -n {namespace}"
                process = subprocess.Popen(
                    cmd.split(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                self.port_forwards[service] = process
                log(f"✓ {service.capitalize()} port forward started on {port_mapping.split(':')[0]}")
                
            except subprocess.CalledProcessError:
                log(f"✗ {service.capitalize()} service not found in {namespace}", "WARNING")
        
        # Wait for port forwards to be ready
        time.sleep(5)
    
    def test_metrics_availability(self) -> None:
        """Test if metrics are available."""
        log("Testing metrics availability...")
        
        if 'prometheus' not in self.port_forwards:
            log("Prometheus not available, skipping metrics tests", "WARNING")
            return
        
        print("\n" + "="*60)
        print("METRICS AVAILABILITY TEST")
        print("="*60)
        
        # Test basic queries
        test_queries = {
            "Cluster Health": "up",
            "Kubernetes Nodes": "count(kube_node_info)",
            "Running Pods": "count(kube_pod_info{phase=\"Running\"})",
            "Articles Counter": "neuro_articles_ingested_total",
            "RAG Queries Counter": "neuro_rag_queries_total",
            "Cost Metrics": "opencost_node_cost_hourly",
            "Cost Recording Rule": "cost:cluster_hourly:sum",
            "Articles Rate Rule": "articles:rate_1h",
            "Unit Economics Rule": "unit_economics:cost_per_1k_articles_hourly"
        }
        
        for name, query in test_queries.items():
            try:
                response = requests.get(
                    f"http://localhost:{self.prometheus_port}/api/v1/query",
                    params={"query": query},
                    timeout=5
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get('status') == 'success' and data.get('data', {}).get('result'):
                        result_count = len(data['data']['result'])
                        if result_count > 0:
                            value = data['data']['result'][0].get('value', [None, 'N/A'])[1]
                            print(f"   ✅ {name}: {result_count} series, value: {value}")
                        else:
                            print(f"   ⚠️  {name}: Query successful but no data")
                    else:
                        print(f"   ⚠️  {name}: Query successful but no results")
                else:
                    print(f"   ❌ {name}: HTTP {response.status_code}")
                    
            except Exception as e:
                print(f"   ❌ {name}: Error - {e}")
    
    def demonstrate_unit_economics_calculations(self) -> None:
        """Show unit economics calculations with sample data."""
        log("Demonstrating unit economics calculations...")
        
        print("\n" + "="*60)
        print("UNIT ECONOMICS CALCULATIONS")
        print("="*60)
        
        # Sample business data
        print("\n📊 Sample Business Metrics (Hourly):")
        sample_data = {
            "Articles Ingested": 500,
            "RAG Queries": 150,
            "Infrastructure Cost": "€12.50"
        }
        
        for metric, value in sample_data.items():
            print(f"   • {metric}: {value}")
        
        print("\n💰 Unit Economics Calculations:")
        
        # Calculate cost per 1k articles
        articles_per_hour = 500
        cost_per_hour = 12.50
        cost_per_1k_articles = (cost_per_hour / articles_per_hour) * 1000
        print(f"   • Cost per 1000 articles: €{cost_per_1k_articles:.3f}")
        print(f"     Formula: (€{cost_per_hour} / {articles_per_hour} articles) × 1000")
        
        # Calculate cost per RAG query
        queries_per_hour = 150
        cost_per_query = cost_per_hour / queries_per_hour
        print(f"   • Cost per RAG query: €{cost_per_query:.3f}")
        print(f"     Formula: €{cost_per_hour} / {queries_per_hour} queries")
        
        print("\n📈 Efficiency Metrics:")
        articles_per_euro = articles_per_hour / cost_per_hour
        queries_per_euro = queries_per_hour / cost_per_hour
        print(f"   • Articles per €: {articles_per_euro:.1f}")
        print(f"   • RAG queries per €: {queries_per_euro:.1f}")
        
        print("\n📅 Monthly Projections:")
        hours_per_month = 24 * 30  # 720 hours
        monthly_articles = articles_per_hour * hours_per_month
        monthly_queries = queries_per_hour * hours_per_month
        monthly_cost = cost_per_hour * hours_per_month
        
        print(f"   • Monthly articles: {monthly_articles:,}")
        print(f"   • Monthly queries: {monthly_queries:,}")
        print(f"   • Monthly cost: €{monthly_cost:,.2f}")
        print(f"   • Monthly cost per 1k articles: €{cost_per_1k_articles:.3f}")
        print(f"   • Monthly cost per query: €{cost_per_query:.3f}")
    
    def demonstrate_grafana_dashboard(self) -> None:
        """Show Grafana dashboard features."""
        log("Demonstrating Grafana dashboard features...")
        
        print("\n" + "="*60)
        print("GRAFANA DASHBOARD FEATURES")
        print("="*60)
        
        print("\n📊 Dashboard Panels:")
        panels = [
            {
                "title": "€ per 1000 Articles",
                "description": "Primary unit economics metric showing cost efficiency of content processing",
                "query": "unit_economics:cost_per_1k_articles_hourly",
                "visualization": "Time series with current value stat"
            },
            {
                "title": "€ per RAG Query", 
                "description": "Cost efficiency of AI inference and question answering",
                "query": "unit_economics:cost_per_rag_query_hourly",
                "visualization": "Time series with current value stat"
            },
            {
                "title": "Business Activity Rates",
                "description": "Hourly rates of articles and queries processing",
                "query": "articles:rate_1h, ragq:rate_1h",
                "visualization": "Multi-series time chart"
            },
            {
                "title": "Infrastructure Costs",
                "description": "Cost breakdown by infrastructure component",
                "query": "cost:compute_hourly:sum, cost:storage_hourly:sum",
                "visualization": "Stacked area chart"
            },
            {
                "title": "Efficiency Metrics",
                "description": "Articles and queries processed per euro spent",
                "query": "efficiency:articles_per_euro_hourly",
                "visualization": "Gauge and stat panels"
            }
        ]
        
        for i, panel in enumerate(panels, 1):
            print(f"\n   {i}. {panel['title']}")
            print(f"      📝 {panel['description']}")
            print(f"      📈 Query: {panel['query']}")
            print(f"      🎨 Visualization: {panel['visualization']}")
        
        print("\n🎛️  Dashboard Features:")
        features = [
            "Real-time updates every 30 seconds",
            "Time range selector (1h to 30d)",
            "Threshold-based color coding",
            "Historical trend analysis",
            "Export capabilities for reporting",
            "Alert integration for cost thresholds"
        ]
        
        for feature in features:
            print(f"   • {feature}")
    
    def show_access_information(self) -> None:
        """Show how to access the monitoring system."""
        log("Showing access information...")
        
        print("\n" + "="*60)
        print("ACCESS INFORMATION")
        print("="*60)
        
        if 'grafana' in self.port_forwards:
            print(f"\n📊 Grafana Dashboard:")
            print(f"   URL: http://localhost:{self.grafana_port}")
            print(f"   Dashboard: 'Unit Economics - Cost per Outcome'")
            print(f"   Default credentials: admin/admin")
        
        if 'prometheus' in self.port_forwards:
            print(f"\n🔍 Prometheus Metrics:")
            print(f"   URL: http://localhost:{self.prometheus_port}")
            print(f"   Try these queries:")
            print(f"   • unit_economics:cost_per_1k_articles_hourly")
            print(f"   • unit_economics:cost_per_rag_query_hourly")
            print(f"   • articles:rate_1h")
            print(f"   • ragq:rate_1h")
        
        if 'opencost' in self.port_forwards:
            print(f"\n💰 OpenCost API:")
            print(f"   URL: http://localhost:{self.opencost_port}")
            print(f"   Endpoints:")
            print(f"   • /metrics - Prometheus metrics")
            print(f"   • /allocation - Cost allocation API")
        
        print(f"\n🔧 Installation Commands:")
        print(f"   Install: ./grafana/install-unit-economics.sh")
        print(f"   Test: ./grafana/test-unit-economics.sh")
        
        print(f"\n📚 Integration Examples:")
        print(f"   Articles: increment_articles_ingested(pipeline='ingest', source='rss', status='success')")
        print(f"   Queries: increment_rag_queries(endpoint='/ask', provider='openai', status='success')")
    
    def simulate_business_activity(self) -> None:
        """Simulate business activity for demo purposes."""
        log("Simulating business activity...")
        
        print("\n" + "="*60)
        print("BUSINESS ACTIVITY SIMULATION")
        print("="*60)
        
        print("\n🔄 Simulating article ingestion...")
        print("   (In real system: articles processed through Kafka consumer)")
        
        # Show how metrics would be incremented
        sources = ["rss", "api", "scraper"]
        for i in range(5):
            source = random.choice(sources)
            print(f"   📰 Article {i+1}: increment_articles_ingested(pipeline='ingest', source='{source}', status='success')")
            time.sleep(0.5)
        
        print("\n🤖 Simulating RAG queries...")
        print("   (In real system: queries processed through /ask endpoint)")
        
        providers = ["openai", "anthropic"]
        for i in range(3):
            provider = random.choice(providers)
            print(f"   ❓ Query {i+1}: increment_rag_queries(endpoint='/ask', provider='{provider}', status='success')")
            time.sleep(0.5)
        
        print("\n📊 Prometheus would now have:")
        print("   • neuro_articles_ingested_total: 5 additional samples")
        print("   • neuro_rag_queries_total: 3 additional samples")
        print("   • Recording rules would calculate new rates and unit costs")
    
    def cleanup_port_forwards(self) -> None:
        """Clean up port forward processes."""
        log("Cleaning up port forwards...")
        
        for service, process in self.port_forwards.items():
            try:
                process.terminate()
                process.wait(timeout=5)
                log(f"✓ Stopped {service} port forward")
            except subprocess.TimeoutExpired:
                process.kill()
                log(f"✓ Killed {service} port forward")
            except Exception as e:
                log(f"Could not stop {service} port forward: {e}", "WARNING")
    
    def run_demo(self) -> None:
        """Run the complete unit economics demo."""
        log("Starting Unit Economics Monitoring Demo", "SUCCESS")
        print("="*60)
        
        try:
            # Prerequisites
            if not self.check_prerequisites():
                log("Prerequisites not met. Demo cannot continue.", "ERROR")
                return
            
            # Architecture overview
            self.demonstrate_metrics_architecture()
            
            # Implementation details
            self.demonstrate_business_counters()
            self.demonstrate_recording_rules()
            
            # Start services for testing
            self.start_port_forwards()
            
            # Test metrics
            self.test_metrics_availability()
            
            # Show calculations
            self.demonstrate_unit_economics_calculations()
            
            # Dashboard features
            self.demonstrate_grafana_dashboard()
            
            # Simulate activity
            self.simulate_business_activity()
            
            # Access information
            self.show_access_information()
            
            print("\n" + "="*60)
            log("Demo completed successfully!", "SUCCESS")
            print("="*60)
            
            if self.port_forwards:
                print(f"\n🚀 Services are accessible:")
                for service, _ in self.port_forwards.items():
                    if service == 'grafana':
                        print(f"   • Grafana: http://localhost:{self.grafana_port}")
                    elif service == 'prometheus':
                        print(f"   • Prometheus: http://localhost:{self.prometheus_port}")
                    elif service == 'opencost':
                        print(f"   • OpenCost: http://localhost:{self.opencost_port}")
                
                print(f"\nPress Ctrl+C or Enter to stop services and exit...")
                try:
                    input()
                except KeyboardInterrupt:
                    pass
            
        except KeyboardInterrupt:
            log("Demo interrupted by user", "WARNING")
        except Exception as e:
            log(f"Demo failed with error: {e}", "ERROR")
        finally:
            self.cleanup_port_forwards()

def main():
    """Main entry point."""
    demo = UnitEconomicsDemo()
    demo.run_demo()

if __name__ == "__main__":
    main()
