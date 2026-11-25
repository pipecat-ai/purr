"""The tools a review agent reports through (instead of touching GitHub)."""

PURR_SERVER = "purr"
PURR_TOOL_PREFIX = f"mcp__{PURR_SERVER}__"

SUBMIT_REVIEW = "submit_review"
PROPOSE_ACTION = "propose_action"
GET_REVIEW_THREADS = "get_review_threads"

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
            "enum": ["request_changes", "approve", "comment", "push"],
            "description": (
                "The GitHub review to submit if the user approves. Or `push`, only when "
                "the user asks for one: the commits you made here go to the PR's branch, "
                "or replace its commits if you rebased it (Purr describes it; the other "
                "fields don't apply)."
            ),
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
        "replies": {
            "type": "array",
            "description": (
                "Replies to existing review threads (ids from get_review_threads), "
                "instead of new comments that would repeat them."
            ),
            "items": {
                "type": "object",
                "properties": {"thread_id": {"type": "string"}, "body": {"type": "string"}},
                "required": ["thread_id", "body"],
            },
        },
    },
    "required": ["kind", "description"],
}
