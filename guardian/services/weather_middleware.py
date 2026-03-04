"""
ResilientEco Guardian - Weather Data Middleware
Transforms raw weather API data into structured reports for agent consumption.
Implements middleware pattern for data transformation.
"""

import logging
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class WeatherDataMiddleware:
    """
    Transforms raw weather data into structured reports before agents process it.
    This provides:
    - Human-readable summaries
    - Metric calculations
    - Alert extraction
    - Narrative generation
    """
    
    def __init__(self):
        self.transformation_count = 0

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _normalize_weather(self, data: dict) -> dict:
        """
        Normalize different weather payload shapes into a canonical
        structure with current/hourly/daily for downstream transforms.
        """
        if not isinstance(data, dict):
            return {'source': 'unknown', 'current': {}, 'hourly': {}, 'daily': {}}

        # Already canonical (raw API shape)
        if isinstance(data.get('current'), dict) and isinstance(data.get('daily'), dict):
            normalized = dict(data)
            if 'source' not in normalized and 'data_source' in normalized:
                normalized['source'] = normalized.get('data_source')
            return normalized

        today = data.get('today_forecast', {}) if isinstance(data.get('today_forecast'), dict) else {}
        tomorrow = data.get('tomorrow_forecast', {}) if isinstance(data.get('tomorrow_forecast'), dict) else {}

        total_24h = self._to_float(data.get('total_rain_24h')) or 0
        current_precip = self._to_float(data.get('current_precipitation'))
        current_rain = self._to_float(data.get('current_rain'))

        return {
            'source': data.get('data_source') or data.get('source', 'unknown'),
            'current': {
                'temperature_2m': self._to_float(data.get('temperature')),
                'precipitation': current_precip if current_precip is not None else (current_rain or 0),
                'rain': current_rain if current_rain is not None else (current_precip or 0),
                'relative_humidity_2m': self._to_float(data.get('humidity')),
                'conditions': data.get('current_conditions', ''),
                'time': data.get('observation_time', 'unknown'),
                'wind_speed': self._to_float(data.get('wind_speed')),
            },
            'hourly': {
                # Keep a minimal signal so 24h aggregations remain meaningful.
                'precipitation': [total_24h] if total_24h > 0 else [],
                'time': [data.get('observation_time', 'unknown')],
            },
            'daily': {
                'rain_sum': [
                    self._to_float(today.get('daily_total_mm')) or 0,
                    self._to_float(tomorrow.get('daily_total_mm')) or 0,
                ],
                'temp_max': [
                    self._to_float(today.get('temp_max')),
                    self._to_float(tomorrow.get('temp_max')),
                ],
                'temp_min': [
                    self._to_float(today.get('temp_min')),
                    self._to_float(tomorrow.get('temp_min')),
                ],
                'precip_prob': [
                    self._to_float(today.get('precip_prob')) or 0,
                    self._to_float(tomorrow.get('precip_prob')) or 0,
                ],
                'conditions': [
                    today.get('conditions', ''),
                    tomorrow.get('conditions', ''),
                ],
            },
        }

    def _build_routing_features(self, original: dict, canonical: dict, metrics: dict) -> dict:
        """Build consistent routing features used by graph selection."""
        daily = canonical.get('daily', {})
        current = canonical.get('current', {})
        rain_sum = daily.get('rain_sum', [None, None])
        precip_prob = daily.get('precip_prob', [None, None])
        if isinstance(precip_prob, list) and precip_prob:
            precip_probability = self._to_float(precip_prob[0])
        else:
            precip_probability = self._to_float(precip_prob)

        if precip_probability is None and isinstance(original, dict):
            today = original.get('today_forecast', {})
            if isinstance(today, dict):
                precip_probability = self._to_float(today.get('precip_prob'))

        return {
            'temperature': self._to_float(current.get('temperature_2m')),
            'heat_index': self._to_float(metrics.get('heat_index')),
            'total_rain_24h': self._to_float(metrics.get('total_precipitation_24h')),
            'precip_probability': precip_probability,
            'forecast_rain_today': self._to_float(rain_sum[0] if isinstance(rain_sum, list) and len(rain_sum) > 0 else None),
            'forecast_rain_tomorrow': self._to_float(rain_sum[1] if isinstance(rain_sum, list) and len(rain_sum) > 1 else None),
            'wind_speed': self._to_float(current.get('wind_speed'))
            if current.get('wind_speed') is not None else self._to_float(original.get('wind_speed') if isinstance(original, dict) else None),
            'rain_30d': self._to_float(original.get('rain_30d') if isinstance(original, dict) else None),
            'soil_moisture': self._to_float(original.get('soil_moisture') if isinstance(original, dict) else None),
        }
    
    def transform(self, raw_weather: dict, location_name: str = "Location") -> dict:
        """
        Main transformation entry point.
        Takes raw weather data and returns enhanced structure.
        """
        self.transformation_count += 1
        canonical = self._normalize_weather(raw_weather)
        metrics = self._calculate_metrics(canonical)

        return {
            'summary': self._generate_summary(canonical),
            'metrics': metrics,
            'alerts': self._extract_alerts(canonical),
            'narrative': self._generate_narrative(canonical, location_name),
            'routing_features': self._build_routing_features(raw_weather, canonical, metrics),
            'enhanced_data': raw_weather,
            'canonical_data': canonical,
            'transformed_at': datetime.now().isoformat()
        }
    
    def _generate_summary(self, data: dict) -> dict:
        """Generate a concise summary of current conditions"""
        current = data.get('current', {})
        daily = data.get('daily', {})
        
        return {
            'current_temp': current.get('temperature_2m'),
            'current_conditions': current.get('conditions', 'Unknown'),
            'is_raining': current.get('precipitation', 0) > 0,
            'humidity': current.get('relative_humidity_2m'),
            'today_rain_total': daily.get('rain_sum', [0, 0])[0] if daily else 0,
            'tomorrow_rain_expected': daily.get('rain_sum', [0, 0])[1] if daily else 0,
            'data_source': data.get('source', 'unknown')
        }
    
    def _calculate_metrics(self, data: dict) -> dict:
        """Calculate derived metrics from raw data"""
        current = data.get('current', {})
        hourly = data.get('hourly', {})
        daily = data.get('daily', {})
        
        # Get precipitation data
        precip_history = hourly.get('precipitation', [])
        total_24h = sum(p or 0 for p in precip_history[-24:]) if precip_history else 0
        
        # Temperature range
        temp_max = daily.get('temp_max', [None, None])
        temp_min = daily.get('temp_min', [None, None])
        
        return {
            'total_precipitation_24h': round(total_24h, 2),
            'precipitation_intensity': self._classify_precip_intensity(total_24h),
            'temperature_range_today': {
                'high': temp_max[0] if temp_max else None,
                'low': temp_min[0] if temp_min else None
            },
            'heat_index': self._calculate_heat_index(
                current.get('temperature_2m', 20),
                current.get('relative_humidity_2m', 50)
            ),
            'comfort_level': self._calculate_comfort(
                current.get('temperature_2m', 20),
                current.get('relative_humidity_2m', 50)
            )
        }
    
    def _extract_alerts(self, data: dict) -> list:
        """Extract potential alerts from weather data"""
        alerts = []
        
        current = data.get('current', {})
        daily = data.get('daily', {})
        metrics = self._calculate_metrics(data)
        
        # Check for heavy rain
        if metrics['total_precipitation_24h'] > 30:
            alerts.append({
                'type': 'heavy_rain',
                'severity': 'high',
                'message': f"Heavy rainfall expected: {metrics['total_precipitation_24h']:.1f}mm in last 24 hours"
            })
        
        # Check for high temperature
        if current.get('temperature_2m', 0) > 35:
            alerts.append({
                'type': 'high_temperature',
                'severity': 'medium',
                'message': f"High temperature alert: {current.get('temperature_2m')}°C"
            })
        
        # Check for heat index danger
        if metrics.get('heat_index', 0) > 40:
            alerts.append({
                'type': 'heat_danger',
                'severity': 'high',
                'message': f"Heat index dangerous: {metrics['heat_index']:.1f}°C"
            })
        
        # Check precipitation probability
        precip_prob = daily.get('precip_prob', [0, 0])
        if len(precip_prob) > 0 and precip_prob[0] > 70:
            alerts.append({
                'type': 'high_precip_probability',
                'severity': 'medium',
                'message': f"High chance of rain today: {precip_prob[0]}%"
            })
        
        return alerts
    
    def _generate_narrative(self, data: dict, location: str) -> str:
        """Generate human-readable weather narrative"""
        current = data.get('current', {})
        daily = data.get('daily', {})
        metrics = self._calculate_metrics(data)
        
        # Base narrative
        temp = current.get('temperature_2m', 'Unknown')
        conditions = current.get('conditions', 'clear')
        humidity = current.get('relative_humidity_2m', 'Unknown')
        
        narrative = f"Weather conditions at {location}: "
        narrative += f"Currently {temp}°C with {conditions.lower()}. "
        narrative += f"Humidity is at {humidity}%. "
        
        # Add precipitation info
        if metrics['total_precipitation_24h'] > 0:
            narrative += f"Rainfall in the last 24 hours totals {metrics['total_precipitation_24h']:.1f}mm. "
        else:
            narrative += "No rainfall recorded in the last 24 hours. "
        
        # Add forecast summary
        rain_today = daily.get('rain_sum', [0, 0])[0] if daily else 0
        rain_tomorrow = daily.get('rain_sum', [0, 0])[1] if daily else 0
        
        if rain_today > 10:
            narrative += "Heavy rain expected today. "
        elif rain_today > 0:
            narrative += "Light to moderate rain expected today. "
        
        if rain_tomorrow > 10:
            narrative += "Heavy rain expected tomorrow. "
        elif rain_tomorrow > 0:
            narrative += "Possibility of rain tomorrow. "
        
        # Add comfort level
        comfort = metrics.get('comfort_level', 'comfortable')
        if comfort == 'uncomfortable':
            narrative += "Conditions may feel uncomfortable due to heat and humidity. "
        elif comfort == 'dangerous':
            narrative += "Caution: Heat-related illness possible. "
        
        return narrative
    
    def _classify_precip_intensity(self, mm: float) -> str:
        """Classify precipitation intensity"""
        if mm < 1:
            return 'none'
        elif mm < 5:
            return 'light'
        elif mm < 15:
            return 'moderate'
        elif mm < 30:
            return 'heavy'
        else:
            return 'extreme'
    
    def _calculate_heat_index(self, temp_c: float, humidity: float) -> float:
        """Calculate heat index (apparent temperature)"""
        if temp_c < 27:
            return temp_c
        
        # Simplified heat index formula (Rothfusz regression)
        T = temp_c
        R = humidity
        
        HI = -8.78469475556 + 1.61139411 * T + 2.33854883889 * R
        HI += -0.14611605 * T * R
        HI += -0.012308094 * T * T
        HI += -0.0164248277778 * R * R
        HI += 0.002211732 * T * T * R
        HI += 0.00072546 * T * R * R
        HI += -0.000003582 * T * T * R * R
        
        return round(HI, 1)
    
    def _calculate_comfort(self, temp_c: float, humidity: float) -> str:
        """Calculate thermal comfort level"""
        heat_index = self._calculate_heat_index(temp_c, humidity)
        
        if heat_index < 27:
            return 'comfortable'
        elif heat_index < 32:
            return 'caution'
        elif heat_index < 41:
            return 'extreme_caution'
        elif heat_index < 54:
            return 'dangerous'
        else:
            return 'extremely_dangerous'


# Singleton instance for reuse
weather_middleware = WeatherDataMiddleware()


def transform_weather_data(raw_data: dict, location_name: str = "Location") -> dict:
    """
    Convenience function to transform weather data.
    Use this in views before passing to agents.
    """
    return weather_middleware.transform(raw_data, location_name)
