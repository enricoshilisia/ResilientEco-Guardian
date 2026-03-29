#!/usr/bin/env bash

set -e

echo "Current directory: $(pwd)"
echo "PORT is: ${PORT}"

export DJANGO_SETTINGS_MODULE=resilienteco.settings

echo "Running Django commands..."
python manage.py migrate --noinput || echo "Migrations failed (non-fatal)"
python manage.py collectstatic --noinput --clear || echo "collectstatic failed (non-fatal)"

echo "Creating default organizations, users, and locations..."

python - <<END
import os
import django
django.setup()

from django.contrib.auth.models import User
from guardian.models import Organization, OrganizationMembership, SavedLocation
from django.utils import timezone

# ────────────── Organizations & Users ──────────────
orgs_users = [
    {
        "org_name": "Agricultural Organization",
        "org_type": "government",
        "username": "enrico",
        "password": "Es@91419271",
    },
    {
        "org_name": "Meteorological Organization",
        "org_type": "institution",
        "username": "echesa",
        "password": "Es@91419271",
    },
    {
        "org_name": "Disaster Relief Organization",
        "org_type": "ngo",
        "username": "enriqs",
        "password": "Es@91419271",
    }
]

locations = ["Nairobi", "Nakuru", "Kisumu", "Nyeri"]

for entry in orgs_users:
    org, created = Organization.objects.get_or_create(
        name=entry["org_name"],
        defaults={"org_type": entry["org_type"]}
    )
    
    user, created_user = User.objects.get_or_create(
        username=entry["username"],
        defaults={"email": f"{entry['username']}@example.com"}
    )
    
    if created_user:
        user.set_password(entry["password"])
        user.save()
    
    # Create membership if not exists
    OrganizationMembership.objects.get_or_create(
        user=user,
        organization=org,
        defaults={"role": "admin", "joined_at": timezone.now(), "is_active": True}
    )
    
    # Create locations for the organization
    for loc_name in locations:
        SavedLocation.objects.get_or_create(
            organization=org,
            name=loc_name,
            defaults={
                "latitude": 0.0,
                "longitude": 0.0,
                "location_type": "other",
                "is_active": True
            }
        )

print("Default data setup complete.")
END

echo "Starting Uvicorn on port ${PORT}..."
exec uvicorn resilienteco.asgi:application \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --workers 2