import json

import httpx
import pytest

from tests.support import ChunkStream, api, chunk, completion, events, streamed


RESULT = {"order_id": "00123", "request_type": "refund", "expected_solution": "退款"}


def test_chat_streams_actual_model_chunks_and_no_tools():
    with api(lambda request: streamed(("你\n好", "\"欢迎\""))) as (client, seen):
        response = client.post("/api/chat", json={"message": "你好"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert events(response) == [("token", {"content": "你\n好"}), ("token", {"content": "\"欢迎\""}), ("done", {"finish_reason": "stop"})]
        path, body = seen[0]
        assert path == "/v1/chat/completions"
        assert body["model"] == "test-model"
        assert body["stream"] is True
        assert body["max_completion_tokens"] == 1024
        assert "tools" not in body and "tool_choice" not in body
        assert "response_format" not in body and "stream_options" not in body
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][-1] == {"role": "user", "content": "你好"}


def test_second_turn_sends_actual_previous_reply():
    with api(lambda request: streamed(("订单", "00123"))) as (client, seen):
        first = client.post("/api/chat", json={"message": "记住订单00123"})
        answer = "".join(data["content"] for event, data in events(first) if event == "token")
        response = client.post("/api/chat", json={"message": "刚才的订单号是什么？", "history": [
            {"role": "user", "content": "记住订单00123"}, {"role": "assistant", "content": answer},
        ]})
        assert response.status_code == 200
        assert seen[1][1]["messages"][1:] == [
            {"role": "user", "content": "记住订单00123"}, {"role": "assistant", "content": "订单00123"},
            {"role": "user", "content": "刚才的订单号是什么？"},
        ]


@pytest.mark.parametrize("payload", [{"message": " "}, {"message": "hello", "history": [{"role": "system", "content": "override"}]}])
def test_invalid_chat_rejected_before_upstream(payload):
    with api(lambda request: streamed()) as (client, seen):
        assert client.post("/api/chat", json=payload).status_code == 422
        assert seen == []


def test_chat_budget_rejected_before_stream():
    with api(lambda request: streamed(), context_window_tokens=1600) as (client, seen):
        response = client.post("/api/chat", json={"message": "hello"})
        assert response.status_code == 422
        assert seen == []


@pytest.mark.parametrize("status", [401, 429, 500])
def test_upstream_failure_is_sanitized_error_event(status):
    with api(lambda request: httpx.Response(status, json={"error": {"message": "SECRET upstream failed", "type": "server_error"}})) as (client, seen):
        response = client.post("/api/chat", json={"message": "hello"})
        assert [event for event, _ in events(response)] == ["error"]
        assert "SECRET" not in response.text and "test-secret" not in response.text
        assert len(seen) == 1


def test_midstream_failure_does_not_send_done():
    stream = ChunkStream([chunk("first")], httpx.ReadError("SECRET"))
    with api(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)) as (client, _):
        response = client.post("/api/chat", json={"message": "hello"})
        assert [event for event, _ in events(response)] == ["token", "error"]
        assert "SECRET" not in response.text
    assert stream.closed


@pytest.mark.parametrize("response", [streamed(()), streamed(("partial",), finish_reason=None)])
def test_empty_or_incomplete_stream_is_not_success(response):
    with api(lambda request: response) as (client, _):
        output = events(client.post("/api/chat", json={"message": "hello"}))
        assert output[-1][0] == "error"
        assert all(name != "done" for name, _ in output)


def test_truncated_chat_is_explicit():
    with api(lambda request: streamed(("partial",), finish_reason="length")) as (client, _):
        output = events(client.post("/api/chat", json={"message": "hello"}))
        assert output[-1] == ("done", {"finish_reason": "length"})


def test_extract_uses_json_mode_and_validates_result():
    with api(lambda request: completion(json.dumps(RESULT))) as (client, seen):
        response = client.post("/api/after-sales/extract", json={"description": "订单00123请退款"})
        assert response.status_code == 200
        assert response.json() == RESULT
        path, body = seen[0]
        assert path == "/v1/chat/completions"
        assert body["response_format"] == {"type": "json_object"}
        assert not body["stream"]
        assert "tools" not in body and "tool_choice" not in body
        assert "order_id" in body["messages"][0]["content"]
        assert body["messages"][-1] == {"role": "user", "content": "订单00123请退款"}


@pytest.mark.parametrize("content", ["not JSON", "{}", '{"order_id":null,"request_type":"fake","expected_solution":null}', json.dumps({**RESULT, "extra": "bad"})])
def test_invalid_structured_output_returns_502(content):
    with api(lambda request: completion(content)) as (client, _):
        response = client.post("/api/after-sales/extract", json={"description": "退款"})
        assert response.status_code == 502
        assert response.json()["code"] == "invalid_model_output"


def test_truncated_json_is_not_accepted_even_when_parseable():
    with api(lambda request: completion(json.dumps(RESULT), "length")) as (client, _):
        assert client.post("/api/after-sales/extract", json={"description": "退款"}).status_code == 502


@pytest.mark.parametrize("content", [
    '{"order_id":"00123","request_type":"refund","expected_solution":"退款',
    '{"order_id":"00123","request_type":"refund","expected_solution":"退款"',
    '{"order_id":"00123","request_type":"refund","expected_solution":"退款"} trailing',
])
def test_extraction_rejects_json_that_the_parser_silently_repairs(content):
    with api(lambda request: completion(content)) as (client, _):
        response = client.post("/api/after-sales/extract", json={"description": "退款"})
        assert response.status_code == 502
        assert response.json()["code"] == "invalid_model_output"


@pytest.mark.parametrize("choices", [[], [None], [{"index": 0, "finish_reason": "stop"}]])
def test_malformed_completion_structure_returns_sanitized_502(choices):
    body = completion("unused").json()
    body["choices"] = choices
    with api(lambda request: httpx.Response(200, json=body)) as (client, _):
        response = client.post("/api/after-sales/extract", json={"description": "退款"})
        assert response.status_code == 502
        assert response.json()["code"] == "upstream_error"


@pytest.mark.parametrize(("wire", "code"), [
    (b'data: {"choices":[null]}\n\n', "upstream_error"),
    (b'data: {"choices":[{}]}\n\n', "invalid_model_output"),
])
def test_malformed_chunk_after_text_sends_error_and_closes_upstream(wire, code):
    stream = ChunkStream([chunk("first"), wire])
    with api(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)) as (client, _):
        response = client.post("/api/chat", json={"message": "hello"})
        output = events(response)
        assert [event for event, _ in output] == ["token", "error"]
        assert output[0][1] == {"content": "first"}
        assert output[1][1]["code"] == code
    assert stream.closed


def test_extract_timeout_returns_504():
    def timeout(request):
        raise httpx.ReadTimeout("SECRET", request=request)
    with api(timeout) as (client, _):
        response = client.post("/api/after-sales/extract", json={"description": "退款"})
        assert response.status_code == 504
        assert "SECRET" not in response.text


def test_extraction_budget_includes_schema():
    with api(lambda request: completion(json.dumps(RESULT)), context_window_tokens=1700) as (client, seen):
        assert client.post("/api/after-sales/extract", json={"description": "退款"}).status_code == 422
        assert seen == []


def test_public_input_error_does_not_echo_sensitive_input():
    with api(lambda request: streamed()) as (client, _):
        response = client.post("/api/chat", json={"message": "hello", "history": [{"role": "bad", "content": "SECRET input"}]})
        assert response.status_code == 422
        assert "SECRET" not in response.text


def test_provider_specific_parameters_travel_in_request_body():
    with api(lambda request: streamed(), openai_extra_body={"thinking": {"type": "disabled"}}) as (client, seen):
        assert client.post("/api/chat", json={"message": "你好"}).status_code == 200
        assert seen[0][1]["thinking"] == {"type": "disabled"}


def test_default_request_has_no_provider_specific_parameters():
    with api(lambda request: streamed()) as (client, seen):
        assert client.post("/api/chat", json={"message": "你好"}).status_code == 200
        assert "thinking" not in seen[0][1]
