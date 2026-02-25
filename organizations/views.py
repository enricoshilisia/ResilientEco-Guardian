"""
organizations/views.py

Auth: JWT-aware — never redirects to allauth /accounts/login/.
"""

import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.db.models import Max, Subquery, OuterRef
from django.utils import timezone

from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status, permissions
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from guardian.models import Organization, OrganizationMembership, SavedLocation, AlertLog
from .routing import get_dashboard_url_name, get_org_config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# JWT-AWARE AUTH
# ─────────────────────────────────────────────

def _get_jwt_user(request):
    """Authenticate via JWT header, rg_access cookie, or existing session."""
    if request.user.is_authenticated:
        return request.user

    jwt_auth = JWTAuthentication()

    # 1. Try Authorization header (fetch() API calls)
    try:
        result = jwt_auth.authenticate(request)
        if result:
            user, _ = result
            return user
    except (InvalidToken, TokenError):
        pass

    # 2. Try rg_access cookie (plain page-load GET requests)
    token = request.COOKIES.get('rg_access')
    if token:
        from django.http import HttpRequest
        fake = HttpRequest()
        fake.META['HTTP_AUTHORIZATION'] = f'Bearer {token}'
        try:
            result = jwt_auth.authenticate(fake)
            if result:
                user, _ = result
                return user
        except (InvalidToken, TokenError):
            pass

    return None


def _jwt_login_required(view_func):
    """Redirect unauthenticated users to /login/, never to /accounts/login/."""
    def wrapper(request, *args, **kwargs):
        user = _get_jwt_user(request)
        if not user:
            return redirect(f'/login/?next={request.path}')
        request.user = user
        return view_func(request, *args, **kwargs)
    wrapper.__name__ = view_func.__name__
    return wrapper


# ─────────────────────────────────────────────
# SMART REDIRECT
# ─────────────────────────────────────────────

@_jwt_login_required
def smart_dashboard_redirect(request):
    profile = getattr(request.user, 'profile', None)
    org = profile.default_organization if profile else None

    if not org:
        membership = OrganizationMembership.objects.filter(
            user=request.user, is_active=True
        ).select_related('organization').first()
        org = membership.organization if membership else None

    if org:
        url_name = get_dashboard_url_name(org)
        try:
            return redirect(reverse(url_name, kwargs={'org_id': str(org.id)}))
        except Exception:
            pass

    return redirect('dashboard')


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _base_context(request, org):
    from guardian.models import OrganizationMembership, SavedLocation, AlertLog

    membership = get_object_or_404(
        OrganizationMembership, user=request.user, organization=org, is_active=True,
    )
    config    = get_org_config(org)
    locations = SavedLocation.objects.filter(organization=org, is_active=True).order_by('-is_primary', 'name')

    # ── Deduplicated recent alerts ────────────────────────────────
    # For each (location, risk_type) pair keep only the latest alert.
    # Then pick the top 8 most recent of those, ordered by severity then time.
    #
    # Step 1: find the latest timestamp per (location, risk_type) group.
    latest_per_group = (
        AlertLog.objects
        .filter(organization=org)
        .values('location_id', 'risk_type')          # group by zone + type
        .annotate(latest=Max('timestamp'))
        .order_by('-latest')[:20]                    # keep top-20 groups max
    )

    # Step 2: collect the PKs of the actual AlertLog rows that match those
    #         (location_id, risk_type, timestamp) triples.
    seen = set()
    group_keys = []
    for row in latest_per_group:
        key = (row['location_id'], row['risk_type'])
        if key not in seen:
            seen.add(key)
            group_keys.append(row)

    # Step 3: fetch the real AlertLog objects (one per group)
    from django.db.models import Q
    import functools, operator

    if group_keys:
        q = functools.reduce(
            operator.or_,
            [
                Q(location_id=g['location_id'], risk_type=g['risk_type'], timestamp=g['latest'])
                for g in group_keys
            ]
        )
        # Also handle alerts with no location (location_id=None)
        no_loc = AlertLog.objects.filter(
            organization=org, location__isnull=True
        ).order_by('-timestamp')[:3]

        with_loc = (
            AlertLog.objects
            .filter(organization=org)
            .filter(q)
            .select_related('location')
            .order_by('-risk_level', '-timestamp')
        )

        # Merge, deduplicate once more, cap at 8
        all_alerts = list(with_loc) + [a for a in no_loc if a not in list(with_loc)]
        # Final dedup by (location_id, risk_type) in Python in case DB returned duplicates
        final_seen = set()
        recent_alerts = []
        for alert in sorted(all_alerts, key=lambda a: (-a.risk_level, -a.timestamp.timestamp())):
            dedup_key = (alert.location_id, alert.risk_type)
            if dedup_key not in final_seen:
                final_seen.add(dedup_key)
                recent_alerts.append(alert)
            if len(recent_alerts) >= 8:
                break
    else:
        # No location-tagged alerts — fall back to most recent unique risk types
        seen_types = set()
        recent_alerts = []
        for alert in AlertLog.objects.filter(organization=org).order_by('-timestamp')[:30]:
            key = (alert.risk_type,)
            if key not in seen_types:
                seen_types.add(key)
                recent_alerts.append(alert)
            if len(recent_alerts) >= 5:
                break

    return {
        'org':               org,
        'membership':        membership,
        'org_config':        config,
        'locations':         locations,
        'recent_alerts':     recent_alerts,
        'user_role':         membership.role,
        'can_manage':        membership.can_manage_alerts(),
        'can_view_analytics':membership.can_view_analytics(),
    }


def _require_membership(request, org_id):
    org = get_object_or_404(Organization, id=org_id, is_active=True)
    get_object_or_404(OrganizationMembership, user=request.user, organization=org, is_active=True)
    return org


# ─────────────────────────────────────────────
# DASHBOARDS
# ─────────────────────────────────────────────

@_jwt_login_required
def dashboard_agriculture(request, org_id):
    org = _require_membership(request, org_id)
    ctx = _base_context(request, org)
    ctx.update({
        'page_title': 'Agricultural Intelligence Dashboard',
        'drought_alerts':  AlertLog.objects.filter(organization=org, risk_type='drought').order_by('-timestamp')[:5],
        'flood_alerts':    AlertLog.objects.filter(organization=org, risk_type='flood').order_by('-timestamp')[:5],
        'heatwave_alerts': AlertLog.objects.filter(organization=org, risk_type='heatwave').order_by('-timestamp')[:5],
    })
    return render(request, 'organizations/agricultural/dashboard.html', ctx)


@_jwt_login_required
def dashboard_ngo(request, org_id):
    org = _require_membership(request, org_id)
    ctx = _base_context(request, org)
    ctx['page_title'] = 'Disaster Relief Operations Dashboard'
    return render(request, 'organizations/ngo/dashboard.html', ctx)


@_jwt_login_required
def dashboard_meteorological(request, org_id):
    org = _require_membership(request, org_id)
    ctx = _base_context(request, org)
    ctx['page_title'] = 'Meteorological Operations Dashboard'
    return render(request, 'organizations/meteorological/dashboard.html', ctx)


@_jwt_login_required
def dashboard_enterprise(request, org_id):
    org = _require_membership(request, org_id)
    ctx = _base_context(request, org)
    ctx['page_title'] = 'Enterprise Operations Dashboard'
    return render(request, 'organizations/enterprise/dashboard.html', ctx)


@_jwt_login_required
def dashboard_government(request, org_id):
    org = _require_membership(request, org_id)
    ctx = _base_context(request, org)
    ctx['page_title'] = 'Government Climate Intelligence Dashboard'
    return render(request, 'organizations/government/dashboard.html', ctx)


@_jwt_login_required
def dashboard_community(request, org_id):
    org = _require_membership(request, org_id)
    ctx = _base_context(request, org)
    ctx['page_title'] = 'Community Climate Dashboard'
    return render(request, 'organizations/community/dashboard.html', ctx)




@_jwt_login_required
def org_members(request, org_id):
    """
    Members & Invitations management page.
    Accessible to all org members; invite/remove actions require can_manage.
    """
    org = _require_membership(request, org_id)
    ctx = _base_context(request, org)

    # All active members ordered by role weight then join date
    role_order = {'admin': 0, 'operator': 1, 'analyst': 2, 'viewer': 3}
    from guardian.models import OrganizationMembership, OrganizationInvitation

    members = list(
        OrganizationMembership.objects
        .filter(organization=org, is_active=True)
        .select_related('user')
        .order_by('joined_at')
    )
    members.sort(key=lambda m: (role_order.get(m.role, 99), m.joined_at))

    # All invitations for this org (not just pending)
    all_invitations = (
        OrganizationInvitation.objects
        .filter(organization=org)
        .select_related('invited_by')
        .order_by('-created_at')[:50]
    )
    pending_invitations = [i for i in all_invitations if i.status == 'pending']

    ctx.update({
        'page_title': 'Members & Invitations',
        'members': members,
        'all_invitations': all_invitations,
        'pending_invitations': pending_invitations,
    })
    return render(request, 'organizations/shared/org_members.html', ctx)


@_jwt_login_required
def org_profile(request, org_id):
    """
    User profile page rendered within the org context.
    Shows the logged-in user's profile settings.
    """
    org = _require_membership(request, org_id)
    ctx = _base_context(request, org)

    from guardian.models import OrganizationMembership

    # All memberships for the sidebar identity card
    all_memberships = (
        OrganizationMembership.objects
        .filter(user=request.user, is_active=True)
        .select_related('organization')
        .order_by('joined_at')
    )

    ctx.update({
        'page_title': 'My Profile',
        'user_profile': request.user.profile,
        'memberships': all_memberships,
    })
    return render(request, 'organizations/shared/org_profile.html', ctx)


@_jwt_login_required
def org_settings(request, org_id):
    """
    Organization settings page — admin/operator only.
    """
    org = _require_membership(request, org_id)
    ctx = _base_context(request, org)

    # Only admins and operators can access settings
    if ctx['user_role'] not in ('admin', 'operator'):
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden('You do not have permission to access organization settings.')

    from guardian.models import Organization, OrganizationMembership

    # Members list for the transfer ownership modal
    members = (
        OrganizationMembership.objects
        .filter(organization=org, is_active=True)
        .select_related('user')
        .order_by('joined_at')
    )

    ctx.update({
        'page_title': 'Organization Settings',
        'org_type_choices': Organization.ORG_TYPES,
        'members': members,
        # Pass API key if stored; otherwise None
        # Replace with your actual APIKey model lookup if you have one
        'org_api_key': None,
    })
    return render(request, 'organizations/shared/org_settings.html', ctx)
