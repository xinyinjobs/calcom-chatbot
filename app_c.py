import os
import json
import time
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:  # pragma: no cover - fallback for older Pythons
    ZoneInfo = None  # type: ignore
try:
    from pytz import timezone as pytz_timezone  # fallback
except Exception:  # pragma: no cover
    pytz_timezone = None  # type: ignore
from typing import Optional, List, Dict, Any
import requests
from openai import OpenAI
import streamlit as st

# Configuration
try:
    OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", "")
except:
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

try:
    CALCOM_API_KEY = st.secrets.get("CALCOM_API_KEY", "")
except:
    CALCOM_API_KEY = os.getenv("CALCOM_API_KEY", "")

CALCOM_BASE_URL = "https://api.cal.com/v2"

# Validate OpenAI API key
if not OPENAI_API_KEY:
    st.error("⚠️ OpenAI API key not configured.")
    st.stop()

client = OpenAI(api_key=OPENAI_API_KEY)

# Timezone utilities
def _get_tz(name: str):
    if ZoneInfo is not None:
        return ZoneInfo(name)
    if pytz_timezone is not None:
        return pytz_timezone(name)
    raise RuntimeError("No timezone implementation available. Install Python 3.9+ or pytz.")


def _localize_naive(dt_naive: datetime, tz) -> datetime:
    # pytz requires localize(); zoneinfo uses tzinfo assignment
    if hasattr(tz, "localize"):
        return tz.localize(dt_naive)  # type: ignore[attr-defined]
    return dt_naive.replace(tzinfo=tz)


def format_time_pst(iso_time: str) -> str:
    """Convert ISO time to readable America/Los_Angeles local time (PST/PDT)."""
    try:
        dt_utc = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        la = _get_tz("America/Los_Angeles")
        dt_la = dt_utc.astimezone(la)
        tz_abbr = dt_la.tzname() or "PT"
        return dt_la.strftime(f"%Y-%m-%d %I:%M %p {tz_abbr}")
    except Exception:
        return iso_time


# Date context helpers
def _get_effective_la_now() -> datetime:
    """Return the current datetime in America/Los_Angeles, with optional override.

    If the environment variable TODAY_OVERRIDE (YYYY-MM-DD) is set, use that date
    at 12:00 (noon) local time to avoid ambiguity around midnight and DST.
    """
    la = _get_tz("America/Los_Angeles")
    override = (os.getenv("TODAY_OVERRIDE") or "").strip()
    if override:
        try:
            base_date = datetime.strptime(override, "%Y-%m-%d")
            # Use noon to mitigate DST boundary edge cases
            base_noon = base_date.replace(hour=12, minute=0, second=0, microsecond=0)
            return _localize_naive(base_noon, la)
        except Exception:
            pass
    return datetime.now(la)


def _build_runtime_date_context() -> str:
    """Construct a concise runtime date/time context for the model."""
    la_now = _get_effective_la_now()
    utc = _get_tz("UTC")
    utc_now = la_now.astimezone(utc)
    tz_abbr = la_now.tzname() or "PT"
    today_line = f"Today's date is {la_now.strftime('%Y-%m-%d')} (America/Los_Angeles, {tz_abbr})."
    time_line = (
        f"Current time: LA {la_now.strftime('%Y-%m-%d %H:%M')} {tz_abbr} | "
        f"UTC {utc_now.strftime('%Y-%m-%d %H:%M')} UTC"
    )
    return today_line + "\n" + time_line + "\nAlways interpret relative dates from this context."

def get_booking_status(booking: Dict[str, Any]) -> tuple[str, str, str]:
    """
    Determine booking status and return (status, emoji, color).
    
    Returns:
        tuple: (status_text, emoji, streamlit_color)
    """
    from datetime import datetime
    
    # Check explicit status field first
    status = str(booking.get("status", "")).lower()
    
    if status == "cancelled":
        return ("Cancelled", "❌", "red")
    elif status == "rescheduled":
        return ("Rescheduled", "🔄", "orange")
    elif status == "pending":
        return ("Pending", "⏳", "yellow")
    elif status == "accepted" or status == "confirmed":
        # Check if it's in the past or future
        pass  # Continue to time-based check
    
    # Time-based status determination
    start_time = booking.get("start") or booking.get("startTime")
    if start_time:
        try:
            # Parse ISO time
            dt_utc = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            now_utc = datetime.now(dt_utc.tzinfo)
            
            if dt_utc < now_utc:
                return ("Past", "📋", "gray")
            else:
                # Check how soon it is
                time_until = dt_utc - now_utc
                hours_until = time_until.total_seconds() / 3600
                
                if hours_until < 24:
                    return ("Today", "🔥", "red")
                elif hours_until < 48:
                    return ("Tomorrow", "⏰", "orange")
                elif hours_until < 168:  # 7 days
                    return ("This Week", "📅", "blue")
                else:
                    return ("Upcoming", "✅", "green")
        except Exception:
            pass
    
    # Default status
    return ("Scheduled", "📌", "blue")

# Cal.com API Class
class CalComAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "cal-api-version": "2024-08-13"
        }
        self.request_cache = {}  # Simple cache to prevent duplicate requests
        self.error_log = []  # Track errors for debugging
    
    def _log_error(self, operation: str, error: str, details: Dict[str, Any] = None):
        """Log errors for debugging purposes"""
        error_entry = {
            "timestamp": datetime.now().isoformat(),
            "operation": operation,
            "error": error,
            "details": details or {}
        }
        self.error_log.append(error_entry)
        
        # Keep only last 50 errors to prevent memory issues
        if len(self.error_log) > 50:
            self.error_log = self.error_log[-50:]
    
    def get_error_log(self) -> List[Dict[str, Any]]:
        """Get recent error log for debugging"""
        return self.error_log[-10:]  # Return last 10 errors
    
    def _make_request_with_retry(self, method: str, url: str, max_retries: int = 3, 
                                retry_delay: float = 1.0, **kwargs) -> requests.Response:
        """Make HTTP request with exponential backoff retry logic"""
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                # Add cache key for GET requests to prevent duplicates
                cache_key = None
                if method.upper() == "GET":
                    cache_key = f"{method}:{url}:{str(kwargs.get('params', {}))}"
                    if cache_key in self.request_cache:
                        cached_response, timestamp = self.request_cache[cache_key]
                        # Use cache if less than 30 seconds old
                        if time.time() - timestamp < 30:
                            st.sidebar.info(f"🔄 Using cached response for {url}")
                            return cached_response
                
                st.sidebar.info(f"🔄 Attempt {attempt + 1}/{max_retries} for {method} {url}")
                
                response = requests.request(method, url, **kwargs)
                
                # Cache successful GET responses
                if cache_key and response.status_code < 400:
                    self.request_cache[cache_key] = (response, time.time())
                
                # If successful or client error (4xx), return immediately
                if response.status_code < 500:
                    return response
                    
                # For server errors (5xx), retry
                if attempt < max_retries - 1:
                    delay = retry_delay * (2 ** attempt)  # Exponential backoff
                    st.sidebar.warning(f"⚠️ Server error {response.status_code}, retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    return response
                    
            except requests.exceptions.RequestException as e:
                last_exception = e
                if attempt < max_retries - 1:
                    delay = retry_delay * (2 ** attempt)
                    st.sidebar.warning(f"⚠️ Request failed: {str(e)}, retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    raise e
        
        # This should never be reached, but just in case
        if last_exception:
            raise last_exception
        return response

    def validate_booking_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Validate booking payload before sending to API"""
        errors = []
        warnings = []
        
        # Check required fields
        if not payload.get("eventTypeId"):
            errors.append("eventTypeId is required")
        elif not isinstance(payload["eventTypeId"], (int, str)):
            errors.append("eventTypeId must be a number or string")
            
        if not payload.get("start"):
            errors.append("start time is required")
        else:
            # Validate ISO format
            try:
                datetime.fromisoformat(payload["start"].replace("Z", "+00:00"))
            except:
                errors.append("start time must be in ISO format (YYYY-MM-DDTHH:MM:SSZ)")
        
        # Check attendee info
        attendee = payload.get("attendee", {})
        if not attendee.get("email"):
            errors.append("attendee email is required")
        elif "@" not in attendee["email"]:
            errors.append("attendee email must be valid")
            
        if not attendee.get("name"):
            errors.append("attendee name is required")
            
        # Check timezone
        if attendee.get("timeZone") and attendee["timeZone"] not in ["America/Los_Angeles", "America/New_York", "UTC"]:
            warnings.append(f"Timezone {attendee['timeZone']} might not be supported by Cal.com")
        
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings
        }

    def get_event_types(self) -> Dict[str, Any]:
        """Get available event types"""
        try:
            st.sidebar.info("📤 Fetching event types...")
            
            # Try v2 API first with Bearer token and retry logic
            try:
                st.sidebar.info("🔄 Trying Cal.com v2 API for event types...")
                response = self._make_request_with_retry(
                    "GET",
                    "https://api.cal.com/v2/event-types",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "cal-api-version": "2024-08-13"
                    },
                    timeout=15,  # Increased timeout
                    max_retries=2
                )
                
                st.sidebar.info(f"📥 V2 response status: {response.status_code}")
                
                if response.status_code >= 400:
                    st.sidebar.warning(f"V2 failed ({response.status_code}): {response.text[:200]}")
                    raise requests.exceptions.HTTPError(f"V2 API returned {response.status_code}")
                    
            except Exception as v2_error:
                st.sidebar.warning(f"V2 API failed: {str(v2_error)}, trying V1 API...")
                response = self._make_request_with_retry(
                    "GET",
                    f"https://api.cal.com/v1/event-types?apiKey={self.api_key}",
                    headers={"Content-Type": "application/json"},
                    timeout=15,  # Increased timeout
                    max_retries=2
                )
                st.sidebar.info(f"📥 V1 response status: {response.status_code}")
            
            # Check for errors before processing
            if response.status_code >= 400:
                error_details = {
                    "status_code": response.status_code,
                    "response_text": response.text,
                    "api_version": "v2" if "v2" in response.url else "v1"
                }
                st.sidebar.error(f"❌ Event types fetch failed with status {response.status_code}")
                st.sidebar.code(json.dumps(error_details, indent=2), language="json")
                # Log and return failure
                self._log_error("get_event_types", f"API returned {response.status_code}", error_details)
                return {
                    "success": False,
                    "error": f"Failed to fetch event types: {response.text}",
                    "error_details": error_details,
                    "event_types": []
                }
            
            # Success: parse response
            response.raise_for_status()
            data = response.json()
            
            # Log raw response for debugging
            st.sidebar.code(f"Event types raw response:\n{json.dumps(data, indent=2)[:800]}", language="json")
            
            # Parse event types from different response structures
            event_types = []
            
            # Structure 1: {data: [...]}
            if isinstance(data, dict) and "data" in data:
                if isinstance(data["data"], list):
                    event_types = data["data"]
                elif isinstance(data["data"], dict):
                    inner = data["data"]
                    # Common nested keys
                    for key in ("event_types", "eventTypes", "items"):
                        if key in inner and isinstance(inner[key], list):
                            event_types = inner[key]
                            break
            # Structure 2: {event_types: [...]}
            if not event_types and isinstance(data, dict) and "event_types" in data:
                event_types = data["event_types"] if isinstance(data["event_types"], list) else []
            # Structure 3: {eventTypes: [...]}
            if not event_types and isinstance(data, dict) and "eventTypes" in data:
                event_types = data["eventTypes"] if isinstance(data["eventTypes"], list) else []
            # Structure 4: direct array
            if not event_types and isinstance(data, list):
                event_types = data
            # Structure 5: fallback - find first list of dicts
            if not event_types and isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        event_types = v
                        break

            if event_types:
                st.sidebar.success(f"✅ Found {len(event_types)} event types")
                for et in event_types[:3]:
                    st.sidebar.info(f"Event Type: {et.get('title')} (ID: {et.get('id')})")
            else:
                st.sidebar.warning("⚠️ No event types found. Configure them in Cal.com first.")

            return {
                "success": True, 
                "event_types": event_types,
                "api_version": "v2" if "v2" in response.url else "v1"
            }
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to fetch event types: {str(e)}"
            error_details = {}
            if hasattr(e, "response") and e.response is not None:
                error_details = {
                    "status_code": e.response.status_code,
                    "response_text": e.response.text
                }
                error_msg += f"\nStatus: {e.response.status_code}\nResponse: {e.response.text}"
            st.sidebar.error(error_msg)
            # Log the error
            self._log_error("get_event_types", error_msg, error_details)
            
            return {
                "success": False, 
                "error": error_msg, 
                "error_details": error_details,
                "event_types": []
            }

    def get_available_slots(self, event_type_id: Any, start_date: str, end_date: str) -> Dict[str, Any]:
        """
        Get available time slots. 
        
        Args:
            event_type_id: Event type ID
            start_date: ISO UTC string (e.g., "2024-10-16T00:00:00Z" or simple "2024-10-16")
            end_date: ISO UTC string (e.g., "2024-10-17T23:59:59Z" or simple "2024-10-17")
        
        Returns:
            Dict with success status and slots list
        """
        try:
            st.sidebar.info(f"🔍 Checking slots for event type {event_type_id}")
            st.sidebar.info(f"Date range: {start_date} to {end_date}")
            
            # Clean up date strings - v2 API can accept simple dates like "2024-10-16"
            # or full ISO strings like "2024-10-16T00:00:00Z"
            def normalize_date(date_str: str) -> str:
                """Normalize date to simple YYYY-MM-DD format for v2 API"""
                try:
                    # If it's already a simple date, return as-is
                    if len(date_str) == 10 and date_str.count('-') == 2:
                        return date_str
                    # If it's an ISO string, extract just the date part
                    if 'T' in date_str:
                        return date_str.split('T')[0]
                    return date_str
                except:
                    return date_str
            
            # Try v2 API first - CORRECT endpoint is /v2/slots (not /v2/slots/available)
            try:
                st.sidebar.info("🔄 Trying Cal.com v2 API for slots...")
                
                # Normalize dates for v2 API
                start_simple = normalize_date(start_date)
                end_simple = normalize_date(end_date)
                
                st.sidebar.info(f"📅 Using dates: start={start_simple}, end={end_simple}")
                
                # CRITICAL FIX: v2 API uses different parameter names!
                # - Endpoint: /v2/slots (NOT /v2/slots/available)
                # - Parameters: start, end (NOT startTime, endTime)
                response = self._make_request_with_retry(
                    "GET",
                    "https://api.cal.com/v2/slots",  # ✅ CORRECT: /v2/slots
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "cal-api-version": "2024-08-13",
                    },
                    params={
                        "eventTypeId": event_type_id,
                        "start": start_simple,  # ✅ CORRECT: 'start' not 'startTime'
                        "end": end_simple,      # ✅ CORRECT: 'end' not 'endTime'
                        "timeZone": "America/Los_Angeles",
                    },
                    timeout=15,
                    max_retries=2
                )
                st.sidebar.info(f"V2 API Status: {response.status_code}")
                
                # Don't retry on client errors
                if 400 <= response.status_code < 500:
                    error_text = response.text[:500]
                    st.sidebar.error(f"V2 API client error ({response.status_code}): {error_text}")
                    
                    # Provide helpful error messages
                    if response.status_code == 404:
                        suggestion = "Event type not found. Check that the event type ID is correct and you have access to it."
                    elif response.status_code == 401:
                        suggestion = "Authentication failed. Check that your API key is valid."
                    elif response.status_code == 403:
                        suggestion = "Access denied. Your API key may not have permission to access this event type."
                    else:
                        suggestion = "Check the error message above for details."
                    
                    raise requests.exceptions.HTTPError(
                        f"V2 API returned {response.status_code}: {suggestion}"
                    )
                
                if response.status_code >= 500:
                    st.sidebar.warning(f"V2 API server error ({response.status_code}): {response.text[:200]}")
                    raise requests.exceptions.HTTPError(f"V2 API returned {response.status_code}")
                    
            except Exception as v2_error:
                st.sidebar.warning(f"V2 API failed: {str(v2_error)}, trying v1...")
                
                # Fallback to v1 API with full ISO timestamps
                # v1 uses startTime/endTime parameters
                response = self._make_request_with_retry(
                    "GET",
                    f"https://api.cal.com/v1/slots",
                    headers={
                        "Content-Type": "application/json"
                    },
                    params={
                        "apiKey": self.api_key,
                        "eventTypeId": event_type_id,
                        "startTime": start_date,  # v1 uses 'startTime'
                        "endTime": end_date,      # v1 uses 'endTime'
                        "timeZone": "America/Los_Angeles",
                    },
                    timeout=15,
                    max_retries=2
                )
                st.sidebar.info(f"V1 API Status: {response.status_code}")
            
            # Check for errors before processing
            if response.status_code >= 400:
                error_details = {
                    "status_code": response.status_code,
                    "response_text": response.text,
                    "event_type_id": event_type_id,
                    "date_range": f"{start_date} to {end_date}",
                    "api_version": "v2" if "v2" in response.url else "v1"
                }
                st.sidebar.error(f"❌ Slot check failed with status {response.status_code}")
                st.sidebar.code(json.dumps(error_details, indent=2), language="json")
                
                return {
                    "success": False, 
                    "error": f"Failed to fetch slots: {response.text}",
                    "error_details": error_details,
                    "slots": [],
                    "suggestion": "Check that your Cal.com event type has availability configured and the date is within your availability window"
                }
            
            response.raise_for_status()
            data = response.json()
            
            st.sidebar.code(f"Raw response: {json.dumps(data, indent=2)[:1000]}", language="json")
            
            # Parse slots from response - handle multiple formats
            slots: List[str] = []
            
            # CRITICAL: v2 API has DIFFERENT response structure than v1!
            # v2 structure: {"status": "success", "data": {"2024-10-16": [{"start": "..."}], "2024-10-17": [...]}}
            # v1 structure: {"slots": {"2024-10-16": [{"time": "..."}]}}
            
            is_v2 = "v2" in response.url
            
            if is_v2:
                # V2 API response structure
                st.sidebar.info("📋 Parsing v2 API response structure")
                
                if isinstance(data, dict) and data.get("status") == "success":
                    slots_data = data.get("data", {})
                    
                    if isinstance(slots_data, dict):
                        # Iterate through each date key
                        for date_key, date_slots in slots_data.items():
                            if isinstance(date_slots, list):
                                for slot in date_slots:
                                    if isinstance(slot, dict):
                                        # v2 uses "start" key
                                        start_time = slot.get("start") or slot.get("time")
                                        if start_time and isinstance(start_time, str):
                                            slots.append(start_time)
                                    elif isinstance(slot, str):
                                        slots.append(slot)
                else:
                    st.sidebar.warning("⚠️ Unexpected v2 response format")
                    # Try generic parsing as fallback
                    def walk_v2(obj: Any):
                        if isinstance(obj, dict):
                            for key in ("start", "time", "startTime"):
                                if key in obj and isinstance(obj[key], str):
                                    slots.append(obj[key])
                            for v in obj.values():
                                walk_v2(v)
                        elif isinstance(obj, list):
                            for item in obj:
                                walk_v2(item)
                    
                    walk_v2(data)
            else:
                # V1 API response structure
                st.sidebar.info("📋 Parsing v1 API response structure")
                
                if isinstance(data, dict) and "slots" in data:
                    slots_data = data["slots"]
                    
                    if isinstance(slots_data, dict):
                        # Iterate through each date key
                        for date_key, date_slots in slots_data.items():
                            if isinstance(date_slots, list):
                                for slot in date_slots:
                                    if isinstance(slot, dict):
                                        # v1 uses "time" key
                                        time_value = slot.get("time")
                                        if time_value and isinstance(time_value, str):
                                            slots.append(time_value)
                                    elif isinstance(slot, str):
                                        slots.append(slot)
                else:
                    st.sidebar.warning("⚠️ Unexpected v1 response format")
                    # Generic fallback
                    def walk_v1(obj: Any):
                        if isinstance(obj, dict):
                            for key in ("time", "start", "startTime"):
                                if key in obj and isinstance(obj[key], str):
                                    slots.append(obj[key])
                            for v in obj.values():
                                walk_v1(v)
                        elif isinstance(obj, list):
                            for item in obj:
                                walk_v1(item)
                    
                    walk_v1(data)
            
            # Remove duplicates while preserving order
            slots = list(dict.fromkeys(slots))
            
            st.sidebar.success(f"📅 Found {len(slots)} available slots")
            if slots:
                st.sidebar.info(f"First slot example: {slots[0]}")
                if len(slots) > 1:
                    st.sidebar.info(f"Last slot example: {slots[-1]}")
            else:
                st.sidebar.warning("⚠️ No slots found. This could mean:")
                st.sidebar.info("1. No availability configured for this date")
                st.sidebar.info("2. All slots are already booked")
                st.sidebar.info("3. Date is outside your availability window")
            
            return {
                "success": True, 
                "slots": slots, 
                "count": len(slots),
                "event_type_id": event_type_id,
                "api_version": "v2" if is_v2 else "v1"
            }
            
        except requests.exceptions.RequestException as e:
            error_msg = str(e)
            error_details = {}
            if hasattr(e, "response") and e.response is not None:
                error_details = {
                    "status_code": e.response.status_code,
                    "response_text": e.response.text,
                    "event_type_id": event_type_id,
                    "date_range": f"{start_date} to {end_date}"
                }
                error_msg += f" | Status: {e.response.status_code} | Response: {e.response.text[:500]}"
            
            st.sidebar.error(f"❌ Slot check failed: {error_msg}")
            st.sidebar.warning("💡 Tip: Check that your Cal.com event type has availability configured")
            
            # Return error with helpful message
            return {
                "success": False, 
                "error": error_msg,
                "error_details": error_details,
                "slots": [],
                "suggestion": "Make sure your Cal.com event type has availability hours set up and the date is within your availability window"
            }
        
    def create_booking(
        self,
        event_type_id: Any,
        start_time: str,
        attendee_email: str,
        attendee_name: str,
        attendee_timezone: str = "America/Los_Angeles",
        attendee_language: str = "en",
        meeting_reason: str = ""
    ) -> Dict[str, Any]:
        """Create a new booking with deduplication"""
        try:
            # Create a unique key for this booking request to prevent duplicates
            booking_key = f"{event_type_id}:{start_time}:{attendee_email}"
            
            # Check if we're already processing this booking
            if hasattr(self, '_processing_bookings'):
                if booking_key in self._processing_bookings:
                    return {
                        "success": False,
                        "error": "This booking is already being processed. Please wait...",
                        "duplicate_request": True
                    }
            else:
                self._processing_bookings = set()
            
            # Mark this booking as being processed
            self._processing_bookings.add(booking_key)
            
            try:
                payload = {
                    "eventTypeId": event_type_id,
                    "start": start_time,
                    "attendee": {
                        "name": attendee_name,
                        "email": attendee_email,
                        "timeZone": attendee_timezone,
                        "language": attendee_language
                    }
                }

                if meeting_reason:
                    payload["metadata"] = {"reason": meeting_reason}
            
            except Exception as e:
                st.sidebar.error(f"❌ Failed to build booking payload: {str(e)}")
                return {"success": False, "error": f"Payload construction failed: {str(e)}"}

            # Validate payload before sending
            validation = self.validate_booking_payload(payload)
            if not validation["valid"]:
                error_msg = f"❌ Invalid booking payload: {', '.join(validation['errors'])}"
                st.sidebar.error(error_msg)
                return {"success": False, "error": error_msg, "validation_errors": validation["errors"]}
            
            if validation["warnings"]:
                for warning in validation["warnings"]:
                    st.sidebar.warning(f"⚠️ {warning}")

            st.sidebar.info("📤 Creating booking...")
            st.sidebar.code(json.dumps(payload, indent=2), language="json")
            st.sidebar.info(f"🌍 Timezone: {attendee_timezone} (PDT) | 🗣️ Language: {attendee_language}")

            # Try v2 API first with proper headers and retry logic
            try:
                st.sidebar.info("🔄 Trying Cal.com v2 API...")
                response = self._make_request_with_retry(
                    "POST",
                    f"https://api.cal.com/v2/bookings",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "cal-api-version": "2024-08-13"
                    },
                    json=payload,
                    timeout=20,  # Increased timeout for booking creation
                    max_retries=3  # More retries for critical booking operations
                )
                st.sidebar.info(f"📥 V2 Response: {response.status_code}")
                
                if response.status_code >= 400:
                    st.sidebar.warning(f"V2 API failed ({response.status_code}): {response.text[:200]}")
                    raise requests.exceptions.HTTPError(f"V2 API returned {response.status_code}")
                    
            except Exception as v2_error:
                st.sidebar.warning(f"V2 API failed: {str(v2_error)}, trying v1...")
                
                # Fallback to v1 API with proper headers and retry logic
                response = self._make_request_with_retry(
                    "POST",
                    f"https://api.cal.com/v1/bookings?apiKey={self.api_key}",
                    headers={
                        "Content-Type": "application/json"
                    },
                    json=payload,
                    timeout=20,  # Increased timeout
                    max_retries=3  # More retries for critical operations
                )
                st.sidebar.info(f"📥 V1 Response: {response.status_code}")
            
            # Detailed error logging with better error parsing
            if response.status_code >= 400:
                error_details = {
                    "status_code": response.status_code,
                    "response_text": response.text,
                    "request_payload": payload,
                    "api_version": "v2" if "v2" in response.url else "v1",
                    "timestamp": datetime.now().isoformat()
                }
                st.sidebar.error(f"❌ Booking failed with status {response.status_code}")
                st.sidebar.code(json.dumps(error_details, indent=2), language="json")
                
                # Enhanced error message parsing
                error_message = "Unknown error"
                try:
                    error_data = response.json()
                    
                    # Try multiple error message locations
                    if isinstance(error_data, dict):
                        error_message = (error_data.get("message") or 
                                       error_data.get("error") or 
                                       error_data.get("errorMessage") or 
                                       "Unknown error")
                        
                        # Check nested data structure
                        if "data" in error_data and isinstance(error_data["data"], dict):
                            nested_error = (error_data["data"].get("message") or 
                                          error_data["data"].get("error") or 
                                          error_data["data"].get("errorMessage"))
                            if nested_error:
                                error_message = nested_error
                        
                        # Check for validation errors
                        if "errors" in error_data and isinstance(error_data["errors"], list):
                            validation_errors = [str(err) for err in error_data["errors"]]
                            error_message = f"Validation errors: {', '.join(validation_errors)}"
                            
                except Exception as parse_error:
                    st.sidebar.warning(f"Could not parse error response: {parse_error}")
                    error_message = response.text[:500]  # Truncate long responses
                
                # Add specific error handling for common issues
                if response.status_code == 409:
                    error_message = "Time slot is no longer available. Please try a different time."
                elif response.status_code == 422:
                    error_message = "Invalid booking data. Please check your meeting details."
                elif response.status_code == 429:
                    error_message = "Too many requests. Please wait a moment and try again."
                elif response.status_code >= 500:
                    error_message = "Cal.com server error. Please try again in a few minutes."
                
                # Log the error for debugging
                self._log_error("create_booking", error_message, error_details)
                
                return {
                    "success": False, 
                    "error": f"Booking failed: {error_message}",
                    "error_details": error_details,
                    "status_code": response.status_code,
                    "retry_suggested": response.status_code >= 500
                }
            
            response.raise_for_status()
            result = response.json()
            
            # Handle different response structures
            try:
                booking_data = result.get("data", {})
                if not booking_data and isinstance(result, dict):
                    # Sometimes the booking data is directly in the response
                    booking_data = result

                    st.sidebar.success(f"✅ Booking created! ID: {booking_data.get('id')}, UID: {booking_data.get('uid')}")
                    return {
                        "success": True, 
                        "data": booking_data, 
                        "booking_id": booking_data.get("id"), 
                        "booking_uid": booking_data.get("uid"),
                        "api_version": "v2" if "v2" in response.url else "v1"
                    }
            except Exception as e:
                st.sidebar.error(f"❌ Failed to handle booking response: {e}")
                return {"success": False, "error": str(e)}

            finally:
                # Always remove from processing set
                if hasattr(self, '_processing_bookings') and booking_key in self._processing_bookings:
                    self._processing_bookings.remove(booking_key)
                    
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to create booking: {str(e)}"
            error_details = {}
            if hasattr(e, "response") and e.response is not None:
                error_details = {
                    "status_code": e.response.status_code,
                    "response_text": e.response.text,
                    "request_payload": payload if 'payload' in locals() else {}
                }
                error_msg += f"\nStatus: {e.response.status_code}\nResponse: {e.response.text}"
            st.sidebar.error(error_msg)
            
            # Clean up processing set on error
            if hasattr(self, '_processing_bookings') and 'booking_key' in locals() and booking_key in self._processing_bookings:
                self._processing_bookings.remove(booking_key)
                
            return {
                "success": False, 
                "error": error_msg,
                "error_details": error_details
            }

    def get_bookings(self, attendee_email: Optional[str] = None, attendee_name: Optional[str] = None) -> Dict[str, Any]:
        """Get bookings with optional filtering by attendee email or name, and include useful links."""
        try:
            params = {}
            if attendee_email:
                params["attendeeEmail"] = attendee_email

            st.sidebar.info(f"📤 Fetching bookings (filters: {params if params else 'none'})")

            # Try v2 API first with retry/backoff
            try:
                response = self._make_request_with_retry(
                    "GET",
                    "https://api.cal.com/v2/bookings",
                    headers=self.headers,
                    params=params,
                    timeout=15,
                    max_retries=2,
                )
                st.sidebar.info(f"📥 V2 Bookings Response: {response.status_code}")
                if response.status_code >= 400:
                    raise requests.exceptions.HTTPError(f"V2 API returned {response.status_code}")
            except Exception as v2_error:
                st.sidebar.warning(f"V2 bookings failed ({v2_error}), trying v1...")
                response = self._make_request_with_retry(
                    "GET",
                    f"https://api.cal.com/v1/bookings",
                    headers={"Content-Type": "application/json"},
                    params={**params, "apiKey": self.api_key},
                    timeout=15,
                    max_retries=2,
                )
                st.sidebar.info(f"📥 V1 Bookings Response: {response.status_code}")

            response.raise_for_status()
            raw = response.json()

            # Parse flexible shapes
            bookings: List[Dict[str, Any]] = []
            if isinstance(raw, list):
                bookings = raw
            elif isinstance(raw, dict):
                if isinstance(raw.get("data"), list):
                    bookings = raw.get("data", [])
                elif isinstance(raw.get("data"), dict) and isinstance(raw["data"].get("bookings"), list):
                    bookings = raw["data"].get("bookings", [])
                elif isinstance(raw.get("bookings"), list):
                    bookings = raw.get("bookings", [])
                else:
                    # Last resort: look for an array value in dict
                    for v in raw.values():
                        if isinstance(v, list) and v and isinstance(v[0], dict):
                            bookings = v
                            break

            # Optional client-side filtering by attendee name/email
            def booking_matches_attendee(b: Dict[str, Any]) -> bool:
                if not attendee_email and not attendee_name:
                    return True
                possible_attendees = b.get("attendees") or b.get("attendee") or []
                if isinstance(possible_attendees, dict):
                    possible_attendees = [possible_attendees]
                for a in possible_attendees:
                    a_email = str(a.get("email", "")).lower()
                    a_name = str(a.get("name", "")).lower()
                    if attendee_email and a_email == attendee_email.lower():
                        return True
                    if attendee_name:
                        name_q = attendee_name.lower()
                        if a_name == name_q or name_q in a_name:
                            return True
                return False

            bookings = [b for b in bookings if booking_matches_attendee(b)]

            # Enrich with friendly fields and collect useful links
            def first_url_from(
                booking_dict: Dict[str, Any],
                prefer: List[str],
                allow_generic_fallback: bool = False,
            ) -> Optional[str]:
                """Find a URL by preferred key names.

                - Checks keys case-insensitively at top level first
                - Then checks common nested containers like 'links', 'urls', etc.
                - Optionally falls back to the first generic '*url*'/'*link*' it can find
                """

                def find_preferred(d: Dict[str, Any]) -> Optional[str]:
                    if not isinstance(d, dict):
                        return None
                    lower_map: Dict[str, Any] = {str(k).lower(): v for k, v in d.items()}
                    for key in prefer:
                        v = lower_map.get(key.lower())
                        if isinstance(v, str) and v.startswith("http"):
                            return v
                    return None

                # Try preferred keys at top level
                url = find_preferred(booking_dict)
                if url:
                    return url

                # Try common containers
                for container_key in ["links", "link", "urls", "url", "data"]:
                    nested = booking_dict.get(container_key)
                    if isinstance(nested, dict):
                        url = find_preferred(nested)
                        if url:
                            return url

                # Try shallow nested dicts
                for _k, _v in booking_dict.items():
                    if isinstance(_v, dict):
                        url = find_preferred(_v)
                        if url:
                            return url

                if allow_generic_fallback:
                    # Generic fallback at top level
                    for key, val in booking_dict.items():
                        if isinstance(val, str):
                            kl = str(key).lower()
                            if ("url" in kl or "link" in kl) and val.startswith("http"):
                                return val
                    # Generic fallback inside shallow dicts
                    for _k, _v in booking_dict.items():
                        if isinstance(_v, dict):
                            for k2, v2 in _v.items():
                                if isinstance(v2, str):
                                    kl2 = str(k2).lower()
                                    if ("url" in kl2 or "link" in kl2) and v2.startswith("http"):
                                        return v2

                return None

            for booking in bookings:
                # Times and UID
                if "start" in booking:
                    booking["start_pst"] = format_time_pst(booking["start"])
                booking["display_uid"] = booking.get("uid") or booking.get("id", "N/A")

                # Attendee primary
                attendees = booking.get("attendees") or booking.get("attendee") or []
                if isinstance(attendees, dict):
                    attendees = [attendees]
                primary = None
                if attendee_email:
                    for a in attendees:
                        if str(a.get("email", "")).lower() == attendee_email.lower():
                            primary = a
                            break
                if primary is None and attendees:
                    primary = attendees[0]
                if isinstance(primary, dict):
                    booking["primary_attendee_email"] = primary.get("email")
                    booking["primary_attendee_name"] = primary.get("name")

                # Links
                booking["booking_url"] = first_url_from(
                    booking,
                    prefer=[
                        "meetingUrl",
                        "joinUrl",
                        "join_url",
                        "bookingUrl",
                        "eventPageUrl",
                        "statusPageUrl",
                        "booking_url",
                        "meeting_link",
                    ],
                    allow_generic_fallback=False,
                )
                # For reschedule and cancel, do NOT fall back to generic URLs — only explicit links
                booking["reschedule_url"] = first_url_from(
                    booking,
                    prefer=["rescheduleUrl", "rescheduleLink", "reschedule"],
                    allow_generic_fallback=False,
                )
                booking["cancel_url"] = first_url_from(
                    booking,
                    prefer=["cancelUrl", "cancelLink", "cancel"],
                    allow_generic_fallback=False,
                )

            st.sidebar.success(f"✅ Found {len(bookings)} booking(s)")
            return {"success": True, "bookings": bookings, "count": len(bookings)}
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to get bookings: {str(e)}"
            if hasattr(e, "response") and e.response is not None:
                error_msg += f"\nStatus: {e.response.status_code}\nResponse: {e.response.text}"
            st.sidebar.error(error_msg)
            return {"success": False, "error": error_msg, "bookings": []}

    def _resolve_booking_uid(self, booking_uid: Optional[str] = None, booking_id: Optional[str] = None) -> Optional[str]:
        """Resolve a booking UID from either a provided UID or a booking ID.

        Tries v2/v1 direct fetch by ID, then scans current bookings as a fallback.
        Returns a UID string if found; otherwise None.
        """
        # If caller already provided a UID-looking token, prefer it
        if booking_uid:
            return str(booking_uid)

        if not booking_id:
            return None

        bid = str(booking_id)

        # Try v2 direct fetch
        try:
            resp = self._make_request_with_retry(
                "GET",
                f"https://api.cal.com/v2/bookings/{bid}",
                headers=self.headers,
                timeout=15,
                max_retries=2,
            )
            if resp.status_code < 400:
                data = resp.json()
                node = data.get("data", data) if isinstance(data, dict) else {}
                if isinstance(node, dict):
                    uid_val = node.get("uid") or node.get("bookingUid")
                    if uid_val:
                        return str(uid_val)
        except Exception:
            pass

        # Try v1 direct fetch
        try:
            resp = self._make_request_with_retry(
                "GET",
                f"https://api.cal.com/v1/bookings/{bid}",
                headers={"Content-Type": "application/json"},
                params={"apiKey": self.api_key},
                timeout=15,
                max_retries=2,
            )
            if resp.status_code < 400:
                data = resp.json()
                node = data.get("data", data) if isinstance(data, dict) else {}
                if isinstance(node, dict):
                    uid_val = node.get("uid") or node.get("bookingUid")
                    if uid_val:
                        return str(uid_val)
        except Exception:
            pass

        # Fallback: scan list of bookings for matching id
        try:
            all_bookings_resp = self.get_bookings()
            for b in all_bookings_resp.get("bookings", []):
                if str(b.get("id")) == bid and b.get("uid"):
                    return str(b.get("uid"))
        except Exception:
            pass

        return None

    def cancel_booking(self, booking_uid: Optional[str] = None, booking_id: Optional[str] = None, reason: str = "Cancelled by user") -> Dict[str, Any]:
        """Cancel a booking by UID or ID with v2-first strategy and v1 fallback."""
        try:
            resolved_uid = self._resolve_booking_uid(booking_uid, booking_id)
            path_token = resolved_uid or (booking_uid or booking_id)
            st.sidebar.info(f"📤 Cancelling booking: token={path_token}")
    
            # Try Cal.com v2 first (preferred)
            try:
                st.sidebar.info("🔄 Trying Cal.com v2 API for cancellation...")
                response = self._make_request_with_retry(
                    "POST",
                    f"https://api.cal.com/v2/bookings/{path_token}/cancel",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "cal-api-version": "2024-08-13",
                    },
                    json={
                        "cancellationReason": reason,  # ✅ Only use cancellationReason for v2
                    },
                    timeout=15,
                    max_retries=2,
                )
                st.sidebar.info(f"📥 V2 cancel status: {response.status_code}")
    
                # Don't retry on 4xx errors - these are client errors
                if 400 <= response.status_code < 500:
                    error_details = {
                        "status_code": response.status_code,
                        "response_text": response.text,
                        "booking_uid": resolved_uid,
                        "requested_token": path_token,
                        "api_version": "v2",
                    }
                    st.sidebar.error(f"❌ Client error {response.status_code}: {response.text[:200]}")
                    self._log_error("cancel_booking", "Client error - check booking UID", error_details)
                    return {
                        "success": False,
                        "error": f"Cannot cancel booking: {response.text[:500]}",
                        "error_details": error_details,
                        "suggestion": "Check that the booking UID is correct and the booking exists"
                    }
    
                if response.status_code >= 500:
                    raise requests.exceptions.HTTPError(f"V2 API returned {response.status_code}")
                    
            except requests.exceptions.HTTPError as v2_error:
                st.sidebar.warning(f"V2 cancel failed ({str(v2_error)}), trying v1...")
                # Fallback to Cal.com v1
                response = self._make_request_with_retry(
                    "DELETE",  # ✅ V1 uses DELETE, not POST
                    f"https://api.cal.com/v1/bookings/{path_token}",
                    headers={"Content-Type": "application/json"},
                    params={
                        "apiKey": self.api_key,
                        "cancellationReason": reason
                    },
                    timeout=15,
                    max_retries=2,
                )
                st.sidebar.info(f"📥 V1 cancel status: {response.status_code}")
    
            # Handle error responses with richer details
            if response.status_code >= 400:
                error_details = {
                    "status_code": response.status_code,
                    "response_text": response.text,
                    "booking_uid": resolved_uid,
                    "requested_token": path_token,
                    "api_version": "v2" if "/v2/" in response.url else "v1",
                }
                st.sidebar.error(f"❌ Cancellation failed with status {response.status_code}")
                st.sidebar.code(json.dumps(error_details, indent=2), language="json")
                self._log_error("cancel_booking", "Cancellation failed", error_details)
                return {
                    "success": False,
                    "error": f"Cancellation failed: {response.text[:500]}",
                    "error_details": error_details,
                }
    
            # Success
            response.raise_for_status()
            try:
                result_json = response.json()
            except Exception:
                result_json = {}
            st.sidebar.success("✅ Booking cancelled!")
            return {
                "success": True,
                "message": f"Booking {path_token} cancelled",
                "data": result_json.get("data", result_json),
            }
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to cancel: {str(e)}"
            error_details: Dict[str, Any] = {}
            if hasattr(e, "response") and e.response is not None:
                error_details = {
                    "status_code": e.response.status_code,
                    "response_text": e.response.text,
                    "booking_uid": resolved_uid if 'resolved_uid' in locals() else booking_uid,
                }
                error_msg += f"\nStatus: {e.response.status_code}\nResponse: {e.response.text}"
            st.sidebar.error(error_msg)
            self._log_error("cancel_booking", error_msg, error_details)
            return {"success": False, "error": error_msg, "error_details": error_details}
    
    
    def reschedule_booking(self, booking_uid: Optional[str] = None, booking_id: Optional[str] = None, new_start_time: str = "", reason: str = "") -> Dict[str, Any]:
        """Reschedule a booking by UID or ID with v2-first strategy and cancel+create fallback."""
        try:
            resolved_uid = self._resolve_booking_uid(booking_uid, booking_id)
            path_token = resolved_uid or (booking_uid or booking_id)
            
            # Build payload compatible with v2 API
            payload = {
                "start": new_start_time,
            }
            if reason:
                payload["reschedulingReason"] = reason
    
            st.sidebar.info(f"📤 Rescheduling booking: token={path_token} to {new_start_time}")
    
            # Try Cal.com v2 POST /reschedule endpoint (correct method!)
            try:
                st.sidebar.info("🔄 Trying Cal.com v2 API reschedule endpoint...")
                response = self._make_request_with_retry(
                    "POST",  # ✅ CORRECT: v2 uses POST, not PATCH
                    f"https://api.cal.com/v2/bookings/{path_token}/reschedule",  # ✅ CORRECT endpoint
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "cal-api-version": "2024-08-13",
                    },
                    json=payload,
                    timeout=15,
                    max_retries=2,
                )
                st.sidebar.info(f"📥 V2 POST /reschedule status: {response.status_code}")
                
                # Don't retry on 4xx errors
                if 400 <= response.status_code < 500:
                    error_details = {
                        "status_code": response.status_code,
                        "response_text": response.text,
                        "booking_uid": resolved_uid,
                        "requested_token": path_token,
                        "api_version": "v2",
                    }
                    st.sidebar.error(f"❌ Client error {response.status_code}: {response.text[:200]}")
                    self._log_error("reschedule_booking", "Client error", error_details)
                    return {
                        "success": False,
                        "error": f"Cannot reschedule: {response.text[:500]}",
                        "error_details": error_details,
                        "suggestion": "Check that the booking UID is correct and the new time is available"
                    }
                
                if response.status_code >= 500:
                    raise requests.exceptions.HTTPError(f"V2 POST /reschedule returned {response.status_code}")
                    
            except requests.exceptions.HTTPError as v2_err:
                st.sidebar.warning(f"🔄 V2 reschedule failed: {str(v2_err)}")
                st.sidebar.info("📝 Trying v1 approach: cancel + create new booking...")
                
                # V1 doesn't have a reschedule endpoint - need to cancel + create
                # First, get the original booking details
                try:
                    booking_resp = self.get_bookings()
                    if not booking_resp.get("success"):
                        raise Exception("Could not fetch booking details for v1 reschedule")
                    
                    # Find the booking to reschedule
                    original_booking = None
                    for b in booking_resp.get("bookings", []):
                        if b.get("uid") == path_token or str(b.get("id")) == path_token:
                            original_booking = b
                            break
                    
                    if not original_booking:
                        raise Exception(f"Could not find booking {path_token}")
                    
                    # Extract details needed for new booking
                    event_type_id = original_booking.get("eventTypeId")
                    attendees = original_booking.get("attendees", [])
                    if not attendees:
                        raise Exception("No attendees found in original booking")
                    
                    attendee = attendees[0]
                    attendee_email = attendee.get("email")
                    attendee_name = attendee.get("name")
                    
                    # Cancel the old booking
                    cancel_result = self.cancel_booking(
                        booking_uid=path_token,
                        reason=reason or "Rescheduling to new time"
                    )
                    
                    if not cancel_result.get("success"):
                        raise Exception(f"Failed to cancel original booking: {cancel_result.get('error')}")
                    
                    st.sidebar.info("✅ Original booking cancelled, creating new booking...")
                    
                    # Create new booking
                    new_booking_result = self.create_booking(
                        event_type_id=event_type_id,
                        start_time=new_start_time,
                        attendee_email=attendee_email,
                        attendee_name=attendee_name,
                        meeting_reason=reason or "Rescheduled meeting"
                    )
                    
                    if not new_booking_result.get("success"):
                        # Uh oh - we cancelled but couldn't create new one
                        st.sidebar.error("⚠️ WARNING: Cancelled old booking but failed to create new one!")
                        raise Exception(f"Failed to create new booking: {new_booking_result.get('error')}")
                    
                    st.sidebar.success("✅ Successfully rescheduled via cancel + create!")
                    return {
                        "success": True,
                        "message": f"Booking rescheduled (via cancel + create)",
                        "data": new_booking_result.get("data", {}),
                        "api_version": "v1-cancel-create",
                        "new_booking_uid": new_booking_result.get("booking_uid"),
                    }
                    
                except Exception as v1_error:
                    error_details = {
                        "error": str(v1_error),
                        "booking_uid": resolved_uid,
                        "requested_token": path_token,
                    }
                    st.sidebar.error(f"❌ V1 cancel+create approach failed: {str(v1_error)}")
                    self._log_error("reschedule_booking", "V1 fallback failed", error_details)
                    return {
                        "success": False,
                        "error": f"Reschedule failed on both v2 and v1: {str(v1_error)}",
                        "error_details": error_details,
                    }
    
            # If we reach here, v2 succeeded
            response.raise_for_status()
            try:
                result_json = response.json()
            except Exception:
                result_json = {}
    
            st.sidebar.success("✅ Booking rescheduled!")
            return {
                "success": True,
                "message": f"Booking {path_token} rescheduled",
                "data": result_json.get("data", result_json),
                "api_version": "v2",
            }
            
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to reschedule: {str(e)}"
            error_details: Dict[str, Any] = {}
            if hasattr(e, "response") and e.response is not None:
                error_details = {
                    "status_code": e.response.status_code,
                    "response_text": e.response.text,
                    "booking_uid": resolved_uid if 'resolved_uid' in locals() else booking_uid,
                }
                error_msg += f"\nStatus: {e.response.status_code}\nResponse: {e.response.text}"
            st.sidebar.error(error_msg)
            self._log_error("reschedule_booking", error_msg, error_details)
            return {"success": False, "error": error_msg, "error_details": error_details}
    # Add this diagnostic function to your CalComAPI class

    def diagnose_slots_issue(self, event_type_id: Any, test_date: str = None) -> Dict[str, Any]:
        """
        Comprehensive diagnostic for slots API issues.
        
        Args:
            event_type_id: Event type ID to test
            test_date: Optional test date (YYYY-MM-DD), defaults to tomorrow
        
        Returns:
            Diagnostic report with detailed test results
        """
        from datetime import datetime, timedelta
        
        if not test_date:
            test_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        
        st.sidebar.markdown("---")
        st.sidebar.markdown("### 🔬 Slots API Diagnostics")
        st.sidebar.info(f"Testing event type {event_type_id} for date {test_date}")
        
        results = {
            "event_type_id": event_type_id,
            "test_date": test_date,
            "tests": [],
            "overall_success": False
        }
        
        # Test 1: Verify event type exists
        st.sidebar.write("**Test 1:** Verifying event type exists...")
        try:
            evt_result = self.get_event_types()
            if evt_result.get("success"):
                event_types = evt_result.get("event_types", [])
                found = False
                for et in event_types:
                    if str(et.get("id")) == str(event_type_id):
                        found = True
                        st.sidebar.success(f"✅ Event type found: {et.get('title')}")
                        results["tests"].append({
                            "name": "Event Type Exists",
                            "passed": True,
                            "details": et
                        })
                        break
                
                if not found:
                    st.sidebar.error(f"❌ Event type {event_type_id} not found in your account")
                    results["tests"].append({
                        "name": "Event Type Exists",
                        "passed": False,
                        "error": "Event type not found",
                        "available_types": [f"{et.get('id')}: {et.get('title')}" for et in event_types]
                    })
                    return results
            else:
                st.sidebar.error(f"❌ Could not fetch event types: {evt_result.get('error')}")
                results["tests"].append({
                    "name": "Event Type Exists",
                    "passed": False,
                    "error": evt_result.get("error")
                })
                return results
        except Exception as e:
            st.sidebar.error(f"❌ Test 1 failed: {str(e)}")
            results["tests"].append({
                "name": "Event Type Exists",
                "passed": False,
                "error": str(e)
            })
        
        # Test 2: Try v2 API with simple date format
        st.sidebar.write("**Test 2:** Testing v2 API with simple dates...")
        try:
            response = self._make_request_with_retry(
                "GET",
                "https://api.cal.com/v2/slots",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "cal-api-version": "2024-08-13",
                },
                params={
                    "eventTypeId": event_type_id,
                    "start": test_date,
                    "end": test_date,
                    "timeZone": "America/Los_Angeles",
                },
                timeout=15,
                max_retries=1
            )
            
            if response.status_code == 200:
                data = response.json()
                st.sidebar.success(f"✅ v2 API responded successfully")
                st.sidebar.code(json.dumps(data, indent=2)[:500], language="json")
                
                # Try to parse slots
                slots_count = 0
                if isinstance(data, dict) and "data" in data:
                    for date_key, slots_list in data["data"].items():
                        if isinstance(slots_list, list):
                            slots_count += len(slots_list)
                
                results["tests"].append({
                    "name": "v2 API Simple Dates",
                    "passed": True,
                    "status_code": response.status_code,
                    "slots_found": slots_count,
                    "response_sample": str(data)[:300]
                })
                
                if slots_count == 0:
                    st.sidebar.warning("⚠️ API responded but returned 0 slots")
                    st.sidebar.info("This means the event type has no availability for this date")
            else:
                st.sidebar.error(f"❌ v2 API failed: {response.status_code}")
                st.sidebar.code(response.text[:300], language="text")
                results["tests"].append({
                    "name": "v2 API Simple Dates",
                    "passed": False,
                    "status_code": response.status_code,
                    "error": response.text[:300]
                })
        except Exception as e:
            st.sidebar.error(f"❌ Test 2 failed: {str(e)}")
            results["tests"].append({
                "name": "v2 API Simple Dates",
                "passed": False,
                "error": str(e)
            })
        
        # Test 3: Try v2 API with ISO timestamps
        st.sidebar.write("**Test 3:** Testing v2 API with ISO timestamps...")
        try:
            la = _get_tz("America/Los_Angeles")
            utc = _get_tz("UTC")
            local_start = _localize_naive(datetime.strptime(test_date, "%Y-%m-%d"), la)
            local_end = (local_start + timedelta(days=1)) - timedelta(seconds=1)
            start_iso = local_start.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            end_iso = local_end.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            
            response = self._make_request_with_retry(
                "GET",
                "https://api.cal.com/v2/slots",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "cal-api-version": "2024-08-13",
                },
                params={
                    "eventTypeId": event_type_id,
                    "start": start_iso,
                    "end": end_iso,
                    "timeZone": "America/Los_Angeles",
                },
                timeout=15,
                max_retries=1
            )
            
            if response.status_code == 200:
                st.sidebar.success(f"✅ v2 API with ISO timestamps works")
                results["tests"].append({
                    "name": "v2 API ISO Timestamps",
                    "passed": True,
                    "status_code": response.status_code
                })
            else:
                st.sidebar.warning(f"⚠️ v2 API with ISO timestamps: {response.status_code}")
                results["tests"].append({
                    "name": "v2 API ISO Timestamps",
                    "passed": False,
                    "status_code": response.status_code,
                    "error": response.text[:200]
                })
        except Exception as e:
            st.sidebar.error(f"❌ Test 3 failed: {str(e)}")
            results["tests"].append({
                "name": "v2 API ISO Timestamps",
                "passed": False,
                "error": str(e)
            })
        
        # Test 4: Try v1 API as fallback
        st.sidebar.write("**Test 4:** Testing v1 API fallback...")
        try:
            la = _get_tz("America/Los_Angeles")
            utc = _get_tz("UTC")
            local_start = _localize_naive(datetime.strptime(test_date, "%Y-%m-%d"), la)
            local_end = (local_start + timedelta(days=1)) - timedelta(seconds=1)
            start_iso = local_start.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            end_iso = local_end.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            
            response = self._make_request_with_retry(
                "GET",
                "https://api.cal.com/v1/slots",
                headers={"Content-Type": "application/json"},
                params={
                    "apiKey": self.api_key,
                    "eventTypeId": event_type_id,
                    "startTime": start_iso,
                    "endTime": end_iso,
                    "timeZone": "America/Los_Angeles",
                },
                timeout=15,
                max_retries=1
            )
            
            if response.status_code == 200:
                st.sidebar.success(f"✅ v1 API works as fallback")
                data = response.json()
                slots_count = 0
                if isinstance(data, dict) and "slots" in data:
                    for date_key, slots_list in data["slots"].items():
                        if isinstance(slots_list, list):
                            slots_count += len(slots_list)
                
                results["tests"].append({
                    "name": "v1 API Fallback",
                    "passed": True,
                    "status_code": response.status_code,
                    "slots_found": slots_count
                })
            else:
                st.sidebar.error(f"❌ v1 API failed: {response.status_code}")
                results["tests"].append({
                    "name": "v1 API Fallback",
                    "passed": False,
                    "status_code": response.status_code,
                    "error": response.text[:200]
                })
        except Exception as e:
            st.sidebar.error(f"❌ Test 4 failed: {str(e)}")
            results["tests"].append({
                "name": "v1 API Fallback",
                "passed": False,
                "error": str(e)
            })
        
        # Overall assessment
        st.sidebar.markdown("---")
        passed_tests = sum(1 for t in results["tests"] if t.get("passed"))
        total_tests = len(results["tests"])
        results["overall_success"] = passed_tests > 0
        
        if passed_tests == total_tests:
            st.sidebar.success(f"🎉 All {total_tests} tests passed!")
        elif passed_tests > 0:
            st.sidebar.warning(f"⚠️ {passed_tests}/{total_tests} tests passed")
        else:
            st.sidebar.error(f"❌ All tests failed")
        
        # Recommendations
        st.sidebar.markdown("### 💡 Recommendations")
        if passed_tests == 0:
            st.sidebar.error("🔴 Critical: No API endpoints working")
            st.sidebar.info("1. Verify your API key is correct")
            st.sidebar.info("2. Check the event type ID exists")
            st.sidebar.info("3. Ensure your API key has proper permissions")
        elif any(t.get("slots_found", 0) == 0 for t in results["tests"] if t.get("passed")):
            st.sidebar.warning("🟡 API works but no slots available")
            st.sidebar.info("1. Check event type has availability configured")
            st.sidebar.info("2. Verify the date is within your availability window")
            st.sidebar.info("3. Ensure the time zone is correct")
        else:
            st.sidebar.success("🟢 Everything looks good!")
        
        return results
    
    
    # Add this button to your sidebar in main():
    # Place this in the sidebar section where you have other diagnostic buttons
    
    if calcom_key and st.button("🔬 Diagnose Slots Issue"):
        with st.spinner("Running diagnostics..."):
            cal_api = CalComAPI(calcom_key)
            
            # Get event type ID
            event_id = safe_get_session_state('manual_event_id')
            if not event_id:
                # Try to get first available event type
                evt_result = cal_api.get_event_types()
                if evt_result.get("success") and evt_result.get("event_types"):
                    event_id = evt_result["event_types"][0].get("id")
                    st.sidebar.info(f"Using first available event type: {event_id}")
                else:
                    st.sidebar.error("No event type found. Please set event type ID manually.")
                    st.stop()
            
            # Run diagnostics
            from datetime import datetime, timedelta
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            
            results = cal_api.diagnose_slots_issue(event_id, tomorrow)
            
            # Show detailed results
            st.sidebar.json(results)
            
# OpenAI function definitions
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_event_types",
            "description": "Get all available event types to show user their options. Use this when user's meeting reason doesn't match any event type.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_available_slots",
            "description": "Get available time slots for booking. Times are in PST/PDT. Always call this before booking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "event_type_id": {"type": "integer", "description": "Event type ID (optional, will use first available if not provided)"}
                },
                "required": ["date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking_manual",
            "description": "Create a booking with manual time when slots API fails. Use this if get_available_slots returns an error.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type_id": {"type": "integer", "description": "Event type ID"},
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "time": {"type": "string", "description": "Time in HH:MM format (24-hour, PST)"},
                    "attendee_email": {"type": "string"},
                    "attendee_name": {"type": "string"},
                    "meeting_reason": {"type": "string"}
                },
                "required": ["date", "time", "attendee_email", "attendee_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": "Create a new booking. IMPORTANT: First check if meeting_reason matches an event type title. If it matches, use that event_type_id. If no match, call get_event_types and ask user to choose.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type_id": {"type": "integer", "description": "Event type ID from get_available_slots or matched from meeting_reason"},
                    "start_time": {"type": "string", "description": "ISO format timestamp from available slots (YYYY-MM-DDTHH:MM:SSZ)"},
                    "attendee_email": {"type": "string"},
                    "attendee_name": {"type": "string"},
                    "meeting_reason": {"type": "string", "description": "Meeting reason - will be matched against event type titles"}
                },
                "required": ["start_time", "attendee_email", "attendee_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_bookings",
            "description": "Get all bookings. Always show the booking UID and booking link in your response if available.",
            "parameters": {
                "type": "object",
                "properties": {
                    "attendee_email": {"type": "string"},
                    "attendee_name": {"type": "string"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_booking",
            "description": "Cancel a booking using its UID or booking ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_uid": {"type": "string", "description": "The booking UID (preferred)"},
                    "booking_id": {"type": "string", "description": "The numeric booking ID (alternative)"},
                    "reason": {"type": "string"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_booking",
            "description": "Reschedule a booking using its UID or booking ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_uid": {"type": "string", "description": "The booking UID (preferred)"},
                    "booking_id": {"type": "string", "description": "The numeric booking ID (alternative)"},
                    "new_start_time": {"type": "string", "description": "ISO format"},
                    "reason": {"type": "string"}
                },
                "required": ["new_start_time"]
            }
        }
    }
]


def safe_get_session_state(key: str, default=None):
    """Safely get session state value with error handling"""
    try:
        return st.session_state.get(key, default)
    except Exception as e:
        st.sidebar.warning(f"Session state error for key '{key}': {str(e)}")
        return default

def safe_set_session_state(key: str, value):
    """Safely set session state value with error handling"""
    try:
        st.session_state[key] = value
        return True
    except Exception as e:
        st.sidebar.warning(f"Session state error setting '{key}': {str(e)}")
        return False

def execute_function(function_name: str, arguments: Dict[str, Any], cal_api: CalComAPI) -> str:
    """Execute function calls"""
    
    if function_name == "get_event_types":
        """Return all available event types for user to choose"""
        evt_resp = cal_api.get_event_types()
        
        if not evt_resp.get("success"):
            return json.dumps({
                "success": False,
                "error": f"Failed to fetch event types: {evt_resp.get('error')}"
            })
        
        event_types = evt_resp.get("event_types", [])
        if not event_types:
            return json.dumps({
                "success": False,
                "error": "No event types configured",
                "message": "Please create event types at https://app.cal.com/event-types"
            })
        
        # Format event types for user
        formatted_types = []
        for et in event_types:
            formatted_types.append({
                "id": et.get("id"),
                "title": et.get("title") or et.get("name"),
                "slug": et.get("slug", ""),
                "length": et.get("length", "N/A"),
                "description": et.get("description", "")
            })
        
        return json.dumps({
            "success": True,
            "event_types": formatted_types,
            "count": len(formatted_types),
            "message": f"Found {len(formatted_types)} available event types"
        })
    
    elif function_name == "get_available_slots":
        date = arguments.get("date")
        event_type_id = arguments.get("event_type_id")

        # Check for manual override first
        if not event_type_id:
            manual_event_id = safe_get_session_state('manual_event_id')
            if manual_event_id:
                event_type_id = manual_event_id
                st.sidebar.success(f"🎯 Using manually specified event type ID: {event_type_id}")

        # Get event type if not provided
        if not event_type_id:
            st.sidebar.info("🔍 No event_type_id provided, fetching available event types...")
            evt_resp = cal_api.get_event_types()
            
            if not evt_resp.get("success"):
                return json.dumps({
                    "success": False, 
                    "error": f"Failed to fetch event types: {evt_resp.get('error')}",
                    "user_message": "I couldn't connect to your Cal.com account to fetch event types. Please check your API key or enter the event type ID manually in the sidebar."
                })
            
            event_types = evt_resp.get("event_types", [])
            if not event_types:
                return json.dumps({
                    "success": False,
                    "error": "No event types configured in Cal.com",
                    "user_message": "I can see you have an event type at cal.com/xin-tnkutt/interview, but the API can't access it. This usually means:\n\n1. Your API key doesn't have permission (try regenerating it)\n2. The event type is in a team workspace (use a personal API key)\n3. You can manually enter the event type ID in the sidebar instead.",
                    "action_required": "Check API key permissions or enter event type ID manually"
                })
            
            # Try to find "interview" event type
            interview_et = next((et for et in event_types if 'interview' in str(et.get('slug', '')).lower() or 'interview' in str(et.get('title', '')).lower()), None)
            if interview_et:
                event_type_id = interview_et.get("id")
                st.sidebar.success(f"🎯 Found interview event type! ID: {event_type_id}")
            else:
                event_type_id = event_types[0].get("id")
                st.sidebar.success(f"✅ Using first event type: {event_types[0].get('title')} (ID: {event_type_id})")

        # Build an America/Los_Angeles local day window and convert to UTC
        la = _get_tz("America/Los_Angeles")
        utc = _get_tz("UTC")
        local_start = _localize_naive(datetime.strptime(date, "%Y-%m-%d"), la)
        local_end = (local_start + timedelta(days=1)) - timedelta(seconds=1)
        start_date = local_start.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_date = local_end.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        result = cal_api.get_available_slots(event_type_id, start_date, end_date)

        if result.get("success"):
            slots = result.get("slots", [])
            formatted_slots = [format_time_pst(s) for s in slots[:10]]
            return json.dumps({
                "success": True,
                "available_slots": formatted_slots,
                "raw_slots": slots[:10],
                "event_type_id": event_type_id,
                "message": f"Found {len(slots)} available slots for {date} (PST/PDT)"
            })
        else:
            # Return error but also suggest manual booking
            return json.dumps({
                "success": False,
                "error": result.get("error"),
                "event_type_id": event_type_id,
                "message": "Could not fetch slots. You can try manual booking with create_booking_manual instead.",
                "suggestion": result.get("suggestion", "")
            })

    elif function_name == "create_booking_manual":
        # Convert America/Los_Angeles local time to UTC for booking
        date = arguments.get("date")
        time = arguments.get("time")  # Format: "14:00"
        
        try:
            # Get event type first
            event_type_id = arguments.get("event_type_id")
            
            # Check for manual override
            if not event_type_id:
                manual_event_id = safe_get_session_state('manual_event_id')
                if manual_event_id:
                    event_type_id = manual_event_id
                    st.sidebar.success(f"🎯 Using manually specified event type ID: {event_type_id}")
            
            if not event_type_id:
                st.sidebar.info("🔍 Fetching event types for manual booking...")
                evt_resp = cal_api.get_event_types()
                
                if not evt_resp.get("success"):
                    return json.dumps({
                        "success": False,
                        "error": f"Failed to fetch event types: {evt_resp.get('error')}",
                        "user_message": "I couldn't connect to your Cal.com account. Try entering the event type ID manually in the sidebar."
                    })
                
                event_types = evt_resp.get("event_types", [])
                if not event_types:
                    return json.dumps({
                        "success": False,
                        "error": "No event types available",
                        "user_message": "Can't auto-detect event types. Please enter your event type ID manually in the sidebar."
                    })
                
                # Try to find interview event type
                interview_et = next((et for et in event_types if 'interview' in str(et.get('slug', '')).lower()), None)
                if interview_et:
                    event_type_id = interview_et.get("id")
                    st.sidebar.success(f"🎯 Found interview event type: {interview_et.get('title')} (ID: {event_type_id})")
                else:
                    event_type_id = event_types[0].get("id")
                    st.sidebar.success(f"✅ Using event type: {event_types[0].get('title')} (ID: {event_type_id})")
            
            # Parse local LA time and convert to UTC (handles DST)
            try:
                la = _get_tz("America/Los_Angeles")
                utc = _get_tz("UTC")
                local_dt = _localize_naive(datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M"), la)
                utc_datetime = local_dt.astimezone(utc)
                start_time = utc_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")
                
                st.sidebar.info(f"Converting {date} {time} America/Los_Angeles → {start_time} UTC")
                
                # Validate the converted time
                if not start_time.endswith('Z'):
                    raise ValueError("Converted time must end with Z")
                    
            except Exception as time_error:
                error_msg = f"Failed to convert time: {str(time_error)}"
                st.sidebar.error(f"❌ {error_msg}")
                return json.dumps({"success": False, "error": error_msg})
            
            result = cal_api.create_booking(
                event_type_id=event_type_id,
                start_time=start_time,
                attendee_email=arguments["attendee_email"],
                attendee_name=arguments["attendee_name"],
                attendee_timezone="America/Los_Angeles",  # PDT timezone
                attendee_language="en",  # English
                meeting_reason=arguments.get("meeting_reason", "")
            )
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"success": False, "error": f"Failed to parse time: {str(e)}"})

    elif function_name == "create_booking":
        event_type_id = arguments.get("event_type_id")
        meeting_reason = arguments.get("meeting_reason", "")
        
        # Check for manual override
        if not event_type_id:
            manual_event_id = safe_get_session_state('manual_event_id')
            if manual_event_id:
                event_type_id = manual_event_id
                st.sidebar.success(f"🎯 Using manually specified event type ID: {event_type_id}")
        
        # Try to match meeting reason with event type title
        if not event_type_id:
            st.sidebar.info("🔍 Fetching event types to match with meeting reason...")
            evt_resp = cal_api.get_event_types()
            
            if not evt_resp.get("success"):
                return json.dumps({
                    "success": False,
                    "error": f"Failed to fetch event types: {evt_resp.get('error')}",
                    "user_message": "I couldn't connect to your Cal.com account. Try entering the event type ID manually in the sidebar."
                })
            
            event_types = evt_resp.get("event_types", [])
            if not event_types:
                return json.dumps({
                    "success": False,
                    "error": "No event types available",
                    "user_message": "Can't auto-detect event types. Please enter your event type ID manually in the sidebar."
                })
            
            # Try to match meeting_reason with event type title
            matched_et = None
            if meeting_reason:
                reason_lower = meeting_reason.lower().strip()
                st.sidebar.info(f"🔍 Looking for event type matching: '{meeting_reason}'")
                
                for et in event_types:
                    title = (et.get("title") or et.get("name") or "").lower().strip()
                    slug = (et.get("slug") or "").lower().strip()
                    
                    # Check for exact or partial match
                    if (reason_lower == title or 
                        reason_lower == slug or 
                        reason_lower in title or 
                        title in reason_lower):
                        matched_et = et
                        st.sidebar.success(f"✅ Matched '{meeting_reason}' with event type: {et.get('title')} (ID: {et.get('id')})")
                        break
            
            if matched_et:
                event_type_id = matched_et.get("id")
            else:
                # No match found - return available event types for user to choose
                st.sidebar.warning(f"⚠️ No event type matches '{meeting_reason}'")
                
                formatted_types = []
                for et in event_types:
                    formatted_types.append({
                        "id": et.get("id"),
                        "title": et.get("title") or et.get("name"),
                        "slug": et.get("slug"),
                        "length": f"{et.get('length', 'N/A')} min"
                    })
                
                return json.dumps({
                    "success": False,
                    "error": "no_matching_event_type",
                    "available_event_types": formatted_types,
                    "user_message": f"I couldn't find an event type matching '{meeting_reason}'. Here are your available event types. Please specify which one you'd like to book.",
                    "action_required": "user_must_choose_event_type"
                })

        result = cal_api.create_booking(
            event_type_id=event_type_id,
            start_time=arguments["start_time"],
            attendee_email=arguments["attendee_email"],
            attendee_name=arguments["attendee_name"],
            attendee_timezone="America/Los_Angeles",  # PDT timezone
            attendee_language="en",  # English
            meeting_reason=meeting_reason
        )
        return json.dumps(result)

    elif function_name == "get_bookings":
        result = cal_api.get_bookings(
            attendee_email=arguments.get("attendee_email"),
            attendee_name=arguments.get("attendee_name")
        )
        return json.dumps(result)

    elif function_name == "cancel_booking":
        result = cal_api.cancel_booking(
            booking_uid=arguments.get("booking_uid"),
            booking_id=arguments.get("booking_id"),
            reason=arguments.get("reason", "Cancelled by user")
        )
        return json.dumps(result)

    elif function_name == "reschedule_booking":
        result = cal_api.reschedule_booking(
            booking_uid=arguments.get("booking_uid"),
            booking_id=arguments.get("booking_id"),
            new_start_time=arguments["new_start_time"],
            reason=arguments.get("reason", "")
        )
        return json.dumps(result)

    return json.dumps({"success": False, "error": "Unknown function"})


def chat_with_assistant(messages: List[Dict[str, Any]], cal_api: CalComAPI) -> tuple:
    """Send messages to OpenAI using tools API"""
    
    # Inject fresh runtime date context into the system message on every turn
    # to avoid stale or hardcoded dates.
    runtime_ctx = _build_runtime_date_context()
    enriched_messages = []
    inserted = False
    for msg in messages:
        if not inserted and msg.get("role") == "system":
            enriched_messages.append({
                "role": "system",
                "content": (msg.get("content") or "") + "\n\n" + runtime_ctx
            })
            inserted = True
        else:
            enriched_messages.append(msg)
    if not inserted:
        enriched_messages.insert(0, {"role": "system", "content": runtime_ctx})

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=enriched_messages,
        tools=tools,
        tool_choice="auto"
    )

    assistant_message = response.choices[0].message

    # Check if tool calls were made
    if assistant_message.tool_calls:
        tool_call = assistant_message.tool_calls[0]
        function_name = tool_call.function.name
        function_args = json.loads(tool_call.function.arguments or "{}")

        # Execute the function
        function_response = execute_function(function_name, function_args, cal_api)

        # Add assistant message with tool call
        messages.append({
            "role": "assistant",
            "content": assistant_message.content,
            "tool_calls": [{
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": function_name,
                    "arguments": tool_call.function.arguments
                }
            }]
        })

        # Add tool response
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "name": function_name,
            "content": function_response
        })

        # Get final response
        # Also include runtime context for the second model turn
        runtime_ctx_2 = _build_runtime_date_context()
        enriched_messages_2 = []
        inserted_2 = False
        for msg in messages:
            if not inserted_2 and msg.get("role") == "system":
                enriched_messages_2.append({
                    "role": "system",
                    "content": (msg.get("content") or "") + "\n\n" + runtime_ctx_2
                })
                inserted_2 = True
            else:
                enriched_messages_2.append(msg)
        if not inserted_2:
            enriched_messages_2.insert(0, {"role": "system", "content": runtime_ctx_2})

        second_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=enriched_messages_2
        )

        return second_response.choices[0].message.content, messages

    else:
        return assistant_message.content, messages


# Streamlit UI
def render_enhanced_bookings_section(cal_api, user_email, attendee_name, show_all=False):
    """Render enhanced bookings section with status indicators"""
    from datetime import datetime
    
    st.markdown("---")
    st.subheader("📆 Scheduled Events")
    
    # Filter options
    col1, col2, col3, col4 = st.columns([2, 2, 1, 1])
    
    with col1:
        status_filter = st.selectbox(
            "Filter by Status",
            ["All", "Upcoming", "Today", "This Week", "Past", "Cancelled"],
            index=0
        )
    
    with col2:
        sort_by = st.selectbox(
            "Sort by",
            ["Date (Newest First)", "Date (Oldest First)", "Status"],
            index=0
        )
    
    with col3:
        fetch_btn = st.button("🔄 Refresh", use_container_width=True)
    
    with col4:
        show_all_btn = st.button("Show All", use_container_width=True)
    
    if fetch_btn or show_all_btn:
        email_filter = (user_email or "").strip() if not show_all_btn else None
        name_filter = (attendee_name or "").strip() if not show_all_btn else None
        
        try:
            result = cal_api.get_bookings(
                attendee_email=email_filter,
                attendee_name=name_filter,
            )
        except Exception as e:
            result = {"success": False, "error": str(e), "bookings": []}
        
        if result.get("success") and result.get("count", 0) > 0:
            bookings = result.get("bookings", [])
            
            # Add status to each booking
            for b in bookings:
                status_text, emoji, color = get_booking_status(b)
                b["_status"] = status_text
                b["_emoji"] = emoji
                b["_color"] = color
            
            # Apply status filter
            if status_filter != "All":
                bookings = [b for b in bookings if b["_status"] == status_filter]
            
            # Sort bookings
            if sort_by == "Date (Newest First)":
                bookings.sort(key=lambda b: b.get("start") or "", reverse=True)
            elif sort_by == "Date (Oldest First)":
                bookings.sort(key=lambda b: b.get("start") or "")
            elif sort_by == "Status":
                status_order = {"Today": 0, "Tomorrow": 1, "This Week": 2, "Upcoming": 3, "Past": 4, "Cancelled": 5}
                bookings.sort(key=lambda b: status_order.get(b["_status"], 99))
            
            if not bookings:
                st.info(f"No events matching filter: {status_filter}")
                return
            
            # Group by status
            status_groups = {}
            for b in bookings:
                status = b["_status"]
                if status not in status_groups:
                    status_groups[status] = []
                status_groups[status].append(b)
            
            # Display stats
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                upcoming_count = sum(1 for b in bookings if b["_status"] in ["Upcoming", "Today", "Tomorrow", "This Week"])
                st.metric("Upcoming", upcoming_count)
            with col2:
                past_count = sum(1 for b in bookings if b["_status"] == "Past")
                st.metric("Past", past_count)
            with col3:
                cancelled_count = sum(1 for b in bookings if b["_status"] == "Cancelled")
                st.metric("Cancelled", cancelled_count)
            with col4:
                st.metric("Total", len(bookings))
            
            st.markdown("---")
            
            # Display bookings
            for b in bookings:
                start_text = b.get("start_pst") or b.get("start") or ""
                uid = b.get("display_uid") or b.get("uid") or b.get("id") or "N/A"
                pa_email = b.get("primary_attendee_email") or ""
                pa_name = b.get("primary_attendee_name") or ""
                who = pa_email or pa_name or "(no attendee)"
                
                status_text = b["_status"]
                emoji = b["_emoji"]
                color = b["_color"]
                
                # Create card-like display
                with st.container():
                    # Status badge and main info
                    col_badge, col_main = st.columns([1, 5])
                    
                    with col_badge:
                        if color == "red":
                            st.error(f"{emoji} {status_text}")
                        elif color == "orange":
                            st.warning(f"{emoji} {status_text}")
                        elif color == "green":
                            st.success(f"{emoji} {status_text}")
                        elif color == "blue":
                            st.info(f"{emoji} {status_text}")
                        elif color == "yellow":
                            st.warning(f"{emoji} {status_text}")
                        else:  # gray
                            st.text(f"{emoji} {status_text}")
                    
                    with col_main:
                        # Event details
                        st.markdown(f"**{start_text}**")
                        st.caption(f"👤 {who} • 🔑 UID: `{uid}`")
                        
                        # Action buttons and links
                        action_cols = st.columns([1, 1, 1, 3])
                        
                        booking_url = b.get("booking_url")
                        reschedule_url = b.get("reschedule_url")
                        cancel_url = b.get("cancel_url")
                        
                        with action_cols[0]:
                            if booking_url:
                                st.markdown(f"[🔗 Join]({booking_url})")
                        
                        with action_cols[1]:
                            if reschedule_url and status_text not in ["Cancelled", "Past"]:
                                st.markdown(f"[🔄 Reschedule]({reschedule_url})")
                        
                        with action_cols[2]:
                            if cancel_url and status_text not in ["Cancelled", "Past"]:
                                st.markdown(f"[❌ Cancel]({cancel_url})")
                    
                    st.markdown("---")
            
            st.success(f"✅ Showing {len(bookings)} event(s)")
            
        else:
            err = result.get("error")
            if err:
                st.error(f"Failed to fetch bookings: {err}")
            else:
                st.info("No scheduled events found for the given filters.")
    else:
        st.info("👆 Click 'Refresh' to load your events")


# Update the main() function - replace the existing "Scheduled Events" section
# (lines ~888-928) with this single function call:

def main():
    st.set_page_config(page_title="Cal.com Chatbot", page_icon="📅", layout="wide")

    st.title("📅 Cal.com Meeting Assistant (PST/PDT)")
    st.markdown("Book, view, cancel, and reschedule your meetings through natural conversation!")

    with st.sidebar:
        st.header("⚙️ Configuration")

        calcom_key = st.text_input(
            "Cal.com API Key",
            value=CALCOM_API_KEY or "",
            type="password",
            help="Enter your Cal.com API key"
        )

        user_email = st.text_input(
            "Your Email", 
            placeholder="user@example.com", 
            help="Your email for booking management"
        )
        attendee_name = st.text_input(
            "Attendee Name (optional)",
            placeholder="Enter a name to filter scheduled events",
            help="Use either email or name to filter scheduled events"
        )
        
        st.markdown("---")
        st.markdown("### 🎯 Manual Event Type (Optional)")
        manual_event_id = st.text_input(
            "Event Type ID",
            placeholder="Leave empty to auto-detect",
            help="If auto-detection fails, enter your event type ID manually"
        )
        
        if manual_event_id:
            try:
                manual_event_id = int(manual_event_id)
                if safe_set_session_state('manual_event_id', manual_event_id):
                    st.success(f"✅ Will use event type ID: {manual_event_id}")
                else:
                    st.error("❌ Failed to save event type ID to session state")
            except:
                st.error("❌ Event type ID must be a number")

        st.info("🕐 All times shown in PST/PDT")

        st.markdown("---")
        st.markdown("### 💡 Try saying:")
        st.markdown("- Help me book a meeting for tomorrow at 2pm")
        st.markdown("- Show me my scheduled events")
        st.markdown("- Cancel my meeting")
        st.markdown("- Reschedule my meeting to 4pm")
        
        if user_email:
            st.markdown("---")
            st.markdown("### 🔗 Quick Links")
            manual_event_id = safe_get_session_state('manual_event_id')
            if manual_event_id:
                st.markdown(f"[📅 Check Availability](https://cal.com/event-types/{manual_event_id})")
            st.markdown("[⚙️ Manage Event Types](https://app.cal.com/event-types)")
            st.markdown("[🔑 API Keys](https://app.cal.com/settings/developer/api-keys)")

        if st.button("Clear Chat History"):
            st.session_state.messages = []
            st.rerun()
        
        if calcom_key and st.button("🗑️ Clear Error Log"):
            cal_api = CalComAPI(calcom_key)
            cal_api.error_log = []
            st.success("Error log cleared!")
            st.rerun()

    # Initialize session state
    if "messages" not in st.session_state:
        st.session_state.messages = [{
            "role": "system",
            "content": f"""You are a helpful meeting assistant for Cal.com calendar management.
            
IMPORTANT: All times are in PST/PDT timezone (America/Los_Angeles).

When booking:
1. Get meeting reason from user (e.g., "interview", "consultation")
2. FIRST try get_available_slots to check availability
3. When create_booking is called:
   - If meeting_reason matches an event type title, that event type will be used automatically
   - If NO match, you'll receive a list of available event types - SHOW these to the user and ask them to choose
   - Once user chooses, call create_booking again with the specific event_type_id
4. If get_available_slots fails, use create_booking_manual
5. Always use PDT timezone and English language (automatically set)

When listing bookings:
- ALWAYS show the booking UID for each meeting
- Include attendee email/name if available
- If booking_url is available, show it as a link
- Format suggestion: "[start_pst] — [primary_attendee_email or name] — UID: [uid] — [booking_url]"

For cancel/reschedule:
1. First call get_bookings to get UIDs
2. Show user their bookings with UIDs
3. Use the UID for the operation

If no event type matches user's reason, present the available options clearly:
"I found these event types available:
1. [Title] - [Length] 
2. [Title] - [Length]
Which one would you like to book?"

Be conversational and helpful!"""
        }]

    if not calcom_key:
        st.warning("⚠️ Please enter your Cal.com API key in the sidebar.")
        return

    cal_api = CalComAPI(calcom_key)

    # Enhanced Scheduled Events Section
    render_enhanced_bookings_section(cal_api, user_email, attendee_name)

    # Display chat messages
    for message in st.session_state.messages:
        if message.get("role") in ["user", "assistant"] and message.get("content"):
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

    # Chat input
    if prompt := st.chat_input("Ask me about your meetings..."):
        st.session_state.messages.append({"role": "user", "content": prompt})

        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                working_messages = st.session_state.messages.copy()
                
                if user_email and working_messages[0]["role"] == "system":
                    working_messages[0]["content"] = working_messages[0]["content"].replace(
                        f"default: {user_email if user_email else 'ask user'}", 
                        f"default: {user_email}"
                    )

                try:
                    response_text, _ = chat_with_assistant(working_messages, cal_api)
                    st.markdown(response_text or "No response")
                except Exception as e:
                    response_text = f"Error: {str(e)}"
                    st.error(response_text)
                    st.sidebar.error(f"Exception details: {str(e)}")
                    
                    cal_api._log_error("chat_with_assistant", str(e), {
                        "user_input": prompt,
                        "session_state_keys": list(st.session_state.keys()) if hasattr(st, 'session_state') else []
                    })

        st.session_state.messages.append({"role": "assistant", "content": response_text})
        st.rerun()


if __name__ == "__main__":
    main()
