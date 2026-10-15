"""Deterministic views over recorded tool-calling messages."""
import json


def compress_trajectory(events):
    """Extract every actual tool result; retain exact content and source IDs."""
    calls = {}
    kept = []
    for event in events:
        for call in event.get("tool_calls", []):
            calls[call["id"]] = (event["event_id"], call)
        if event["role"] == "tool":
            parent_id, call = calls[event["tool_call_id"]]
            if call["function"]["name"]:
                kept.append({"source_event_id": event["event_id"], "source_call_event_id": parent_id,
                    "tool_call_id": event["tool_call_id"], "tool": call["function"]["name"],
                    "arguments": json.loads(call["function"]["arguments"]), "result": json.loads(event["content"])})
    included = {row[key] for row in kept for key in ("source_event_id", "source_call_event_id")}
    return {"method": "Deterministic extraction of every tool result. Assistant prose, message envelopes and timestamps omitted; no inferred facts.",
            "source_event_ids": sorted(included), "omitted_event_ids": [event["event_id"] for event in events if event["event_id"] not in included],
            "events": kept}
