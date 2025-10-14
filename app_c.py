import os
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import requests
from openai import OpenAI
import streamlit as st

# Configuration
OPENAI_API_KEY = "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"
CALCOM_API_KEY = os.getenv("CALCOM_API_KEY", "cal_live_5c4cb82d6ae5e45f14fd3209042256c3")  # Set this in your environment
CALCOM_BASE_URL = "https://api.cal.com/v2"

client = OpenAI(api_key=OPENAI_API_KEY)

# Cal.com API Functions
class CalComAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
    
    def get_event_types(self) -> List[Dict]:
        """Get available event types"""
        try:
            response = requests.get(
                f"{CALCOM_BASE_URL}/event-types",
                headers=self.headers
            )
            response.raise_for_status()
            data = response.json()
            return data.get("data", [])
        except Exception as e:
            return []
    
    def get_available_slots(self, event_type_id: int, start_date: str, end_date: str) -> List[str]:
        """Get available time slots for an event type"""
        try:
            response = requests.get(
                f"{CALCOM_BASE_URL}/slots/available",
                headers=self.headers,
                params={
                    "eventTypeId": event_type_id,
                    "startTime": start_date,
                    "endTime": end_date
                }
            )
            response.raise_for_status()
            data = response.json()
            slots = data.get("data", {}).get("slots", {})
            # Flatten slots from all dates
            available_slots = []
            for date, times in slots.items():
                available_slots.extend(times)
            return available_slots
        except Exception as e:
            return []
    
    def create_booking(self, event_type_id: int, start_time: str, attendee_email: str, 
                      attendee_name: str, attendee_timezone: str = "America/New_York",
                      meeting_reason: str = "") -> Dict:
        """Create a new booking"""
        try:
            payload = {
                "eventTypeId": event_type_id,
                "start": start_time,
                "attendee": {
                    "name": attendee_name,
                    "email": attendee_email,
                    "timeZone": attendee_timezone
                },
                "meetingUrl": "",
                "metadata": {
                    "reason": meeting_reason
                }
            }
            
            response = requests.post(
                f"{CALCOM_BASE_URL}/bookings",
                headers=self.headers,
                json=payload
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}
    
    def get_bookings(self, attendee_email: Optional[str] = None) -> List[Dict]:
        """Get all bookings, optionally filtered by attendee email"""
        try:
            params = {"status": "accepted"}
            if attendee_email:
                params["attendeeEmail"] = attendee_email
            
            response = requests.get(
                f"{CALCOM_BASE_URL}/bookings",
                headers=self.headers,
                params=params
            )
            response.raise_for_status()
            data = response.json()
            return data.get("data", [])
        except Exception as e:
            return []
    
    def cancel_booking(self, booking_id: int, reason: str = "Cancelled by user") -> Dict:
        """Cancel a booking"""
        try:
            response = requests.delete(
                f"{CALCOM_BASE_URL}/bookings/{booking_id}",
                headers=self.headers,
                json={"reason": reason}
            )
            response.raise_for_status()
            return {"success": True, "message": "Booking cancelled successfully"}
        except Exception as e:
            return {"error": str(e)}
    
    def reschedule_booking(self, booking_id: int, new_start_time: str, reason: str = "") -> Dict:
        """Reschedule a booking"""
        try:
            payload = {
                "start": new_start_time,
                "rescheduledReason": reason
            }
            
            response = requests.patch(
                f"{CALCOM_BASE_URL}/bookings/{booking_id}",
                headers=self.headers,
                json=payload
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}


# Function definitions for OpenAI
functions = [
    {
        "name": "get_available_slots",
        "description": "Get available time slots for booking a meeting. Use this before creating a booking.",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "The date for the meeting in YYYY-MM-DD format"
                },
                "event_type_id": {
                    "type": "integer",
                    "description": "The event type ID. Use 1 as default if not specified."
                }
            },
            "required": ["date"]
        }
    },
    {
        "name": "create_booking",
        "description": "Create a new booking/meeting",
        "parameters": {
            "type": "object",
            "properties": {
                "event_type_id": {
                    "type": "integer",
                    "description": "The event type ID"
                },
                "start_time": {
                    "type": "string",
                    "description": "The start time in ISO format (YYYY-MM-DDTHH:MM:SSZ)"
                },
                "attendee_email": {
                    "type": "string",
                    "description": "Email of the attendee"
                },
                "attendee_name": {
                    "type": "string",
                    "description": "Name of the attendee"
                },
                "meeting_reason": {
                    "type": "string",
                    "description": "Reason or purpose for the meeting"
                }
            },
            "required": ["start_time", "attendee_email", "attendee_name"]
        }
    },
    {
        "name": "get_bookings",
        "description": "Get all scheduled bookings for a user",
        "parameters": {
            "type": "object",
            "properties": {
                "attendee_email": {
                    "type": "string",
                    "description": "Email of the attendee to filter bookings"
                }
            },
            "required": ["attendee_email"]
        }
    },
    {
        "name": "cancel_booking",
        "description": "Cancel a specific booking",
        "parameters": {
            "type": "object",
            "properties": {
                "booking_id": {
                    "type": "integer",
                    "description": "The ID of the booking to cancel"
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for cancellation"
                }
            },
            "required": ["booking_id"]
        }
    },
    {
        "name": "reschedule_booking",
        "description": "Reschedule an existing booking to a new time",
        "parameters": {
            "type": "object",
            "properties": {
                "booking_id": {
                    "type": "integer",
                    "description": "The ID of the booking to reschedule"
                },
                "new_start_time": {
                    "type": "string",
                    "description": "The new start time in ISO format (YYYY-MM-DDTHH:MM:SSZ)"
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for rescheduling"
                }
            },
            "required": ["booking_id", "new_start_time"]
        }
    }
]


def execute_function(function_name: str, arguments: Dict, cal_api: CalComAPI) -> str:
    """Execute the called function and return results"""
    
    if function_name == "get_available_slots":
        date = arguments.get("date")
        event_type_id = arguments.get("event_type_id", 1)
        
        # Get slots for the entire day
        start_date = f"{date}T00:00:00Z"
        end_date = f"{date}T23:59:59Z"
        
        slots = cal_api.get_available_slots(event_type_id, start_date, end_date)
        
        if slots:
            return json.dumps({
                "available_slots": slots[:10],  # Limit to 10 slots
                "message": f"Found {len(slots)} available slots for {date}"
            })
        else:
            return json.dumps({
                "available_slots": [],
                "message": f"No available slots found for {date}"
            })
    
    elif function_name == "create_booking":
        result = cal_api.create_booking(
            event_type_id=arguments.get("event_type_id", 1),
            start_time=arguments["start_time"],
            attendee_email=arguments["attendee_email"],
            attendee_name=arguments["attendee_name"],
            meeting_reason=arguments.get("meeting_reason", "")
        )
        return json.dumps(result)
    
    elif function_name == "get_bookings":
        bookings = cal_api.get_bookings(arguments.get("attendee_email"))
        return json.dumps({
            "bookings": bookings,
            "count": len(bookings)
        })
    
    elif function_name == "cancel_booking":
        result = cal_api.cancel_booking(
            booking_id=arguments["booking_id"],
            reason=arguments.get("reason", "Cancelled by user")
        )
        return json.dumps(result)
    
    elif function_name == "reschedule_booking":
        result = cal_api.reschedule_booking(
            booking_id=arguments["booking_id"],
            new_start_time=arguments["new_start_time"],
            reason=arguments.get("reason", "")
        )
        return json.dumps(result)
    
    return json.dumps({"error": "Unknown function"})


def chat_with_assistant(messages: List[Dict], cal_api: CalComAPI) -> tuple:
    """Send messages to OpenAI and handle function calling"""
    
    response = client.chat.completions.create(
        model="gpt-4-turbo-preview",
        messages=messages,
        functions=functions,
        function_call="auto"
    )
    
    assistant_message = response.choices[0].message
    
    # Check if function call is needed
    if assistant_message.function_call:
        function_name = assistant_message.function_call.name
        function_args = json.loads(assistant_message.function_call.arguments)
        
        # Execute the function
        function_response = execute_function(function_name, function_args, cal_api)
        
        # Add function call and response to messages
        messages.append({
            "role": "assistant",
            "content": None,
            "function_call": {
                "name": function_name,
                "arguments": assistant_message.function_call.arguments
            }
        })
        
        messages.append({
            "role": "function",
            "name": function_name,
            "content": function_response
        })
        
        # Get the final response
        second_response = client.chat.completions.create(
            model="gpt-4-turbo-preview",
            messages=messages
        )
        
        return second_response.choices[0].message.content, messages
    
    else:
        return assistant_message.content, messages


# Streamlit UI
def main():
    st.set_page_config(page_title="Cal.com Chatbot", page_icon="📅", layout="wide")
    
    st.title("📅 Cal.com Meeting Assistant")
    st.markdown("Book, view, cancel, and reschedule your meetings through natural conversation!")
    
    # Sidebar for configuration
    with st.sidebar:
        st.header("⚙️ Configuration")
        
        calcom_key = st.text_input(
            "Cal.com API Key",
            value=CALCOM_API_KEY,
            type="password",
            help="Enter your Cal.com API key"
        )
        
        user_email = st.text_input(
            "Your Email",
            placeholder="user@example.com",
            help="Your email for booking management"
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
        st.session_state.messages = [{
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
            Be friendly and conversational!"""
        }]
    
    if not calcom_key:
        st.warning("⚠️ Please enter your Cal.com API key in the sidebar to get started.")
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
                if user_email and "user email is" not in working_messages[0]["content"].lower():
                    working_messages[0]["content"] += f"\n\nThe user's email is: {user_email}"
                
                response, updated_messages = chat_with_assistant(working_messages, cal_api)
                st.markdown(response)
        
        # Add assistant response to history
        st.session_state.messages.append({"role": "assistant", "content": response})
        st.rerun()


if __name__ == "__main__":
    main()