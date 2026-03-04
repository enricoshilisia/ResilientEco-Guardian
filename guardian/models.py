"""
ResilientEco Guardian - Models
All models in one place inside the guardian app.
"""

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.db.models.signals import post_save
from django.dispatch import receiver
import uuid


# ─────────────────────────────────────────────
# ORGANIZATION
# ─────────────────────────────────────────────

class Organization(models.Model):

    ORG_TYPES = [
        ('government', 'Government'),
        ('ngo', 'NGO'),
        ('institution', 'Institution'),
        ('enterprise', 'Enterprise'),
        ('community', 'Community Group'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    org_type = models.CharField(max_length=20, choices=ORG_TYPES)
    org_subtype = models.CharField(
        max_length=30,
        blank=True,
        null=True,
        help_text="Original wizard choice: agriculture, aviation, developer, disaster_relief, etc."
    )
    country = models.CharField(max_length=100, default="Kenya")
    region = models.CharField(max_length=100, blank=True, null=True)

    logo = models.ImageField(upload_to='org_logos/', blank=True, null=True)
    website = models.URLField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)

    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    azure_subscription_id = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.get_org_type_display()})"

    def get_admin_members(self):
        return self.members.filter(role='admin', is_active=True)

    def get_active_members(self):
        return self.members.filter(is_active=True)


# ─────────────────────────────────────────────
# ORGANIZATION MEMBERSHIP (RBAC)
# ─────────────────────────────────────────────

class OrganizationMembership(models.Model):

    ROLE_CHOICES = [
        ('admin', 'Organization Admin'),
        ('operator', 'Operations Officer'),
        ('analyst', 'Risk Analyst'),
        ('viewer', 'Read Only Viewer'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='memberships')
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='members')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='viewer')

    joined_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    invited_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='sent_invitations'
    )

    class Meta:
        unique_together = ['user', 'organization']

    def __str__(self):
        return f"{self.user.username} → {self.organization.name} [{self.role}]"

    def can_manage_alerts(self):
        return self.role in ('admin', 'operator')

    def can_view_analytics(self):
        return self.role in ('admin', 'operator', 'analyst')

    def can_manage_members(self):
        return self.role == 'admin'

    def can_approve_governance(self):
        return self.role in ('admin', 'operator')


# ─────────────────────────────────────────────
# USER PROFILE
# ─────────────────────────────────────────────

class UserProfile(models.Model):

    NOTIFICATION_CHANNELS = [
        ('email', 'Email'),
        ('sms', 'SMS'),
        ('push', 'Push Notification'),
        ('all', 'All Channels'),
        ('none', 'None'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    default_organization = models.ForeignKey(
        Organization, on_delete=models.SET_NULL, null=True, blank=True
    )

    phone = models.CharField(max_length=20, blank=True)
    bio = models.TextField(blank=True, null=True)
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    timezone = models.CharField(max_length=50, default='Africa/Nairobi')
    language = models.CharField(max_length=10, default='en')

    notifications_enabled = models.BooleanField(default=True)
    notification_channel = models.CharField(
        max_length=10, choices=NOTIFICATION_CHANNELS, default='email'
    )
    alert_threshold = models.IntegerField(
        default=50, help_text="Minimum risk level (0-100) to trigger notification"
    )

    is_public_user = models.BooleanField(default=False)
    is_verified_email = models.BooleanField(default=False)

    last_active = models.DateTimeField(null=True, blank=True)
    login_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} — Profile"

    def get_organizations(self):
        return Organization.objects.filter(members__user=self.user, members__is_active=True)

    def get_role_in(self, organization):
        try:
            return OrganizationMembership.objects.get(
                user=self.user, organization=organization, is_active=True
            ).role
        except OrganizationMembership.DoesNotExist:
            return None

    def update_last_active(self):
        self.last_active = timezone.now()
        self.login_count += 1
        self.save(update_fields=['last_active', 'login_count'])


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)

@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    if hasattr(instance, 'profile'):
        instance.profile.save()


# ─────────────────────────────────────────────
# ORGANIZATION INVITATION
# ─────────────────────────────────────────────

class OrganizationInvitation(models.Model):

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('declined', 'Declined'),
        ('expired', 'Expired'),
        ('revoked', 'Revoked'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='invitations')
    invited_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_invitations')

    email = models.EmailField()
    role = models.CharField(max_length=20, choices=OrganizationMembership.ROLE_CHOICES, default='viewer')
    token = models.UUIDField(default=uuid.uuid4, unique=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Invite → {self.email} to {self.organization.name} [{self.status}]"

    def is_expired(self):
        return timezone.now() > self.expires_at

    def is_valid(self):
        return self.status == 'pending' and not self.is_expired()


# ─────────────────────────────────────────────
# SAVED LOCATION
# ─────────────────────────────────────────────

class SavedLocation(models.Model):

    LOCATION_TYPES = [
        ('farm', 'Farm / Agricultural'),
        ('urban', 'Urban / City'),
        ('coastal', 'Coastal'),
        ('infrastructure', 'Infrastructure'),
        ('community', 'Community Area'),
        ('other', 'Other'),
    ]

    user = models.ForeignKey(
        User, on_delete=models.CASCADE,
        related_name='saved_locations', null=True, blank=True
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE,
        null=True, blank=True, related_name='locations'
    )

    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    location_type = models.CharField(max_length=20, choices=LOCATION_TYPES, default='other')

    latitude = models.FloatField()
    longitude = models.FloatField()
    altitude_m = models.FloatField(null=True, blank=True)
    radius_km = models.FloatField(default=5.0)

    is_primary = models.BooleanField(default=False)
    is_public = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    iot_device_id = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_primary', 'name']

    def __str__(self):
        owner = self.organization.name if self.organization else (
            self.user.username if self.user else "Public"
        )
        return f"{owner} — {self.name}"

    def get_owner_display(self):
        if self.organization:
            return self.organization.name
        if self.user:
            return self.user.get_full_name() or self.user.username
        return "Public"


# ─────────────────────────────────────────────
# ALERT LOG
# ─────────────────────────────────────────────

class AlertLog(models.Model):

    RISK_TYPES = [
        ('flood', 'Flood'),
        ('drought', 'Drought'),
        ('heatwave', 'Heatwave'),
    ]

    ALERT_STATUS = [
        ('pending', 'Pending Governance Review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('escalated', 'Escalated'),
        ('resolved', 'Resolved'),
    ]

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE,
        related_name='alerts', null=True, blank=True
    )
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    location = models.ForeignKey(SavedLocation, on_delete=models.SET_NULL, null=True, blank=True)

    risk_type = models.CharField(max_length=20, choices=RISK_TYPES)
    risk_level = models.IntegerField()          # 0-100
    confidence_score = models.IntegerField(default=0)

    message = models.TextField()
    weather_data = models.JSONField(null=True, blank=True)

    alert_status = models.CharField(max_length=20, choices=ALERT_STATUS, default='pending')
    governance_notes = models.TextField(blank=True, null=True)
    is_system_generated = models.BooleanField(default=True)

    # kept from your old model
    is_read = models.BooleanField(default=False)

    timestamp = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        org = self.organization.name if self.organization else "Public"
        return f"{org} — {self.risk_type} ({self.risk_level}%)"


# ─────────────────────────────────────────────
# AGENT EXECUTION LOG
# ─────────────────────────────────────────────

class AgentExecutionLog(models.Model):

    AGENT_TYPES = [
        ('monitor', 'Monitor Agent'),
        ('predict', 'Predict Agent'),
        ('decision', 'Decision Agent'),
        ('action', 'Action Agent'),
        ('governance', 'Governance Agent'),
    ]

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, null=True, blank=True
    )
    agent_type = models.CharField(max_length=20, choices=AGENT_TYPES)

    input_payload = models.JSONField()
    output_payload = models.JSONField()

    latency_ms = models.IntegerField(null=True, blank=True)
    token_usage = models.IntegerField(null=True, blank=True)

    executed_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.agent_type} — {self.executed_at}"


# ─────────────────────────────────────────────
# ACCOUNT ACTIVITY LOG
# ─────────────────────────────────────────────

class RiskPolicyVersion(models.Model):
    name = models.CharField(max_length=100, default='global_default')
    version = models.CharField(max_length=30)
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=False)
    rules = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ['name', 'version']
        ordering = ['-created_at']

    def __str__(self):
        active = "active" if self.is_active else "inactive"
        return f"{self.name}@{self.version} ({active})"


class WorkflowCheckpoint(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('resumed', 'Resumed'),
        ('expired', 'Expired'),
        ('rejected', 'Rejected'),
    ]

    session_id = models.CharField(max_length=64, unique=True)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, null=True, blank=True, related_name='workflow_checkpoints'
    )
    execution_log = models.ForeignKey(
        AgentExecutionLog, on_delete=models.SET_NULL, null=True, blank=True, related_name='workflow_checkpoints'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_workflow_checkpoints'
    )
    approved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_workflow_checkpoints'
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    required_role = models.CharField(max_length=20, default='admin')
    paused_at_step = models.CharField(max_length=40, blank=True, null=True)
    resume_from_step = models.CharField(max_length=40, blank=True, null=True)
    pending_action = models.CharField(max_length=120, blank=True, null=True)

    user_query = models.TextField(blank=True, default='')
    location_name = models.CharField(max_length=255, blank=True, default='Location')
    lat = models.FloatField(default=0.0)
    lon = models.FloatField(default=0.0)
    selected_graph = models.CharField(max_length=80, default='standard_forecast_graph')

    pipeline = models.JSONField(default=list, blank=True)
    task_ledger = models.JSONField(default=list, blank=True)
    partial_results = models.JSONField(default=dict, blank=True)
    message_state = models.JSONField(default=dict, blank=True)
    checkpoint_payload = models.JSONField(default=dict, blank=True)

    expires_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    resumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.session_id} [{self.status}]"


class AccountActivityLog(models.Model):

    ACTION_TYPES = [
        ('login', 'Login'),
        ('logout', 'Logout'),
        ('register', 'Registration'),
        ('password_change', 'Password Changed'),
        ('profile_update', 'Profile Updated'),
        ('org_join', 'Joined Organization'),
        ('org_leave', 'Left Organization'),
        ('org_created', 'Organization Created'),
        ('location_added', 'Location Added'),
        ('location_removed', 'Location Removed'),
        ('invitation_sent', 'Invitation Sent'),
        ('invitation_accepted', 'Invitation Accepted'),
        ('role_changed', 'Role Changed'),
        ('account_deactivated', 'Account Deactivated'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='activity_logs')
    action = models.CharField(max_length=30, choices=ACTION_TYPES)
    description = models.TextField(blank=True)

    organization = models.ForeignKey(
        Organization, on_delete=models.SET_NULL, null=True, blank=True
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, null=True)

    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.user.username} — {self.action} at {self.timestamp:%Y-%m-%d %H:%M}"
