import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

TABLE_NAME = os.environ.get("TABLE_NAME", "unstamped-pages-prod")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

SENSITIVE_FIELDS = {"password"}
RESERVED_FIELDS = {"PK", "SK", "userId"}

USER_SCHEMA_FIELDS = {
    "userId",
    "username",
    "password",
    "firstName",
    "lastName",
    "email",
    "language",
    "timeZone",
    "firstLogin",
    "lastLogin",
    "mfaEnabled",
    "newsletter",
    "createdTimestamp",
    "updatedTimestamp",
    "checklists",
    "tripLog",
    "passportStamps",
}


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o == int(o) else float(o)
        return super().default(o)


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(body, cls=DecimalEncoder),
    }


def _build_keys(user_id: str) -> dict:
    return {"PK": f"USER#{user_id}", "SK": "METADATA"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_body(event: dict) -> dict:
    body = event.get("body")
    if not body:
        return {}
    if isinstance(body, str):
        return json.loads(body, parse_float=Decimal)
    return body


def _get_http_method(event: dict) -> str:
    method = event.get("httpMethod")
    if method:
        return method.upper()
    rc = event.get("requestContext", {})
    http = rc.get("http", {})
    return http.get("method", "").upper()


def _get_user_id_from_qs(event: dict) -> str | None:
    params = event.get("queryStringParameters") or {}
    return params.get("userId")


# ---------------------------------------------------------------------------
# CRUD handlers
# ---------------------------------------------------------------------------


def _create_user(event: dict) -> dict:
    body = _parse_body(event)
    user_id = body.get("userId")
    if not user_id:
        return _response(400, {"error": "userId is required"})

    now = _now_iso()
    item = {**_build_keys(user_id), "userId": user_id}

    for field in USER_SCHEMA_FIELDS:
        if field in body and field not in {"createdTimestamp", "updatedTimestamp"}:
            item[field] = body[field]

    item["createdTimestamp"] = now
    item["updatedTimestamp"] = now

    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _response(400, {"error": f"User {user_id} already exists"})
        raise

    safe_item = {k: v for k, v in item.items() if k not in SENSITIVE_FIELDS}
    return _response(201, {"message": "User created", "user": safe_item})


def _read_user(event: dict) -> dict:
    user_id = _get_user_id_from_qs(event)
    if not user_id:
        return _response(400, {"error": "userId query parameter is required"})

    resp = table.get_item(Key=_build_keys(user_id))
    item = resp.get("Item")
    if not item:
        return _response(404, {"error": f"User {user_id} not found"})

    safe_item = {k: v for k, v in item.items() if k not in SENSITIVE_FIELDS}
    return _response(200, {"user": safe_item})


def _update_user(event: dict) -> dict:
    body = _parse_body(event)
    user_id = body.get("userId")
    if not user_id:
        return _response(400, {"error": "userId is required in the request body"})

    fields_to_update = {
        k: v for k, v in body.items() if k not in RESERVED_FIELDS and k in USER_SCHEMA_FIELDS
    }
    fields_to_update["updatedTimestamp"] = _now_iso()

    update_parts: list[str] = []
    attr_names: dict[str, str] = {}
    attr_values: dict[str, any] = {}

    for i, (field, value) in enumerate(fields_to_update.items()):
        name_placeholder = f"#f{i}"
        value_placeholder = f":v{i}"
        update_parts.append(f"{name_placeholder} = {value_placeholder}")
        attr_names[name_placeholder] = field
        attr_values[value_placeholder] = value

    update_expr = "SET " + ", ".join(update_parts)

    try:
        resp = table.update_item(
            Key=_build_keys(user_id),
            UpdateExpression=update_expr,
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
            ConditionExpression="attribute_exists(PK)",
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _response(404, {"error": f"User {user_id} not found"})
        raise

    updated = resp.get("Attributes", {})
    safe_item = {k: v for k, v in updated.items() if k not in SENSITIVE_FIELDS}
    return _response(200, {"message": "User updated", "user": safe_item})


def _delete_user(event: dict) -> dict:
    user_id = _get_user_id_from_qs(event)
    if not user_id:
        body = _parse_body(event)
        user_id = body.get("userId")
    if not user_id:
        return _response(400, {"error": "userId is required"})

    table.delete_item(Key=_build_keys(user_id))
    return _response(200, {"message": f"User {user_id} deleted"})


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

_ROUTE = {
    "POST": _create_user,
    "GET": _read_user,
    "PUT": _update_user,
    "PATCH": _update_user,
    "DELETE": _delete_user,
}


def lambda_handler(event: dict, context) -> dict:
    method = _get_http_method(event)

    if method == "OPTIONS":
        return _response(200, {})

    handler = _ROUTE.get(method)
    if not handler:
        return _response(405, {"error": f"Unsupported method: {method}"})

    try:
        return handler(event)
    except json.JSONDecodeError:
        return _response(400, {"error": "Invalid JSON in request body"})
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        return _response(500, {"error": f"DynamoDB error: {error_code}"})
    except Exception:
        return _response(500, {"error": "Internal server error"})
