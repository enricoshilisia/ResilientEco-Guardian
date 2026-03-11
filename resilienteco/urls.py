"""
URL configuration for resilienteco project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
"""
URL configuration for resilienteco project.
"""

from django.contrib import admin
from django.http import HttpResponse
from django.urls import path, include
from django.views.decorators.csrf import csrf_exempt


# ── Health check — returns 200 with no redirects ───────────────
def health_check(request):
    return HttpResponse("OK", status=200)


urlpatterns = [
    # Health — both with and without trailing slash to avoid 301
    path('health', health_check, name='health'),
    path('health/', health_check, name='health-slash'),

    path('admin/', admin.site.urls),
    path('accounts/', include('allauth.urls')),
    path('', include('organizations.urls')),
    path('', include('guardian.urls')),
]