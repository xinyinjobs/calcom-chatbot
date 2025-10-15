import os
import json
from datetime import datetime, timedelta
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
def format_time_pst(iso_time: str) -> str:
    """Convert ISO time to readable PST format"""
    try:
        dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        pst_offset = timedelta(hours=-8)
        pst_time = dt + pst_offset
        return pst_time.strftime("%Y-%m-%d %I:%M %p PST")
    except:
        return iso_time


# Cal.com API Class
class CalComAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "cal-api-version": "2024-08-13"
        }

    def get_event_types(self) -> Dict[str, Any]:
        """Get available event types"""
        try:
            st.sidebar.info("📤 Fetching event types...")
            
            # Try v2 API first with Bearer token
            response = requests.get(
                "https://api.cal.com/v2/event-types",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "cal-api-version": "2024-08-13"
                },
                timeout=10
            )
            
            st.sidebar.info(f"📥 Event types response status: {response.status_code}")
            
            # If v2 fails, try v1
            if response.status_code >= 400:
                st.sidebar.warning(f"V2 failed ({response.status_code}), trying V1 API...")
                response = requests.get(
                    f"https://api.cal.com/v1/event-types?apiKey={self.api_key}",
                    headers={"Content-Type": "application/json"},
                    timeout=10
                )
                st.sidebar.info(f"📥 V1 response status: {response.status_code}")
            
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

            return {"success": True, "event_types": event_types}
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to fetch event types: {str(e)}"
            if hasattr(e, "response") and e.response is not None:
                error_msg += f"\n{e.response.text}"
            st.sidebar.error(error_msg)
            return {"success": False, "error": error_msg, "event_types": []}

    def get_available_slots(self, event_type_id: Any, start_date: str, end_date: str) -> Dict[str, Any]:
        """Get available time slots"""
        try:
            st.sidebar.info(f"🔍 Checking slots for event type {event_type_id}")
            st.sidebar.info(f"Date range: {start_date} to {end_date}")
            
            # Try v2 API first
            try:
                response = requests.get(
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
                    },
                    timeout=10
                )
                st.sidebar.info(f"V2 API Status: {response.status_code}")
            except Exception as v2_error:
                st.sidebar.warning(f"V2 API failed: {str(v2_error)}, trying v1...")
                # Fallback to v1 API with query params
                response = requests.get(
                    f"https://api.cal.com/v1/slots?apiKey={self.api_key}",
                    params={
                        "eventTypeId": event_type_id,
                        "startTime": start_date,
                        "endTime": end_date,
                    },
                    timeout=10
                )
                st.sidebar.info(f"V1 API Status: {response.status_code}")
            
            response.raise_for_status()
            data = response.json()
            
            st.sidebar.code(f"Raw response: {json.dumps(data, indent=2)[:1000]}", language="json")
            
            # Parse slots from response - handle multiple formats
            slots = []
            
            # Try different response structures
            if "data" in data:
                slots_data = data["data"]
                
                # Structure 1: {data: {slots: {"2024-10-16": ["time1", "time2"]}}}
                if isinstance(slots_data, dict) and "slots" in slots_data:
                    inner_slots = slots_data["slots"]
                    if isinstance(inner_slots, dict):
                        for date_key, time_list in inner_slots.items():
                            if isinstance(time_list, list):
                                for slot in time_list:
                                    if isinstance(slot, str):
                                        slots.append(slot)
                                    elif isinstance(slot, dict) and "time" in slot:
                                        slots.append(slot["time"])
                    elif isinstance(inner_slots, list):
                        slots = inner_slots
                
                # Structure 2: {data: ["time1", "time2"]}
                elif isinstance(slots_data, list):
                    slots = slots_data
            
            # Structure 3: {slots: [...]}
            elif "slots" in data:
                slots_data = data["slots"]
                if isinstance(slots_data, dict):
                    for date_key, time_list in slots_data.items():
                        if isinstance(time_list, list):
                            slots.extend(time_list)
                elif isinstance(slots_data, list):
                    slots = slots_data

            st.sidebar.success(f"📅 Found {len(slots)} available slots")
            if slots:
                st.sidebar.info(f"First slot example: {slots[0]}")
            
            return {
                "success": True, 
                "slots": slots, 
                "count": len(slots),
                "event_type_id": event_type_id  # Pass this along for booking
            }
        except requests.exceptions.RequestException as e:
            error_msg = str(e)
            error_details = ""
            if hasattr(e, "response") and e.response is not None:
                error_details = f"Status: {e.response.status_code} | Response: {e.response.text[:500]}"
                error_msg += f" | {error_details}"
            
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
        """Create a new booking"""
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

            st.sidebar.info("📤 Creating booking...")
            st.sidebar.code(json.dumps(payload, indent=2), language="json")
            st.sidebar.info(f"🌍 Timezone: {attendee_timezone} (PDT) | 🗣️ Language: {attendee_language}")

            response = requests.post(
                f"https://api.cal.com/v1/bookings?apiKey={self.api_key}", 
                headers=self.headers, 
                json=payload, 
                timeout=15
            )
            
            st.sidebar.info(f"📥 Response: {response.status_code}")
            
            if response.status_code >= 400:
                st.sidebar.error(f"Error response: {response.text}")
            
            response.raise_for_status()
            result = response.json()
            booking_data = result.get("data", {})

            st.sidebar.success(f"✅ Booking created! ID: {booking_data.get('id')}, UID: {booking_data.get('uid')}")
            return {
                "success": True, 
                "data": booking_data, 
                "booking_id": booking_data.get("id"), 
                "booking_uid": booking_data.get("uid")
            }
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to create booking: {str(e)}"
            if hasattr(e, "response") and e.response is not None:
                error_msg += f"\nStatus: {e.response.status_code}\nResponse: {e.response.text}"
            st.sidebar.error(error_msg)
            return {"success": False, "error": error_msg}

    def get_bookings(self, attendee_email: Optional[str] = None) -> Dict[str, Any]:
        """Get all bookings"""
        try:
            params = {}
            if attendee_email:
                params["attendeeEmail"] = attendee_email

            st.sidebar.info(f"📤 Fetching bookings: {params}")
            response = requests.get(
                f"https://api.cal.com/v1/bookings?apiKey={self.api_key}",
                headers=self.headers, 
                params=params, 
                timeout=15
            )
            
            st.sidebar.info(f"📥 Response: {response.status_code}")
            response.raise_for_status()
            data = response.json()
            bookings = data.get("data", [])

            # Format times and add UIDs
            for booking in bookings:
                if "start" in booking:
                    booking["start_pst"] = format_time_pst(booking["start"])
                booking["display_uid"] = booking.get("uid", "N/A")

            st.sidebar.success(f"✅ Found {len(bookings)} bookings")
            return {"success": True, "bookings": bookings, "count": len(bookings)}
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to get bookings: {str(e)}"
            if hasattr(e, "response") and e.response is not None:
                error_msg += f"\nStatus: {e.response.status_code}\nResponse: {e.response.text}"
            st.sidebar.error(error_msg)
            return {"success": False, "error": error_msg, "bookings": []}

    def cancel_booking(self, booking_uid: str, reason: str = "Cancelled by user") -> Dict[str, Any]:
        """Cancel a booking"""
        try:
            st.sidebar.info(f"📤 Cancelling UID: {booking_uid}")
            response = requests.delete(
                f"https://api.cal.com/v1/bookings/{booking_uid}/cancel?apiKey={self.api_key}", 
                headers=self.headers, 
                json={"cancellationReason": reason}, 
                timeout=15
            )
            
            st.sidebar.info(f"📥 Response: {response.status_code}")
            response.raise_for_status()
            st.sidebar.success("✅ Booking cancelled!")
            return {"success": True, "message": f"Booking {booking_uid} cancelled"}
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to cancel: {str(e)}"
            if hasattr(e, "response") and e.response is not None:
                error_msg += f"\nStatus: {e.response.status_code}\nResponse: {e.response.text}"
            st.sidebar.error(error_msg)
            return {"success": False, "error": error_msg}

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
            "description": "Get all bookings. Always show the booking UID in your response.",
            "parameters": {
                "type": "object",
                "properties": {
                    "attendee_email": {"type": "string"}
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
        if not event_type_id and 'manual_event_id' in st.session_state:
            event_type_id = st.session_state.manual_event_id
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

        start_date = f"{date}T00:00:00Z"
        end_date = f"{date}T23:59:59Z"
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
        # Convert PST time to UTC for booking
        date = arguments.get("date")
        time = arguments.get("time")  # Format: "14:00"
        
        try:
            # Get event type first
            event_type_id = arguments.get("event_type_id")
            
            # Check for manual override
            if not event_type_id and 'manual_event_id' in st.session_state:
                event_type_id = st.session_state.manual_event_id
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
            
            # Parse PST time
            pst_datetime = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
            # Add 8 hours to convert PST to UTC
            utc_datetime = pst_datetime + timedelta(hours=8)
            start_time = utc_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")
            
            st.sidebar.info(f"Converting {date} {time} PST → {start_time} UTC")
            
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
        if not event_type_id and 'manual_event_id' in st.session_state:
            event_type_id = st.session_state.manual_event_id
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
        result = cal_api.get_bookings(arguments.get("attendee_email"))
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
    
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
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
        second_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages
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
                st.session_state.manual_event_id = manual_event_id
                st.success(f"✅ Will use event type ID: {manual_event_id}")
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
            if 'manual_event_id' in st.session_state:
                event_id = st.session_state.manual_event_id
                st.markdown(f"[📅 Check Availability](https://cal.com/event-types/{event_id})")
            st.markdown("[⚙️ Manage Event Types](https://app.cal.com/event-types)")
            st.markdown("[🔑 API Keys](https://app.cal.com/settings/developer/api-keys)")

        if st.button("Clear Chat History"):
            st.session_state.messages = []
            st.rerun()

        st.markdown("---")
        st.markdown("### 🔍 Debug Info")
        
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
- Format: "Meeting at [time] (UID: [uid])"

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

        st.session_state.messages.append({"role": "assistant", "content": response_text})
        st.rerun()


if __name__ == "__main__":
    main()
