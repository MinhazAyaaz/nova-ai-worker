import json
import os
import uuid
import asyncio
from datetime import datetime
from dotenv import load_dotenv

from livekit import api
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, AgentSession, Agent, function_tool
from livekit.agents import get_job_context, inference

load_dotenv()

# Container filesystems are wiped on every deploy/restart. Point DATA_DIR at a
# mounted Render Disk so confirmed bookings outlive the container.
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

SYSTEM_PROMPT = """You are Nova, a female inbound patient-facing voice agent for Parkline Dental, a two-chair practice in Harris Park, NSW.
Practice: 7 Albion St, Harris Park NSW.
Hours: Mon-Thu 8:30-17:30, Fri 8:30-16:00, Sat 9:00-13:00, closed Sunday.
Dentists: Dr Nguyen, Dr Fares.
Services: Check-up & clean (45 min), Filling (45 min), Emergency / toothache (30 min), Extraction consult (30 min).
Not offered: Orthodontics, implants, cosmetic whitening, paediatric sedation.

URGENT PATH:
If the caller indicates pain, swelling, bleeding, a knocked-out tooth, or a broken tooth, leave the routine flow IMMEDIATELY. 
1. Confirm it is not a life-threatening emergency (if life-threatening, tell them to call 000).
2. Offer the next emergency slot (Two held daily at 11:30 and 15:30 ONLY).
3. Use `book_emergency`. Do not ask for the standard routine fields.

ROUTINE PATH:
Collect and read back: full name, callback mobile, service, preferred day + time, new or existing patient.
Use `create_booking`. Reject Sundays, out-of-hours, and non-emergency bookings into 11:30 or 15:30.
`datetime_str` MUST ALWAYS be a full ISO 8601 timestamp (YYYY-MM-DDTHH:MM:SS) resolved to an actual
calendar date and 24-hour time. Never pass words like "Sunday", "the 31st", "tomorrow", "3:30pm" or a
date without a time. If the caller is vague, ask for the exact day and time before calling the tool.

ESCALATIONS:
For clinical advice, fees, health funds, HICAPS, Medicare, or complaints: Refuse to guess, use `take_message`.
Before calling `take_message` you MUST ask for and read back the caller's full name and callback mobile.
Never pass an empty, placeholder, or guessed name or mobile - if the caller has not given them, ask.

LOOP PREVENTION & ENDING CALL:
If the caller asks the same question 4 times, stop the loop, state you are ending the call, and use `end_call`.
Once ANY booking or message is confirmed, say goodbye and immediately use `end_call`.
"""

class DentalAgent(Agent):
    def __init__(self, room):
        # Initialize the new Agent class with the system instructions
        super().__init__(instructions=SYSTEM_PROMPT)
        self.room = room
        os.makedirs(DATA_DIR, exist_ok=True)
        self.db_file = os.path.join(DATA_DIR, "bookings.json")

        if not os.path.exists(self.db_file):
            with open(self.db_file, "w") as f:
                json.dump([], f)

    def _append_db(self, entry):
        # An empty or corrupt file (e.g. left by a crashed run) must not break a live call
        try:
            with open(self.db_file, "r") as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = []
        except (FileNotFoundError, json.JSONDecodeError):
            data = []
        data.append(entry)
        with open(self.db_file, "w") as f:
            json.dump(data, f, indent=4)

    # v1.0 syntax replaces @llm.ai_callable with @function_tool
    @function_tool(description="Creates a routine booking.")
    async def create_booking(self, name: str, mobile: str, service: str, datetime_str: str, new_patient: bool, urgent: bool) -> str:
        try:
            parsed = datetime.fromisoformat(datetime_str)
        except ValueError:
            return (
                "Error: datetime_str must be an ISO 8601 timestamp (YYYY-MM-DDTHH:MM:SS). "
                "Ask the caller for the exact date and time and try again."
            )

        if parsed.weekday() == 6:  # Sunday
            return "Error: Cannot book on Sundays. Practice is closed."
        if not urgent and (parsed.hour, parsed.minute) in ((11, 30), (15, 30)):
            return "Error: 11:30 and 15:30 are for emergencies only."

        ref = str(uuid.uuid4())[:8].upper()
        entry = {
            "type": "booking",
            "ref": ref,
            "name": name,
            "mobile": mobile,
            "service": service,
            "datetime": datetime_str,
            "new_patient": new_patient,
            "urgent": urgent,
            "created_at": datetime.now().isoformat()
        }
        self._append_db(entry)
        return f"Success. Reference number: {ref}"

    @function_tool(description="Books an emergency dental appointment into the 11:30 or 15:30 slot.")
    async def book_emergency(self, name: str, mobile: str, symptom: str) -> str:
        ref = str(uuid.uuid4())[:8].upper()
        entry = {
            "type": "booking",
            "ref": ref,
            "name": name,
            "mobile": mobile,
            "service": "Emergency / toothache",
            "symptom": symptom,
            "urgent": True,
            "created_at": datetime.now().isoformat()
        }
        self._append_db(entry)
        return f"Success. Emergency reference number: {ref}"

    @function_tool(description="Takes a message for the practice manager for out-of-scope queries. Requires the caller's full name and callback mobile - ask for both before calling.")
    async def take_message(self, name: str, mobile: str, reason: str) -> str:
        # The practice manager cannot action a message with no one to call back
        missing = [
            label
            for label, value in (("full name", name), ("callback mobile", mobile))
            if not value or not value.strip()
        ]
        if missing:
            return (
                f"Error: message not saved - missing {' and '.join(missing)}. "
                "Ask the caller for these, then call take_message again."
            )

        entry = {
            "type": "message",
            "name": name.strip(),
            "mobile": mobile.strip(),
            "reason": reason,
            "created_at": datetime.now().isoformat()
        }
        self._append_db(entry)
        return "Success. Message saved."

    @function_tool(description="Ends the call. Use IMMEDIATELY after confirming a booking/message, or if looping.")
    async def end_call(self) -> str:
        # Schedule the shutdown slightly in the future so TTS can finish
        ctx = get_job_context()
        asyncio.create_task(self._shutdown_delayed(ctx))
        return "Call ended."

    async def _shutdown_delayed(self, ctx):
        # Deleting the room tears down the session and the room together:
        # no hanging agent, no orphaned room.
        await asyncio.sleep(2)
        await ctx.api.room.delete_room(
            api.DeleteRoomRequest(room=ctx.room.name)
        )

async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Initialize the new AgentSession replacing VoicePipelineAgent
    session = AgentSession(
        stt=inference.STT(model="deepgram/nova-3", language="en"),
        llm=inference.LLM(model="openai/gpt-4o-mini"),
        tts=inference.TTS(model="deepgram/aura-2", voice="thalia"),
    )
    
    agent = DentalAgent(ctx.room)

    # Start the session
    await session.start(
        room=ctx.room,
        agent=agent
    )
    
    # In v1.x, we instruct the session to generate an initial reply to meet the greeting requirement
    await session.generate_reply(
        instructions="Say exactly this word for word: 'Hello, you've reached Parkline Dental in Harris Park. I'm Nova. How can I help you today?'"
    )

if __name__ == "__main__":
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        port=int(os.environ.get("HEALTH_PORT", 8082)),
    ))