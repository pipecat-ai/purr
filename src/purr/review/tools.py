"""The tools a review agent reports through (instead of touching GitHub)."""

PURR_SERVER = "purr"
PURR_TOOL_PREFIX = f"mcp__{PURR_SERVER}__"

SUBMIT_REVIEW = "submit_review"
PROPOSE_ACTION = "propose_action"

SUBMIT_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Two or three sentences: what the PR does and your overall verdict.",
        },
        "findings": {
            "type": "array",
            "description": "Concrete problems or improvements, most important first.",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["issue", "suggestion", "nit"]},
                    "text": {"type": "string", "description": "What's wrong and why it matters."},
                    "file": {"type": "string", "description": "Path relative to the repo root."},
                    "line": {"type": "integer", "description": "Line in the PR's version."},
                },
                "required": ["severity", "text"],
            },
        },
    },
    "required": ["summary", "findings"],
}

PROPOSE_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["request_changes", "approve", "comment"],
            "description": "The GitHub review to submit if the user approves.",
        },
        "description": {
            "type": "string",
            "description": "One line for the user, e.g. 'Post 3 comments and request changes'.",
        },
        "body": {"type": "string", "description": "The review's overall comment, if any."},
        "comments": {
            "type": "array",
            "description": "Inline comments to post with the review.",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer"},
                    "body": {"type": "string"},
                },
                "required": ["path", "line", "body"],
            },
        },
    },
    "required": ["kind", "description"],
}
