"""``submit_assessment``: terminal action; the agent loop ends after this call.

Persona allowlist: SOC, Threat, Hunter, DE (mandatory for all).
The args schema mirrors :class:`socbench.models.SubmitAssessment`; strict
pydantic validation (uniqueness of indices, etc.) happens in the agent loop
after the tool returns.
"""
from __future__ import annotations

from typing import Any, ClassVar

from socbench.tools.base import Tool, ToolContext


class SubmitAssessmentTool(Tool):
    name: ClassVar[str] = "submit_assessment"
    description: ClassVar[str] = (
        "Submit the final verdict for this rendering. The agent loop ends "
        "immediately after this tool is called. Provide verdict, confidence "
        "in [0, 1], the flow_ids judged malicious, and a rationale. For "
        "host_egress (fan-out) units you may instead name the malicious "
        "destination IPs in `malicious_destinations` rather than listing "
        "every flow_id; the harness expands each destination to its flows."
    )
    args_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "confidence", "rationale"],
        "properties": {
            "verdict": {"type": "string", "enum": ["benign", "malicious"]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "malicious_flow_indices": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
                "default": [],
            },
            "malicious_destinations": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
                "description": (
                    "Destination IPs judged malicious (host_egress shorthand). "
                    "Each is expanded to all of its in-scope flows."
                ),
            },
            "rationale": {"type": "string", "minLength": 1, "maxLength": 8000},
        },
    }

    def call(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return {"accepted": True, "assessment": dict(args)}
