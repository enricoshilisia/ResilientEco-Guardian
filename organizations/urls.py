"""
organizations/urls.py

All org-type dashboard routes live here.
Include this in your root urls.py:

    path('', include('organizations.urls')),
"""

from django.urls import path
from . import views

urlpatterns = [

    # ── Smart redirect (post-login & wizard "Go to Dashboard") ────
    path(
        'my-dashboard/',
        views.smart_dashboard_redirect,
        name='smart_dashboard_redirect',
    ),

    # ── Agricultural ─────────────────────────────────────────────
    path(
        'org/<uuid:org_id>/dashboard/agriculture/',
        views.dashboard_agriculture,
        name='org_dashboard_agriculture',
    ),

    # ── NGO / Disaster Relief ────────────────────────────────────
    path(
        'org/<uuid:org_id>/dashboard/ngo/',
        views.dashboard_ngo,
        name='org_dashboard_ngo',
    ),

    # ── Meteorological ───────────────────────────────────────────
    path(
        'org/<uuid:org_id>/dashboard/meteorological/',
        views.dashboard_meteorological,
        name='org_dashboard_meteorological',
    ),

    # ── Enterprise (aviation, developer, generic) ─────────────────
    path(
        'org/<uuid:org_id>/dashboard/enterprise/',
        views.dashboard_enterprise,
        name='org_dashboard_enterprise',
    ),

    # ── Government ────────────────────────────────────────────────
    path(
        'org/<uuid:org_id>/dashboard/government/',
        views.dashboard_government,
        name='org_dashboard_government',
    ),

    # ── Community ─────────────────────────────────────────────────
    path(
        'org/<uuid:org_id>/dashboard/community/',
        views.dashboard_community,
        name='org_dashboard_community',
    ),
]