import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import lambda_function as lf


FIXED_TIMESTAMP = "2026-06-17T12:00:00+00:00"

SAMPLE_USER_PAYLOAD = {
    "userId": "u-001",
    "username": "jdoe",
    "password": "hashed_pw_abc123",
    "firstName": "John",
    "lastName": "Doe",
    "email": "jdoe@example.com",
    "language": "en",
    "timeZone": "America/New_York",
    "firstLogin": "2026-01-01T00:00:00Z",
    "lastLogin": "2026-06-17T10:00:00Z",
    "mfaEnabled": True,
    "newsletter": False,
    "checklists": {"packing": {"items": ["passport", "charger"]}},
    "tripLog": {"trip-1": {"destination": "Tokyo", "date": "2026-05-01"}},
    "passportStamps": ["JP", "FR", "DE"],
}


def _make_event(method, body=None, qs=None, *, use_http_api=False):
    event = {}
    if use_http_api:
        event["requestContext"] = {"http": {"method": method}}
    else:
        event["httpMethod"] = method
    if body is not None:
        event["body"] = json.dumps(body)
    if qs is not None:
        event["queryStringParameters"] = qs
    return event


def _parse_response(resp):
    assert "statusCode" in resp
    assert "headers" in resp
    assert resp["headers"]["Content-Type"] == "application/json"
    body = json.loads(resp["body"])
    return resp["statusCode"], body


@pytest.fixture(autouse=True)
def _freeze_time():
    with patch.object(lf, "_now_iso", return_value=FIXED_TIMESTAMP):
        yield


@pytest.fixture(autouse=True)
def _mock_table():
    mock = MagicMock()
    with patch.object(lf, "table", mock):
        yield mock



class TestBuildKeys:
    def test_builds_correct_pk_and_sk(self):
        keys = lf._build_keys("u-001")
        assert keys == {"PK": "USER#u-001", "SK": "METADATA"}

    def test_special_characters_in_user_id(self):
        keys = lf._build_keys("user@domain.com")
        assert keys["PK"] == "USER#user@domain.com"
        assert keys["SK"] == "METADATA"


class TestParseBody:
    def test_json_string_body(self):
        event = {"body": '{"userId": "u-001"}'}
        assert lf._parse_body(event) == {"userId": "u-001"}

    def test_none_body(self):
        assert lf._parse_body({}) == {}

    def test_empty_string_body(self):
        assert lf._parse_body({"body": ""}) == {}

    def test_dict_body_passthrough(self):
        event = {"body": {"userId": "u-001"}}
        assert lf._parse_body(event) == {"userId": "u-001"}

    def test_float_parsed_as_decimal(self):
        event = {"body": '{"score": 9.5}'}
        result = lf._parse_body(event)
        assert isinstance(result["score"], Decimal)
        assert result["score"] == Decimal("9.5")


class TestGetHttpMethod:
    def test_rest_api_event(self):
        assert lf._get_http_method({"httpMethod": "POST"}) == "POST"

    def test_http_api_v2_event(self):
        event = {"requestContext": {"http": {"method": "GET"}}}
        assert lf._get_http_method(event) == "GET"

    def test_lowercase_method_uppercased(self):
        assert lf._get_http_method({"httpMethod": "post"}) == "POST"

    def test_missing_method_returns_empty(self):
        assert lf._get_http_method({}) == ""

    def test_rest_api_takes_precedence(self):
        event = {
            "httpMethod": "PUT",
            "requestContext": {"http": {"method": "DELETE"}},
        }
        assert lf._get_http_method(event) == "PUT"


class TestGetUserIdFromQs:
    def test_extracts_user_id(self):
        event = {"queryStringParameters": {"userId": "u-001"}}
        assert lf._get_user_id_from_qs(event) == "u-001"

    def test_missing_qs_returns_none(self):
        assert lf._get_user_id_from_qs({}) is None

    def test_null_qs_returns_none(self):
        assert lf._get_user_id_from_qs({"queryStringParameters": None}) is None

    def test_missing_key_returns_none(self):
        event = {"queryStringParameters": {"other": "val"}}
        assert lf._get_user_id_from_qs(event) is None


class TestDecimalEncoder:
    def test_integer_decimal(self):
        assert json.dumps({"n": Decimal("42")}, cls=lf.DecimalEncoder) == '{"n": 42}'

    def test_float_decimal(self):
        assert json.dumps({"n": Decimal("3.14")}, cls=lf.DecimalEncoder) == '{"n": 3.14}'

    def test_non_decimal_raises(self):
        with pytest.raises(TypeError):
            json.dumps({"d": datetime.now(timezone.utc)}, cls=lf.DecimalEncoder)


class TestResponse:
    def test_structure(self):
        resp = lf._response(200, {"ok": True})
        assert resp["statusCode"] == 200
        assert resp["headers"]["Content-Type"] == "application/json"
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"
        assert json.loads(resp["body"]) == {"ok": True}

    def test_cors_headers_present(self):
        resp = lf._response(200, {})
        h = resp["headers"]
        assert "Access-Control-Allow-Methods" in h
        assert "Access-Control-Allow-Headers" in h



class TestCreateUser:
    def test_success(self, _mock_table):
        event = _make_event("POST", body=SAMPLE_USER_PAYLOAD)
        resp = lf.lambda_handler(event, None)
        status, body = _parse_response(resp)

        assert status == 201
        assert body["message"] == "User created"
        assert body["user"]["userId"] == "u-001"
        assert body["user"]["createdTimestamp"] == FIXED_TIMESTAMP
        assert body["user"]["updatedTimestamp"] == FIXED_TIMESTAMP
        assert "password" not in body["user"]

        call_kwargs = _mock_table.put_item.call_args.kwargs
        assert call_kwargs["Item"]["PK"] == "USER#u-001"
        assert call_kwargs["Item"]["SK"] == "METADATA"
        assert call_kwargs["ConditionExpression"] == "attribute_not_exists(PK)"

    def test_schema_fields_stored(self, _mock_table):
        event = _make_event("POST", body=SAMPLE_USER_PAYLOAD)
        lf.lambda_handler(event, None)
        item = _mock_table.put_item.call_args.kwargs["Item"]

        assert item["username"] == "jdoe"
        assert item["email"] == "jdoe@example.com"
        assert item["checklists"] == {"packing": {"items": ["passport", "charger"]}}
        assert item["passportStamps"] == ["JP", "FR", "DE"]
        assert item["mfaEnabled"] is True
        assert item["newsletter"] is False

    def test_ignores_unknown_fields(self, _mock_table):
        payload = {**SAMPLE_USER_PAYLOAD, "rogue_field": "evil"}
        event = _make_event("POST", body=payload)
        lf.lambda_handler(event, None)
        item = _mock_table.put_item.call_args.kwargs["Item"]
        assert "rogue_field" not in item

    def test_timestamps_overridden(self, _mock_table):
        payload = {
            **SAMPLE_USER_PAYLOAD,
            "createdTimestamp": "1999-01-01T00:00:00Z",
            "updatedTimestamp": "1999-01-01T00:00:00Z",
        }
        event = _make_event("POST", body=payload)
        lf.lambda_handler(event, None)
        item = _mock_table.put_item.call_args.kwargs["Item"]
        assert item["createdTimestamp"] == FIXED_TIMESTAMP
        assert item["updatedTimestamp"] == FIXED_TIMESTAMP

    def test_missing_user_id(self):
        event = _make_event("POST", body={"username": "oops"})
        status, body = _parse_response(lf.lambda_handler(event, None))
        assert status == 400
        assert "userId is required" in body["error"]

    def test_duplicate_user(self, _mock_table):
        _mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
            "PutItem",
        )
        event = _make_event("POST", body=SAMPLE_USER_PAYLOAD)
        status, body = _parse_response(lf.lambda_handler(event, None))
        assert status == 400
        assert "already exists" in body["error"]

    def test_unexpected_client_error_propagates(self, _mock_table):
        _mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": ""}},
            "PutItem",
        )
        event = _make_event("POST", body=SAMPLE_USER_PAYLOAD)
        status, body = _parse_response(lf.lambda_handler(event, None))
        assert status == 500
        assert "DynamoDB error" in body["error"]

    def test_password_stored_but_not_returned(self, _mock_table):
        event = _make_event("POST", body=SAMPLE_USER_PAYLOAD)
        resp = lf.lambda_handler(event, None)
        _, body = _parse_response(resp)

        item = _mock_table.put_item.call_args.kwargs["Item"]
        assert item["password"] == "hashed_pw_abc123"
        assert "password" not in body["user"]



class TestReadUser:
    def test_success(self, _mock_table):
        stored = {
            "PK": "USER#u-001",
            "SK": "METADATA",
            "userId": "u-001",
            "username": "jdoe",
            "password": "hashed_pw_abc123",
            "email": "jdoe@example.com",
        }
        _mock_table.get_item.return_value = {"Item": stored}

        event = _make_event("GET", qs={"userId": "u-001"})
        status, body = _parse_response(lf.lambda_handler(event, None))

        assert status == 200
        assert body["user"]["userId"] == "u-001"
        assert body["user"]["email"] == "jdoe@example.com"
        assert "password" not in body["user"]

    def test_not_found(self, _mock_table):
        _mock_table.get_item.return_value = {}
        event = _make_event("GET", qs={"userId": "u-999"})
        status, body = _parse_response(lf.lambda_handler(event, None))
        assert status == 404
        assert "not found" in body["error"]

    def test_missing_query_param(self):
        event = _make_event("GET")
        status, body = _parse_response(lf.lambda_handler(event, None))
        assert status == 400
        assert "query parameter" in body["error"]

    def test_null_query_string_parameters(self):
        event = _make_event("GET")
        event["queryStringParameters"] = None
        status, _ = _parse_response(lf.lambda_handler(event, None))
        assert status == 400

    def test_uses_correct_keys(self, _mock_table):
        _mock_table.get_item.return_value = {}
        event = _make_event("GET", qs={"userId": "u-001"})
        lf.lambda_handler(event, None)
        _mock_table.get_item.assert_called_once_with(
            Key={"PK": "USER#u-001", "SK": "METADATA"}
        )

    def test_decimal_values_serialized(self, _mock_table):
        stored = {
            "PK": "USER#u-001",
            "SK": "METADATA",
            "userId": "u-001",
            "score": Decimal("42"),
            "rating": Decimal("4.5"),
        }
        _mock_table.get_item.return_value = {"Item": stored}
        event = _make_event("GET", qs={"userId": "u-001"})
        resp = lf.lambda_handler(event, None)
        body = json.loads(resp["body"])
        assert body["user"]["score"] == 42
        assert body["user"]["rating"] == 4.5



class TestUpdateUser:
    def _make_update_response(self, attrs):
        return {"Attributes": attrs}

    def test_put_success(self, _mock_table):
        _mock_table.update_item.return_value = self._make_update_response(
            {"userId": "u-001", "email": "new@example.com", "updatedTimestamp": FIXED_TIMESTAMP}
        )
        event = _make_event("PUT", body={"userId": "u-001", "email": "new@example.com"})
        status, body = _parse_response(lf.lambda_handler(event, None))

        assert status == 200
        assert body["message"] == "User updated"
        assert body["user"]["email"] == "new@example.com"

    def test_patch_routes_same_as_put(self, _mock_table):
        _mock_table.update_item.return_value = self._make_update_response(
            {"userId": "u-001", "updatedTimestamp": FIXED_TIMESTAMP}
        )
        event = _make_event("PATCH", body={"userId": "u-001", "firstName": "Jane"})
        status, _ = _parse_response(lf.lambda_handler(event, None))
        assert status == 200
        _mock_table.update_item.assert_called_once()

    def test_dynamic_expression_built_correctly(self, _mock_table):
        _mock_table.update_item.return_value = self._make_update_response({})
        event = _make_event("PUT", body={
            "userId": "u-001",
            "email": "new@example.com",
            "firstName": "Jane",
        })
        lf.lambda_handler(event, None)

        call_kwargs = _mock_table.update_item.call_args.kwargs
        expr = call_kwargs["UpdateExpression"]
        names = call_kwargs["ExpressionAttributeNames"]
        values = call_kwargs["ExpressionAttributeValues"]

        assert expr.startswith("SET ")
        assert "email" in names.values()
        assert "firstName" in names.values()
        assert "updatedTimestamp" in names.values()
        assert "new@example.com" in values.values()
        assert "Jane" in values.values()
        assert FIXED_TIMESTAMP in values.values()

    def test_updated_timestamp_auto_injected(self, _mock_table):
        _mock_table.update_item.return_value = self._make_update_response({})
        event = _make_event("PUT", body={"userId": "u-001", "language": "fr"})
        lf.lambda_handler(event, None)

        call_kwargs = _mock_table.update_item.call_args.kwargs
        names = call_kwargs["ExpressionAttributeNames"]
        values = call_kwargs["ExpressionAttributeValues"]
        assert "updatedTimestamp" in names.values()
        assert FIXED_TIMESTAMP in values.values()

    def test_reserved_fields_excluded(self, _mock_table):
        _mock_table.update_item.return_value = self._make_update_response({})
        event = _make_event("PUT", body={
            "userId": "u-001",
            "PK": "EVIL",
            "SK": "EVIL",
            "email": "ok@example.com",
        })
        lf.lambda_handler(event, None)

        call_kwargs = _mock_table.update_item.call_args.kwargs
        names = call_kwargs["ExpressionAttributeNames"]
        assert "PK" not in names.values()
        assert "SK" not in names.values()
        assert "userId" not in names.values()

    def test_unknown_fields_excluded(self, _mock_table):
        _mock_table.update_item.return_value = self._make_update_response({})
        event = _make_event("PUT", body={
            "userId": "u-001",
            "rogue": "value",
            "email": "ok@example.com",
        })
        lf.lambda_handler(event, None)

        names = _mock_table.update_item.call_args.kwargs["ExpressionAttributeNames"]
        assert "rogue" not in names.values()

    def test_nested_map_replacement(self, _mock_table):
        new_checklists = {"travel": {"items": ["sunscreen"]}}
        _mock_table.update_item.return_value = self._make_update_response({})
        event = _make_event("PUT", body={
            "userId": "u-001",
            "checklists": new_checklists,
        })
        lf.lambda_handler(event, None)

        values = _mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert new_checklists in values.values()

    def test_list_replacement(self, _mock_table):
        new_stamps = ["US", "CA", "MX"]
        _mock_table.update_item.return_value = self._make_update_response({})
        event = _make_event("PUT", body={
            "userId": "u-001",
            "passportStamps": new_stamps,
        })
        lf.lambda_handler(event, None)

        values = _mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert new_stamps in values.values()

    def test_condition_expression_set(self, _mock_table):
        _mock_table.update_item.return_value = self._make_update_response({})
        event = _make_event("PUT", body={"userId": "u-001", "email": "a@b.com"})
        lf.lambda_handler(event, None)

        call_kwargs = _mock_table.update_item.call_args.kwargs
        assert call_kwargs["ConditionExpression"] == "attribute_exists(PK)"
        assert call_kwargs["ReturnValues"] == "ALL_NEW"

    def test_user_not_found(self, _mock_table):
        _mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
            "UpdateItem",
        )
        event = _make_event("PUT", body={"userId": "u-999", "email": "a@b.com"})
        status, body = _parse_response(lf.lambda_handler(event, None))
        assert status == 404
        assert "not found" in body["error"]

    def test_missing_user_id(self):
        event = _make_event("PUT", body={"email": "a@b.com"})
        status, body = _parse_response(lf.lambda_handler(event, None))
        assert status == 400
        assert "userId is required" in body["error"]

    def test_password_stripped_from_response(self, _mock_table):
        _mock_table.update_item.return_value = self._make_update_response(
            {"userId": "u-001", "password": "secret", "email": "a@b.com"}
        )
        event = _make_event("PUT", body={"userId": "u-001", "password": "new_hash"})
        _, body = _parse_response(lf.lambda_handler(event, None))
        assert "password" not in body["user"]

    def test_unexpected_client_error_propagates(self, _mock_table):
        _mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": ""}},
            "UpdateItem",
        )
        event = _make_event("PUT", body={"userId": "u-001", "email": "a@b.com"})
        status, body = _parse_response(lf.lambda_handler(event, None))
        assert status == 500



class TestDeleteUser:
    def test_from_query_string(self, _mock_table):
        event = _make_event("DELETE", qs={"userId": "u-001"})
        status, body = _parse_response(lf.lambda_handler(event, None))
        assert status == 200
        assert "deleted" in body["message"]
        _mock_table.delete_item.assert_called_once_with(
            Key={"PK": "USER#u-001", "SK": "METADATA"}
        )

    def test_from_body_fallback(self, _mock_table):
        event = _make_event("DELETE", body={"userId": "u-002"})
        status, _ = _parse_response(lf.lambda_handler(event, None))
        assert status == 200
        _mock_table.delete_item.assert_called_once_with(
            Key={"PK": "USER#u-002", "SK": "METADATA"}
        )

    def test_query_string_takes_precedence(self, _mock_table):
        event = _make_event("DELETE", body={"userId": "u-body"}, qs={"userId": "u-qs"})
        lf.lambda_handler(event, None)
        _mock_table.delete_item.assert_called_once_with(
            Key={"PK": "USER#u-qs", "SK": "METADATA"}
        )

    def test_missing_user_id(self):
        event = _make_event("DELETE")
        status, body = _parse_response(lf.lambda_handler(event, None))
        assert status == 400
        assert "userId is required" in body["error"]



class TestLambdaHandlerRouting:
    def test_options_returns_200(self):
        event = _make_event("OPTIONS")
        status, _ = _parse_response(lf.lambda_handler(event, None))
        assert status == 200

    def test_unsupported_method(self):
        event = {"httpMethod": "TRACE"}
        status, body = _parse_response(lf.lambda_handler(event, None))
        assert status == 405
        assert "Unsupported method" in body["error"]

    def test_invalid_json_body(self):
        event = {"httpMethod": "POST", "body": "{bad json!!!"}
        status, body = _parse_response(lf.lambda_handler(event, None))
        assert status == 400
        assert "Invalid JSON" in body["error"]

    def test_generic_exception_caught(self, _mock_table):
        _mock_table.put_item.side_effect = RuntimeError("boom")
        event = _make_event("POST", body=SAMPLE_USER_PAYLOAD)
        status, _ = _parse_response(lf.lambda_handler(event, None))
        assert status == 500

    def test_http_api_v2_routing(self, _mock_table):
        _mock_table.get_item.return_value = {}
        event = _make_event("GET", qs={"userId": "u-001"}, use_http_api=True)
        status, _ = _parse_response(lf.lambda_handler(event, None))
        assert status == 404
        _mock_table.get_item.assert_called_once()

    def test_empty_method(self):
        status, _ = _parse_response(lf.lambda_handler({}, None))
        assert status == 405



class TestCrudFlow:
    def test_full_lifecycle(self, _mock_table):
        # CREATE
        _mock_table.put_item.return_value = {}
        create_event = _make_event("POST", body=SAMPLE_USER_PAYLOAD)
        status, body = _parse_response(lf.lambda_handler(create_event, None))
        assert status == 201
        assert "password" not in body["user"]

        # READ
        stored_item = {
            **_mock_table.put_item.call_args.kwargs["Item"],
        }
        _mock_table.get_item.return_value = {"Item": stored_item}
        read_event = _make_event("GET", qs={"userId": "u-001"})
        status, body = _parse_response(lf.lambda_handler(read_event, None))
        assert status == 200
        assert body["user"]["username"] == "jdoe"
        assert "password" not in body["user"]

        # UPDATE
        updated_attrs = {**stored_item, "email": "updated@example.com"}
        _mock_table.update_item.return_value = {"Attributes": updated_attrs}
        update_event = _make_event("PUT", body={"userId": "u-001", "email": "updated@example.com"})
        status, body = _parse_response(lf.lambda_handler(update_event, None))
        assert status == 200
        assert "password" not in body["user"]

        # DELETE
        delete_event = _make_event("DELETE", qs={"userId": "u-001"})
        status, body = _parse_response(lf.lambda_handler(delete_event, None))
        assert status == 200
        assert "deleted" in body["message"]
