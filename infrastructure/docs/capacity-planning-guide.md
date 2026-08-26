# Kubernetes Cluster Capacity Planning Guide

## Overview

This guide describes how to use the cluster capacity planning tool to analyze current utilization, project future resource needs, and make informed scaling decisions for Kubernetes clusters.

## Table of Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Getting Started](#getting-started)
- [Data Sources](#data-sources)
- [Growth Rate Configuration](#growth-rate-configuration)
- [Understanding the Report](#understanding-the-report)
- [Capacity Projection Charts](#capacity-projection-charts)
- [Scaling Recommendations](#scaling-recommendations)
- [Cost Analysis](#cost-analysis)
- [Prometheus Metrics Reference](#prometheus-metrics-reference)
- [Automation and CI Integration](#automation-and-ci-integration)
- [Best Practices](#best-practices)

## Architecture

The capacity planner operates in two modes:

1. **Live Mode** (Prometheus): Queries a Prometheus server for real-time cluster metrics
2. **Snapshot Mode** (JSON file): Loads a previously saved cluster snapshot for offline analysis

```
┌─────────────┐     ┌──────────────┐     ┌──────────────────┐
│  Prometheus  │────▶│  Capacity    │────▶│  Report Output   │
│  Server      │     │  Planner     │     │  (text / JSON)   │
└─────────────┘     └──────┬───────┘     └──────────────────┘
                           │
┌─────────────┐            │
│  Snapshot   │────────────┘
│  JSON File  │
└─────────────┘
```

## Prerequisites

- Python 3.9+
- Access to a Prometheus server with kube-state-metrics and cAdvisor enabled (live mode)
- Or a previously saved cluster snapshot (snapshot mode)

### Required Prometheus Metrics

The following metric sources must be available:

| Metric Source | Purpose |
|---|---|
| `kube_node_status_allocatable` | Node CPU/memory/GPU capacity |
| `kube_pod_container_resource_requests` | Pod resource requests |
| `kube_pod_container_resource_limits` | Pod resource limits |
| `container_cpu_usage_seconds_total` | Actual CPU usage |
| `container_memory_working_set_bytes` | Actual memory usage |
| `kube_pod_info` | Pod count |
| `kube_node_info` | Node count |

## Getting Started

### Live Mode with Prometheus

```bash
# Basic usage with default projection horizons (7, 14, 30, 60, 90 days)
python capacity-planner.py --prometheus-url http://prometheus:9090

# Namespace-filtered analysis
python capacity-planner.py \
  --prometheus-url http://prometheus:9090 \
  --namespace production

# Custom growth rate and projection horizons
python capacity-planner.py \
  --prometheus-url http://prometheus:9090 \
  --growth-rate 0.10 \
  --days 30 60 90 180
```

### Snapshot Mode

```bash
# Save a snapshot for later analysis
python capacity-planner.py \
  --prometheus-url http://prometheus:9090 \
  --save-snapshot cluster_snapshot.json

# Analyze from saved snapshot
python capacity-planner.py --cluster-data cluster_snapshot.json

# JSON output for programmatic consumption
python capacity-planner.py \
  --cluster-data cluster_snapshot.json \
  --output-format json \
  --output-file report.json
```

### Custom Node Sizing

```bash
# For clusters with different instance types
python capacity-planner.py \
  --prometheus-url http://prometheus:9090 \
  --avg-node-cpu 16 \
  --avg-node-memory 64 \
  --cost-per-node-hour 0.384
```

## Data Sources

### Prometheus (Live Mode)

Connects to Prometheus and executes PromQL queries to gather:

- **CPU**: Allocatable capacity, requests, limits, and actual usage
- **Memory**: Allocatable capacity, requests, limits, and actual usage
- **GPU**: Allocatable capacity and requests
- **Pods**: Total pod count across all namespaces
- **Nodes**: Total node count and status

### Snapshot Files (JSON Mode)

Snapshot files capture a point-in-time view of the cluster. Use the `--save-snapshot` flag when running in Prometheus mode to create one.

Example snapshot structure:

```json
{
  "timestamp": "2025-01-15T12:00:00Z",
  "cluster_name": "production-us-east-1",
  "total_nodes": 12,
  "total_cpu_capacity": 96.0,
  "total_memory_capacity": 386547056640,
  "total_gpu_capacity": 4,
  "current_usage": {
    "cpu_request": 48.0,
    "cpu_limit": 80.0,
    "cpu_usage_avg": 32.5,
    "cpu_usage_peak": 65.0,
    "memory_request": 161061273600,
    "memory_limit": 274877906944,
    "memory_usage_avg": 128849018880,
    "memory_usage_peak": 206158430208,
    "gpu_request": 2,
    "gpu_usage_avg": 1.5,
    "pod_count": 156,
    "storage_requested": 536870912000
  },
  "nodes": [
    {
      "name": "ip-10-0-1-100.ec2.internal",
      "instance_type": "m5.2xlarge",
      "cpu_capacity": 8.0,
      "memory_capacity": 32212254720,
      "gpu_capacity": 0,
      "cpu_allocatable": 7.84,
      "memory_allocatable": 30416437248,
      "status": "Ready",
      "zone": "us-east-1a",
      "cpu_cost_hourly": 0.384,
      "memory_cost_hourly": 0.0
    }
  ],
  "namespaces": {
    "production": {
      "cpu_request": 32.0,
      "memory_request": 107374182400,
      "pod_count": 98
    }
  }
}
```

## Growth Rate Configuration

The `--growth-rate` parameter specifies the **daily** resource consumption growth rate as a decimal.

| Growth Rate | Description | Typical Use Case |
|---|---|---|
| `0.01` (1%/day) | Very slow growth | Stable production workloads |
| `0.05` (5%/day) | Moderate growth | Active development clusters |
| `0.10` (10%/day) | Fast growth | Rapidly scaling applications |
| `0.15` (15%/day) | Very fast growth | New product launches, seasonal spikes |

### Calculating Growth Rate

To estimate your growth rate from historical data:

```bash
# If you have two snapshots taken N days apart:
# growth_rate = (usage_end / usage_start - 1) / N
#
# Example: CPU usage went from 30 cores to 45 cores over 30 days:
# growth_rate = (45/30 - 1) / 30 = 0.0167 (1.67% per day)
```

### Compound Growth Projection

The tool uses compound growth: `projected = current * (1 + growth_rate)^days`

For a 5% daily growth rate over 90 days:
- Factor = (1.05)^90 = 80.3x

This models realistic exponential growth patterns seen in production clusters.

## Understanding the Report

### Current Utilization Baseline

The baseline section shows:

- **Capacity**: Total allocatable resources across all nodes
- **Requests**: Sum of pod resource requests (what Kubernetes schedules for)
- **Usage (avg)**: Actual average consumption (what's really being used)
- **Usage (peak)**: Peak consumption observed
- **Utilization %**: Requested resources as a percentage of capacity

Key relationships:
- **Request/Capacity ratio**: Indicates how much of the cluster is committed
- **Usage/Request ratio**: Indicates how accurately requests match actual usage
- **Usage/Capacity ratio**: Indicates true cluster utilization

### Capacity Projections

The projections table shows predicted resource states at each time horizon:

| Column | Description |
|---|---|
| Days | Projection horizon from today |
| CPU Util% | Projected CPU utilization percentage |
| Mem Util% | Projected memory utilization percentage |
| Pods | Projected total pod count |
| Nodes | Minimum nodes needed to satisfy projected demand |
| Cost/mo | Estimated monthly infrastructure cost |

## Capacity Projection Charts

The tool generates ASCII charts for visual trend analysis:

- **CPU Utilization Projection**: Shows CPU usage trend over time
- **Memory Utilization Projection**: Shows memory usage trend over time
- **Nodes Needed Projection**: Shows when additional nodes are required
- **Monthly Cost Projection**: Shows infrastructure cost trajectory

Thresholds:
- **Green zone** (0-70%): Normal operation
- **Yellow zone** (70-85%): Plan for scaling
- **Red zone** (85%+): Immediate action required

## Scaling Recommendations

Recommendations are categorized by urgency:

### Immediate Actions
- Resource utilization exceeds critical thresholds
- Cluster is at risk of performance degradation or outages
- Requires immediate operator attention

### Short-term Actions (1-30 days)
- Utilization trending toward critical levels
- Upcoming capacity constraints identified
- Should be addressed in the current sprint

### Long-term Actions (30+ days)
- Projected capacity needs based on growth trends
- Budget planning and procurement lead times
- Architecture optimization opportunities

### Cost Optimization
- Over-provisioned resources identified
- Request-to-usage ratio improvements
- Right-sizing recommendations for pods and nodes

## Cost Analysis

The cost model considers:

- **Per-node hourly cost**: Based on instance type (AWS pricing defaults)
- **Monthly projection**: Nodes × hourly cost × 730 hours/month
- **Cost delta**: Difference between current and projected monthly spend

### Instance Cost Reference (AWS on-demand)

| Instance Type | vCPUs | Memory | Cost/Hour |
|---|---|---|---|
| m5.large | 2 | 8 GiB | $0.096 |
| m5.xlarge | 4 | 16 GiB | $0.192 |
| m5.2xlarge | 8 | 32 GiB | $0.384 |
| m5.4xlarge | 16 | 64 GiB | $0.768 |
| c5.xlarge | 4 | 8 GiB | $0.170 |
| c5.2xlarge | 8 | 16 GiB | $0.340 |
| r5.xlarge | 4 | 32 GiB | $0.252 |
| r5.2xlarge | 8 | 64 GiB | $0.504 |

Override defaults with `--cost-per-node-hour`.

## Prometheus Metrics Reference

### Node-Level Metrics

```promql
# Total allocatable CPU across all nodes
sum(kube_node_status_allocatable{resource="cpu"})

# Total allocatable memory across all nodes (bytes)
sum(kube_node_status_allocatable{resource="memory"})

# Number of ready nodes
count(kube_node_info{condition="ready"})
```

### Pod-Level Metrics

```promql
# Total CPU requests
sum(kube_pod_container_resource_requests{resource="cpu"})

# Total memory requests (bytes)
sum(kube_pod_container_resource_requests{resource="memory"})

# Actual CPU usage (5-minute average)
sum(rate(container_cpu_usage_seconds_total{container!="POD",container!=""}[5m]))

# Actual memory usage (working set)
sum(container_memory_working_set_bytes{container!="POD",container!=""})
```

### Namespace-Level Metrics

```promql
# CPU requests per namespace
sum by (namespace) (
  kube_pod_container_resource_requests{resource="cpu"}
)

# Memory requests per namespace
sum by (namespace) (
  kube_pod_container_resource_requests{resource="memory"}
)
```

## Automation and CI Integration

### Scheduled Reports

Run capacity planning on a schedule to track trends:

```yaml
# .github/workflows/capacity-report.yml
name: Weekly Capacity Report
on:
  schedule:
    - cron: "0 9 * * 1"

jobs:
  capacity-report:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Generate Capacity Report
        run: |
          python infrastructure/scripts/capacity-planner.py \
            --prometheus-url ${{ secrets.PROMETHEUS_URL }} \
            --output-format json \
            --output-file capacity_report.json
      - name: Upload Report
        uses: actions/upload-artifact@v4
        with:
          name: capacity-report
          path: capacity_report.json
```

### Alert Thresholds

Integrate with alerting by using JSON output:

```python
import json
import subprocess

result = subprocess.run(
    ["python", "capacity-planner.py", "--cluster-data", "snapshot.json",
     "--output-format", "json"],
    capture_output=True, text=True
)

report = json.loads(result.stdout)
for rec in report["recommendations"]["immediate"]:
    if rec["severity"] == "critical":
        send_alert(rec["message"])
```

## Best Practices

1. **Baseline first**: Establish a baseline before configuring growth rates. Collect at least 7 days of data.

2. **Use realistic growth rates**: Base growth rates on historical data, not assumptions. Start conservative and adjust.

3. **Account for seasonality**: Daily growth rates may vary. Consider running projections for different rates to model best/worst/expected scenarios.

4. **Monitor request-to-usage ratios**: Large gaps between requests and actual usage indicate over-provisioning. Aim for requests within 20-30% of actual usage.

5. **Set headroom targets**: Plan for peak usage plus 20-30% headroom rather than average usage.

6. **Review GPU separately**: GPU resources are typically the bottleneck. Track GPU utilization independently.

7. **Save snapshots regularly**: Historical snapshots enable trend analysis and validate projection accuracy.

8. **Consider node disruption budget**: When recommending scale-downs, account for pod disruption budgets and disruption budgets.

9. **Factor in maintenance windows**: Capacity projections should account for planned maintenance, node drains, and rolling updates.

10. **Review cost alongside capacity**: High utilization isn't always optimal. Balance cost efficiency with reliability targets.
