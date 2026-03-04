from django.contrib import admin
from .models import (
    IdempotencyRequest,
    OfflineEvaluationRun,
    RiskPolicyVersion,
    WorkflowCheckpoint,
    WorkflowGraphConfig,
)


@admin.register(RiskPolicyVersion)
class RiskPolicyVersionAdmin(admin.ModelAdmin):
    list_display = ("name", "version", "is_active", "created_at", "activated_at")
    list_filter = ("is_active", "name")
    search_fields = ("name", "version")


@admin.register(WorkflowCheckpoint)
class WorkflowCheckpointAdmin(admin.ModelAdmin):
    list_display = (
        "session_id", "status", "required_role", "organization",
        "paused_at_step", "resume_from_step", "created_at", "expires_at",
    )
    list_filter = ("status", "required_role")
    search_fields = ("session_id", "location_name", "user_query")


@admin.register(IdempotencyRequest)
class IdempotencyRequestAdmin(admin.ModelAdmin):
    list_display = (
        "action", "key", "actor", "status", "response_status_code",
        "created_at", "updated_at", "expires_at",
    )
    list_filter = ("action", "status")
    search_fields = ("key", "actor", "action")
    readonly_fields = ("request_fingerprint", "response_payload", "error_message")


@admin.register(WorkflowGraphConfig)
class WorkflowGraphConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "version", "is_active", "created_at", "activated_at")
    list_filter = ("is_active", "name")
    search_fields = ("name", "version")


@admin.register(OfflineEvaluationRun)
class OfflineEvaluationRunAdmin(admin.ModelAdmin):
    list_display = ("scenario_pack", "status", "started_at", "completed_at", "triggered_by")
    list_filter = ("status", "scenario_pack")
    search_fields = ("scenario_pack", "notes")
