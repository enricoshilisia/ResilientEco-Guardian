from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from dotenv import load_dotenv
load_dotenv()

import os
from openai import OpenAI
from .services.weather_service import assess_flood_risk
from .models import SavedLocation, AlertLog
from guardian.agents.core_agents import run_all_agents

def run_climate_agent(query, weather_data=None):
    client = OpenAI(
        base_url=os.getenv('AZURE_OPENAI_ENDPOINT'),
        api_key=os.getenv('AZURE_OPENAI_KEY')
    )
    
    prompt = query
    if weather_data:
        if isinstance(weather_data, str):
            prompt = f"{query}\n\n{weather_data}"
        else:
            current = weather_data.get('current', {})
            hourly = weather_data.get('hourly', {})
            daily = weather_data.get('daily', {})
            
            # Get last 24 hours of precipitation
            precip_history = hourly.get('precipitation', [])[-24:] if hourly.get('precipitation') else []
            total_last_24h = sum(p for p in precip_history if p) if precip_history else 0
            
            prompt = f"""{query}

REAL-TIME WEATHER DATA:

CURRENT (NOW - {current.get('time', 'unknown')}):
- Precipitation: {current.get('precipitation', 0)} mm
- Rain: {current.get('rain', 0)} mm
- Temperature: {current.get('temperature_2m', 'N/A')}°C
- Humidity: {current.get('relative_humidity_2m', 'N/A')}%
- Weather code: {current.get('weather_code', 0)}

LAST 24 HOURS:
- Total precipitation: {total_last_24h} mm
- Hourly breakdown: {precip_history}

DAILY SUMMARY:
- Yesterday's rain: {daily.get('rain_sum', [0])[0] if daily.get('rain_sum') else 0} mm

INSTRUCTIONS:
- If current precipitation > 0: It IS raining now
- If total_last_24h > 0: It DID rain in the last 24 hours
- Use hourly data to determine when it last rained
- Answer the user's specific question about timing"""
    
    response = client.chat.completions.create(
        model=os.getenv('FOUNDRY_DEPLOYMENT'),
        messages=[
            {"role": "system", "content": "You are ResilientEco Guardian. Analyze the full weather data provided. Answer questions about current AND past weather accurately. Look at hourly precipitation to determine when it last rained."},
            {"role": "user", "content": prompt}
        ]
    )
    return response.choices[0].message.content

def dashboard(request):
    return render(request, 'guardian/dashboard.html')

@csrf_exempt
def api_run_agent(request):
    if request.method == 'POST':
        query = request.POST.get('query', 'Check climate risk')
        lat = float(request.POST.get('lat', '-1.2921'))
        lon = float(request.POST.get('lon', '36.8219'))
        location_name = request.POST.get('location_name', 'Nairobi')

        results = run_all_agents(query, lat, lon, location_name)
        
        # Save alert from action agent output
        action_output = results.get('action', '')
        
        return JsonResponse({'result': results, 'status': 'success'})
    return JsonResponse({'error': 'POST required'})
@csrf_exempt
def save_location(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        lat = request.POST.get('lat')
        lon = request.POST.get('lon')
        
        # Save without user (anonymous for now)
        location = SavedLocation.objects.create(
            user=None,
            name=name,
            latitude=float(lat),
            longitude=float(lon)
        )
        return JsonResponse({
            'status': 'success',
            'location': {
                'id': location.id,
                'name': location.name,
                'latitude': location.latitude,
                'longitude': location.longitude
            }
        })
    return JsonResponse({'error': 'POST required'})

def get_locations(request):
    # Get all locations (anonymous for now)
    locations = SavedLocation.objects.filter(user=None).values('id', 'name', 'latitude', 'longitude')
    return JsonResponse({'locations': list(locations)})

def get_alerts(request):
    # Get recent alerts (all for now)
    alerts = AlertLog.objects.order_by('-timestamp')[:10].values(
        'id', 'risk_type', 'risk_level', 'message', 'timestamp'
    )
    return JsonResponse({'alerts': list(alerts)})

@csrf_exempt
def create_alert(request):
    if request.method == 'POST':
        location_id = request.POST.get('location_id')
        risk_type = request.POST.get('risk_type', 'flood')
        risk_level = request.POST.get('risk_level', 50)
        message = request.POST.get('message')
        
        location = SavedLocation.objects.filter(id=location_id).first() if location_id else None
        
        alert = AlertLog.objects.create(
            user=None,
            location=location,
            risk_type=risk_type,
            risk_level=int(risk_level),
            message=message
        )
        return JsonResponse({'status': 'success', 'alert_id': alert.id})
    return JsonResponse({'error': 'POST required'})