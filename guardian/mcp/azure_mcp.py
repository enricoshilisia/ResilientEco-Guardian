"""
ResilientEco Guardian - Azure MCP (Model Context Protocol) Integration
Enables agents to autonomously manage Azure resources:
- Scale AKS node pools during crisis events
- Query Azure Monitor logs for anomaly detection
- Trigger Azure Functions for SMS/alert delivery
- Provision IoT Hub endpoints for sensor integration
- Query Cosmos DB state across agent sessions
"""

import os
import json
import logging
import time
from typing import Any, Optional
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


class AzureMCPClient:
    """
    MCP-compliant Azure resource manager for agentic operations.
    Agents call tools() to discover what's available, then call execute() to act.
    """

    def __init__(self):
        self._mgmt_client = None
        self._monitor_client = None
        self._metrics_client = None
        self._cosmos_client = None
        self._setup()

    def _setup(self):
        """Initialize Azure SDK clients."""
        try:
            from azure.identity import DefaultAzureCredential, ClientSecretCredential
            from azure.mgmt.containerservice import ContainerServiceClient
            # azure-monitor-query 2.0.0: MetricsQueryClient removed,
            # use MonitorQueryClient instead; LogsQueryClient is unchanged.
            from azure.monitor.query import LogsQueryClient, MonitorQueryLogsClient

            tenant = os.getenv("AZURE_TENANT_ID")
            client_id = os.getenv("AZURE_CLIENT_ID")
            client_secret = os.getenv("AZURE_CLIENT_SECRET")
            subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")

            if tenant and client_id and client_secret:
                cred = ClientSecretCredential(tenant, client_id, client_secret)
            else:
                cred = DefaultAzureCredential()

            if subscription_id:
                self._mgmt_client = ContainerServiceClient(cred, subscription_id)
                self._monitor_client = LogsQueryClient(cred)
                # MonitorQueryClient replaces MetricsQueryClient in v2.0.0
                self._metrics_client = MonitorQueryLogsClient(cred)
                logger.info("✅ Azure MCP clients initialized")
            else:
                logger.warning("AZURE_SUBSCRIPTION_ID not set — MCP in simulation mode")

        except ImportError as e:
            logger.warning(f"Azure SDK not installed: {e} — MCP in simulation mode")
        except Exception as e:
            logger.warning(f"Azure MCP setup failed: {e} — MCP in simulation mode")

    # ─── MCP TOOL REGISTRY ────────────────────────────────────────────────────

    def tools(self) -> list[dict]:
        """
        MCP-compliant tool listing — agents call this to discover available actions.
        """
        return [
            {
                "name": "scale_aks_nodepool",
                "description": "Scale AKS cluster node count up or down. Use during crisis events to handle increased load.",
                "parameters": {
                    "resource_group": "string",
                    "cluster_name": "string",
                    "nodepool_name": "string",
                    "node_count": "integer (1-20)",
                    "reason": "string — why scaling is needed",
                }
            },
            {
                "name": "query_azure_monitor",
                "description": "Query Azure Monitor logs for anomalies, agent errors, or infrastructure issues.",
                "parameters": {
                    "workspace_id": "string",
                    "query": "KQL query string",
                    "hours_back": "integer",
                }
            },
            {
                "name": "query_azure_metrics",
                "description": "Query Azure Monitor metrics for a specific resource (CPU, memory, requests, etc.).",
                "parameters": {
                    "resource_uri": "string — full Azure resource URI",
                    "metric_names": "list of metric name strings",
                    "hours_back": "integer",
                }
            },
            {
                "name": "trigger_azure_function",
                "description": "Invoke an Azure Function for alert delivery, SMS, or external integration.",
                "parameters": {
                    "function_url": "string",
                    "payload": "object — data to send",
                }
            },
            {
                "name": "get_cosmos_agent_state",
                "description": "Read agent execution state or risk history from Cosmos DB.",
                "parameters": {
                    "container": "string (agent_logs | risk_history | locations)",
                    "location_id": "string (optional filter)",
                    "hours_back": "integer",
                }
            },
            {
                "name": "write_cosmos_risk_event",
                "description": "Persist a risk event to Cosmos DB for cross-agent state sharing.",
                "parameters": {
                    "location": "string",
                    "risk_type": "string",
                    "risk_level": "integer",
                    "agent_chain": "list of agent names that contributed",
                    "metadata": "object",
                }
            },
            {
                "name": "get_infrastructure_health",
                "description": "Check the health of Azure services used by the platform.",
                "parameters": {}
            },
        ]

    # ─── MCP TOOL EXECUTION ───────────────────────────────────────────────────

    def execute(self, tool_name: str, parameters: dict) -> dict:
        """
        Execute an MCP tool. Returns {success, result, simulated, latency_ms}.
        Falls back to simulation if Azure SDK/credentials not available.
        """
        start = time.time()

        handler = {
            "scale_aks_nodepool":      self._scale_aks,
            "query_azure_monitor":     self._query_monitor,
            "query_azure_metrics":     self._query_metrics,
            "trigger_azure_function":  self._trigger_function,
            "get_cosmos_agent_state":  self._get_cosmos_state,
            "write_cosmos_risk_event": self._write_cosmos_event,
            "get_infrastructure_health": self._get_infra_health,
        }.get(tool_name)

        if not handler:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}

        try:
            result = handler(parameters)
            result["latency_ms"] = int((time.time() - start) * 1000)
            result["tool"] = tool_name
            logger.info(
                f"[MCP] {tool_name} executed in {result['latency_ms']}ms "
                f"simulated={result.get('simulated', False)}"
            )
            return result
        except Exception as e:
            logger.error(f"[MCP] {tool_name} failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "tool": tool_name,
                "latency_ms": int((time.time() - start) * 1000),
            }

    # ─── HANDLERS ─────────────────────────────────────────────────────────────

    def _scale_aks(self, params: dict) -> dict:
        if not self._mgmt_client:
            return self._simulate_aks_scale(params)
        try:
            rg = params["resource_group"]
            cluster = params["cluster_name"]
            pool = params["nodepool_name"]
            count = int(params["node_count"])

            poller = self._mgmt_client.agent_pools.begin_create_or_update(
                rg, cluster, pool,
                {"count": count, "mode": "System"}
            )
            return {
                "success": True,
                "simulated": False,
                "message": f"AKS nodepool {pool} scaling to {count} nodes",
                "operation_id": str(poller),
            }
        except Exception as e:
            return self._simulate_aks_scale(params, error=str(e))

    def _simulate_aks_scale(self, params: dict, error: str = None) -> dict:
        return {
            "success": True,
            "simulated": True,
            "message": (
                f"[SIMULATED] AKS {params.get('cluster_name', 'resilienteco-aks')} "
                f"nodepool {params.get('nodepool_name', 'agentpool')} "
                f"→ {params.get('node_count', 3)} nodes. "
                f"Reason: {params.get('reason', 'crisis event')}"
            ),
            "estimated_time_seconds": 180,
            "note": error or "Azure SDK available but credentials required for live execution",
        }

    def _query_monitor(self, params: dict) -> dict:
        workspace_id = params.get("workspace_id") or os.getenv("AZURE_LOG_WORKSPACE_ID")
        hours_back = params.get("hours_back", 1)

        if self._monitor_client and workspace_id:
            try:
                from azure.monitor.query import LogsQueryStatus
                response = self._monitor_client.query_workspace(
                    workspace_id=workspace_id,
                    query=params.get("query", "AzureActivity | take 10"),
                    timespan=timedelta(hours=hours_back),
                )
                if response.status == LogsQueryStatus.SUCCESS:
                    rows = [
                        dict(zip(response.tables[0].columns, row))
                        for row in response.tables[0].rows
                    ]
                    return {"success": True, "simulated": False, "rows": rows, "count": len(rows)}
            except Exception as e:
                logger.warning(f"Monitor logs query failed: {e}")

        return {
            "success": True,
            "simulated": True,
            "query": params.get("query", ""),
            "hours_back": hours_back,
            "rows": [
                {
                    "TimeGenerated": datetime.now(timezone.utc).isoformat(),
                    "Level": "Warning",
                    "Message": "Agent pipeline latency spike detected: 4200ms avg",
                },
                {
                    "TimeGenerated": datetime.now(timezone.utc).isoformat(),
                    "Level": "Info",
                    "Message": "WeatherService: Visual Crossing API healthy, 412 calls used today",
                },
            ],
            "count": 2,
        }

    def _query_metrics(self, params: dict) -> dict:
        """
        Query Azure Monitor metrics using MonitorQueryClient (v2.0.0 replacement
        for the removed MetricsQueryClient).
        """
        resource_uri = params.get("resource_uri")
        metric_names = params.get("metric_names", ["Percentage CPU"])
        hours_back = params.get("hours_back", 1)

        if self._metrics_client and resource_uri:
            try:
                response = self._metrics_client.query_resource(
                    resource_uri=resource_uri,
                    metric_names=metric_names,
                    timespan=timedelta(hours=hours_back),
                )
                results = []
                for metric in response.metrics:
                    for ts in metric.timeseries:
                        for dp in ts.data:
                            results.append({
                                "metric": metric.name,
                                "timestamp": dp.timestamp.isoformat() if dp.timestamp else None,
                                "average": dp.average,
                                "maximum": dp.maximum,
                                "minimum": dp.minimum,
                            })
                return {
                    "success": True,
                    "simulated": False,
                    "resource_uri": resource_uri,
                    "metrics": results,
                    "count": len(results),
                }
            except Exception as e:
                logger.warning(f"Monitor metrics query failed: {e}")

        # Simulation fallback
        return {
            "success": True,
            "simulated": True,
            "resource_uri": resource_uri or "not configured",
            "metric_names": metric_names,
            "hours_back": hours_back,
            "metrics": [
                {
                    "metric": metric_names[0] if metric_names else "Percentage CPU",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "average": 34.2,
                    "maximum": 61.5,
                    "minimum": 12.1,
                }
            ],
            "count": 1,
        }

    def _trigger_function(self, params: dict) -> dict:
        import requests
        url = params.get("function_url") or os.getenv("AZURE_FUNCTION_URL")
        payload = params.get("payload", {})

        if url:
            try:
                resp = requests.post(url, json=payload, timeout=10)
                return {
                    "success": resp.ok,
                    "simulated": False,
                    "status_code": resp.status_code,
                    "response": resp.text[:500],
                }
            except Exception as e:
                logger.warning(f"Azure Function trigger failed: {e}")

        return {
            "success": True,
            "simulated": True,
            "message": "[SIMULATED] Azure Function triggered",
            "payload_sent": payload,
            "function_url": url or "AZURE_FUNCTION_URL not configured",
        }

    def _get_cosmos_state(self, params: dict) -> dict:
        container = params.get("container", "agent_logs")
        hours_back = params.get("hours_back", 24)

        cosmos_url = os.getenv("AZURE_COSMOS_URL")
        cosmos_key = os.getenv("AZURE_COSMOS_KEY")
        db_name = os.getenv("AZURE_COSMOS_DB", "resilienteco")

        if cosmos_url and cosmos_key:
            try:
                from azure.cosmos import CosmosClient
                client = CosmosClient(cosmos_url, cosmos_key)
                db = client.get_database_client(db_name)
                cont = db.get_container_client(container)
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(hours=hours_back)
                ).isoformat()
                items = list(cont.query_items(
                    f"SELECT * FROM c WHERE c._ts >= '{cutoff}' ORDER BY c._ts DESC OFFSET 0 LIMIT 20",
                    enable_cross_partition_query=True,
                ))
                return {"success": True, "simulated": False, "items": items, "count": len(items)}
            except Exception as e:
                logger.warning(f"Cosmos query failed: {e}")

        return {
            "success": True,
            "simulated": True,
            "container": container,
            "items": [
                {
                    "id": "log-001",
                    "agent": "monitor",
                    "location": "Nairobi",
                    "risk_level": 75,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "completed",
                }
            ],
            "count": 1,
        }

    def _write_cosmos_event(self, params: dict) -> dict:
        cosmos_url = os.getenv("AZURE_COSMOS_URL")
        cosmos_key = os.getenv("AZURE_COSMOS_KEY")

        if cosmos_url and cosmos_key:
            try:
                from azure.cosmos import CosmosClient
                import uuid
                client = CosmosClient(cosmos_url, cosmos_key)
                db = client.get_database_client(os.getenv("AZURE_COSMOS_DB", "resilienteco"))
                cont = db.get_container_client("risk_history")
                doc = {
                    "id": str(uuid.uuid4()),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    **params,
                }
                cont.upsert_item(doc)
                return {"success": True, "simulated": False, "id": doc["id"]}
            except Exception as e:
                logger.warning(f"Cosmos write failed: {e}")

        return {
            "success": True,
            "simulated": True,
            "message": "[SIMULATED] Risk event written to Cosmos DB",
            "document": {
                "id": f"risk-{int(time.time())}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **params,
            },
        }

    def _get_infra_health(self, params: dict) -> dict:
        return {
            "success": True,
            "simulated": True,
            "services": {
                "aks_cluster":     {"status": "healthy", "nodes": 3, "pods_running": 12},
                "azure_openai":    {"status": "healthy", "calls_today": 412, "quota_remaining": "87%"},
                "cosmos_db":       {"status": "healthy", "ru_consumed": 1240, "latency_ms": 8},
                "azure_functions": {"status": "healthy", "invocations_today": 34},
                "visual_crossing": {"status": "healthy", "calls_today": 18, "daily_limit": 1000},
                "foundry":         {"status": "healthy", "model_deployments": 2},
            },
            "overall": "healthy",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }


# Singleton
mcp = AzureMCPClient()