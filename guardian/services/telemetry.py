"""
ResilientEco Guardian - OpenTelemetry Observability
Traces agent calls and weather API interactions.
"""

import os
import logging
from functools import wraps
from typing import Callable, Any

logger = logging.getLogger(__name__)

# OpenTelemetry is already in requirements.txt
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.trace import Status, StatusCode
    
    # Initialize tracer
    _resource = Resource.create({
        "service.name": "resilienteco-guardian",
        "service.version": "1.0.0"
    })
    
    _provider = TracerProvider(resource=_resource)
    _processor = BatchSpanProcessor(ConsoleSpanExporter())
    _provider.add_span_processor(_processor)
    trace.set_tracer_provider(_provider)
    
    _tracer = trace.get_tracer(__name__)
    TELEMETRY_ENABLED = True
    
except ImportError:
    logger.warning("OpenTelemetry not available - running without observability")
    TELEMETRY_ENABLED = False
    _tracer = None


def trace_agent(agent_name: str) -> Callable:
    """
    Decorator to trace agent execution with OpenTelemetry.
    
    Usage:
        @trace_agent("monitor")
        def run(self, msg):
            ...
    """
    def decorator(func: Callable) -> Callable:
        if not TELEMETRY_ENABLED:
            return func
            
        @wraps(func)
        def wrapper(*args, **kwargs):
            with _tracer.start_as_current_span(f"agent.{agent_name}") as span:
                span.set_attribute("agent.type", agent_name)
                span.set_attribute("agent.name", agent_name)
                
                try:
                    # Add location if available in args
                    if len(args) > 1:  # self + msg
                        msg = args[1]
                        if hasattr(msg, 'location'):
                            span.set_attribute("location.name", msg.location)
                        if hasattr(msg, 'lat'):
                            span.set_attribute("location.lat", msg.lat)
                        if hasattr(msg, 'lon'):
                            span.set_attribute("location.lon", msg.lon)
                    
                    result = func(*args, **kwargs)
                    
                    # Add result attributes
                    if hasattr(result, 'risk_assessment'):
                        risk = result.risk_assessment.get('risk_level') or result.risk_assessment.get('flood_risk', 0)
                        if risk:
                            span.set_attribute("result.risk_level", risk)
                    
                    if hasattr(result, 'checkpoint') and result.checkpoint:
                        span.set_attribute("result.checkpointed", True)
                    
                    span.set_status(Status(StatusCode.OK))
                    return result
                    
                except Exception as e:
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise
                    
        return wrapper
    return decorator


def trace_weather_call(provider_name: str) -> Callable:
    """
    Decorator to trace weather API calls.
    
    Usage:
        @trace_weather_call("visual_crossing")
        def fetch_weather(lat, lon):
            ...
    """
    def decorator(func: Callable) -> Callable:
        if not TELEMETRY_ENABLED:
            return func
            
        @wraps(func)
        def wrapper(*args, **kwargs):
            with _tracer.start_as_current_span(f"weather.{provider_name}") as span:
                span.set_attribute("weather.provider", provider_name)
                
                # Add location if available
                if len(args) >= 2:
                    span.set_attribute("location.lat", args[0])
                    span.set_attribute("location.lon", args[1])
                
                try:
                    import time
                    start = time.time()
                    result = func(*args, **kwargs)
                    duration = (time.time() - start) * 1000
                    
                    span.set_attribute("duration.ms", duration)
                    span.set_attribute("result.success", True)
                    span.set_status(Status(StatusCode.OK))
                    
                    return result
                    
                except Exception as e:
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise
                    
        return wrapper
    return decorator


def log_security_validation(user_id: int, action: str, result: str):
    """Log security validations for middleware"""
    if TELEMETRY_ENABLED:
        with _tracer.start_as_current_span("security.validation") as span:
            span.set_attribute("security.user_id", user_id)
            span.set_attribute("security.action", action)
            span.set_attribute("security.result", result)
    else:
        logger.info(f"[Security] User {user_id}: {action} -> {result}")


class AgentTelemetry:
    """
    Helper class for manual telemetry tracking in agents.
    """
    
    @staticmethod
    def start_span(name: str, **attributes) -> Any:
        """Start a new span with attributes"""
        if not TELEMETRY_ENABLED:
            return None
            
        span = _tracer.start_span(name)
        for key, value in attributes.items():
            span.set_attribute(key, value)
        return span
    
    @staticmethod
    def end_span(span, status: StatusCode = StatusCode.OK, error: str = None):
        """End a span with status"""
        if span:
            if status == StatusCode.ERROR and error:
                span.set_status(Status(status, error))
            else:
                span.set_status(status)
            span.end()


# Singleton instance
telemetry = AgentTelemetry()
