import requests
import os

def assess_flood_risk(location_lat, location_lon):
    """Get real-time weather from Visual Crossing (backup: Open-Meteo)"""
    
    # Try Visual Crossing first (more accurate)
    vc_key = os.getenv('VISUAL_CROSSING_KEY')
    if vc_key:
        try:
            url = f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/{location_lat},{location_lon}"
            params = {
                "key": vc_key,
                "include": "current,hours",
                "unitGroup": "metric"
            }
            response = requests.get(url, params=params, timeout=5)
            if response.ok:
                return response.json()
        except:
            pass
    
    # Fallback to Open-Meteo
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": location_lat,
        "longitude": location_lon,
        "current": "temperature_2m,precipitation,rain,weather_code",
        "hourly": "precipitation",
        "past_hours": 24,
        "timezone": "auto"
    }
    response = requests.get(url, params=params)
    return response.json() if response.ok else None