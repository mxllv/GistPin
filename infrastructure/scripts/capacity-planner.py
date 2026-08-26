#!/usr/bin/env python3
"""
Kubernetes Cluster Capacity Planning Tool

Analyzes current cluster utilization, projects future resource needs based on
configurable growth rates, and generates scaling recommendations with cost
implications.

Usage:
    python capacity-planner.py --kubeconfig ~/.kube/config --namespace production
    python capacity-planner.py --prometheus-url http://prometheus:9090 --days 90
    python capacity-planner.py --cluster-data cluster_snapshot.json --growth-rate 0.15
"""

import argparse
import json
import sys
import os
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path

try:
    import urllib.request
    import urllib.parse

    HAS_URLLIB = True
except ImportError:
    HAS_URLLIB = False


@dataclass
class ResourceUsage:
    cpu_request: float = 0.0
    cpu_limit: float = 0.0
    cpu_usage_avg: float = 0.0
    cpu_usage_peak: float = 0.0
    memory_request: float = 0.0
    memory_limit: float = 0.0
    memory_usage_avg: float = 0.0
    memory_usage_peak: float = 0.0
    gpu_request: float = 0.0
    gpu_usage_avg: float = 0.0
    pod_count: int = 0
    storage_requested: float = 0.0

    @property
    def cpu_utilization_pct(self) -> float:
        if self.cpu_limit == 0:
            return 0.0
        return (self.cpu_usage_avg / self.cpu_limit) * 100

    @property
    def memory_utilization_pct(self) -> float:
        if self.memory_limit == 0:
            return 0.0
        return (self.memory_usage_avg / self.memory_limit) * 100


@dataclass
class NodeInfo:
    name: str
    instance_type: str = "unknown"
    cpu_capacity: float = 0.0
    memory_capacity: float = 0.0
    gpu_capacity: float = 0.0
    cpu_allocatable: float = 0.0
    memory_allocatable: float = 0.0
    status: str = "Ready"
    zone: str = ""
    cpu_cost_hourly: float = 0.0
    memory_cost_hourly: float = 0.0


@dataclass
class CapacitySnapshot:
    timestamp: str
    cluster_name: str
    total_nodes: int
    total_cpu_capacity: float
    total_memory_capacity: float
    total_gpu_capacity: float
    current_usage: ResourceUsage
    nodes: list = field(default_factory=list)
    namespaces: dict = field(default_factory=dict)


@dataclass
class GrowthProjection:
    days_ahead: int
    projected_cpu_usage: float
    projected_memory_usage: float
    projected_pod_count: int
    cpu_utilization_pct: float
    memory_utilization_pct: float
    nodes_needed_cpu: int
    nodes_needed_memory: int
    nodes_needed: int
    estimated_monthly_cost: float
    scaling_actions: list = field(default_factory=list)


COST_PER_NODE_HOUR = {
    "m5.large": 0.096,
    "m5.xlarge": 0.192,
    "m5.2xlarge": 0.384,
    "m5.4xlarge": 0.768,
    "m5.8xlarge": 1.536,
    "c5.large": 0.085,
    "c5.xlarge": 0.17,
    "c5.2xlarge": 0.34,
    "c5.4xlarge": 0.68,
    "r5.large": 0.126,
    "r5.xlarge": 0.252,
    "r5.2xlarge": 0.504,
    "g4dn.xlarge": 0.526,
    "g4dn.2xlarge": 0.752,
    "p3.2xlarge": 3.06,
}


def query_prometheus(url: str, query: str, timeout: int = 30) -> dict:
    """Execute a PromQL query against a Prometheus server."""
    endpoint = f"{url}/api/v1/query"
    params = urllib.parse.urlencode({"query": query})
    full_url = f"{endpoint}?{params}"

    req = urllib.request.Request(full_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"Warning: Prometheus query failed: {e}", file=sys.stderr)
        return {"data": {"result": []}}


def collect_metrics_from_prometheus(prometheus_url: str) -> CapacitySnapshot:
    """Collect cluster metrics from Prometheus."""
    queries = {
        "cpu_capacity": 'sum(kube_node_status_allocatable{resource="cpu"})',
        "memory_capacity": 'sum(kube_node_status_allocatable{resource="memory"})',
        "gpu_capacity": 'sum(kube_node_status_allocatable{resource="nvidia_com_gpu"})',
        "cpu_requests": 'sum(kube_pod_container_resource_requests{resource="cpu"})',
        "cpu_limits": 'sum(kube_pod_container_resource_limits{resource="cpu"})',
        "cpu_usage": "sum(rate(container_cpu_usage_seconds_total{container!=\"POD\",container!=\"\"}[5m]))",
        "memory_requests": 'sum(kube_pod_container_resource_requests{resource="memory"})',
        "memory_limits": 'sum(kube_pod_container_resource_limits{resource="memory"})',
        "memory_usage": "sum(container_memory_working_set_bytes{container!=\"POD\",container!=\"\"})",
        "pod_count": 'count(kube_pod_info)',
        "node_count": 'count(kube_node_info)',
        "gpu_requests": 'sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"})',
    }

    results = {}
    for name, query in queries.items():
        resp = query_prometheus(prometheus_url, query)
        if resp.get("data", {}).get("result"):
            value = float(resp["data"]["result"][0]["value"][1])
            results[name] = value
        else:
            results[name] = 0.0

    current_usage = ResourceUsage(
        cpu_request=results.get("cpu_requests", 0),
        cpu_limit=results.get("cpu_limits", 0),
        cpu_usage_avg=results.get("cpu_usage", 0),
        memory_request=results.get("memory_requests", 0),
        memory_limit=results.get("memory_limits", 0),
        memory_usage_avg=results.get("memory_usage", 0),
        gpu_request=results.get("gpu_requests", 0),
        pod_count=int(results.get("pod_count", 0)),
    )

    return CapacitySnapshot(
        timestamp=datetime.utcnow().isoformat() + "Z",
        cluster_name="prometheus-sourced",
        total_nodes=int(results.get("node_count", 0)),
        total_cpu_capacity=results.get("cpu_capacity", 0),
        total_memory_capacity=results.get("memory_capacity", 0),
        total_gpu_capacity=results.get("gpu_capacity", 0),
        current_usage=current_usage,
    )


def load_snapshot_from_file(file_path: str) -> CapacitySnapshot:
    """Load a capacity snapshot from a JSON file."""
    with open(file_path, "r") as f:
        data = json.load(f)

    usage_data = data.get("current_usage", {})
    current_usage = ResourceUsage(**usage_data)

    nodes = []
    for n in data.get("nodes", []):
        nodes.append(NodeInfo(**n))

    return CapacitySnapshot(
        timestamp=data.get("timestamp", datetime.utcnow().isoformat() + "Z"),
        cluster_name=data.get("cluster_name", "unknown"),
        total_nodes=data.get("total_nodes", 0),
        total_cpu_capacity=data.get("total_cpu_capacity", 0),
        total_memory_capacity=data.get("total_memory_capacity", 0),
        total_gpu_capacity=data.get("total_gpu_capacity", 0),
        current_usage=current_usage,
        nodes=nodes,
        namespaces=data.get("namespaces", {}),
    )


def project_growth(
    snapshot: CapacitySnapshot,
    daily_growth_rate: float,
    projection_days: list[int],
    avg_node_cpu: float = 8.0,
    avg_node_memory: float = 32.0,
    cost_per_node_hour: float = 0.192,
) -> list[GrowthProjection]:
    """Project resource needs based on historical growth rate."""
    projections = []

    for days in projection_days:
        growth_factor = 1.0 + (daily_growth_rate * days)

        projected_cpu = snapshot.current_usage.cpu_usage_avg * growth_factor
        projected_memory = snapshot.current_usage.memory_usage_avg * growth_factor
        projected_pods = int(snapshot.current_usage.pod_count * growth_factor)

        avg_node_cpu = avg_node_cpu or 8.0
        avg_node_memory_gb = avg_node_memory or 32.0
        avg_node_memory_bytes = avg_node_memory_gb * (1024 ** 3)

        nodes_for_cpu = max(
            1,
            int(-(-projected_cpu // avg_node_cpu)),
        )
        nodes_for_memory = max(
            1,
            int(-(-projected_memory // avg_node_memory_bytes)),
        )

        nodes_needed = max(nodes_for_cpu, nodes_for_memory)

        cpu_util_pct = (
            (projected_cpu / (snapshot.total_cpu_capacity * growth_factor)) * 100
            if snapshot.total_cpu_capacity > 0
            else 0
        )
        mem_util_pct = (
            (projected_memory / (snapshot.total_memory_capacity * growth_factor)) * 100
            if snapshot.total_memory_capacity > 0
            else 0
        )

        estimated_monthly_cost = nodes_needed * cost_per_node_hour * 730

        scaling_actions = []
        current_nodes = snapshot.total_nodes
        if nodes_needed > current_nodes:
            scale_up = nodes_needed - current_nodes
            scaling_actions.append(
                f"Scale up by {scale_up} nodes "
                f"(current: {current_nodes}, needed: {nodes_needed})"
            )
        elif nodes_needed < current_nodes * 0.7:
            scale_down = current_nodes - nodes_needed
            scaling_actions.append(
                f"Consider scaling down by {scale_down} nodes "
                f"(current: {current_nodes}, needed: {nodes_needed})"
            )

        if cpu_util_pct > 80:
            scaling_actions.append(
                f"CPU utilization projected at {cpu_util_pct:.1f}% - "
                f"consider optimizing workloads or adding nodes"
            )
        if mem_util_pct > 80:
            scaling_actions.append(
                f"Memory utilization projected at {mem_util_pct:.1f}% - "
                f"consider right-sizing pods or adding nodes"
            )

        projections.append(
            GrowthProjection(
                days_ahead=days,
                projected_cpu_usage=projected_cpu,
                projected_memory_usage=projected_memory,
                projected_pod_count=projected_pods,
                cpu_utilization_pct=cpu_util_pct,
                memory_utilization_pct=mem_util_pct,
                nodes_needed_cpu=nodes_for_cpu,
                nodes_needed_memory=nodes_for_memory,
                nodes_needed=nodes_needed,
                estimated_monthly_cost=estimated_monthly_cost,
                scaling_actions=scaling_actions,
            )
        )

    return projections


def generate_recommendations(
    snapshot: CapacitySnapshot, projections: list[GrowthProjection]
) -> dict:
    """Generate actionable capacity planning recommendations."""
    recommendations = {
        "immediate": [],
        "short_term": [],
        "long_term": [],
        "cost_optimization": [],
    }

    usage = snapshot.current_usage

    if usage.cpu_utilization_pct > 85:
        recommendations["immediate"].append(
            {
                "severity": "critical",
                "message": (
                    f"CPU utilization at {usage.cpu_utilization_pct:.1f}% exceeds "
                    f"85% threshold. Immediate scaling recommended."
                ),
            }
        )
    elif usage.cpu_utilization_pct > 70:
        recommendations["short_term"].append(
            {
                "severity": "warning",
                "message": (
                    f"CPU utilization at {usage.cpu_utilization_pct:.1f}% - "
                    f"plan for additional capacity within 30 days."
                ),
            }
        )

    if usage.memory_utilization_pct > 85:
        recommendations["immediate"].append(
            {
                "severity": "critical",
                "message": (
                    f"Memory utilization at {usage.memory_utilization_pct:.1f}% "
                    f"exceeds 85% threshold."
                ),
            }
        )

    if usage.cpu_request > 0 and usage.cpu_usage_avg > 0:
        request_to_usage = usage.cpu_usage_avg / usage.cpu_request
        if request_to_usage < 0.5:
            recommendations["cost_optimization"].append(
                {
                    "severity": "info",
                    "message": (
                        f"CPU requests are {((1 - request_to_usage) * 100):.0f}% "
                        f"over actual usage. Consider reducing requests to improve "
                        f"scheduling efficiency and reduce costs."
                    ),
                }
            )

    if usage.memory_request > 0 and usage.memory_usage_avg > 0:
        request_to_usage = usage.memory_usage_avg / usage.memory_request
        if request_to_usage < 0.5:
            recommendations["cost_optimization"].append(
                {
                    "severity": "info",
                    "message": (
                        f"Memory requests are {((1 - request_to_usage) * 100):.0f}% "
                        f"over actual usage. Right-size pod resource requests."
                    ),
                }
            )

    if projections:
        ninety_day = next(
            (p for p in projections if p.days_ahead == 90), projections[-1]
        )
        if ninety_day.nodes_needed > snapshot.total_nodes:
            additional = ninety_day.nodes_needed - snapshot.total_nodes
            recommendations["long_term"].append(
                {
                    "severity": "warning",
                    "message": (
                        f"Projected to need {ninety_day.nodes_needed} nodes in "
                        f"90 days (+{additional} from current {snapshot.total_nodes}). "
                        f"Estimated additional monthly cost: "
                        f"${additional * 0.192 * 730:,.2f}"
                    ),
                }
            )

        for p in projections:
            if p.scaling_actions:
                recommendations["short_term"].extend(
                    [
                        {"severity": "info", "message": f"Day {p.days_ahead}: {a}"}
                        for a in p.scaling_actions
                    ]
                )

    return recommendations


def generate_ascii_chart(
    projections: list[GrowthProjection], metric: str, width: int = 60, height: int = 20
) -> str:
    """Generate an ASCII chart for the given metric across projections."""
    if not projections:
        return "No projection data available."

    if metric == "cpu_utilization":
        values = [p.cpu_utilization_pct for p in projections]
        labels = [f"Day {p.days_ahead}" for p in projections]
        title = "CPU Utilization Projection (%)"
    elif metric == "memory_utilization":
        values = [p.memory_utilization_pct for p in projections]
        labels = [f"Day {p.days_ahead}" for p in projections]
        title = "Memory Utilization Projection (%)"
    elif metric == "nodes_needed":
        values = [float(p.nodes_needed) for p in projections]
        labels = [f"Day {p.days_ahead}" for p in projections]
        title = "Nodes Needed Projection"
    elif metric == "monthly_cost":
        values = [p.estimated_monthly_cost for p in projections]
        labels = [f"Day {p.days_ahead}" for p in projections]
        title = "Estimated Monthly Cost ($)"
    else:
        return f"Unknown metric: {metric}"

    if not values:
        return "No data."

    max_val = max(values) * 1.1 or 1.0
    min_val = 0

    lines = [f"  {title}", ""]

    for i in range(height, -1, -1):
        threshold = min_val + (max_val - min_val) * (i / height)
        line = f"  {threshold:>10.1f} |"
        for v in values:
            normalized = (v - min_val) / (max_val - min_val) * height
            if normalized >= i:
                line += " #"
            else:
                line += "  "
        lines.append(line)

    x_axis = "            +" + "--" * len(values)
    lines.append(x_axis)

    label_line = "             "
    for label in labels:
        label_line += f"{label[:4]:>4}"
    lines.append(label_line)

    return "\n".join(lines)


def format_bytes(size_bytes: float) -> str:
    """Format bytes into human-readable string."""
    for unit in ["B", "Ki", "Mi", "Gi", "Ti"]:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f}Pi"


def print_report(
    snapshot: CapacitySnapshot,
    projections: list[GrowthProjection],
    recommendations: dict,
    output_format: str = "text",
):
    """Print the capacity planning report."""
    if output_format == "json":
        report = {
            "snapshot": asdict(snapshot),
            "projections": [asdict(p) for p in projections],
            "recommendations": recommendations,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }
        print(json.dumps(report, indent=2, default=str))
        return

    usage = snapshot.current_usage

    print("=" * 70)
    print("  KUBERNETES CLUSTER CAPACITY PLANNING REPORT")
    print("=" * 70)
    print(f"\n  Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Cluster:   {snapshot.cluster_name}")
    print(f"  Nodes:     {snapshot.total_nodes}")
    print()

    print("-" * 70)
    print("  CURRENT UTILIZATION BASELINE")
    print("-" * 70)
    print(f"  CPU Capacity:    {snapshot.total_cpu_capacity:.1f} cores")
    print(f"  CPU Requests:    {usage.cpu_request:.2f} cores")
    print(f"  CPU Usage (avg): {usage.cpu_usage_avg:.2f} cores")
    print(f"  CPU Usage (peak):{usage.cpu_usage_peak:.2f} cores")
    print(f"  CPU Utilization: {usage.cpu_utilization_pct:.1f}%")
    print()

    mem_cap_gb = snapshot.total_memory_capacity / (1024 ** 3)
    mem_req_gb = usage.memory_request / (1024 ** 3)
    mem_avg_gb = usage.memory_usage_avg / (1024 ** 3)
    mem_peak_gb = usage.memory_usage_peak / (1024 ** 3)
    print(f"  Memory Capacity:    {mem_cap_gb:.1f} GiB")
    print(f"  Memory Requests:    {mem_req_gb:.1f} GiB")
    print(f"  Memory Usage (avg): {mem_avg_gb:.1f} GiB")
    print(f"  Memory Usage (peak):{mem_peak_gb:.1f} GiB")
    print(f"  Memory Utilization: {usage.memory_utilization_pct:.1f}%")
    print()

    if snapshot.total_gpu_capacity > 0:
        print(f"  GPU Capacity:    {snapshot.total_gpu_capacity:.0f}")
        print(f"  GPU Requests:    {usage.gpu_request:.0f}")
        print()

    print(f"  Active Pods:     {usage.pod_count}")
    print()

    if projections:
        print("-" * 70)
        print("  CAPACITY PROJECTIONS")
        print("-" * 70)
        print(
            f"  {'Days':>5} | {'CPU Util%':>10} | {'Mem Util%':>10} "
            f"| {'Pods':>8} | {'Nodes':>6} | {'Cost/mo':>12}"
        )
        print("  " + "-" * 65)
        for p in projections:
            print(
                f"  {p.days_ahead:>5} | {p.cpu_utilization_pct:>9.1f}% "
                f"| {p.memory_utilization_pct:>9.1f}% "
                f"| {p.projected_pod_count:>8} | {p.nodes_needed:>6} "
                f"| ${p.estimated_monthly_cost:>10,.2f}"
            )
        print()

        if len(projections) > 1:
            print("-" * 70)
            print("  PROJECTION CHARTS")
            print("-" * 70)
            print(generate_ascii_chart(projections, "cpu_utilization"))
            print()
            print(generate_ascii_chart(projections, "memory_utilization"))
            print()

    print("-" * 70)
    print("  NODE SCALING RECOMMENDATIONS")
    print("-" * 70)
    all_recs = []
    for category, items in recommendations.items():
        for item in items:
            all_recs.append((category, item))

    if not all_recs:
        print("  No scaling actions required at this time.")
    else:
        for category, item in all_recs:
            severity_marker = {
                "critical": "[!!!]",
                "warning": "[!]",
                "info": "[i]",
            }.get(item.get("severity", "info"), "[i]")
            print(f"  {severity_marker} [{category.upper()}] {item['message']}")
    print()

    print("-" * 70)
    print("  COST IMPLICATIONS")
    print("-" * 70)
    if projections:
        current_monthly = snapshot.total_nodes * 0.192 * 730
        ninety_day = next(
            (p for p in projections if p.days_ahead == 90), projections[-1]
        )
        print(f"  Current estimated monthly cost:  ${current_monthly:>10,.2f}")
        print(
            f"  Projected 90-day monthly cost:   "
            f"${ninety_day.estimated_monthly_cost:>10,.2f}"
        )
        delta = ninety_day.estimated_monthly_cost - current_monthly
        print(f"  Cost delta:                      ${delta:>+10,.2f}")
        additional_nodes = ninety_day.nodes_needed - snapshot.total_nodes
        if additional_nodes > 0:
            print(
                f"  Additional nodes needed:         {additional_nodes:>10}"
            )
    print()
    print("=" * 70)


def save_snapshot(snapshot: CapacitySnapshot, output_path: str):
    """Save a snapshot to a JSON file for historical tracking."""
    data = asdict(snapshot)
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"Snapshot saved to {output_path}")


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Kubernetes Cluster Capacity Planning Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --prometheus-url http://prometheus:9090
  %(prog)s --cluster-data cluster_snapshot.json --growth-rate 0.10
  %(prog)s --prometheus-url http://prometheus:9090 --days 30 60 90 180
  %(prog)s --cluster-data snapshot.json --output-format json --output-file report.json
        """,
    )

    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--prometheus-url",
        help="Prometheus server URL for live metrics collection",
    )
    source_group.add_argument(
        "--cluster-data",
        help="Path to a JSON file containing cluster capacity snapshot",
    )

    parser.add_argument(
        "--namespace",
        help="Filter metrics to a specific namespace (Prometheus mode only)",
    )
    parser.add_argument(
        "--growth-rate",
        type=float,
        default=0.05,
        help="Daily resource growth rate as a decimal (default: 0.05 = 5%% per day)",
    )
    parser.add_argument(
        "--days",
        nargs="+",
        type=int,
        default=[7, 14, 30, 60, 90],
        help="Projection horizons in days (default: 7 14 30 60 90)",
    )
    parser.add_argument(
        "--avg-node-cpu",
        type=float,
        default=8.0,
        help="Average CPU cores per node for planning (default: 8.0)",
    )
    parser.add_argument(
        "--avg-node-memory",
        type=float,
        default=32.0,
        help="Average memory (GiB) per node for planning (default: 32.0)",
    )
    parser.add_argument(
        "--cost-per-node-hour",
        type=float,
        default=0.192,
        help="Cost per node per hour in USD (default: 0.192)",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--output-file",
        help="Write report to a file instead of stdout",
    )
    parser.add_argument(
        "--save-snapshot",
        help="Save the collected snapshot to a JSON file for future use",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    if args.prometheus_url:
        if not HAS_URLLIB:
            print("Error: urllib is required for Prometheus queries", file=sys.stderr)
            sys.exit(1)
        snapshot = collect_metrics_from_prometheus(args.prometheus_url)
    else:
        if not os.path.exists(args.cluster_data):
            print(f"Error: File not found: {args.cluster_data}", file=sys.stderr)
            sys.exit(1)
        snapshot = load_snapshot_from_file(args.cluster_data)

    if args.save_snapshot:
        save_snapshot(snapshot, args.save_snapshot)

    projections = project_growth(
        snapshot,
        daily_growth_rate=args.growth_rate,
        projection_days=sorted(args.days),
        avg_node_cpu=args.avg_node_cpu,
        avg_node_memory=args.avg_node_memory,
        cost_per_node_hour=args.cost_per_node_hour,
    )

    recommendations = generate_recommendations(snapshot, projections)

    if args.output_file:
        import io

        old_stdout = sys.stdout
        sys.stdout = buffer = io.StringIO()
        print_report(snapshot, projections, recommendations, args.output_format)
        sys.stdout = old_stdout
        with open(args.output_file, "w") as f:
            f.write(buffer.getvalue())
        print(f"Report written to {args.output_file}")
    else:
        print_report(snapshot, projections, recommendations, args.output_format)


if __name__ == "__main__":
    main()
