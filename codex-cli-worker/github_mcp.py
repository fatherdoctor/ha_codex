"""Stdio tool for GitHub through the worker: Codex gets a tool, never the sign-in.

The worker does the git work with the add-on's GitHub sign-in and runs the push guard before every push.
Each call carries the chat's thread id that Codex sent, and is refused once the chat's exchange ends.
"""

from __future__ import annotations

import json
import socket
import sys

import tool_threads

SOCKET = "/data/github.sock"
PATH = {
    "type": "string",
    "maxLength": 4096,
    "description": "The full path of the git work tree under /share or /homeassistant, starting with /, for example /share/house_repo or /homeassistant.",
}
REPOSITORY = {
    "type": "string",
    "maxLength": 140,
    "description": "A connected repository, owner/repository.",
}
FOLDER = {
    "type": "string",
    "maxLength": 120,
    "description": "For clone: one new folder name; the clone goes to /share/<folder> (default: the repository name).",
}
BRANCH = {
    "type": "string",
    "maxLength": 200,
    "description": "A plain branch name; the checked-out branch when left out.",
}
TOOLS = [
    {
        "name": "github_git",
        "description": (
            "Clone, link (set origin), fetch, pull or push a GitHub repository connected on the add-on "
            "page, or list the connected repositories. The worker runs git with the sign-in, outside your sandbox. A push runs the push "
            "guard first and is refused when it finds a secret; never force, delete or tag. pull fetches the branch only; fast-forward it yourself with git merge --ff-only. Read-only chats "
            "can use repos and fetch only. clone takes repository and an optional folder; link takes repository and path."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "operation": {
                    "enum": ["repos", "fetch", "pull", "push", "clone", "link"]
                },
                "path": PATH,
                "branch": BRANCH,
                "repository": REPOSITORY,
                "folder": FOLDER,
            },
            "required": ["operation"],
        },
    },
]


def fetch(name, arguments, thread_id):
    if name != "github_git":
        raise ValueError("The only tool is github_git.")
    if not isinstance(arguments, dict) or set(arguments) - {
        "operation",
        "path",
        "branch",
        "repository",
        "folder",
    }:
        raise ValueError(
            "github_git accepts only operation, path, branch, repository and folder."
        )
    arguments = dict(arguments)
    operation = arguments.pop("operation", None)
    data = (
        json.dumps(
            {"operation": operation, "arguments": arguments, "thread_id": thread_id}
        ).encode()
        + b"\n"
    )
    if len(data) > 65536:
        raise ValueError("The request is larger than 64 KiB.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(700)
        client.connect(SOCKET)
        client.sendall(data)
        with client.makefile("rb") as response:
            raw = response.readline(65537)
    if not raw or len(raw) > 65536:
        raise ValueError("The GitHub service sent no usable answer.")
    return json.loads(raw)


def respond(request):
    method = request.get("method")
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "codex_github", "version": "1"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        try:
            params = request.get("params", {})
            result = fetch(
                params.get("name"),
                params.get("arguments", {}),
                tool_threads.thread_id_from_call(params),
            )
        except OSError:
            result = {
                "ok": False,
                "text": "The GitHub service is not reachable; the add-on may be restarting.",
            }
        except ValueError as exc:
            reason = (
                str(exc)
                if not isinstance(exc, json.JSONDecodeError)
                else "The GitHub service sent no usable answer."
            )
            result = {"ok": False, "text": reason}
        except (TypeError, AttributeError):
            result = {
                "ok": False,
                "text": "A tool call needs params with a name and an arguments object.",
            }
        if not isinstance(result, dict):
            result = {"ok": False, "text": "The GitHub service sent no usable answer."}
        return {
            "content": [{"type": "text", "text": str(result.get("text", ""))}],
            "isError": not result.get("ok"),
        }
    raise ValueError("Unsupported method")


def main():
    while raw := sys.stdin.buffer.readline(65537):
        if len(raw) > 65536:
            return 1
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or "id" not in request:
                continue
            response = {"jsonrpc": "2.0", "id": request["id"]}
            try:
                response["result"] = respond(request)
            except (ValueError, TypeError, AttributeError):
                response["error"] = {"code": -32601, "message": "Unsupported request"}
        except (ValueError, UnicodeError):
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Invalid JSON"},
            }
        print(json.dumps(response), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
