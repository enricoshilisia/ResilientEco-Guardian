"""
ResilientEco Guardian - Views
All imports come from .models — no separate accounts app needed.
"""

import os
import re
import time
import logging

from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone
from datetime import timedelta

from rest_framework import status, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from dotenv import load_dotenv
from openai import OpenAI

from .services.weather_service import assess_flood_risk
from .agents.core_agents import run_all_agents

# ── All models from THIS app ──
from .models import (
    Organization, OrganizationMembership, UserProfile,
    OrganizationInvitation, SavedLocation,
    AlertLog, AgentExecutionLog, AccountActivityLog
)

load_dotenv()
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _get_ai_client():
    return OpenAI(
        base_url=os.getenv('AZURE_OPENAI_ENDPOINT'),
        api_key=os.getenv('AZURE_OPENAI_KEY')
    )


def _get_client_ip(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _log_activity(user, action, description='', request=None, organization=None):
    AccountActivityLog.objects.create(
        user=user,
        action=action,
        description=description,
        organization=organization,
        ip_address=_get_client_ip(request) if request else None,
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:500] if request else None,
    )


def _extract_risk_level(results):
    if isinstance(results, dict):
        if 'risk_level' in results:
            try:
                return int(results['risk_level'])
            except (ValueError, TypeError):
                pass
        action = results.get('action', '')
        if isinstance(action, str):
            numbers = re.findall(r'\b([6-9][0-9]|100)\b', action)
            if numbers:
                return int(numbers[0])
    return None


def _auto_create_alert(results, risk_level, location_obj, organization, user):
    risk_type = 'flood'
    action_text = ''
    if isinstance(results, dict):
        action_text = results.get('action', '') or results.get('decision', '') or ''
        lower = action_text.lower()
        if 'drought' in lower:
            risk_type = 'drought'
        elif 'heat' in lower:
            risk_type = 'heatwave'
    try:
        AlertLog.objects.create(
            user=user,
            organization=organization,
            location=location_obj,
            risk_type=risk_type,
            risk_level=risk_level,
            message=action_text[:2000] if action_text else "Auto-generated alert from agent pipeline.",
            weather_data=results.get('weather') if isinstance(results, dict) else None,
            is_system_generated=True,
            alert_status='pending',
        )
    except Exception:
        logger.warning("Could not auto-create alert", exc_info=True)


# ─────────────────────────────────────────────
# CLIMATE AI
# ─────────────────────────────────────────────

def run_climate_agent(query, weather_data=None):
    client = _get_ai_client()

    if not weather_data:
        prompt = query
    elif isinstance(weather_data, str):
        prompt = f"{query}\n\n{weather_data}"
    else:
        current = weather_data.get('current', {})
        hourly  = weather_data.get('hourly', {})
        daily   = weather_data.get('daily', {})
        precip_history = hourly.get('precipitation', [])[-24:] if hourly.get('precipitation') else []
        total_last_24h = sum(p for p in precip_history if p) if precip_history else 0

        prompt = f"""{query}

REAL-TIME WEATHER DATA:

CURRENT (NOW - {current.get('time', 'unknown')}):
- Precipitation: {current.get('precipitation', 0)} mm
- Rain: {current.get('rain', 0)} mm
- Temperature: {current.get('temperature_2m', 'N/A')}°C
- Humidity: {current.get('relative_humidity_2m', 'N/A')}%
- Weather code: {current.get('weather_code', 0)}

LAST 24 HOURS:
- Total precipitation: {total_last_24h} mm
- Hourly breakdown: {precip_history}

DAILY SUMMARY:
- Yesterday's rain: {daily.get('rain_sum', [0])[0] if daily.get('rain_sum') else 0} mm

INSTRUCTIONS:
- If current precipitation > 0: It IS raining now
- If total_last_24h > 0: It DID rain in the last 24 hours
- Use hourly data to determine when it last rained
- Answer the user's specific question about timing"""

    response = client.chat.completions.create(
        model=os.getenv('FOUNDRY_DEPLOYMENT'),
        messages=[
            {"role": "system", "content": (
                "You are ResilientEco Guardian. Analyze the full weather data provided. "
                "Answer questions about current AND past weather accurately. "
                "Look at hourly precipitation to determine when it last rained."
            )},
            {"role": "user", "content": prompt}
        ]
    )
    return response.choices[0].message.content


# ─────────────────────────────────────────────
# PAGE
# ─────────────────────────────────────────────

def dashboard(request):
    context = {}
    if request.user.is_authenticated:
        profile = getattr(request.user, 'profile', None)
        if profile and profile.default_organization:
            context['default_org'] = profile.default_organization
        context['memberships'] = OrganizationMembership.objects.filter(
            user=request.user, is_active=True
        ).select_related('organization')
    return render(request, 'guardian/dashboard.html', context)


# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────

class RegisterView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username   = request.data.get('username', '').strip()
        email      = request.data.get('email', '').strip()
        password   = request.data.get('password', '')
        password2  = request.data.get('password2', '')
        first_name = request.data.get('first_name', '').strip()
        last_name  = request.data.get('last_name', '').strip()
        is_public  = request.data.get('is_public_user', False)

        if not username or not email or not password:
            return Response({"error": "username, email, and password are required."}, status=status.HTTP_400_BAD_REQUEST)
        if password != password2:
            return Response({"error": "Passwords do not match."}, status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(username=username).exists():
            return Response({"error": "Username already taken."}, status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(email=email).exists():
            return Response({"error": "Email already registered."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_password(password)
        except ValidationError as e:
            return Response({"error": list(e.messages)}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.create_user(
            username=username, email=email, password=password,
            first_name=first_name, last_name=last_name
        )
        user.profile.is_public_user = is_public
        user.profile.save()

        refresh = RefreshToken.for_user(user)
        _log_activity(user, 'register', 'New account created', request)

        return Response({
            'user_id': user.id,
            'username': user.username,
            'email': user.email,
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'message': 'Registration successful. Welcome to ResilientEco Guardian!'
        }, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = request.data.get('username', '').strip()
        password = request.data.get('password', '')
        if not username or not password:
            return Response({"error": "Username and password required."}, status=status.HTTP_400_BAD_REQUEST)
        user = authenticate(username=username, password=password)
        if not user:
            return Response({"error": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)
        if not user.is_active:
            return Response({"error": "Account deactivated."}, status=status.HTTP_403_FORBIDDEN)

        user.profile.update_last_active()
        refresh = RefreshToken.for_user(user)
        _log_activity(user, 'login', 'User logged in', request)

        return Response({
            'user_id': user.id,
            'username': user.username,
            'email': user.email,
            'full_name': user.get_full_name(),
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'is_public_user': user.profile.is_public_user,
            'default_org': str(user.profile.default_organization.id) if user.profile.default_organization else None,
        })


class LogoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            token = RefreshToken(request.data.get('refresh'))
            token.blacklist()
            _log_activity(request.user, 'logout', 'User logged out', request)
            return Response({"detail": "Logged out."})
        except Exception:
            return Response({"error": "Invalid token."}, status=status.HTTP_400_BAD_REQUEST)


# ─────────────────────────────────────────────
# PROFILE
# ─────────────────────────────────────────────

class ProfileView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        u = request.user
        p = u.profile
        memberships = OrganizationMembership.objects.filter(user=u, is_active=True).select_related('organization')
        return Response({
            'id': u.id,
            'username': u.username,
            'email': u.email,
            'full_name': u.get_full_name(),
            'phone': p.phone,
            'bio': p.bio,
            'timezone': p.timezone,
            'language': p.language,
            'notifications_enabled': p.notifications_enabled,
            'notification_channel': p.notification_channel,
            'alert_threshold': p.alert_threshold,
            'is_public_user': p.is_public_user,
            'is_verified_email': p.is_verified_email,
            'last_active': p.last_active,
            'organizations': [
                {
                    'id': str(m.organization.id),
                    'name': m.organization.name,
                    'org_type': m.organization.org_type,
                    'role': m.role,
                    'is_default': p.default_organization_id == m.organization.id,
                }
                for m in memberships
            ]
        })

    def patch(self, request):
        u = request.user
        p = u.profile
        for field in ('first_name', 'last_name'):
            if field in request.data:
                setattr(u, field, request.data[field])
        if 'email' in request.data:
            new_email = request.data['email']
            if User.objects.exclude(pk=u.pk).filter(email=new_email).exists():
                return Response({"error": "Email already in use."}, status=status.HTTP_400_BAD_REQUEST)
            u.email = new_email
        u.save()
        for field in ('phone', 'bio', 'timezone', 'language',
                      'notifications_enabled', 'notification_channel', 'alert_threshold'):
            if field in request.data:
                setattr(p, field, request.data[field])
        if 'default_organization' in request.data:
            org_id = request.data['default_organization']
            if OrganizationMembership.objects.filter(user=u, organization_id=org_id, is_active=True).exists():
                p.default_organization_id = org_id
        p.save()
        _log_activity(u, 'profile_update', 'Profile updated', request)
        return Response({"detail": "Profile updated."})

    def delete(self, request):
        request.user.is_active = False
        request.user.save()
        _log_activity(request.user, 'account_deactivated', 'Account deactivated', request)
        return Response({"detail": "Account deactivated."})


class ChangePasswordView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        old_pw  = request.data.get('old_password', '')
        new_pw  = request.data.get('new_password', '')
        new_pw2 = request.data.get('new_password2', '')
        if not request.user.check_password(old_pw):
            return Response({"error": "Old password is incorrect."}, status=status.HTTP_400_BAD_REQUEST)
        if new_pw != new_pw2:
            return Response({"error": "New passwords do not match."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_password(new_pw)
        except ValidationError as e:
            return Response({"error": list(e.messages)}, status=status.HTTP_400_BAD_REQUEST)
        request.user.set_password(new_pw)
        request.user.save()
        _log_activity(request.user, 'password_change', 'Password changed', request)
        return Response({"detail": "Password changed successfully."})


class SetDefaultOrgView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        org_id = request.data.get('organization_id')
        if not org_id:
            return Response({"error": "organization_id required."}, status=status.HTTP_400_BAD_REQUEST)
        if not OrganizationMembership.objects.filter(user=request.user, organization_id=org_id, is_active=True).exists():
            return Response({"error": "Not a member of that organization."}, status=status.HTTP_403_FORBIDDEN)
        request.user.profile.default_organization_id = org_id
        request.user.profile.save()
        return Response({"detail": "Default organization updated."})


class MyActivityLogView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        logs = AccountActivityLog.objects.filter(user=request.user).order_by('-timestamp')[:50]
        return Response({'activity': list(logs.values('action', 'description', 'ip_address', 'timestamp'))})


class MyInvitationsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        invitations = OrganizationInvitation.objects.filter(
            email=request.user.email
        ).select_related('organization', 'invited_by')
        return Response({'invitations': [
            {
                'id': str(i.id),
                'organization': i.organization.name,
                'org_type': i.organization.org_type,
                'role': i.role,
                'invited_by': i.invited_by.get_full_name() or i.invited_by.username,
                'status': i.status,
                'expires_at': i.expires_at,
                'is_valid': i.is_valid(),
            }
            for i in invitations
        ]})


class DashboardSummaryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        u = request.user
        p = u.profile
        memberships = OrganizationMembership.objects.filter(user=u, is_active=True).select_related('organization')
        return Response({
            "user": {
                "id": u.id, "username": u.username,
                "full_name": u.get_full_name(), "email": u.email,
                "is_public_user": p.is_public_user, "last_active": p.last_active,
            },
            "stats": {
                "organizations": memberships.count(),
                "saved_locations": SavedLocation.objects.filter(user=u, is_active=True).count(),
                "pending_invitations": OrganizationInvitation.objects.filter(email=u.email, status='pending').count(),
            },
            "organizations": [
                {
                    "id": str(m.organization.id), "name": m.organization.name,
                    "org_type": m.organization.org_type, "role": m.role,
                    "is_verified": m.organization.is_verified,
                }
                for m in memberships
            ]
        })


# ─────────────────────────────────────────────
# ORGANIZATIONS
# ─────────────────────────────────────────────

class MyOrganizationsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        memberships = OrganizationMembership.objects.filter(
            user=request.user, is_active=True
        ).select_related('organization')
        return Response({'organizations': [
            {
                'id': str(m.organization.id), 'name': m.organization.name,
                'slug': m.organization.slug, 'org_type': m.organization.org_type,
                'country': m.organization.country, 'is_verified': m.organization.is_verified,
                'my_role': m.role, 'joined_at': m.joined_at,
            }
            for m in memberships
        ]})


class CreateOrganizationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from django.utils.text import slugify
        name     = request.data.get('name', '').strip()
        org_type = request.data.get('org_type', 'community')
        country  = request.data.get('country', 'Kenya')
        region   = request.data.get('region', '')
        if not name:
            return Response({"error": "Organization name required."}, status=status.HTTP_400_BAD_REQUEST)
        slug = slugify(name)
        base, n = slug, 1
        while Organization.objects.filter(slug=slug).exists():
            slug = f"{base}-{n}"; n += 1
        org = Organization.objects.create(name=name, slug=slug, org_type=org_type, country=country, region=region)
        OrganizationMembership.objects.create(user=request.user, organization=org, role='admin', invited_by=request.user)
        if not request.user.profile.default_organization:
            request.user.profile.default_organization = org
            request.user.profile.save()
        _log_activity(request.user, 'org_created', f'Created: {org.name}', request, org)
        return Response({'id': str(org.id), 'name': org.name, 'slug': org.slug,
                         'org_type': org.org_type, 'my_role': 'admin'}, status=status.HTTP_201_CREATED)


class OrganizationDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get(self, request, org_id):
        org = get_object_or_404(Organization, id=org_id, is_active=True)
        membership = get_object_or_404(OrganizationMembership, user=request.user, organization=org, is_active=True)
        return org, membership

    def get(self, request, org_id):
        org, m = self._get(request, org_id)
        return Response({
            'id': str(org.id), 'name': org.name, 'slug': org.slug,
            'org_type': org.org_type, 'country': org.country, 'region': org.region,
            'description': org.description, 'website': org.website,
            'is_verified': org.is_verified, 'my_role': m.role,
            'member_count': org.members.filter(is_active=True).count(),
        })

    def patch(self, request, org_id):
        org, m = self._get(request, org_id)
        if m.role != 'admin':
            return Response({"error": "Admin only."}, status=status.HTTP_403_FORBIDDEN)
        for field in ('name', 'country', 'region', 'description', 'website'):
            if field in request.data:
                setattr(org, field, request.data[field])
        org.save()
        return Response({"detail": "Organization updated."})


class LeaveOrganizationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, org_id):
        org = get_object_or_404(Organization, id=org_id, is_active=True)
        membership = get_object_or_404(OrganizationMembership, user=request.user, organization=org, is_active=True)
        if membership.role == 'admin':
            if OrganizationMembership.objects.filter(organization=org, role='admin', is_active=True).count() <= 1:
                return Response({"error": "Assign another admin before leaving."}, status=status.HTTP_400_BAD_REQUEST)
        membership.is_active = False
        membership.save()
        if request.user.profile.default_organization == org:
            request.user.profile.default_organization = None
            request.user.profile.save()
        _log_activity(request.user, 'org_leave', f'Left: {org.name}', request, org)
        return Response({"detail": f"Left {org.name}."})


# ─────────────────────────────────────────────
# MEMBERS
# ─────────────────────────────────────────────

class OrgMembersView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, org_id):
        org = get_object_or_404(Organization, id=org_id, is_active=True)
        get_object_or_404(OrganizationMembership, user=request.user, organization=org, is_active=True)
        members = OrganizationMembership.objects.filter(organization=org, is_active=True).select_related('user')
        return Response({'members': [
            {
                'user_id': m.user.id, 'username': m.user.username,
                'full_name': m.user.get_full_name(), 'email': m.user.email,
                'role': m.role, 'joined_at': m.joined_at,
            }
            for m in members
        ]})


class UpdateMemberRoleView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, org_id, user_id):
        org = get_object_or_404(Organization, id=org_id, is_active=True)
        requester = get_object_or_404(OrganizationMembership, user=request.user, organization=org, is_active=True)
        if requester.role != 'admin':
            return Response({"error": "Admin only."}, status=status.HTTP_403_FORBIDDEN)
        if str(request.user.id) == str(user_id):
            return Response({"error": "Cannot change your own role."}, status=status.HTTP_400_BAD_REQUEST)
        new_role = request.data.get('role')
        if new_role not in dict(OrganizationMembership.ROLE_CHOICES):
            return Response({"error": "Invalid role."}, status=status.HTTP_400_BAD_REQUEST)
        target = get_object_or_404(OrganizationMembership, user_id=user_id, organization=org, is_active=True)
        target.role = new_role
        target.save()
        _log_activity(request.user, 'role_changed', f'Changed {target.user.username} to {new_role}', request, org)
        return Response({"detail": "Role updated."})


class RemoveMemberView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, org_id, user_id):
        org = get_object_or_404(Organization, id=org_id, is_active=True)
        requester = get_object_or_404(OrganizationMembership, user=request.user, organization=org, is_active=True)
        is_self = str(request.user.id) == str(user_id)
        if not is_self and requester.role != 'admin':
            return Response({"error": "Admin only."}, status=status.HTTP_403_FORBIDDEN)
        target = get_object_or_404(OrganizationMembership, user_id=user_id, organization=org, is_active=True)
        target.is_active = False
        target.save()
        return Response({"detail": "Member removed."})


# ─────────────────────────────────────────────
# INVITATIONS
# ─────────────────────────────────────────────

class SendInvitationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, org_id):
        org = get_object_or_404(Organization, id=org_id, is_active=True)
        membership = get_object_or_404(OrganizationMembership, user=request.user, organization=org, is_active=True)
        if not membership.can_manage_members():
            return Response({"error": "Admin only."}, status=status.HTTP_403_FORBIDDEN)
        email = request.data.get('email', '').strip()
        role  = request.data.get('role', 'viewer')
        if not email:
            return Response({"error": "Email required."}, status=status.HTTP_400_BAD_REQUEST)
        if OrganizationMembership.objects.filter(user__email=email, organization=org, is_active=True).exists():
            return Response({"error": "Already a member."}, status=status.HTTP_400_BAD_REQUEST)
        if OrganizationInvitation.objects.filter(email=email, organization=org, status='pending').exists():
            return Response({"error": "Pending invitation already exists."}, status=status.HTTP_400_BAD_REQUEST)
        invitation = OrganizationInvitation.objects.create(
            organization=org, invited_by=request.user, email=email,
            role=role, expires_at=timezone.now() + timedelta(days=7)
        )
        _log_activity(request.user, 'invitation_sent', f'Invited {email}', request, org)
        return Response({
            'id': str(invitation.id), 'email': invitation.email,
            'role': invitation.role, 'token': str(invitation.token), 'expires_at': invitation.expires_at,
        }, status=status.HTTP_201_CREATED)


class AcceptInvitationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, token):
        invitation = get_object_or_404(OrganizationInvitation, token=token)
        if not invitation.is_valid():
            return Response({"error": "Invitation expired or invalid."}, status=status.HTTP_400_BAD_REQUEST)
        if invitation.email.lower() != request.user.email.lower():
            return Response({"error": "This invitation belongs to a different email."}, status=status.HTTP_403_FORBIDDEN)
        OrganizationMembership.objects.get_or_create(
            user=request.user, organization=invitation.organization,
            defaults={'role': invitation.role, 'invited_by': invitation.invited_by}
        )
        invitation.status = 'accepted'
        invitation.responded_at = timezone.now()
        invitation.save()
        _log_activity(request.user, 'invitation_accepted', f'Joined {invitation.organization.name}', request, invitation.organization)
        return Response({"detail": f"Welcome to {invitation.organization.name}!"})


class DeclineInvitationView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request, token):
        invitation = get_object_or_404(OrganizationInvitation, token=token)
        if invitation.status != 'pending':
            return Response({"error": "Already responded."}, status=status.HTTP_400_BAD_REQUEST)
        invitation.status = 'declined'
        invitation.responded_at = timezone.now()
        invitation.save()
        return Response({"detail": "Invitation declined."})


# ─────────────────────────────────────────────
# AGENT RUNNER
# ─────────────────────────────────────────────

class RunAgentView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        query         = request.data.get('query', 'Check climate risk')
        location_name = request.data.get('location_name', 'Nairobi')
        org_id        = request.data.get('org_id')
        lat           = request.data.get('lat')
        lon           = request.data.get('lon')
        location_obj  = None

        if not lat or not lon:
            location_id = request.data.get('location_id')
            if location_id:
                location_obj = SavedLocation.objects.filter(id=location_id).first()
                if location_obj:
                    lat = location_obj.latitude
                    lon = location_obj.longitude
                    location_name = location_obj.name

        lat = float(lat or -1.2921)
        lon = float(lon or 36.8219)

        organization = None
        if request.user.is_authenticated:
            if org_id:
                m = OrganizationMembership.objects.filter(
                    user=request.user, organization_id=org_id, is_active=True
                ).first()
                if m:
                    organization = m.organization
            else:
                profile = getattr(request.user, 'profile', None)
                if profile:
                    organization = profile.default_organization

        start = time.time()
        try:
            results = run_all_agents(query, lat, lon, location_name)
        except Exception as e:
            logger.exception("Agent pipeline failed")
            return Response({"error": "Agent pipeline failed.", "detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        latency_ms = int((time.time() - start) * 1000)

        try:
            AgentExecutionLog.objects.create(
                organization=organization,
                agent_type='decision',
                input_payload={"query": query, "lat": lat, "lon": lon, "location": location_name},
                output_payload=results,
                latency_ms=latency_ms,
            )
        except Exception:
            logger.warning("Could not write AgentExecutionLog", exc_info=True)

        risk_level = _extract_risk_level(results)
        if risk_level and risk_level >= 50:
            _auto_create_alert(
                results=results, risk_level=risk_level, location_obj=location_obj,
                organization=organization,
                user=request.user if request.user.is_authenticated else None,
            )

        return Response({'result': results, 'status': 'success', 'latency_ms': latency_ms})


# ─────────────────────────────────────────────
# LOCATIONS
# ─────────────────────────────────────────────

class LocationListView(APIView):

    def get_permissions(self):
        if self.request.method == 'GET':
            return [permissions.IsAuthenticatedOrReadOnly()]
        return [permissions.IsAuthenticated()]

    def get(self, request):
        if request.user.is_authenticated:
            personal = SavedLocation.objects.filter(user=request.user, is_active=True)
            org_ids  = OrganizationMembership.objects.filter(user=request.user, is_active=True).values_list('organization_id', flat=True)
            org_locs = SavedLocation.objects.filter(organization_id__in=org_ids, is_active=True)
            all_locs = (personal | org_locs).distinct()
            return Response({'locations': list(all_locs.values(
                'id', 'name', 'latitude', 'longitude', 'location_type', 'is_primary', 'radius_km'
            ))})
        return Response({'locations': list(
            SavedLocation.objects.filter(is_public=True, is_active=True).values('id', 'name', 'latitude', 'longitude')
        )})

    def post(self, request):
        name     = request.data.get('name')
        lat      = request.data.get('lat')
        lon      = request.data.get('lon')
        org_id   = request.data.get('org_id')
        loc_type = request.data.get('location_type', 'other')
        radius   = request.data.get('radius_km', 5.0)
        if not name or lat is None or lon is None:
            return Response({"error": "name, lat, and lon are required."}, status=status.HTTP_400_BAD_REQUEST)
        kwargs = {"name": name, "latitude": float(lat), "longitude": float(lon),
                  "location_type": loc_type, "radius_km": float(radius)}
        if org_id:
            if not OrganizationMembership.objects.filter(user=request.user, organization_id=org_id, is_active=True).exists():
                return Response({"error": "Not a member of that organization."}, status=status.HTTP_403_FORBIDDEN)
            kwargs['organization_id'] = org_id
        else:
            kwargs['user'] = request.user
        location = SavedLocation.objects.create(**kwargs)
        _log_activity(request.user, 'location_added', f'Added: {location.name}', request)
        return Response({
            'status': 'success',
            'location': {'id': location.id, 'name': location.name,
                         'latitude': location.latitude, 'longitude': location.longitude}
        }, status=status.HTTP_201_CREATED)


class LocationDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get_location(self, request, location_id):
        location = get_object_or_404(SavedLocation, id=location_id, is_active=True)
        is_owner = location.user == request.user
        is_member = location.organization and OrganizationMembership.objects.filter(
            user=request.user, organization=location.organization, is_active=True
        ).exists()
        if not (is_owner or is_member):
            return None, Response({"error": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        return location, None

    def get(self, request, location_id):
        loc, err = self._get_location(request, location_id)
        if err: return err
        return Response({'id': loc.id, 'name': loc.name, 'latitude': loc.latitude,
                         'longitude': loc.longitude, 'location_type': loc.location_type,
                         'radius_km': loc.radius_km, 'is_primary': loc.is_primary})

    def patch(self, request, location_id):
        loc, err = self._get_location(request, location_id)
        if err: return err
        for field in ('name', 'location_type', 'radius_km', 'is_primary', 'is_public'):
            if field in request.data:
                setattr(loc, field, request.data[field])
        loc.save()
        return Response({"status": "updated"})

    def delete(self, request, location_id):
        loc, err = self._get_location(request, location_id)
        if err: return err
        loc.is_active = False
        loc.save()
        _log_activity(request.user, 'location_removed', f'Removed: {loc.name}', request)
        return Response({"status": "removed"}, status=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────────────────────
# ALERTS
# ─────────────────────────────────────────────

class AlertListView(APIView):

    def get_permissions(self):
        return [permissions.IsAuthenticatedOrReadOnly()]

    def get(self, request):
        if request.user.is_authenticated:
            org_ids = OrganizationMembership.objects.filter(
                user=request.user, is_active=True
            ).values_list('organization_id', flat=True)
            alerts = AlertLog.objects.filter(organization_id__in=org_ids).order_by('-timestamp')[:20]
        else:
            alerts = AlertLog.objects.filter(alert_status='approved').order_by('-timestamp')[:10]
        return Response({'alerts': list(alerts.values(
            'id', 'risk_type', 'risk_level', 'message',
            'alert_status', 'confidence_score', 'timestamp', 'organization__name'
        ))})

    def post(self, request):
        if not request.user.is_authenticated:
            return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
        org_id      = request.data.get('org_id')
        location_id = request.data.get('location_id')
        risk_type   = request.data.get('risk_type', 'flood')
        risk_level  = int(request.data.get('risk_level', 50))
        message     = request.data.get('message', '')
        organization = None
        if org_id:
            membership = OrganizationMembership.objects.filter(
                user=request.user, organization_id=org_id, is_active=True
            ).first()
            if not membership:
                return Response({"error": "Not a member of that organization."}, status=status.HTTP_403_FORBIDDEN)
            if not membership.can_manage_alerts():
                return Response({"error": "Operator or admin role required."}, status=status.HTTP_403_FORBIDDEN)
            organization = membership.organization
        location = SavedLocation.objects.filter(id=location_id).first() if location_id else None
        alert = AlertLog.objects.create(
            user=request.user, organization=organization, location=location,
            risk_type=risk_type, risk_level=risk_level, message=message, is_system_generated=False,
        )
        return Response({'status': 'success', 'alert_id': alert.id, 'alert_status': alert.alert_status},
                        status=status.HTTP_201_CREATED)


class AlertDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get_alert(self, request, alert_id, require_manage=False):
        alert = get_object_or_404(AlertLog, id=alert_id)
        if alert.organization:
            m = OrganizationMembership.objects.filter(
                user=request.user, organization=alert.organization, is_active=True
            ).first()
            if not m:
                return None, Response({"error": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
            if require_manage and not m.can_manage_alerts():
                return None, Response({"error": "Operator or admin role required."}, status=status.HTTP_403_FORBIDDEN)
        return alert, None

    def get(self, request, alert_id):
        alert, err = self._get_alert(request, alert_id)
        if err: return err
        return Response({
            'id': alert.id, 'risk_type': alert.risk_type, 'risk_level': alert.risk_level,
            'confidence_score': alert.confidence_score, 'message': alert.message,
            'alert_status': alert.alert_status, 'governance_notes': alert.governance_notes,
            'is_system_generated': alert.is_system_generated,
            'timestamp': alert.timestamp, 'updated_at': alert.updated_at,
        })

    def patch(self, request, alert_id):
        alert, err = self._get_alert(request, alert_id, require_manage=True)
        if err: return err
        new_status = request.data.get('alert_status')
        valid = [s[0] for s in AlertLog.ALERT_STATUS]
        if new_status and new_status not in valid:
            return Response({"error": f"Valid statuses: {valid}"}, status=status.HTTP_400_BAD_REQUEST)
        if new_status:
            alert.alert_status = new_status
        if 'governance_notes' in request.data:
            alert.governance_notes = request.data['governance_notes']
        alert.save()
        return Response({"status": "updated", "alert_id": alert.id, "alert_status": alert.alert_status})
    
class WeatherView(APIView):
    """
    Public endpoint to get current weather for any location.
    Uses Visual Crossing (or Open-Meteo fallback).
    """
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        lat = request.query_params.get('lat')
        lon = request.query_params.get('lon')
        location_name = request.query_params.get('name', 'Location')

        if not lat or not lon:
            return Response(
                {"error": "lat and lon query parameters required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            from .services.weather_service import get_weather_summary
            summary = get_weather_summary(float(lat), float(lon), location_name)
            
            return Response({
                'location': summary['location'],
                'source': summary['data_source'],
                'temperature': summary['temperature'],
                'precipitation': summary['current_precipitation'],
                'rain': summary['current_rain'],
                'humidity': summary['humidity'],
                'is_raining': summary['is_raining_now'],
                'rain_24h': summary['total_rain_24h'],
                'observation_time': summary['observation_time'],
            })
        except Exception as e:
            logger.exception("Weather fetch failed")
            return Response(
                {"error": "Could not fetch weather data", "detail": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
