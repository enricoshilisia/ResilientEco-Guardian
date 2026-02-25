"""
ResilientEco Guardian - Weather Service
Fetches REAL weather observations + today/tomorrow forecast from Visual Crossing.
"""

import requests
import os
from datetime import datetime, timedelta


def assess_flood_risk(location_lat, location_lon):
    """
    Get REAL weather observations + forecast from Visual Crossing.
    Falls back to Open-Meteo if no key.
    """

    vc_key = os.getenv('VISUAL_CROSSING_KEY')
    if vc_key:
        try:
            url = (
                f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services"
                f"/timeline/{location_lat},{location_lon}/today/tomorrow"
            )
            params = {
                "key": vc_key,
                "include": "current,hours,days",
                "unitGroup": "metric",
                "contentType": "json",
            }
            response = requests.get(url, params=params, timeout=10)
            if response.ok:
                data = response.json()
                days = data.get('days', [])
                today    = days[0] if len(days) > 0 else {}
                tomorrow = days[1] if len(days) > 1 else {}

                today_hours    = today.get('hours', [])
                tomorrow_hours = tomorrow.get('hours', [])

                return {
                    'source': 'visual_crossing',
                    'current': {
                        'time': data.get('currentConditions', {}).get('datetime', 'unknown'),
                        'temperature_2m': data.get('currentConditions', {}).get('temp'),
                        'precipitation': data.get('currentConditions', {}).get('precip') or 0,
                        'rain': data.get('currentConditions', {}).get('precip') or 0,
                        'relative_humidity_2m': data.get('currentConditions', {}).get('humidity'),
                        'weather_code': 0,
                        'conditions': data.get('currentConditions', {}).get('conditions', ''),
                    },
                    'hourly': {
                        'precipitation': [h.get('precip') or 0 for h in today_hours],
                        'time': [h.get('datetime', '') for h in today_hours],
                    },
                    'daily': {
                        'rain_sum':   [today.get('precip') or 0, tomorrow.get('precip') or 0],
                        'dates':      [today.get('datetime', ''), tomorrow.get('datetime', '')],
                        'description':[today.get('description', ''), tomorrow.get('description', '')],
                        'temp_max':   [today.get('tempmax'), tomorrow.get('tempmax')],
                        'temp_min':   [today.get('tempmin'), tomorrow.get('tempmin')],
                        'conditions': [today.get('conditions', ''), tomorrow.get('conditions', '')],
                        'precip_prob':[today.get('precipprob') or 0, tomorrow.get('precipprob') or 0],
                    },
                    'today_hours': [
                        {
                            'time': h.get('datetime', ''),
                            'temp': h.get('temp'),
                            'precip': h.get('precip') or 0,
                            'precip_prob': h.get('precipprob') or 0,
                            'humidity': h.get('humidity'),
                            'conditions': h.get('conditions', ''),
                        }
                        for h in today_hours
                    ],
                    'tomorrow_hours': [
                        {
                            'time': h.get('datetime', ''),
                            'temp': h.get('temp'),
                            'precip': h.get('precip') or 0,
                            'precip_prob': h.get('precipprob') or 0,
                            'humidity': h.get('humidity'),
                            'conditions': h.get('conditions', ''),
                        }
                        for h in tomorrow_hours
                    ],
                }
        except Exception as e:
            print(f"Visual Crossing failed: {e}")

    # Fallback: Open-Meteo ARCHIVE + forecast
    today = datetime.now()
    start_date = (today - timedelta(days=1)).strftime('%Y-%m-%d')
    end_date   = (today + timedelta(days=1)).strftime('%Y-%m-%d')

    try:
        archive_url = "https://archive-api.open-meteo.com/v1/archive"
        archive_params = {
            "latitude": location_lat,
            "longitude": location_lon,
            "start_date": start_date,
            "end_date": end_date,
            "hourly": "temperature_2m,precipitation,relative_humidity_2m,rain",
            "timezone": "auto",
        }
        archive_response = requests.get(archive_url, params=archive_params, timeout=10)
        if archive_response.ok:
            archive_data = archive_response.json()
            hourly  = archive_data.get('hourly', {})
            times   = hourly.get('time', [])
            temps   = [v for v in hourly.get('temperature_2m', []) if v is not None]
            precips = [v or 0 for v in hourly.get('precipitation', [])]
            humids  = hourly.get('relative_humidity_2m', [])
            rains   = [v or 0 for v in hourly.get('rain', [])]

            if times:
                last_temp  = next((v for v in reversed(hourly.get('temperature_2m', [])) if v is not None), None)
                last_precip = precips[-1] if precips else 0
                last_rain   = rains[-1]   if rains   else 0
                last_humid  = next((v for v in reversed(humids) if v is not None), None)

                t24 = [v or 0 for v in precips[:24]]
                t48 = [v or 0 for v in precips[24:]]
                raw_temps_24 = [v for v in hourly.get('temperature_2m', [])[:24] if v is not None]
                raw_temps_48 = [v for v in hourly.get('temperature_2m', [])[24:] if v is not None]

                return {
                    'source': 'open_meteo_archive',
                    'current': {
                        'time': times[-1],
                        'temperature_2m': last_temp,
                        'precipitation': last_precip,
                        'rain': last_rain,
                        'relative_humidity_2m': last_humid,
                        'weather_code': 0,
                    },
                    'hourly': {
                        'precipitation': precips[-24:],
                        'time': times[-24:],
                    },
                    'daily': {
                        'rain_sum':    [sum(t24), sum(t48)],
                        'dates':       [start_date, end_date],
                        'description': ['', ''],
                        'temp_max':    [max(raw_temps_24) if raw_temps_24 else None, max(raw_temps_48) if raw_temps_48 else None],
                        'temp_min':    [min(raw_temps_24) if raw_temps_24 else None, min(raw_temps_48) if raw_temps_48 else None],
                        'conditions':  ['', ''],
                        'precip_prob': [0, 0],
                    },
                    'today_hours':    [],
                    'tomorrow_hours': [],
                }

        # Current forecast fallback
        current_url = "https://api.open-meteo.com/v1/forecast"
        current_params = {
            "latitude": location_lat,
            "longitude": location_lon,
            "current": "temperature_2m,precipitation,rain,relative_humidity_2m,weather_code",
            "hourly": "precipitation,rain,temperature_2m",
            "past_hours": 24,
            "forecast_hours": 24,
            "timezone": "auto",
        }
        current_response = requests.get(current_url, params=current_params, timeout=10)
        if current_response.ok:
            return {
                'source': 'open_meteo_current',
                **current_response.json(),
                'today_hours':    [],
                'tomorrow_hours': [],
            }

    except Exception as e:
        print(f"Weather API error: {e}")

    # Final fallback — all zeros, no None values
    return {
        'source': 'fallback',
        'error': 'Could not fetch weather data',
        'current': {
            'time': 'unknown',
            'temperature_2m': None,
            'precipitation': 0,
            'rain': 0,
            'relative_humidity_2m': None,
            'weather_code': 0,
        },
        'hourly': {'precipitation': [], 'time': []},
        'daily': {
            'rain_sum':    [0, 0],
            'dates':       ['', ''],
            'description': ['', ''],
            'temp_max':    [None, None],
            'temp_min':    [None, None],
            'conditions':  ['', ''],
            'precip_prob': [0, 0],
        },
        'today_hours':    [],
        'tomorrow_hours': [],
    }


def _safe_max(values):
    """max() that skips None values; returns None if list is empty."""
    clean = [v for v in values if v is not None]
    return max(clean) if clean else None


def _safe_min(values):
    """min() that skips None values; returns None if list is empty."""
    clean = [v for v in values if v is not None]
    return min(clean) if clean else None


def _safe_avg(values):
    """mean of non-None values; returns None if list is empty."""
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 1) if clean else None


def get_weather_summary(lat, lon, location_name="Location"):
    """
    Get current conditions + today/tomorrow forecast.
    Returns a dict passed to all agents.
    """
    data = assess_flood_risk(lat, lon)

    current        = data.get('current', {})
    hourly         = data.get('hourly', {})
    daily          = data.get('daily', {})
    today_hours    = data.get('today_hours', [])
    tomorrow_hours = data.get('tomorrow_hours', [])

    # Guard against None in precipitation list
    precip_history = [v or 0 for v in hourly.get('precipitation', [])[-24:]]
    total_24h      = sum(precip_history)

    def _period_summary(hours, label):
        """Summarise a list of hourly dicts into a forecast period dict."""
        if not hours:
            return {}
        precips = [h.get('precip') or 0 for h in hours]
        probs   = [h.get('precip_prob') or 0 for h in hours]
        temps   = [h.get('temp') for h in hours]       # may contain None
        return {
            'period':           label,
            'total_precip_mm':  round(sum(precips), 1),
            'max_precip_prob_pct': max(probs) if probs else 0,
            'avg_temp_c':       _safe_avg(temps),
            'conditions':       hours[len(hours) // 2].get('conditions', '') if hours else '',
        }

    # Split today's hours into periods
    morning   = [h for h in today_hours if '06:00' <= h['time'] < '12:00']
    afternoon = [h for h in today_hours if '12:00' <= h['time'] < '18:00']
    evening   = [h for h in today_hours if '18:00' <= h['time'] < '22:00']
    night     = [h for h in today_hours if h['time'] >= '22:00' or h['time'] < '06:00']

    # Daily lists — guard against short arrays and None entries
    rain_sum    = daily.get('rain_sum',    [0, 0])
    temp_max    = daily.get('temp_max',    [None, None])
    temp_min    = daily.get('temp_min',    [None, None])
    description = daily.get('description', ['', ''])
    precip_prob = daily.get('precip_prob', [0, 0])
    conditions  = daily.get('conditions',  ['', ''])

    def _safe_idx(lst, idx, default=None):
        try:
            v = lst[idx]
            return v if v is not None else default
        except (IndexError, TypeError):
            return default

    today_forecast = {
        'morning':        _period_summary(morning,   'Morning (6am–12pm)'),
        'afternoon':      _period_summary(afternoon, 'Afternoon (12pm–6pm)'),
        'evening':        _period_summary(evening,   'Evening (6pm–10pm)'),
        'night':          _period_summary(night,     'Night (10pm–6am)'),
        'daily_total_mm': _safe_idx(rain_sum,    0, 0),
        'temp_max':       _safe_idx(temp_max,    0),
        'temp_min':       _safe_idx(temp_min,    0),
        'description':    _safe_idx(description, 0, ''),
        'precip_prob':    _safe_idx(precip_prob, 0, 0),
        'conditions':     _safe_idx(conditions,  0, ''),
    }

    tomorrow_forecast = {
        'daily_total_mm': _safe_idx(rain_sum,    1, 0),
        'temp_max':       _safe_idx(temp_max,    1),
        'temp_min':       _safe_idx(temp_min,    1),
        'description':    _safe_idx(description, 1, ''),
        'precip_prob':    _safe_idx(precip_prob, 1, 0),
        'conditions':     _safe_idx(conditions,  1, ''),
        'morning':   _period_summary([h for h in tomorrow_hours if '06:00' <= h['time'] < '12:00'], 'Tomorrow Morning'),
        'afternoon': _period_summary([h for h in tomorrow_hours if '12:00' <= h['time'] < '18:00'], 'Tomorrow Afternoon'),
        'evening':   _period_summary([h for h in tomorrow_hours if '18:00' <= h['time'] < '22:00'], 'Tomorrow Evening'),
    }

    current_precip = current.get('precipitation') or 0
    current_rain   = current.get('rain') or 0

    return {
        'location':             location_name,
        'data_source':          data.get('source', 'unknown'),
        'temperature':          current.get('temperature_2m'),
        'current_precipitation': current_precip,
        'current_rain':         current_rain,
        'humidity':             current.get('relative_humidity_2m'),
        'total_rain_24h':       round(total_24h, 2),
        'observation_time':     current.get('time', 'unknown'),
        'is_raining_now':       (current_precip > 0 or current_rain > 0),
        'rained_last_24h':      total_24h > 0,
        'current_conditions':   current.get('conditions', ''),
        'today_forecast':       today_forecast,
        'tomorrow_forecast':    tomorrow_forecast,
    }