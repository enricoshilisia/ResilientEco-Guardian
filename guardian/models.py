from django.db import models
from django.contrib.auth.models import User

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone = models.CharField(max_length=20, blank=True)
    notifications_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} Profile"

class SavedLocation(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='saved_locations', null=True, blank=True)
    name = models.CharField(max_length=100)  # e.g., "Home", "Farm"
    latitude = models.FloatField()
    longitude = models.FloatField()
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['user', 'name']

    def __str__(self):
        return f"{self.user.username} - {self.name}"

class AlertLog(models.Model):
    RISK_TYPES = [
        ('flood', 'Flood'),
        ('drought', 'Drought'),
        ('heatwave', 'Heatwave'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    location = models.ForeignKey(SavedLocation, on_delete=models.SET_NULL, null=True)
    risk_type = models.CharField(max_length=20, choices=RISK_TYPES)
    risk_level = models.IntegerField()  # 0-100
    message = models.TextField()
    weather_data = models.JSONField(null=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.risk_type} - {self.location.name if self.location else 'Unknown'} ({self.timestamp})"