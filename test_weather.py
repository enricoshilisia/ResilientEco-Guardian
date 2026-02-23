"""
Quick test to verify Visual Crossing API is working
Run this with: python test_weather.py
"""

import os
from dotenv import load_dotenv
load_dotenv()

from guardian.services.weather_service import assess_flood_risk, get_weather_summary

# Test coordinates for Nairobi
lat, lon = -1.2921, 36.8219

print("Testing Visual Crossing API...")
print(f"API Key present: {'Yes' if os.getenv('VISUAL_CROSSING_KEY') else 'No'}")
print("-" * 60)

# Get raw data
data = assess_flood_risk(lat, lon)
print(f"Data source: {data.get('source', 'unknown')}")
print(f"Current temp: {data.get('current', {}).get('temperature_2m')}°C")
print(f"Precipitation: {data.get('current', {}).get('precipitation')}mm")
print("-" * 60)

# Get formatted summary
summary = get_weather_summary(lat, lon, "Nairobi")
print(f"Location: {summary['location']}")
print(f"Source: {summary['data_source']}")
print(f"Temperature: {summary['temperature']}°C")
print(f"Raining now: {summary['is_raining_now']}")
print(f"24h rain total: {summary['total_rain_24h']}mm")
print(f"Observation time: {summary['observation_time']}")
print("-" * 60)

if summary['data_source'] == 'visual_crossing':
    print("✅ SUCCESS! Visual Crossing is working correctly.")
elif summary['data_source'] == 'open_meteo_archive':
    print("⚠️  Using Open-Meteo fallback. Check if VISUAL_CROSSING_KEY is set correctly.")
else:
    print("❌ ERROR: Weather data not available.")