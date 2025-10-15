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
            response = requests.get(f"{CALCOM_BASE_URL}/event-types", headers=self.headers)
            response.raise_for_status()
            data = response.json()
            event_types = data.get("data", [])

            if event_types:
                st.sidebar.success(f"✅ Found {len(event_types)} event types")
                # Show event type details for debugging
                for et in event_types[:3]:  # Show first 3
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
            response = requests.get(
                f"{CALCOM_BASE_URL}/slots/available",
                headers=self.headers,
                params={"eventTypeId": event_type_id, "startTime": start_date, "endTime": end_date},
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
            
            slots_container = data.get("data", {})
            slots = []
            if isinstance(slots_container, dict):
                inner = slots_container.get("slots", {})
                for date_key, times in inner.items():
                    if isinstance(times, list):
                        slots.extend(times)
            elif isinstance(slots_container, list):
                slots = slots_container

            st.sidebar.info(f"📅 Found {len(slots)} available slots")
            return {"success": True, "slots": slots, "count": len(slots)}
        except requests.exceptions.RequestException as e:
            error_msg = str(e)
            if hasattr(e, "response") and e.response is not None:
                error_msg += f" | {e.response.text}"
            st.sidebar.error(f"❌ Slot check failed: {error_msg}")
            return {"success": False, "error": error_msg, "slots": []}

    def create_booking(
        self,
        event_type_id: Any,
        start_time: str,
        attendee_email: str,
        attendee_name: str,
        attendee_timezone: str = "America/Los_Angeles",
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
                    "timeZone": attendee_timezone
                }
            }

            if meeting_reason:
                payload["metadata"] = {"reason": meeting_reason}

            st.sidebar.info("📤 Creating booking...")
            st.sidebar.code(json.dumps(payload, indent=2), language="json")

            response = requests.post(
                f"{CALCOM_BASE_URL}/bookings", 
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
                f"{CALCOM_BASE_URL}/bookings", 
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
                # Make sure UID is visible
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
                f"{CALCOM_BASE_URL}/bookings/{booking_uid}", 
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
                f"{CALCOM_BASE_URL}/bookings/{booking_uid}", 
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
            "name": "get_available_slots",
            "description": "Get available time slots for booking. Times are in PST/PDT.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "event_type_id": {"type": "integer", "description": "Event type ID (optional)"}
                },
                "required": ["date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": "Create a new booking. Timezone is PST/PDT.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type_id": {"type": "integer"},
                    "start_time": {"type": "string", "description": "ISO format (YYYY-MM-DDTHH:MM:SSZ)"},
                    "attendee_email": {"type": "string"},
                    "attendee_name": {"type": "string"},
                    "meeting_reason": {"type": "string"}
                },
                "required": ["start_time", "attendee_email", "attendee_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_bookings",
            "description": "Get all bookings. Always show the booking UID in your response so users can cancel/reschedule.",
            "parameters": {
                "type": "object",
                "properties": {
                    "attendee_email": {"type": "string"}
                },
                "required": ["attendee_email"]
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
    
    if function_name == "get_available_slots":
        date = arguments.get("date")
        event_type_id = arguments.get("event_type_id")

        if not event_type_id:
            evt_resp = cal_api.get_event_types()
            if evt_resp.get("success") and evt_resp.get("event_types"):
                event_type_id = evt_resp["event_types"][0].get("id")
            else:
                return json.dumps({"success": False, "error": "No event types configured in Cal.com"})

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
                "message": f"Found {len(slots)} available slots for {date} (PST/PDT)"
            })
        return json.dumps(result)

    elif function_name == "create_booking":
        result = cal_api.create_booking(
            event_type_id=arguments.get("event_type_id", 1),
            start_time=arguments["start_time"],
            attendee_email=arguments["attendee_email"],
            attendee_name=arguments["attendee_name"],
            attendee_timezone="America/Los_Angeles",
            meeting_reason=arguments.get("meeting_reason", "")
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

        st.info("🕐 All times shown in PST/PDT")

        st.markdown("---")
        st.markdown("### 💡 Try saying:")
        st.markdown("- Help me book a meeting for tomorrow at 2pm")
        st.markdown("- Show me my scheduled events")
        st.markdown("- Cancel my meeting")
        st.markdown("- Reschedule my meeting to 4pm")

        if st.button("Clear Chat History"):
            st.session_state.messages = []
            st.rerun()

        st.markdown("---")
        st.markdown("### 🔍 Debug Info")

    # Initialize session state
    if "messages" not in st.session_state:
        st.session_state.messages = [{
            "role": "system",
            "content": f"""You are a helpful meeting assistant for Cal.com calendar management.
            
IMPORTANT: All times are in PST/PDT timezone (America/Los_Angeles).

When booking:
1. Ask for date/time, email (default: {user_email}), name, and reason
2. Always check available slots first
3. Confirm the booking details

When listing bookings:
- ALWAYS show the booking UID for each meeting
- Format: "Meeting at [time] (UID: [uid])"
- This lets users cancel/reschedule

For cancel/reschedule:
1. First call get_bookings to get UIDs
2. Show user their bookings with UIDs
3. Use the UID (not ID) for the operation

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
                if user_email and "default:" not in working_messages[0]["content"]:
                    working_messages[0]["content"] = working_messages[0]["content"].replace(
                        f"default: {user_email}", 
                        f"default: {user_email}"
                    )

                try:
                    response_text, _ = chat_with_assistant(working_messages, cal_api)
                    st.markdown(response_text or "No response")
                except Exception as e:
                    response_text = f"Error: {str(e)}"
                    st.error(response_text)

        st.session_state.messages.append({"role": "assistant", "content": response_text})
        st.rerun()


if __name__ == "__main__":
    main()
