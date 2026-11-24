"""phone_call: ALICE calls the user by voice from the ALICE app on their phone (alice/app/call.py)."""

from __future__ import annotations

from alice.tools.base import READ, Tool, ToolContext, ToolError, ToolResult, obj


class PhoneCall(Tool):
    name = "phone_call"
    description = (
        "Calls the user by voice through the ALICE app on their phone: an incoming call screen opens on the phone, "
        "and if they answer they talk with you (in a separate voice call, with your tools available).\n"
        "- Use when the user says \"call me\", \"call me at X\", \"call me when it's done\" (if it is timed, set up a "
        "task/reminder first and call this tool when the time comes), or when something is too important to leave to "
        "a notification.\n"
        "- message: the first sentence you say when they answer (short, natural, why you are calling). reason: the "
        "short reason shown in the notification.\n"
        "- The tool starts the call and returns at once; if it isn't answered it becomes a missed call after 45 s."
    )
    parameters = obj({
        "message": {"type": "string", "description": "The first sentence you say when they answer."},
        "reason": {"type": "string", "description": "Short reason shown in the notification (e.g. 'Reminder: "
                                                    "medicine')."},
    }, ["message"])
    risk = READ  # calling the user themselves sends nothing outside

    def run(self, ctx: ToolContext, message: str, reason: str = "") -> ToolResult:
        from alice.app import call

        mgr = call.current()
        if mgr is None:
            raise ToolError("the phone bridge is off: Settings → Setup → ALICE from the phone must be on")
        r = mgr.ring(reason or message[:60], message)
        if not r.get("ok"):
            raise ToolError(r.get("error") or "could not call")
        return ToolResult("The phone is ringing. If the user picks up, the call continues in a separate voice conversation; you don't "
                          "need to say anything else here.")


TOOLS = [PhoneCall()]
