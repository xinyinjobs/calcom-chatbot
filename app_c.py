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
            else:
                error_details = {}
            # Log the error
            self._log_error("get_event_types", f"API returned {response.status_code}", error_details)
            
            return {
                "success": False, 
                "error": f"Failed to fetch event types: {response.text}",
                "error_details": error_details,
                "event_types": []
            }
            
            response.raise_for_status()
            data = response.json()
            
            # Log raw response for debugging
            st.sidebar.code(f"Event types raw response:\n{json.dumps(data, indent=2)[:800]}", language="json")
            
            # Parse event types from different response structures
            event_types = []
            
            # Structure 1: {data: [...]}
            if "data" in data:
                event_types = data["data"] if isinstance(data["data"], list) else []
            # Structure 2: {event_types: [...]}
            elif "event_types" in data:
                event_types = data["event_types"] if isinstance(data["event_types"], list) else []
            # Structure 3: direct array
            elif isinstance(data, list):
                event_types = data

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
        """Get available time slots. start_date/end_date must be ISO UTC strings (Z)."""
        try:
            st.sidebar.info(f"🔍 Checking slots for event type {event_type_id}")
            st.sidebar.info(f"Date range: {start_date} to {end_date}")
            
            # Try v2 API first with retry logic
            try:
                st.sidebar.info("🔄 Trying Cal.com v2 API for slots...")
                response = self._make_request_with_retry(
                    "GET",
                    f"https://api.cal.com/v2/slots/available",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "cal-api-version": "2024-08-13",
                    },
                    params={
                        "eventTypeId": event_type_id,
                        "startTime": start_date,
                        "endTime": end_date,
                        # Ensure slots are computed for LA timezone
                        "timeZone": "America/Los_Angeles",
                    },
                    timeout=15,  # Increased timeout
                    max_retries=2
                )
                st.sidebar.info(f"V2 API Status: {response.status_code}")
                
                if response.status_code >= 400:
                    st.sidebar.warning(f"V2 API failed ({response.status_code}): {response.text[:200]}")
                    raise requests.exceptions.HTTPError(f"V2 API returned {response.status_code}")
                    
            except Exception as v2_error:
                st.sidebar.warning(f"V2 API failed: {str(v2_error)}, trying v1...")
                # Fallback to v1 API with query params and retry logic
                response = self._make_request_with_retry(
                    "GET",
                    f"https://api.cal.com/v1/slots?apiKey={self.api_key}",
                    headers={
                        "Content-Type": "application/json"
                    },
                    params={
                        "eventTypeId": event_type_id,
                        "startTime": start_date,
                        "endTime": end_date,
                        "timeZone": "America/Los_Angeles",
                    },
                    timeout=15,  # Increased timeout
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

            def maybe_add(value: Any):
                # Accept ISO strings like 2025-10-16T22:00:00Z or with offset
                if isinstance(value, str) and "T" in value:
                    try:
                        # Normalize to Z if offset is +00:00
                        _ = datetime.fromisoformat(value.replace("Z", "+00:00"))
                        slots.append(value)
                    except Exception:
                        pass

            def walk(obj: Any):
                if isinstance(obj, dict):
                    # Common keys we see in Cal.com API
                    for key in ("time", "start", "startTime"):
                        if key in obj:
                            maybe_add(obj[key])
                    # Continue walking nested structures
                    for v in obj.values():
                        walk(v)
                elif isinstance(obj, list):
                    for item in obj:
                        walk(item)
                else:
                    maybe_add(obj)
            
            # Try different response structures
            if isinstance(data, dict):
                # Try known shapes first
                if "data" in data:
                    slots_data = data["data"]
                    if isinstance(slots_data, dict) and "slots" in slots_data:
                        inner_slots = slots_data["slots"]
                        if isinstance(inner_slots, dict):
                            for _, time_list in inner_slots.items():
                                if isinstance(time_list, list):
                                    for slot in time_list:
                                        if isinstance(slot, str):
                                            slots.append(slot)
                                        elif isinstance(slot, dict) and "time" in slot:
                                            slots.append(slot["time"])
                        elif isinstance(inner_slots, list):
                            for item in inner_slots:
                                if isinstance(item, str):
                                    slots.append(item)
                                elif isinstance(item, dict):
                                    if "time" in item:
                                        maybe_add(item["time"])
                                    if "start" in item:
                                        maybe_add(item["start"])
                    elif isinstance(slots_data, list):
                        for item in slots_data:
                            if isinstance(item, str):
                                slots.append(item)
                            else:
                                walk(item)
                    else:
                        walk(slots_data)
                elif "slots" in data:
                    walk(data["slots"])
                else:
                    # Fallback: walk entire response
                    walk(data)

            st.sidebar.success(f"📅 Found {len(slots)} available slots")
            if slots:
                st.sidebar.info(f"First slot example: {slots[0]}")
            
            return {
                "success": True, 
                "slots": slots, 
                "count": len(slots),
                "event_type_id": event_type_id,  # Pass this along for booking
                "api_version": "v2" if "v2" in response.url else "v1"
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
            def first_url_from(booking_dict: Dict[str, Any], prefer: List[str]) -> Optional[str]:
                # Prefer specific keys, else any field containing 'url' or 'link'
                for key in prefer:
                    if key in booking_dict and isinstance(booking_dict[key], str) and booking_dict[key].startswith("http"):
                        return booking_dict[key]
                for key, val in booking_dict.items():
                    kl = key.lower()
                    if ("url" in kl or "link" in kl) and isinstance(val, str) and val.startswith("http"):
                        return val
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
                    prefer=["bookingUrl", "eventPageUrl", "statusPageUrl", "booking_url"],
                )
                booking["reschedule_url"] = first_url_from(
                    booking,
                    prefer=["rescheduleUrl", "rescheduleLink"],
                )
                booking["cancel_url"] = first_url_from(
                    booking,
                    prefer=["cancelUrl", "cancelLink"],
                )

            st.sidebar.success(f"✅ Found {len(bookings)} booking(s)")
            return {"success": True, "bookings": bookings, "count": len(bookings)}
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to get bookings: {str(e)}"
            if hasattr(e, "response") and e.response is not None:
                error_msg += f"\nStatus: {e.response.status_code}\nResponse: {e.response.text}"
            st.sidebar.error(error_msg)
            return {"success": False, "error": error_msg, "bookings": []}

    def cancel_booking(self, booking_uid: str, reason: str = "Cancelled by user") -> Dict[str, Any]:
        """Cancel a booking by UID with v2-first strategy and v1 fallback."""
        try:
            st.sidebar.info(f"📤 Cancelling UID: {booking_uid}")

            # Try Cal.com v2 first (preferred)
            try:
                st.sidebar.info("🔄 Trying Cal.com v2 API for cancellation...")
                response = self._make_request_with_retry(
                    "POST",
                    f"https://api.cal.com/v2/bookings/{booking_uid}/cancel",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "cal-api-version": "2024-08-13",
                    },
                    json={
                        # Include both common keys to maximize compatibility across versions
                        "cancellationReason": reason,
                        "reason": reason,
                    },
                    timeout=15,
                    max_retries=2,
                )
                st.sidebar.info(f"📥 V2 cancel status: {response.status_code}")

                if response.status_code >= 400:
                    raise requests.exceptions.HTTPError(
                        f"V2 API returned {response.status_code}"
                    )
            except Exception as v2_error:
                st.sidebar.warning(
                    f"V2 cancel failed ({str(v2_error)}), trying v1..."
                )
                # Fallback to Cal.com v1
                response = self._make_request_with_retry(
                    "POST",
                    f"https://api.cal.com/v1/bookings/{booking_uid}/cancel?apiKey={self.api_key}",
                    headers={
                        "Content-Type": "application/json",
                    },
                    json={"cancellationReason": reason},
                    timeout=15,
                    max_retries=2,
                )
                st.sidebar.info(f"📥 V1 cancel status: {response.status_code}")

            # Handle error responses with richer details
            if response.status_code >= 400:
                error_details = {
                    "status_code": response.status_code,
                    "response_text": response.text,
                    "booking_uid": booking_uid,
                    "api_version": "v2" if "/v2/" in response.url else "v1",
                }
                st.sidebar.error(
                    f"❌ Cancellation failed with status {response.status_code}"
                )
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
                "message": f"Booking {booking_uid} cancelled",
                "data": result_json.get("data", result_json),
            }
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to cancel: {str(e)}"
            error_details: Dict[str, Any] = {}
            if hasattr(e, "response") and e.response is not None:
                error_details = {
                    "status_code": e.response.status_code,
                    "response_text": e.response.text,
                    "booking_uid": booking_uid,
                }
                error_msg += (
                    f"\nStatus: {e.response.status_code}\nResponse: {e.response.text}"
                )
            st.sidebar.error(error_msg)
            self._log_error("cancel_booking", error_msg, error_details)
            return {"success": False, "error": error_msg, "error_details": error_details}

    def reschedule_booking(self, booking_uid: str, new_start_time: str, reason: str = "") -> Dict[str, Any]:
        """Reschedule a booking"""
        try:
            payload = {"start": new_start_time}
            if reason:
                payload["reschedulingReason"] = reason

            st.sidebar.info(f"📤 Rescheduling UID: {booking_uid} to {new_start_time}")
            response = requests.patch(
                f"https://api.cal.com/v1/bookings/{booking_uid}?apiKey={self.api_key}",
                headers=self.headers, 
                json=payload, 
                timeout=15
            )
            
            st.sidebar.info(f"📥 Response: {response.status_code}")
            response.raise_for_status()
            result = response.json()
            st.sidebar.success("✅ Booking rescheduled!")
            return {"success": True, "data": result.get("data", {})}
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to reschedule: {str(e)}"
            if hasattr(e, "response") and e.response is not None:
                error_msg += f"\nStatus: {e.response.status_code}\nResponse: {e.response.text}"
            st.sidebar.error(error_msg)
            return {"success": False, "error": error_msg}


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
            "description": "Cancel a booking using its UID (string). First get bookings to find the UID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_uid": {"type": "string", "description": "The booking UID"},
                    "reason": {"type": "string"}
                },
                "required": ["booking_uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_booking",
            "description": "Reschedule a booking using its UID (string). First get bookings to find the UID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_uid": {"type": "string", "description": "The booking UID"},
                    "new_start_time": {"type": "string", "description": "ISO format"},
                    "reason": {"type": "string"}
                },
                "required": ["booking_uid", "new_start_time"]
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
            booking_uid=arguments["booking_uid"],
            reason=arguments.get("reason", "Cancelled by user")
        )
        return json.dumps(result)

    elif function_name == "reschedule_booking":
        result = cal_api.reschedule_booking(
            booking_uid=arguments["booking_uid"],
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
        
        # Add clear error log button
        if calcom_key and st.button("🗑️ Clear Error Log"):
            cal_api = CalComAPI(calcom_key)
            cal_api.error_log = []
            st.success("Error log cleared!")
            st.rerun()

        st.markdown("---")
        st.markdown("### 🔍 Debug Info")
        
        # Add error log display
        if calcom_key:
            cal_api = CalComAPI(calcom_key)
            error_log = cal_api.get_error_log()
            if error_log:
                with st.expander("📋 Recent Error Log", expanded=False):
                    for i, error in enumerate(error_log):
                        st.write(f"**Error #{i+1}** ({error['timestamp']})")
                        st.write(f"Operation: {error['operation']}")
                        st.write(f"Error: {error['error']}")
                        if error['details']:
                            st.json(error['details'])
                        st.markdown("---")
        
        # Add diagnostic button
        if calcom_key and st.button("🔧 Test Cal.com Connection"):
            with st.spinner("Testing..."):
                test_api = CalComAPI(calcom_key)
                
                st.write("**Testing Event Types API...**")
                result = test_api.get_event_types()
                
                if result.get("success"):
                    event_types = result.get("event_types", [])
                    if event_types:
                        st.success(f"✅ Successfully found {len(event_types)} event types!")
                        
                        # Show all event types in detail
                        for i, et in enumerate(event_types):
                            st.write(f"**Event Type #{i+1}**")
                            st.json(et)
                            
                            # Generate availability link
                            et_id = et.get('id')
                            et_slug = et.get('slug', '')
                            if et_id:
                                st.markdown(f"🔗 [Check Availability for this Event Type](https://cal.com/event-types/{et_id})")
                            if et_slug:
                                st.info(f"Public booking link: https://cal.com/{et_slug}")
                            st.markdown("---")
                            
                        # Check for "interview" event type
                        interview_et = next((et for et in event_types if 'interview' in str(et.get('slug', '')).lower() or 'interview' in str(et.get('title', '')).lower()), None)
                        if interview_et:
                            st.success(f"🎯 Found your 'interview' event type! ID: {interview_et.get('id')}")
                        else:
                            st.warning("⚠️ Couldn't find event type with 'interview' in name/slug")
                    else:
                        st.error("⚠️ API connected but NO event types found!")
                        st.info("👉 This might mean:")
                        st.write("- Your API key doesn't have permission to read event types")
                        st.write("- The event type is in a team workspace (try personal API key)")
                        st.write("- The event type exists but API can't access it")
                else:
                    st.error(f"❌ API call failed: {result.get('error')}")
                    st.warning("Check your API key permissions")
        
        # Add booking test button
        if calcom_key and st.button("🧪 Test Booking Process"):
            with st.spinner("Testing booking process..."):
                test_api = CalComAPI(calcom_key)
                
                # Test with tomorrow's date
                from datetime import datetime, timedelta
                tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                
                st.write(f"**Testing booking process for {tomorrow}...**")
                
                # Test event types
                st.write("1. Testing event types...")
                evt_result = test_api.get_event_types()
                if not evt_result.get("success"):
                    st.error(f"❌ Event types failed: {evt_result.get('error')}")
                    return
                
                event_types = evt_result.get("event_types", [])
                if not event_types:
                    st.error("❌ No event types found")
                    return
                
                event_type_id = event_types[0].get("id")
                st.success(f"✅ Found event type: {event_types[0].get('title')} (ID: {event_type_id})")
                
                # Test slots
                st.write("2. Testing available slots...")
                la = _get_tz("America/Los_Angeles")
                utc = _get_tz("UTC")
                local_start = _localize_naive(datetime.strptime(tomorrow, "%Y-%m-%d"), la)
                local_end = (local_start + timedelta(days=1)) - timedelta(seconds=1)
                start_date = local_start.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                end_date = local_end.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                
                slots_result = test_api.get_available_slots(event_type_id, start_date, end_date)
                if not slots_result.get("success"):
                    st.error(f"❌ Slots check failed: {slots_result.get('error')}")
                    st.json(slots_result.get('error_details', {}))
                    return
                
                slots = slots_result.get("slots", [])
                if not slots:
                    st.warning("⚠️ No available slots found")
                    return
                
                st.success(f"✅ Found {len(slots)} available slots")
                
                # Test booking validation
                st.write("3. Testing booking validation...")
                test_payload = {
                    "eventTypeId": event_type_id,
                    "start": slots[0],
                    "attendee": {
                        "name": "Test User",
                        "email": "test@example.com",
                        "timeZone": "America/Los_Angeles",
                        "language": "en"
                    }
                }
                
                validation = test_api.validate_booking_payload(test_payload)
                if validation["valid"]:
                    st.success("✅ Booking payload validation passed")
                    if validation["warnings"]:
                        for warning in validation["warnings"]:
                            st.warning(f"⚠️ {warning}")
                else:
                    st.error(f"❌ Booking payload validation failed: {validation['errors']}")
                    return
                
                st.success("🎉 All booking tests passed! The booking system should work correctly.")
                
                # Show error log if there are any errors
                error_log = test_api.get_error_log()
                if error_log:
                    st.warning("⚠️ Some errors were logged during testing:")
                    for error in error_log[-3:]:  # Show last 3 errors
                        st.write(f"- {error['operation']}: {error['error']}")
                else:
                    st.info("✅ No errors logged during testing.")

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

    # Scheduled Events UI
    st.markdown("---")
    st.subheader("📆 Scheduled Events")
    st.caption("Filter by your email or attendee name. Shows booking links when available.")
    col_a, col_b = st.columns(2)
    with col_a:
        fetch_btn = st.button("🔄 Refresh Events")
    with col_b:
        show_all_btn = st.button("Show All Events")

    if fetch_btn or show_all_btn:
        email_filter = (user_email or "").strip()
        name_filter = (attendee_name or "").strip()
        try:
            result = cal_api.get_bookings(
                attendee_email=None if show_all_btn else (email_filter or None),
                attendee_name=None if show_all_btn else (name_filter or None),
            )
        except Exception as e:
            result = {"success": False, "error": str(e), "bookings": []}

        if result.get("success") and result.get("count", 0) > 0:
            bookings = result.get("bookings", [])
            st.success(f"Found {len(bookings)} scheduled event(s)")
            for b in bookings:
                start_text = b.get("start_pst") or b.get("start") or ""
                uid = b.get("display_uid") or b.get("uid") or b.get("id") or "N/A"
                pa_email = b.get("primary_attendee_email") or ""
                pa_name = b.get("primary_attendee_name") or ""
                who = pa_email or pa_name or "(no attendee)"
                booking_url = b.get("booking_url")
                reschedule_url = b.get("reschedule_url")
                cancel_url = b.get("cancel_url")

                parts = [f"{start_text}", f"UID: `{uid}`"]
                if who:
                    parts.insert(1, who)
                line = " — ".join(parts)
                st.markdown(line)
                links = []
                if booking_url:
                    links.append(f"[Open]({booking_url})")
                if reschedule_url:
                    links.append(f"[Reschedule]({reschedule_url})")
                if cancel_url:
                    links.append(f"[Cancel]({cancel_url})")
                if links:
                    st.markdown(" ".join(links))
                st.markdown("---")
        else:
            err = result.get("error")
            if err:
                st.error(f"Failed to fetch bookings: {err}")
            else:
                st.info("No scheduled events found for the given filters.")

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
                
                # Update system message with user email if provided
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
                    
                    # Log the error for debugging
                    cal_api._log_error("chat_with_assistant", str(e), {
                        "user_input": prompt,
                        "session_state_keys": list(st.session_state.keys()) if hasattr(st, 'session_state') else []
                    })

        st.session_state.messages.append({"role": "assistant", "content": response_text})
        st.rerun()


if __name__ == "__main__":
    main()
