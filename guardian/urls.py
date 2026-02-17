from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('api/run/', views.api_run_agent, name='api_run'),
    path('api/save-location/', views.save_location, name='save_location'),
    path('api/get-locations/', views.get_locations, name='get_locations'),
    path('api/get-alerts/', views.get_alerts, name='get_alerts'),
]