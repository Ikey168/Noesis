# Unit Economics Monitoring

**Issue #337: Unit economics: "€ per 1k articles" & "€ per RAG query"**

This implementation provides comprehensive unit economics monitoring for NeuroNews, enabling cost-per-outcome analysis with two key metrics:

- **€ per 1000 articles** - Cost efficiency of content processing
- **€ per RAG query** - Cost efficiency of AI inference

## 🎯 Overview

Unit economics monitoring tracks business outcomes against infrastructure costs, providing the essential "#1 FinOps narrative" that shows cost per business value delivered. This system integrates with OpenCost for real-time cost data and Prometheus for metrics collection.

## 📊 Architecture

```
Application Counters → Prometheus Recording Rules → Grafana Dashboards
        │                       │                       │
        ▼                       ▼                       ▼
Business Events          Cost Calculations        Unit Economics
• Articles ingested      • €/hour cluster cost    • € per 1k articles
• RAG queries answered   • €/hour by component     • € per RAG query
• Pipeline operations    • Rate calculations       • Efficiency metrics
```

## 🚀 Quick Start

### 1. Installation

```bash
# Install unit economics monitoring system
cd grafana && chmod +x install-unit-economics.sh
./install-unit-economics.sh
```

### 2. Verification

```bash
# Run comprehensive test suite
chmod +x test-unit-economics.sh
./test-unit-economics.sh
```

### 3. Demo

```bash
# Run interactive demo
python3 demo_unit_economics_monitoring.py
```

### 4. Access Dashboards

```bash
# Port forward Grafana
kubectl port-forward svc/grafana 3000:80 -n monitoring

# Open browser: http://localhost:3000
# Find "Unit Economics - Cost per Outcome" dashboard
```

## 📈 Key Metrics

### Primary Unit Economics

| Metric | Description | Formula | Target |
|--------|-------------|---------|---------|
| **€ per 1k Articles** | Cost efficiency of content processing | `cost_hourly / (articles_rate_1h / 1000)` | < €0.50 |
| **€ per RAG Query** | Cost efficiency of AI inference | `cost_hourly / rag_queries_rate_1h` | < €0.05 |

### Supporting Metrics

| Metric | Description | Purpose |
|--------|-------------|---------|
| **Articles per €** | Processing efficiency | Optimization target |
| **Queries per €** | Query efficiency | Cost optimization |
| **Success Rate** | Quality metrics | Service reliability |
| **Cost Breakdown** | Infrastructure analysis | Cost allocation |

## 🔧 Implementation Details

### Business Counters

The system tracks business events using Prometheus counters:

```python
# Articles ingested (services/ingest/consumer.py)
from services.monitoring.unit_economics import increment_articles_ingested

increment_articles_ingested(
    pipeline="ingest",
    source="rss",
    status="success",
    count=1
)

# RAG queries (legacy/services/api/routes/ask.py)
from services.monitoring.unit_economics import increment_rag_queries

increment_rag_queries(
    endpoint="/ask",
    provider="openai", 
    status="success",
    count=1
)
```

### Prometheus Recording Rules

Cost and business metrics are pre-calculated using recording rules:

```yaml
# Cost metrics
- record: cost:cluster_hourly:sum
  expr: sum(opencost_node_cost_hourly)

# Business rates  
- record: articles:rate_1h
  expr: rate(neuro_articles_ingested_total[1h]) * 3600

- record: ragq:rate_1h
  expr: rate(neuro_rag_queries_total[1h]) * 3600

# Unit economics
- record: unit_economics:cost_per_1k_articles_hourly
  expr: (cost:cluster_hourly:sum / (articles:rate_1h / 1000)) > 0

- record: unit_economics:cost_per_rag_query_hourly
  expr: (cost:cluster_hourly:sum / ragq:rate_1h) > 0
```

### Grafana Dashboard

The dashboard provides real-time visualization with:

- **Time series** showing cost trends over time
- **Stat panels** for current unit economics values
- **Efficiency metrics** showing optimization opportunities
- **Cost breakdown** by infrastructure component
- **Business activity** rates and volumes

## 📁 File Structure

```
services/monitoring/
└── unit_economics.py              # Business counters implementation

k8s/monitoring/
└── prometheus-unit-economics-rules.yaml  # Prometheus recording rules

grafana/
├── dashboards/
│   └── unit-economics.json        # Grafana dashboard definition
├── provisioning/
│   └── dashboards/finops.yaml     # Dashboard auto-discovery config
├── install-unit-economics.sh      # Installation script
├── test-unit-economics.sh         # Test suite
└── README-unit-economics.md       # This documentation

demo_unit_economics_monitoring.py  # Interactive demo script
```

## 🧪 Testing

### Automated Tests

The test suite verifies:

```bash
./grafana/test-unit-economics.sh
```

- ✅ Prerequisites (kubectl, cluster connectivity)
- ✅ ConfigMaps installation and labeling
- ✅ Prometheus recording rules configuration
- ✅ Grafana dashboard JSON validation
- ✅ Service connectivity (Prometheus, Grafana, OpenCost)
- ✅ Metrics availability and query validity
- ✅ Unit economics calculation accuracy

### Manual Testing

1. **Generate test data**:
   ```python
   from services.monitoring.unit_economics import increment_articles_ingested, increment_rag_queries
   
   # Simulate article ingestion
   for i in range(100):
       increment_articles_ingested("ingest", "test", "success")
   
   # Simulate RAG queries
   for i in range(50):
       increment_rag_queries("/ask", "openai", "success")
   ```

2. **Verify metrics in Prometheus**:
   ```bash
   kubectl port-forward svc/prometheus-server 9090:80 -n monitoring
   # Query: neuro_articles_ingested_total
   # Query: neuro_rag_queries_total
   ```

3. **Check calculations**:
   ```bash
   # Query: unit_economics:cost_per_1k_articles_hourly
   # Query: unit_economics:cost_per_rag_query_hourly
   ```

## 🔍 Troubleshooting

### Common Issues

**No data in dashboard panels**

1. Check business counters are being incremented:
   ```bash
   # Prometheus query
   neuro_articles_ingested_total
   neuro_rag_queries_total
   ```

2. Verify OpenCost integration:
   ```bash
   # Check OpenCost metrics
   kubectl port-forward svc/opencost 9003:9003 -n opencost
   curl http://localhost:9003/metrics | grep opencost_node_cost_hourly
   ```

3. Validate recording rules:
   ```bash
   # Check if rules are loaded
   kubectl logs deployment/prometheus-server -n monitoring | grep "recording rule"
   ```

**Division by zero errors**

Recording rules include `> 0` filters to prevent division by zero when:
- No articles are being ingested (`articles:rate_1h = 0`)
- No RAG queries are being processed (`ragq:rate_1h = 0`)

**High unit costs**

If costs seem high:
1. Check if OpenCost is properly configured for your cloud provider
2. Verify business counters are being incremented correctly
3. Consider the cluster is processing other workloads affecting cost allocation

### Debug Commands

```bash
# Check service status
kubectl get pods -n monitoring
kubectl get pods -n opencost

# View Prometheus config
kubectl get configmap prometheus-unit-economics-rules -n monitoring -o yaml

# Check Grafana logs
kubectl logs deployment/grafana -n monitoring

# Test metrics collection
curl -s "http://localhost:9090/api/v1/query?query=up" | jq .
```

## 📊 Business Value

### Cost Optimization

- **Baseline establishment**: Track current cost per outcome
- **Trend analysis**: Identify cost increases or optimization wins
- **Comparative analysis**: Compare cost efficiency across time periods
- **Budget planning**: Project costs based on business volume

### Performance Monitoring

- **Efficiency tracking**: Articles/queries processed per euro
- **Resource optimization**: Identify underutilized resources
- **Scaling decisions**: Data-driven capacity planning
- **SLA management**: Cost vs. performance trade-offs

### Stakeholder Communication

- **Executive reporting**: Clear cost-per-outcome metrics
- **Engineering insights**: Technical efficiency measurements
- **Financial planning**: Budget allocation and forecasting
- **Customer value**: Demonstrate cost-effective service delivery

## 🔗 Integration

### FinOps Labeling

Unit economics monitoring integrates with [FinOps labeling governance](../k8s/finops/) to provide:
- Cost allocation by team and pipeline
- Multi-dimensional unit economics analysis
- Detailed cost attribution

### OpenCost Integration

Requires OpenCost for real-time cost data:
- Node cost allocation
- Storage and network costs
- Cloud provider integration
- Cost breakdown by resource type

### Prometheus Stack

Leverages existing Prometheus infrastructure:
- Recording rules for pre-calculation
- Long-term metrics storage
- Alert integration capabilities
- Grafana dashboard integration

## 📝 Configuration

### Sampling Rate

Adjust business counter collection:

```python
# High-frequency services may want sampling
from services.monitoring.unit_economics import UnitEconomicsCollector

collector = UnitEconomicsCollector(
    enable_http_server=True,
    port=8000
)

# Only track every 10th article for high-volume ingestion
if article_count % 10 == 0:
    collector.increment_articles_ingested(...)
```

### Cost Allocation

Customize cost calculation in recording rules:

```yaml
# Include specific cost types
- record: cost:custom_allocation:sum
  expr: |
    sum(opencost_node_cost_hourly{cost_type="compute"}) +
    sum(opencost_pv_cost_hourly) +
    sum(opencost_load_balancer_cost)
```

### Time Windows

Adjust time ranges for different analysis needs:

```yaml
# Daily averages
- record: unit_economics:cost_per_1k_articles_daily
  expr: avg_over_time(unit_economics:cost_per_1k_articles_hourly[24h])

# Weekly averages  
- record: unit_economics:cost_per_1k_articles_weekly
  expr: avg_over_time(unit_economics:cost_per_1k_articles_hourly[7d])
```

## 🎯 Next Steps

1. **Alert Configuration**: Set up alerts for cost threshold breaches
2. **Historical Analysis**: Implement long-term trend analysis
3. **Forecasting**: Add predictive cost modeling
4. **Multi-dimensional Analysis**: Break down by team, pipeline, environment
5. **Cost Attribution**: Integrate with cloud billing APIs for precise allocation

---

**Ready to optimize your unit economics!** 🚀

For questions or issues, see the troubleshooting section above or check the [main FinOps documentation](../k8s/finops/README.md).
