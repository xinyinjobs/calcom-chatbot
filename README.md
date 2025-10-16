Cal.com Meeting Assistant (Streamlit)
A Streamlit app that lets you book, list, cancel, and reschedule Cal.com meetings through a conversational UI powered by OpenAI.

Features
Conversational assistant to manage meetings via OpenAI
Book meetings by intent (e.g., “interview”, “30 Minute Meeting”) with auto event-type matching
Check availability and book specific event types
List scheduled events with filters, sorting, and quick action links
Cancel and reschedule (v2-first API strategy with v1 fallback)
Robust HTTP retry with exponential backoff and short-lived GET caching
Timezone-aware display and parsing (PST/PDT, America/Los_Angeles)
Sidebar tools: configuration, quick links, manual event type override, diagnostics, clear chat/error log
Requirements
Python 3.9+ recommended (uses zoneinfo); for Python < 3.9 install pytz
OpenAI API key with access to the specified model (defaults to gpt-4o-mini)
Cal.com API key with permissions to read/write bookings and list event types
Install
# from repo root
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
Configuration (choose one)
Environment variables (local development):
export OPENAI_API_KEY="sk-..."
export CALCOM_API_KEY="cal_..."
# Optional: force “today” for natural date parsing (YYYY-MM-DD)
export TODAY_OVERRIDE="2025-10-16"
Streamlit secrets (Streamlit Cloud or local .streamlit/secrets.toml):
# .streamlit/secrets.toml
OPENAI_API_KEY = "sk-..."
CALCOM_API_KEY = "cal_..."
Optional .env via python-dotenv:
# .env
OPENAI_API_KEY=sk-...
CALCOM_API_KEY=cal_...
TODAY_OVERRIDE=2025-10-16

# run with dotenv
python -m dotenv run -- streamlit run app_c.py
Run
streamlit run app_c.py
Then open the URL shown in your terminal.

Using the App
Sidebar
Enter your Cal.com API key
Provide your email (to filter/list bookings) and optional attendee name
Optionally set a manual Event Type ID if auto-detection doesn’t match your intent
Quick links to Cal.com dashboard pages
Utilities: Clear Chat History, Clear Error Log
Chat examples
“Help me book a meeting for tomorrow at 2pm”
“Show me my scheduled events”
“Cancel my meeting”
“Reschedule my meeting to 4pm”
Scheduled Events view
Filter by status (Upcoming, Today, This Week, Past, Cancelled)
Sort by date or status
Quick links to Join / Reschedule / Cancel when available
How it Works (High level)
UI: Streamlit app with a chat interface and a rich “Scheduled Events” section
AI: OpenAI Chat Completions with function calling to drive booking flows
Cal.com: v2 REST API by default with v1 fallback; parses multiple response shapes
Reliability: Exponential backoff on server errors; 30s cache for identical GETs
Cal.com API Endpoints (examples)
Event Types: GET https://api.cal.com/v2/event-types
Availability: POST https://api.cal.com/v2/slots
Create Booking: POST https://api.cal.com/v2/bookings
List Bookings: GET https://api.cal.com/v2/bookings
Cancel Booking: POST https://api.cal.com/v2/bookings/{uid}/cancel
Reschedule: v2 endpoint preferred; falls back to “cancel + create” strategy when needed
Fallbacks: v1 equivalents are used automatically if v2 fails (with apiKey query param)
Timezone Notes
All date parsing and display is in PST/PDT (America/Los_Angeles)
Optional TODAY_OVERRIDE lets you pin “today” for deterministic testing
Troubleshooting
OpenAI key error: “OpenAI API key not configured.” — set OPENAI_API_KEY or Streamlit secrets
No event types: Ensure your Cal.com API key has access and event types are configured
No slots found:
Event type has no availability on that date
All slots are booked
Date/time outside configured availability window
Invalid time input: Use ISO 8601 or natural phrases like “tomorrow 2pm” (interpreted in LA time)
Rate limits/server errors: The app retries automatically with exponential backoff
Development
Main app: app_c.py
Dependencies: requirements.txt (OpenAI, Streamlit, Requests, python-dotenv)
Python < 3.9: install pytz if zoneinfo is unavailable
Security
Keep your API keys in environment variables or Streamlit secrets (do not commit them)
Error logs are kept in-memory for the session and can be cleared from the sidebar
License
Add a LICENSE file if you intend to distribute this project.
