import os
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import requests
from openai import OpenAI
import streamlit as st

# Configuration
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", "")
CALCOM_API_KEY = os.getenv(
    "CALCOM_API_KEY", "cal_live_5c4cb82d6ae5e45f14fd3209042256c3"
)  # Set this in your environment
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


# Cal.com API Functions
class CalComAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "cal-api-version": "2024-08-13",  # CRITICAL: Required for V2 API!
        }

    def get_event_types(self) -> List[Dict]:
        """Get available event types"""
        try:
            response = requests.get(
                f"{CALCOM_BASE_URL}/event-types", headers=self.headers
            )
            response.raise_for_status()
            data = response.json()
            event_types = data.get("data", [])

            if event_types:
                st.sidebar.success(f"✅ Found {len(event_types)} event types")
            else:
                st.sidebar.warning(
                    "⚠️ No event types found. Configure them in Cal.com first."
                )

            return event_types
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to fetch event types: {str(e)}"
            if hasattr(e, "response") and e.response is not None:
                error_msg += f"\n```\n{e.response.text}\n```"
            st.sidebar.error(error_msg)
            return []

    def get_available_slots(
        self, event_type_id: int, start_date: str, end_date: str
    ) -> List[str]:
        """Get available time slots for an event type"""
        try:
            response = requests.get(
                f"{CALCOM_BASE_URL}/slots/available",
                headers=self.headers,
                params={
                    "eventTypeId": event_type_id,
                    "startTime": start_date,
                    "endTime": end_date,
                },
            )
            response.raise_for_status()
            data = response.json()
            slots = data.get("data", {}).get("slots", {})
            # Flatten slots from all dates
            available_slots = []
            for date, times in slots.items():
                available_slots.extend(times)
            return {
                "success": True,
                "slots": available_slots,
                "count": len(available_slots),
            }
        except requests.exceptions.RequestException as e:
            error_msg = str(e)
            if hasattr(e, "response") and e.response is not None:
                error_msg += f" | Response: {e.response.text}"
            return {"success": False, "error": error_msg, "slots": []}

    def create_booking(
        self,
        event_type_id: int,
        start_time: str,
        attendee_email: str,
        attendee_name: str,
        attendee_timezone: str = "America/New_York",
        meeting_reason: str = "",
    ) -> Dict:
        """Create a new booking"""
        try:
            payload = {
                "eventTypeId": event_type_id,
                "start": start_time,
                "attendee": {
                    "name": attendee_name,
                    "email": attendee_email,
                    "timeZone": attendee_timezone,
                },
            }

            if meeting_reason:
                payload["metadata"] = {"reason": meeting_reason}

            st.sidebar.info(f"📤 Creating booking...")
            st.sidebar.code(json.dumps(payload, indent=2), language="json")

            response = requests.post(
                f"{CALCOM_BASE_URL}/bookings", headers=self.headers, json=payload
            )

            st.sidebar.info(f"📥 Response: {response.status_code}")

            response.raise_for_status()
            result = response.json()

            booking_data = result.get("data", {})
            st.sidebar.success(
                f"✅ Booking created! ID: {booking_data.get('id')}, UID: {booking_data.get('uid')}"
            )

            return {
                "success": True,
                "data": booking_data,
                "booking_id": booking_data.get("id"),
                "booking_uid": booking_data.get("uid"),
            }
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to create booking: {str(e)}"
            if hasattr(e, "response") and e.response is not None:
                error_msg += f"\nStatus: {e.response.status_code}\nResponse:\n```\n{e.response.text}\n```"
            st.sidebar.error(error_msg)
            return {"success": False, "error": error_msg}

    def get_bookings(self, attendee_email: Optional[str] = None) -> List[Dict]:
        """Get all bookings, optionally filtered by attendee email"""
        try:
            params = {}
            if attendee_email:
                params["attendeeEmail"] = attendee_email

            st.sidebar.info(f"📤 Fetching bookings with filters: {params}")

            response = requests.get(
                f"{CALCOM_BASE_URL}/bookings", headers=self.headers, params=params
            )

            st.sidebar.info(f"📥 Response: {response.status_code}")

            response.raise_for_status()
            data = response.json()
            bookings = data.get("data", [])

            # Add formatted times
            for booking in bookings:
                if "start" in booking:
                    booking["start_pst"] = format_time_pst(booking["start"])

            st.sidebar.success(f"✅ Found {len(bookings)} bookings")

            return {"success": True, "bookings": bookings, "count": len(bookings)}
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to get bookings: {str(e)}"
            if hasattr(e, "response") and e.response is not None:
                error_msg += f"\nStatus: {e.response.status_code}\nResponse:\n```\n{e.response.text}\n```"
            st.sidebar.error(error_msg)
            return {"success": False, "error": error_msg, "bookings": []}

    def cancel_booking(
        self, booking_id: int, reason: str = "Cancelled by user"
    ) -> Dict:
        """Cancel a booking"""
        try:
            st.sidebar.info(f"📤 Cancelling booking UID: {booking_uid}")

            response = requests.delete(
                f"{CALCOM_BASE_URL}/bookings/{booking_uid}",
                headers=self.headers,
                json={"cancellationReason": reason},
            )

            st.sidebar.info(f"📥 Response: {response.status_code}")

            response.raise_for_status()
            st.sidebar.success(f"✅ Booking cancelled!")

            return {
                "success": True,
                "message": f"Booking {booking_uid} cancelled successfully",
            }
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to cancel: {str(e)}"
            if hasattr(e, "response") and e.response is not None:
                error_msg += f"\nStatus: {e.response.status_code}\nResponse:\n```\n{e.response.text}\n```"
            st.sidebar.error(error_msg)
            return {"success": False, "error": error_msg}

    def reschedule_booking(
        self, booking_id: int, new_start_time: str, reason: str = ""
    ) -> Dict:
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
            )

            st.sidebar.info(f"📥 Response: {response.status_code}")

            response.raise_for_status()
            result = response.json()

            st.sidebar.success(f"✅ Booking rescheduled!")

            return {"success": True, "data": result.get("data")}
        except requests.exceptions.RequestException as e:
            error_msg = f"❌ Failed to reschedule: {str(e)}"
            if hasattr(e, "response") and e.response is not None:
                error_msg += f"\nStatus: {e.response.status_code}\nResponse:\n```\n{e.response.text}\n```"
            st.sidebar.error(error_msg)
            return {"success": False, "error": error_msg}


# Function definitions for OpenAI
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_available_slots",
            "description": "Get available time slots for booking. Times are in PST.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format",
                    },
                    "event_type_id": {
                        "type": "integer",
                        "description": "Event type ID (optional)",
                    },
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": "Create a new booking. Timezone is PST.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type_id": {"type": "integer"},
                    "start_time": {
                        "type": "string",
                        "description": "ISO format (YYYY-MM-DDTHH:MM:SSZ)",
                    },
                    "attendee_email": {"type": "string"},
                    "attendee_name": {"type": "string"},
                    "meeting_reason": {"type": "string"},
                },
                "required": ["start_time", "attendee_email", "attendee_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bookings",
            "description": "Get all bookings. Times shown in PST.",
            "parameters": {
                "type": "object",
                "properties": {"attendee_email": {"type": "string"}},
                "required": ["attendee_email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_booking",
            "description": "Cancel a booking using its UID (not ID).",
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_uid": {
                        "type": "string",
                        "description": "The booking UID (string)",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["booking_uid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_booking",
            "description": "Reschedule a booking using its UID (not ID).",
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_uid": {
                        "type": "string",
                        "description": "The booking UID (string)",
                    },
                    "new_start_time": {"type": "string", "description": "ISO format"},
                    "reason": {"type": "string"},
                },
                "required": ["booking_uid", "new_start_time"],
            },
        },
    },
]


def execute_function(function_name: str, arguments: Dict, cal_api: CalComAPI) -> str:
    """Execute the called function and return results"""

    if function_name == "get_available_slots":
        date = arguments.get("date")
        event_type_id = arguments.get("event_type_id")

        if not event_type_id:
            event_types = cal_api.get_event_types()
            if event_types:
                event_type_id = event_types[0].get("id", 1)
            else:
                return json.dumps(
                    {"success": False, "error": "No event types configured"}
                )

        # Get slots for the entire day
        start_date = f"{date}T00:00:00Z"
        end_date = f"{date}T23:59:59Z"

        result = cal_api.get_available_slots(event_type_id, start_date, end_date)

        if result.get("success"):
            slots = result.get("slots", [])
            formatted_slots = [format_time_pst(slot) for slot in slots[:10]]
            return json.dumps(
                {
                    "success": True,
                    "available_slots": formatted_slots,
                    "raw_slots": slots[:10],
                    "message": f"Found {len(slots)} slots (PST)",
                }
            )
        return json.dumps(result)

    elif function_name == "create_booking":
        result = cal_api.create_booking(
            event_type_id=arguments.get("event_type_id", 1),
            start_time=arguments["start_time"],
            attendee_email=arguments["attendee_email"],
            attendee_name=arguments["attendee_name"],
            attendee_timezone="America/Los_Angeles",
            meeting_reason=arguments.get("meeting_reason", ""),
        )
        return json.dumps(result)

    elif function_name == "get_bookings":
        result = cal_api.get_bookings(arguments.get("attendee_email"))
        return json.dumps(result)

    elif function_name == "cancel_booking":
        result = cal_api.cancel_booking(
            booking_uid=arguments["booking_uid"],
            reason=arguments.get("reason", "Cancelled by user"),
        )
        return json.dumps(result)

    elif function_name == "reschedule_booking":
        result = cal_api.reschedule_booking(
            booking_uid=arguments["booking_uid"],
            new_start_time=arguments["new_start_time"],
            reason=arguments.get("reason", ""),
        )
        return json.dumps(result)

    return json.dumps({"success": False, "error": "Unknown function"})


def chat_with_assistant(messages: List[Dict], cal_api: CalComAPI) -> tuple:
    """Send messages to OpenAI and handle function calling"""

    response = client.chat.completions.create(
        model="gpt-4-turbo-preview",
        messages=messages,
        functions=functions,
        function_call="auto",
    )

    assistant_message = response.choices[0].message

    # Check if function call is needed
    if assistant_message.function_call:
        function_name = assistant_message.function_call.name
        function_args = json.loads(assistant_message.function_call.arguments)

        # Execute the function
        function_response = execute_function(function_name, function_args, cal_api)

        # Add function call and response to messages
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "function_call": {
                    "name": function_name,
                    "arguments": assistant_message.function_call.arguments,
                },
            }
        )

        messages.append(
            {"role": "function", "name": function_name, "content": function_response}
        )

        # Get the final response
        second_response = client.chat.completions.create(
            model="gpt-4-turbo-preview", messages=messages
        )

        return second_response.choices[0].message.content, messages

    else:
        return assistant_message.content, messages


# Streamlit UI
def main():
    st.set_page_config(page_title="Cal.com Chatbot", page_icon="📅", layout="wide")

    st.title("📅 Cal.com Meeting Assistant")
    st.markdown(
        "Book, view, cancel, and reschedule your meetings through natural conversation!"
    )

    # Sidebar for configuration
    with st.sidebar:
        st.header("⚙️ Configuration")

        calcom_key = st.text_input(
            "Cal.com API Key",
            value=CALCOM_API_KEY,
            type="password",
            help="Enter your Cal.com API key",
        )

        user_email = st.text_input(
            "Your Email",
            placeholder="user@example.com",
            help="Your email for booking management",
        )

        st.markdown("---")
        st.markdown("### 💡 Try saying:")
        st.markdown("- Help me book a meeting")
        st.markdown("- Show me my scheduled events")
        st.markdown("- Cancel my meeting at 3pm today")
        st.markdown("- Reschedule my 2pm meeting to 4pm")

        if st.button("Clear Chat History"):
            st.session_state.messages = []
            st.rerun()

    # Initialize session state
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {
                "role": "system",
                "content": f"""You are a helpful meeting assistant that helps users manage their Cal.com calendar. 
            You can help them book meetings, view scheduled events, cancel bookings, and reschedule meetings.
            
            When booking meetings, always ask for:
            1. The date and time
            2. Their email (if not already provided: {user_email})
            3. Their name
            4. The reason for the meeting
            
            Always check for available slots before booking.
            When showing bookings, format them clearly with date, time, and details.
            For cancellations, confirm the specific booking before cancelling.
            Be friendly and conversational!""",
            }
        ]

    if not calcom_key:
        st.warning(
            "⚠️ Please enter your Cal.com API key in the sidebar to get started."
        )
        return

    cal_api = CalComAPI(calcom_key)

    # Display chat messages
    for message in st.session_state.messages:
        if message["role"] in ["user", "assistant"]:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

    # Chat input
    if prompt := st.chat_input("Ask me about your meetings..."):
        # Add user message
        st.session_state.messages.append({"role": "user", "content": prompt})

        with st.chat_message("user"):
            st.markdown(prompt)

        # Get assistant response
        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                # Inject user email into the conversation context if provided
                working_messages = st.session_state.messages.copy()
                if (
                    user_email
                    and "user email is" not in working_messages[0]["content"].lower()
                ):
                    working_messages[0][
                        "content"
                    ] += f"\n\nThe user's email is: {user_email}"

                response, updated_messages = chat_with_assistant(
                    working_messages, cal_api
                )
                st.markdown(response)

        # Add assistant response to history
        st.session_state.messages.append({"role": "assistant", "content": response})
        st.rerun()


if __name__ == "__main__":
    main()
