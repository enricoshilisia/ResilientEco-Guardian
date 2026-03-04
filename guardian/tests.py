from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from guardian.agents.core_agents import TypeBasedRouter, _build_agent_pipeline
from guardian.models import (
    AgentExecutionLog,
    Organization,
    OrganizationMembership,
    RiskPolicyVersion,
    WorkflowCheckpoint,
)
from guardian.services.policy_engine import evaluate_risk_policy
from guardian.services.weather_middleware import transform_weather_data
from guardian.services.weather_service import geocode_location_name
from guardian.views import _find_execution_by_session, _upsert_workflow_checkpoint, _public_result_payload


class RouterTests(SimpleTestCase):
    def test_no_false_drought_without_explicit_signals(self):
        weather = {
            "temperature": 24,
            "total_rain_24h": 0,
            "today_forecast": {"precip_prob": 20},
            "_middleware": {"routing_features": {}, "metrics": {"heat_index": 26}},
        }
        graph, features = TypeBasedRouter.route(weather, "general_forecast")
        self.assertEqual(graph, "standard_forecast_graph")
        self.assertEqual(features["rain_30d"], None)
        self.assertEqual(features["soil_moisture"], None)

    def test_intent_fallback_routes_when_weather_is_inconclusive(self):
        weather = {"temperature": 24, "total_rain_24h": 0}
        graph, _ = TypeBasedRouter.route(weather, "flood_specialist")
        self.assertEqual(graph, "flood_graph")

    def test_severe_weather_selected_from_compound_rain_signal(self):
        weather = {
            "temperature": 26,
            "total_rain_24h": 15,
            "_middleware": {
                "routing_features": {
                    "precip_probability": 95,
                    "forecast_rain_today": 30,
                }
            },
        }
        graph, _ = TypeBasedRouter.route(weather, "general_forecast")
        self.assertEqual(graph, "severe_weather_graph")

    def test_high_precip_probability_alone_is_not_severe(self):
        weather = {
            "temperature": 28,
            "total_rain_24h": 1.0,
            "_middleware": {"routing_features": {"precip_probability": 100}},
        }
        graph, _ = TypeBasedRouter.route(weather, "general_forecast")
        self.assertEqual(graph, "standard_forecast_graph")

    def test_heatwave_graph_selected_from_heat_index(self):
        weather = {
            "temperature": 33,
            "_middleware": {"routing_features": {"heat_index": 37.5}},
        }
        graph, _ = TypeBasedRouter.route(weather, "general_forecast")
        self.assertEqual(graph, "heatwave_graph")

    def test_pipeline_order_changes_for_severe_weather(self):
        pipeline = [name for name, _ in _build_agent_pipeline("severe_weather_graph")]
        self.assertEqual(pipeline, ["monitor", "predict", "decision", "governance", "action"])


class WeatherMiddlewareTests(SimpleTestCase):
    def test_transform_supports_flat_summary_shape(self):
        payload = {
            "location": "Nairobi",
            "data_source": "visual_crossing",
            "temperature": 27,
            "current_precipitation": 1.2,
            "current_rain": 1.2,
            "humidity": 78,
            "total_rain_24h": 12.5,
            "current_conditions": "Rain",
            "observation_time": "2026-03-04T10:00:00",
            "today_forecast": {"daily_total_mm": 7, "precip_prob": 82, "temp_max": 31, "temp_min": 22},
            "tomorrow_forecast": {"daily_total_mm": 5, "precip_prob": 55, "temp_max": 30, "temp_min": 21},
        }

        transformed = transform_weather_data(payload, "Nairobi")
        summary = transformed["summary"]
        routing = transformed["routing_features"]

        self.assertEqual(summary["current_temp"], 27.0)
        self.assertEqual(summary["today_rain_total"], 7.0)
        self.assertEqual(summary["data_source"], "visual_crossing")
        self.assertEqual(routing["precip_probability"], 82.0)
        self.assertEqual(routing["total_rain_24h"], 12.5)
        self.assertEqual(routing["forecast_rain_today"], 7.0)
        self.assertEqual(routing["forecast_rain_tomorrow"], 5.0)


class WeatherGeocodeTests(TestCase):
    @patch("guardian.services.weather_service.requests.get")
    def test_geocode_location_name_returns_coordinates(self, mock_get):
        mock_get.return_value.ok = True
        mock_get.return_value.json.return_value = {
            "results": [
                {
                    "name": "Lagos",
                    "country": "Nigeria",
                    "admin1": "Lagos",
                    "latitude": 6.455,
                    "longitude": 3.384,
                }
            ]
        }
        geocode_location_name.cache_clear()
        result = geocode_location_name("Lagos")
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "Lagos")
        self.assertEqual(result["country"], "Nigeria")
        self.assertEqual(result["lat"], 6.455)
        self.assertEqual(result["lon"], 3.384)


class ExecutionLookupTests(TestCase):
    def test_find_execution_by_session_from_output_payload(self):
        execution = AgentExecutionLog.objects.create(
            agent_type="decision",
            input_payload={"query": "flood risk", "lat": -1.2921, "lon": 36.8219},
            output_payload={"session_id": "sess-1234", "checkpoint_status": {"requires_approval": True}},
        )

        found = _find_execution_by_session("sess-1234")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, execution.id)

    def test_find_execution_by_session_from_input_payload(self):
        execution = AgentExecutionLog.objects.create(
            agent_type="decision",
            input_payload={"query": "flood risk", "session_id": "sess-5678"},
            output_payload={"status": "ok"},
        )

        found = _find_execution_by_session("sess-5678")
        self.assertIsNotNone(found)
        self.assertEqual(found.id, execution.id)


class PolicyEngineTests(TestCase):
    def test_default_policy_requires_checkpoint_for_critical_risk(self):
        result = evaluate_risk_policy(
            risk_assessment={"flood_risk": 92, "drought_risk": 15, "heatwave_risk": 10},
            weather_data={"_middleware": {"routing_features": {"precip_probability": 90, "total_rain_24h": 65}}},
            intent_classification="flood_specialist",
        )
        self.assertEqual(result["alert_level"], "RED")
        self.assertTrue(result["requires_checkpoint"])
        self.assertEqual(result["required_role"], "admin")

    def test_database_policy_overrides_default(self):
        RiskPolicyVersion.objects.create(
            name="global_default",
            version="test-1",
            is_active=True,
            rules={
                "rules": [
                    {
                        "id": "test_medium",
                        "risk_type": "any",
                        "threshold": 10,
                        "alert_level": "YELLOW",
                        "priority": "medium",
                        "immediate_action_required": False,
                        "response_timeline_hours": 12,
                        "requires_checkpoint": False,
                        "required_role": "operator",
                        "auto_expire_minutes": 30,
                        "recommended_actions": ["Test action"],
                    }
                ]
            },
        )

        result = evaluate_risk_policy(
            risk_assessment={"flood_risk": 15, "drought_risk": 0, "heatwave_risk": 0},
            weather_data={},
        )
        self.assertEqual(result["policy_source"], "database")
        self.assertEqual(result["policy_version"], "test-1")
        self.assertEqual(result["alert_level"], "YELLOW")
        self.assertEqual(result["rule_id"], "test_medium")


class WorkflowCheckpointTests(TestCase):
    def test_upsert_workflow_checkpoint_creates_pending_record(self):
        execution = AgentExecutionLog.objects.create(
            agent_type="decision",
            input_payload={"query": "flood risk"},
            output_payload={},
        )
        session_id = "sess-check-1"
        results = {
            "session_id": session_id,
            "selected_graph": "flood_graph",
            "pipeline": ["monitor", "predict", "decision", "governance", "action"],
            "task_ledger": [{"task": "Decision Analysis", "status": "completed"}],
            "workflow_state": {"session_id": session_id, "selected_graph": "flood_graph"},
            "checkpoint_status": {
                "requires_approval": True,
                "pending_action": "issue_critical_alert",
                "approval_role": "admin",
                "auto_expire_minutes": 30,
                "paused_at_step": "decision",
                "resume_from_step": "governance",
            },
        }
        _upsert_workflow_checkpoint(
            results=results,
            query="flood risk",
            location_name="Nairobi",
            lat=-1.2921,
            lon=36.8219,
            organization=None,
            user=None,
            execution_log=execution,
        )
        checkpoint = WorkflowCheckpoint.objects.get(session_id=session_id)
        self.assertEqual(checkpoint.status, "pending")
        self.assertEqual(checkpoint.resume_from_step, "governance")
        self.assertEqual(checkpoint.execution_log_id, execution.id)

    def test_public_result_payload_hides_internal_state(self):
        payload = {"session_id": "x", "workflow_state": {"foo": "bar"}, "selected_graph": "flood_graph"}
        clean = _public_result_payload(payload)
        self.assertIn("session_id", clean)
        self.assertIn("selected_graph", clean)
        self.assertNotIn("workflow_state", clean)


class RunAgentApiContractTests(TestCase):
    def test_run_agent_returns_required_contract_fields(self):
        mocked_result = {
            "session_id": "sess-api-1",
            "monitor": "ok",
            "predict": "ok",
            "decision": "ok",
            "action": "ok",
            "governance": "ok",
            "intent_classification": "general_forecast",
            "selected_graph": "standard_forecast_graph",
            "routing_features": {"precip_probability": 45.0},
            "pipeline": ["monitor", "predict", "decision", "action", "governance"],
            "task_ledger": [{"task": "Intent Classification", "status": "completed"}],
            "workflow_state": {"session_id": "sess-api-1"},
        }

        with patch("guardian.views.run_all_agents", return_value=mocked_result):
            response = self.client.post(
                reverse("run_agent"),
                data={
                    "query": "Weather update",
                    "location_name": "Nairobi",
                    "lat": -1.2921,
                    "lon": 36.8219,
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        result = payload["result"]
        for key in (
            "intent_classification",
            "selected_graph",
            "routing_features",
            "pipeline",
            "task_ledger",
        ):
            self.assertIn(key, result)
        self.assertNotIn("workflow_state", result)

    def test_run_agent_persists_checkpoint_when_critical(self):
        mocked_result = {
            "session_id": "sess-api-critical",
            "monitor": "ok",
            "predict": "ok",
            "decision": "checkpoint pending",
            "action": "skipped",
            "governance": "skipped",
            "intent_classification": "flood_specialist",
            "selected_graph": "flood_graph",
            "routing_features": {"precip_probability": 95.0, "total_rain_24h": 70.0},
            "pipeline": ["monitor", "predict", "decision", "governance", "action"],
            "task_ledger": [{"task": "Decision Analysis", "status": "completed"}],
            "workflow_state": {
                "session_id": "sess-api-critical",
                "location": "Nairobi",
                "lat": -1.2921,
                "lon": 36.8219,
                "user_query": "Flood alert",
            },
            "checkpoint_status": {
                "requires_approval": True,
                "approved": False,
                "pending_action": "issue_critical_alert",
                "approval_role": "admin",
                "auto_expire_minutes": 30,
                "paused_at_step": "decision",
                "resume_from_step": "governance",
            },
        }

        with patch("guardian.views.run_all_agents", return_value=mocked_result):
            response = self.client.post(
                reverse("run_agent"),
                data={
                    "query": "Flood alert",
                    "location_name": "Nairobi",
                    "lat": -1.2921,
                    "lon": 36.8219,
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertIn("checkpoint_status", result)
        checkpoint = WorkflowCheckpoint.objects.get(session_id="sess-api-critical")
        self.assertEqual(checkpoint.status, "pending")
        self.assertEqual(checkpoint.resume_from_step, "governance")


class ApproveCheckpointApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin1", password="testpass123")
        self.org = Organization.objects.create(
            name="Test Org",
            slug="test-org",
            org_type="government",
            country="Kenya",
        )
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            role="admin",
            is_active=True,
        )
        self.client.force_login(self.user)

    def test_approve_checkpoint_resumes_saved_step(self):
        checkpoint = WorkflowCheckpoint.objects.create(
            session_id="sess-resume-1",
            organization=self.org,
            status="pending",
            required_role="admin",
            paused_at_step="decision",
            resume_from_step="governance",
            pending_action="issue_critical_alert",
            user_query="Flood alert",
            location_name="Nairobi",
            lat=-1.2921,
            lon=36.8219,
            selected_graph="flood_graph",
            pipeline=["monitor", "predict", "decision", "governance", "action"],
            task_ledger=[],
            partial_results={"session_id": "sess-resume-1", "decision": "checkpoint pending"},
            message_state={
                "session_id": "sess-resume-1",
                "location": "Nairobi",
                "lat": -1.2921,
                "lon": 36.8219,
                "user_query": "Flood alert",
                "selected_graph": "flood_graph",
            },
            checkpoint_payload={"requires_approval": True, "approval_role": "admin"},
            expires_at=timezone.now() + timedelta(minutes=30),
        )

        resumed_result = {
            "session_id": "sess-resume-1",
            "monitor": "ok",
            "predict": "ok",
            "decision": "ok",
            "governance": "ok",
            "action": "ok",
            "intent_classification": "flood_specialist",
            "selected_graph": "flood_graph",
            "routing_features": {"precip_probability": 92.0},
            "pipeline": ["monitor", "predict", "decision", "governance", "action"],
            "task_ledger": [{"task": "Governance Agent Execution", "status": "completed"}],
            "workflow_state": {
                "session_id": "sess-resume-1",
                "location": "Nairobi",
                "lat": -1.2921,
                "lon": 36.8219,
                "user_query": "Flood alert",
            },
        }

        with patch("guardian.views.run_all_agents", return_value=resumed_result) as mocked_resume:
            response = self.client.post(
                reverse("approve_checkpoint"),
                data={"session_id": "sess-resume-1"},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "approved")
        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.status, "resumed")
        self.assertEqual(checkpoint.approved_by_id, self.user.id)
        self.assertIsNotNone(checkpoint.resumed_at)

        mocked_resume.assert_called_once()
        kwargs = mocked_resume.call_args.kwargs
        self.assertEqual(kwargs["session_id"], "sess-resume-1")
        self.assertTrue(kwargs["checkpoint_approved"])
        self.assertEqual(kwargs["resume_from_step"], "governance")
