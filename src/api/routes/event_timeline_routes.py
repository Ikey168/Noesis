"""
Enhanced Event Timeline API Routes - Issue #38

This module provides specialized API endpoints for Issue #38 that extend
the basic event timeline functionality from Issue #37 with:

1. Historical event tracking with detailed metadata
2. Event storage and relationship management in the local service
3. Advanced visualization generation for event evolution
4. Export capabilities for timeline data

Routes:
- GET /api/v1/event-timeline/{topic} - Enhanced timeline with visualizations
- POST /api/v1/event-timeline/track - Track and store historical events
- GET /api/v1/event-timeline/{topic}/visualization - Get visualization data
- GET /api/v1/event-timeline/{topic}/export - Export timeline data
- GET /api/v1/event-timeline/analytics - Timeline analytics
"""

import csv
import io
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator

# Import our enhanced event timeline service
from src.api.event_timeline_service import (
    EventTimelineService,
    HistoricalEvent,
)

# Import authentication and dependencies (if available)
try:
    from src.api.auth import get_current_user

    AUTH_AVAILABLE = True
except ImportError:
    AUTH_AVAILABLE = False

logger = logging.getLogger(__name__)

# Create router with specific prefix for Issue #38
router = APIRouter(
    prefix="/api/v1/event-timeline",
    tags=["event-timeline"],
    responses={404: {"description": "Not found"}},
)

# Global service instance
_event_timeline_service: Optional[EventTimelineService] = None


# Pydantic models for API requests and responses


class EventTrackingRequest(BaseModel):
    """Request model for tracking historical events."""

    topic: str = Field(
        ..., min_length=2, max_length=200, description="Topic to track events for"
    )
    start_date: Optional[datetime] = Field(
        None, description="Start date for event tracking"
    )
    end_date: Optional[datetime] = Field(
        None, description="End date for event tracking"
    )
    event_types: Optional[List[str]] = Field(
        None, description="Specific event types to track"
    )
    max_events: int = Field(
        default=100, ge=1, le=500, description="Maximum number of events to track"
    )
    include_related: bool = Field(
        default=True, description="Include related event relationships"
    )
    persist: bool = Field(
        default=True, description="Persist events in the local timeline service"
    )

    @validator("end_date")
    def end_date_after_start(cls, v, values):
        if (
            v
            and "start_date" in values
            and values["start_date"]
            and v <= values["start_date"]
        ):
            raise ValueError("End date must be after start date")
        return v


class TimelineVisualizationRequest(BaseModel):
    """Request model for timeline visualization."""

    theme: str = Field(default="default", description="Visualization theme")
    chart_type: str = Field(default="timeline", description="Type of chart to generate")
    include_clusters: bool = Field(default=True, description="Include event clusters")
    include_impact_analysis: bool = Field(
        default=True, description="Include impact analysis"
    )
    include_entity_chart: bool = Field(
        default=True, description="Include entity involvement chart"
    )

    @validator("theme")
    def validate_theme(cls, v):
        valid_themes = ["default", "dark", "scientific"]
        if v not in valid_themes:
            raise ValueError("Theme must be one of: {0}".format(valid_themes))
        return v

    @validator("chart_type")
    def validate_chart_type(cls, v):
        valid_types = ["timeline", "scatter", "bar", "line"]
        if v not in valid_types:
            raise ValueError("Chart type must be one of: {0}".format(valid_types))
        return v


class EventTimelineResponse(BaseModel):
    """Enhanced response model for event timeline."""

    topic: str
    timeline_span: Dict[str, Optional[str]]
    total_events: int
    events: List[Dict[str, Any]]
    visualization_included: bool
    visualization_data: Optional[Dict[str, Any]] = None
    analytics: Optional[Dict[str, Any]] = None
    export_options: Optional[Dict[str, str]] = None
    metadata: Dict[str, Any]


class EventTrackingResponse(BaseModel):
    """Response model for event tracking operations."""

    topic: str
    events_tracked: int
    events_stored: int
    relationships_created: int
    processing_time: float
    errors: List[str]
    timeline_id: Optional[str] = None


class VisualizationResponse(BaseModel):
    """Response model for visualization data."""

    topic: str
    visualization_type: str
    chart_data: Dict[str, Any]
    theme: str
    generated_at: datetime
    export_formats: List[str]


# Dependency functions


async def get_event_timeline_service() -> EventTimelineService:
    """Get or create event timeline service instance."""
    global _event_timeline_service

    if _event_timeline_service is None:
        try:
            _event_timeline_service = EventTimelineService()
            logger.info("Event timeline service initialized")
        except Exception as e:
            logger.error("Failed to initialize event timeline service: {0}".format(e))
            raise HTTPException(
                status_code=503, detail="Event timeline service not available"
            )

    return _event_timeline_service


def get_optional_auth():
    """Optional authentication dependency."""
    if AUTH_AVAILABLE:
        return Depends(get_current_user)
    else:
        return None


# API Endpoints


@router.get(
    "/{topic}",
    response_model=EventTimelineResponse,
    summary="Get Enhanced Event Timeline",
    description="""
    Get an enhanced event timeline for a specific topic with visualization support.

    This endpoint implements Issue #38 requirements:
    - Track historical events related to a topic
    - Generate visualizations of event evolution
    - Provide structured timeline data

    Enhanced features over Issue #37:
    - Detailed event metadata and impact scoring
    - Advanced visualization data
    - Event clustering and analytics
    - Export capability links
    """,
)
async def get_enhanced_event_timeline(
    topic: str = Path(..., description="Topic to get timeline for"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    max_events: int = Query(50, ge=1, le=200, description="Maximum number of events"),
    include_visualizations: bool = Query(
        True, description="Include visualization data"
    ),
    include_analytics: bool = Query(True, description="Include analytics data"),
    theme: str = Query("default", description="Visualization theme"),
    service: EventTimelineService = Depends(get_event_timeline_service),
    user=Depends(get_optional_auth),
):
    """Get enhanced event timeline with visualization support."""
    try:
        logger.info("API request for enhanced event timeline: {0}".format(topic))

        # Parse date parameters
        parsed_start_date = None
        parsed_end_date = None

        if start_date:
            try:
                parsed_start_date = datetime.fromisoformat(start_date)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid start_date format. Use YYYY-MM-DD"
                )

        if end_date:
            try:
                parsed_end_date = datetime.fromisoformat(end_date)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Invalid end_date format. Use YYYY-MM-DD"
                )

        # Validate date range
        if (
            parsed_start_date
            and parsed_end_date
            and parsed_start_date >= parsed_end_date
        ):
            raise HTTPException(
                status_code=400, detail="Start date must be before end date"
            )

        # Generate enhanced timeline response
        try:
            response_data = await service.generate_timeline_api_response(
                topic=topic,
                start_date=parsed_start_date,
                end_date=parsed_end_date,
                max_events=max_events,
                include_visualizations=include_visualizations,
            )

            # Ensure basic structure exists
            if not isinstance(response_data, dict):
                raise ValueError("Invalid response format from service")

        except Exception as service_error:
            logger.error("Service error generating timeline: {0}".format(service_error))
            # Create fallback response
            response_data = {
                "topic": topic,
                "timeline_span": {
                    "start_date": (
                        parsed_start_date.isoformat() if parsed_start_date else None
                    ),
                    "end_date": (
                        parsed_end_date.isoformat() if parsed_end_date else None
                    ),
                },
                "total_events": 0,
                "events": [],
                "metadata": {
                    "generated_at": datetime.now().isoformat(),
                    "max_events": max_events,
                    "visualization_included": include_visualizations,
                    "service_error": str(service_error),
                },
            }

        # Add analytics if requested
        if include_analytics and response_data.get("events"):
            try:
                events = []
                for e in response_data["events"]:
                    try:
                        event = HistoricalEvent(
                            event_id=e.get("event_id", "unknown"),
                            title=e.get("title", "Unknown Event"),
                            description=e.get("description", ""),
                            timestamp=datetime.fromisoformat(
                                e.get("timestamp", datetime.now().isoformat())
                            ),
                            topic=e.get(
                                "topic", topic
                            ),  # Use endpoint topic as fallback
                            event_type=e.get("event_type", "general"),
                            entities_involved=e.get("entities_involved", []),
                            confidence=e.get("confidence", 0.7),
                            impact_score=e.get("impact_score", 0.5),
                        )
                        events.append(event)
                    except Exception as event_error:
                        logger.warning(
                            "Failed to create HistoricalEvent from data: {0}".format(
                                event_error
                            )
                        )
                        continue

                if events:
                    # Generate analytics
                    viz_data = await service.generate_visualization_data(
                        topic=topic, events=events, theme=theme
                    )

                    response_data["analytics"] = {
                        "event_clusters": viz_data.get("event_clusters"),
                        "impact_analysis": viz_data.get("impact_analysis"),
                        "entity_involvement": viz_data.get("entity_involvement"),
                    }
                else:
                    logger.warning("No valid events found for analytics generation")
                    response_data["analytics"] = {}

            except Exception as analytics_error:
                logger.error(
                    "Failed to generate analytics: {0}".format(analytics_error)
                )
                response_data["analytics"] = {}

        # Add export options
        response_data["export_options"] = {
            "json": "/api/v1/event-timeline/{0}/export?format=json".format(topic),
            "csv": "/api/v1/event-timeline/{0}/export?format=csv".format(topic),
            "html": "/api/v1/event-timeline/{0}/export?format=html".format(topic),
        }

        # Update metadata
        response_data["metadata"].update(
            {
                "enhanced_api": True,
                "issue_38_implementation": True,
                "visualization_theme": theme,
                "analytics_included": include_analytics,
            }
        )

        logger.info("Successfully generated enhanced timeline for {0}".format(topic))

        # Ensure we have the required fields
        if "topic" not in response_data:
            response_data["topic"] = topic
        if "timeline_span" not in response_data:
            response_data["timeline_span"] = {}
        if "total_events" not in response_data:
            response_data["total_events"] = len(response_data.get("events", []))
        if "events" not in response_data:
            response_data["events"] = []
        if "metadata" not in response_data:
            response_data["metadata"] = {}

        return EventTimelineResponse(
            topic=response_data["topic"],
            timeline_span=response_data["timeline_span"],
            total_events=response_data["total_events"],
            events=response_data["events"],
            visualization_included=include_visualizations,
            visualization_data=response_data.get("visualization"),
            analytics=response_data.get("analytics"),
            export_options=response_data.get("export_options"),
            metadata=response_data["metadata"],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to generate enhanced event timeline: {0}".format(e))
        raise HTTPException(
            status_code=500,
            detail="Internal server error: {0}".format(str(e)),
        )


@router.post(
    "/track",
    response_model=EventTrackingResponse,
    summary="Track Historical Events",
    description="""
    Track and optionally store historical events for a topic.

    This endpoint implements Issue #38 requirements:
    - Track historical events related to a topic
    - Store event timestamps and relationships

    Features:
    - Configurable date ranges and event types
    - Local storage with relationship mapping
    - Event impact scoring and metadata enrichment
    - Batch processing for large event sets
    """,
)
async def track_historical_events(
    request: EventTrackingRequest,
    service: EventTimelineService = Depends(get_event_timeline_service),
    user=Depends(get_optional_auth),
):
    """Track historical events and optionally store them locally."""
    try:
        logger.info(
            "API request for tracking historical events: {0}".format(request.topic)
        )
        start_time = datetime.now()

        # Track historical events
        events = await service.track_historical_events(
            topic=request.topic,
            start_date=request.start_date,
            end_date=request.end_date,
            event_types=request.event_types,
            include_related=request.include_related,
        )

        # Limit events if necessary
        if len(events) > request.max_events:
            events = events[: request.max_events]

        # Persist locally if requested
        storage_result = {"events_stored": 0, "relationships_created": 0, "errors": []}
        if request.persist and events:
            storage_result = await service.store_event_relationships(events)

        # Calculate processing time
        processing_time = (datetime.now() - start_time).total_seconds()

        response = EventTrackingResponse(
            topic=request.topic,
            events_tracked=len(events),
            events_stored=storage_result.get("events_stored", 0),
            relationships_created=storage_result.get("relationships_created", 0),
            processing_time=processing_time,
            errors=storage_result.get("errors", []),
            timeline_id="timeline_{0}_{1}".format(
                request.topic, int(start_time.timestamp())
            ),
        )

        logger.info(
            "Successfully tracked {0} events for {1}".format(len(events), request.topic)
        )
        return response

    except Exception as e:
        logger.error("Failed to track historical events: {0}".format(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to track events: {0}".format(str(e)),
        )


@router.get(
    "/{topic}/visualization",
    response_model=VisualizationResponse,
    summary="Get Timeline Visualization Data",
    description="""
    Generate visualization data for event evolution.

    This endpoint implements Issue #38 requirement:
    - Generate visualizations of event evolution

    Features:
    - Multiple chart types and themes
    - Event clustering and impact analysis
    - Entity involvement charts
    - Interactive visualization data
    """,
)
async def get_timeline_visualization(
    topic: str = Path(..., description="Topic to generate visualization for"),
    start_date: Optional[str] = Query(None, description="Start date filter"),
    end_date: Optional[str] = Query(None, description="End date filter"),
    max_events: int = Query(50, ge=1, le=200, description="Maximum events to include"),
    theme: str = Query("default", description="Visualization theme"),
    chart_type: str = Query("timeline", description="Chart type"),
    service: EventTimelineService = Depends(get_event_timeline_service),
    user=Depends(get_optional_auth),
):
    """Get visualization data for timeline events."""
    try:
        logger.info("API request for timeline visualization: {0}".format(topic))

        # Parse dates
        parsed_start_date = None
        parsed_end_date = None

        if start_date:
            parsed_start_date = datetime.fromisoformat(start_date)
        if end_date:
            parsed_end_date = datetime.fromisoformat(end_date)

        # Get events for visualization
        events = await service.track_historical_events(
            topic=topic, start_date=parsed_start_date, end_date=parsed_end_date
        )

        if len(events) > max_events:
            events = events[:max_events]

        # Generate visualization data
        viz_data = await service.generate_visualization_data(
            topic=topic, events=events, theme=theme, chart_type=chart_type
        )

        response = VisualizationResponse(
            topic=topic,
            visualization_type=chart_type,
            chart_data=viz_data,
            theme=theme,
            generated_at=datetime.now(),
            export_formats=["json", "png", "svg", "html"],
        )

        logger.info("Successfully generated visualization for {0}".format(topic))
        return response

    except Exception as e:
        logger.error("Failed to generate visualization: {0}".format(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to generate visualization: {0}".format(str(e)),
        )


@router.get(
    "/{topic}/export",
    summary="Export Timeline Data",
    description="""
    Export timeline data in various formats.

    Supported formats:
    - json: Structured JSON data
    - csv: Comma-separated values
    - html: Interactive HTML timeline
    """,
)
async def export_timeline_data(
    topic: str = Path(..., description="Topic to export timeline for"),
    format: str = Query("json", description="Export format (json, csv, html)"),
    start_date: Optional[str] = Query(None, description="Start date filter"),
    end_date: Optional[str] = Query(None, description="End date filter"),
    max_events: int = Query(100, ge=1, le=500, description="Maximum events to export"),
    service: EventTimelineService = Depends(get_event_timeline_service),
    user=Depends(get_optional_auth),
):
    """Export timeline data in specified format."""
    try:
        logger.info(
            "API request for timeline export: {0} (format: {1})".format(topic, format)
        )

        # Validate format
        if format not in ["json", "csv", "html"]:
            raise HTTPException(
                status_code=400, detail="Format must be one of: json, csv, html"
            )

        # Parse dates
        parsed_start_date = None
        parsed_end_date = None

        if start_date:
            parsed_start_date = datetime.fromisoformat(start_date)
        if end_date:
            parsed_end_date = datetime.fromisoformat(end_date)

        # Get timeline data
        timeline_data = await service.generate_timeline_api_response(
            topic=topic,
            start_date=parsed_start_date,
            end_date=parsed_end_date,
            max_events=max_events,
            include_visualizations=True,
        )

        # Export based on format
        if format == "json":
            return JSONResponse(
                content=timeline_data,
                headers={
                    "Content-Disposition": f'attachment; filename="{topic}_timeline.json"'
                },
            )

        elif format == "csv":
            # Convert to CSV
            output = io.StringIO()
            writer = csv.writer(output)

            # Write header
            writer.writerow(
                [
                    "Event ID",
                    "Title",
                    "Description",
                    "Timestamp",
                    "Event Type",
                    "Confidence",
                    "Impact Score",
                    "Entities Involved",
                ]
            )

            # Write events
            for event in timeline_data.get("events", []):
                writer.writerow(
                    [
                        event.get("event_id", ""),
                        event.get("title", ""),
                        event.get("description", "")[:100],  # Truncate description
                        event.get("timestamp", ""),
                        event.get("event_type", ""),
                        event.get("confidence", ""),
                        event.get("impact_score", ""),
                        ", ".join(event.get("entities_involved", [])),
                    ]
                )

            csv_content = output.getvalue()
            output.close()

            return Response(
                content=csv_content,
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="{topic}_timeline.csv"'
                },
            )

        elif format == "html":
            # Generate HTML timeline
            html_content = _generate_html_timeline(topic, timeline_data)

            return Response(
                content=html_content,
                media_type="text/html",
                headers={
                    "Content-Disposition": f'attachment; filename="{topic}_timeline.html"'
                },
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to export timeline data: {0}".format(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to export data: {0}".format(str(e)),
        )


@router.get(
    "/analytics",
    summary="Get Timeline Analytics",
    description="""
    Get analytics and insights across all tracked timelines.

    Features:
    - Cross-topic event analysis
    - Trending topics identification
    - Event pattern analysis
    - System usage statistics
    """,
)
async def get_timeline_analytics(
    time_window_days: int = Query(
        30, ge=1, le=365, description="Analysis time window in days"
    ),
    top_topics: int = Query(
        10, ge=1, le=50, description="Number of top topics to include"
    ),
    service: EventTimelineService = Depends(get_event_timeline_service),
    user=Depends(get_optional_auth),
):
    """Get analytics across all timelines."""
    try:
        logger.info("API request for timeline analytics")

        # This would be implemented with actual analytics queries
        # For now, return a basic analytics structure
        analytics = {
            "time_window_days": time_window_days,
            "analysis_date": datetime.now().isoformat(),
            "system_stats": {
                "total_timelines_available": 0,
                "total_events_tracked": 0,
                "active_topics": 0,
                "average_events_per_topic": 0,
            },
            "trending_topics": [],
            "event_patterns": {
                "most_common_event_types": [],
                "peak_activity_periods": [],
                "entity_mentions": [],
            },
            "insights": [
                "Timeline analytics require historical data collection",
                "Implement with actual usage data for meaningful insights",
            ],
        }

        logger.info("Timeline analytics generated")
        return analytics

    except Exception as e:
        logger.error("Failed to generate timeline analytics: {0}".format(e))
        raise HTTPException(
            status_code=500, detail="Failed to generate analytics: {0}".format(str(e))
        )


@router.get(
    "/health",
    summary="Health Check",
    description="Check the health of the event timeline service",
)
async def health_check():
    """Health check endpoint for event timeline service."""
    try:
        # Try to initialize service
        service = await get_event_timeline_service()

        return {
            "status": "healthy",
            "service": "event-timeline-api",
            "timestamp": datetime.now().isoformat(),
            "version": "1.0.0",
            "issue": "#38",
            "components": {
                "service_initialized": service is not None,
                "event_store": service is not None,
                "entity_extractor": (
                    service.entity_extractor is not None if service else False
                ),
            },
        }

    except Exception as e:
        logger.error("Health check failed: {0}".format(e))
        return {
            "status": "unhealthy",
            "service": "event-timeline-api",
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
        }


# Helper functions


# Issue #38: the catch-all GET "/{topic}" is declared before the static
# "/analytics" and "/health" routes, so without this a request to those paths
# would match "/{topic}" (topic="analytics"/"health"). Starlette matches routes
# in list order, so move parameter-free paths ahead of parameterized ones.
router.routes.sort(key=lambda r: "{" in getattr(r, "path", ""))


def _generate_html_timeline(topic: str, timeline_data: Dict[str, Any]) -> str:
    """Generate HTML timeline visualization."""
    html_template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Event Timeline: {topic}</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; }}
            .header {{ background-color: #f8f9fa; padding: 20px; border-radius: 5px; }}
            .timeline {{ margin-top: 20px; }}
            .event {{
                border-left: 3px solid #007bff;
                padding: 10px;
                margin: 10px 0;
                background-color: #f8f9fa;
            }}
            .event-title {{ font-weight: bold; color: #007bff; }}
            .event-meta {{ font-size: 0.9em; color: #666; margin-top: 5px; }}
            canvas {{ max-width: 100%; margin: 20px 0; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>Event Timeline: {topic}</h1>
            <p>Total Events: {timeline_data.get('total_events', 0)}</p>
            <p>Generated: {timeline_data.get('metadata', {}).get('generated_at', 'Unknown')}</p>
        </div>

        <div class="timeline">
            <h2>Events</h2>
    """

    # Add events
    for event in timeline_data.get("events", []):
        html_template += f"""
            <div class="event">
                <div class="event-title">{event.get('title', 'Unknown Event')}</div>
                <div>{event.get('description', '')[:200]}...</div>
                <div class="event-meta">
                    Date: {event.get('timestamp', '')} |
                    Type: {event.get('event_type', 'Unknown')} |
                    Confidence: {event.get('confidence', 0):.2f}
                </div>
            </div>
        """

    html_template += """
        </div>

        <div>
            <h2>Timeline Visualization</h2>
            <canvas id="timelineChart"></canvas>
        </div>

        <script>
            // Add Chart.js visualization here
            const ctx = document.getElementById('timelineChart').getContext('2d');
            const chart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: ['Timeline visualization requires Chart.js implementation'],
                    datasets: [{
                        label: 'Events',
                        data: [1],
                        borderColor: '#007b',
                        backgroundColor: 'rgba(0, 123, 255, 0.1)'
                    }]
                },
                options: {
                    responsive: true,
                    plugins: {
                        title: {
                            display: true,
                            text: 'Event Timeline Chart'
                        }
                    }
                }
            });
        </script>
    </body>
    </html>
    """

    return html_template
