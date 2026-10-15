"""Local settings endpoints for AI provider and Telegram credentials."""
from .core import DataError
from .deribit_settings import local_credential_request
from .ai_prompts import validate_prompt


def register_connection_routes(app, runtime, position_payload):
    from flask import jsonify, request

    def response(payload, status=200):
        result = jsonify(payload)
        result.status_code = status
        result.headers["Cache-Control"] = "no-store"
        return result

    @app.get("/api/settings/connections")
    def notification_connection_status():
        editable = local_credential_request(request)
        statuses = {kind: runtime.connections.status(kind) for kind in ("ai", "telegram")}
        if not editable:
            # Prompts can contain private user context. The public connection
            # indicator needs only provider/model and readiness metadata.
            statuses["ai"].pop("default_prompt", None)
            for profile in statuses["ai"].get("profiles", {}).values():
                profile.pop("prompt", None)
        return response({"editable": editable, **statuses})

    @app.post("/api/settings/connections/<kind>")
    @app.post("/api/settings/connections/<kind>/<action>")
    def change_notification_connection(kind, action="save"):
        if kind not in {"ai", "telegram"} or action not in {"save", "disable", "check"}:
            return response({"error": "Unknown connection action."}, 404)
        if not local_credential_request(request):
            return response({"error": "Edit or check connections directly at this computer's http://127.0.0.1 dashboard address."}, 403)
        if not request.is_json:
            return response({"error": "Expected a JSON connection request."}, 400)
        try:
            fields = ({"provider", "model", "api_key"} if kind == "ai" else {"bot_token", "chat_id"}) if action == "save" else set()
            optional = {"prompt"} if kind == "ai" and action == "save" else set()
            payload = position_payload(fields, optional)
            if action == "check":
                data = runtime.connections.check(kind)
                data["message"] = data["error"] or data["detail"]
            else:
                values = {key: payload[key] for key in fields}
                if "prompt" in payload:
                    values["prompt"] = validate_prompt(payload["prompt"])
                runtime.connections.change(kind, values, disable=action == "disable")
                data = runtime.connections.status(kind)
                data["message"] = ("Connection disabled. Saved credentials are retained for later use." if action == "disable" else
                                   "Connection saved and active. Check credentials to verify access.")
            return response(data)
        except DataError as exc:
            return response({"error": str(exc)}, 400)
        except Exception:
            return response({"error": "Could not update the connection. Reload and check its status before retrying."}, 500)
