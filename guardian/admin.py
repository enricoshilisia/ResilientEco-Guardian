from django.contrib import admin
from .models import RiskPolicyVersion, WorkflowCheckpoint


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
