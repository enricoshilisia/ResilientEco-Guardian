"""
ResilientEco Guardian - URL Configuration
All routes in one place, all from the guardian app.
"""

from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.conf.urls.static import static

from rest_framework_simplejwt.views import TokenRefreshView

from guardian import views


urlpatterns = [

    # ─── ADMIN ──────────────────────────────────────────────────────────
    path('admin/', admin.site.urls),


    # ─── PAGE ───────────────────────────────────────────────────────────
    path('', views.dashboard, name='dashboard'),


    # ─── AUTH ───────────────────────────────────────────────────────────
    path('api/auth/register/',       views.RegisterView.as_view(),      name='register'),
    path('api/auth/login/',          views.LoginView.as_view(),          name='login'),
    path('api/auth/logout/',         views.LogoutView.as_view(),         name='logout'),
    path('api/auth/token/refresh/',  TokenRefreshView.as_view(),         name='token_refresh'),


    # ─── PROFILE ────────────────────────────────────────────────────────
    path('api/me/',              views.ProfileView.as_view(),          name='profile'),
    path('api/me/password/',     views.ChangePasswordView.as_view(),   name='change_password'),
    path('api/me/default-org/',  views.SetDefaultOrgView.as_view(),    name='set_default_org'),
    path('api/me/activity/',     views.MyActivityLogView.as_view(),    name='activity_log'),
    path('api/me/invitations/',  views.MyInvitationsView.as_view(),    name='my_invitations'),
    path('api/me/dashboard/',    views.DashboardSummaryView.as_view(), name='user_dashboard'),


    # ─── AGENT ──────────────────────────────────────────────────────────
    path('api/agent/run/',       views.RunAgentView.as_view(),         name='run_agent'),


    # ─── LOCATIONS ──────────────────────────────────────────────────────
    path('api/locations/',                       views.LocationListView.as_view(),   name='locations'),
    path('api/locations/<int:location_id>/',     views.LocationDetailView.as_view(), name='location_detail'),


    # ─── ALERTS ─────────────────────────────────────────────────────────
    path('api/alerts/',                          views.AlertListView.as_view(),      name='alerts'),
    path('api/alerts/<int:alert_id>/',           views.AlertDetailView.as_view(),    name='alert_detail'),


    # ─── ORGANIZATIONS ──────────────────────────────────────────────────
    path('api/organizations/',                   views.MyOrganizationsView.as_view(),    name='my_orgs'),
    path('api/organizations/create/',            views.CreateOrganizationView.as_view(), name='create_org'),
    path('api/organizations/<uuid:org_id>/',     views.OrganizationDetailView.as_view(), name='org_detail'),
    path('api/organizations/<uuid:org_id>/leave/', views.LeaveOrganizationView.as_view(), name='leave_org'),


    # ─── ORGANIZATION MEMBERS ───────────────────────────────────────────
    path('api/organizations/<uuid:org_id>/members/',
         views.OrgMembersView.as_view(), name='org_members'),
    path('api/organizations/<uuid:org_id>/members/<int:user_id>/role/',
         views.UpdateMemberRoleView.as_view(), name='update_role'),
    path('api/organizations/<uuid:org_id>/members/<int:user_id>/remove/',
         views.RemoveMemberView.as_view(), name='remove_member'),


    # ─── INVITATIONS ────────────────────────────────────────────────────
    path('api/organizations/<uuid:org_id>/invite/',
         views.SendInvitationView.as_view(), name='send_invite'),
    path('api/invitations/<uuid:token>/accept/',
         views.AcceptInvitationView.as_view(), name='accept_invite'),
    path('api/invitations/<uuid:token>/decline/',
         views.DeclineInvitationView.as_view(), name='decline_invite'),


    # ─── LEGACY ROUTES (for old dashboard compatibility) ───────────────
    path('api/run/',            views.RunAgentView.as_view(),     name='api_run_legacy'),
    path('api/save-location/',  views.LocationListView.as_view(), name='save_location_legacy'),
    path('api/get-locations/',  views.LocationListView.as_view(), name='get_locations_legacy'),
    path('api/get-alerts/',     views.AlertListView.as_view(),    name='get_alerts_legacy'),
    path('api/weather/', views.WeatherView.as_view(), name='weather_api'),

] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)