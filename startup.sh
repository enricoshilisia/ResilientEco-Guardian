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
from django.utils.text import slugify
from django.utils import timezone

django.setup()

from django.contrib.auth.models import User
from guardian.models import Organization, OrganizationMembership, SavedLocation

orgs_users = [
    {
        "org_name": "Agricultural Organization",
        "org_type": "enterprise",
        "org_subtype": "agriculture",
        "username": "enrico",
        "password": "Es@91419271",
    },
    {
        "org_name": "Meteorological Organization",
        "org_type": "institution",
        "org_subtype": "meteorological",
        "username": "echesa",
        "password": "Es@91419271",
    },
    {
        "org_name": "Disaster Relief Organization",
        "org_type": "ngo",
        "org_subtype": "disaster_relief",
        "username": "enriqs",
        "password": "Es@91419271",
    }
]

locations = ["Nairobi", "Nakuru", "Kisumu", "Nyeri"]

for entry in orgs_users:
    org_slug = slugify(entry["org_name"])

    org, org_created = Organization.objects.update_or_create(
        slug=org_slug,
        defaults={
            "name": entry["org_name"],
            "org_type": entry["org_type"],
            "org_subtype": entry["org_subtype"],
        }
    )

    user, user_created = User.objects.get_or_create(
        username=entry["username"],
        defaults={"email": f"{entry['username']}@example.com"}
    )

    if user_created:
        user.set_password(entry["password"])
        user.save()

    OrganizationMembership.objects.update_or_create(
        user=user,
        organization=org,
        defaults={
            "role": "admin",
            "joined_at": timezone.now(),
            "is_active": True
        }
    )

    for loc_name in locations:
        SavedLocation.objects.update_or_create(
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