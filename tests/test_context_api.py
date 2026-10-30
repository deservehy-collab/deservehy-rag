import logging

from app.logging_config import configure_file_logging
from tests.support import api, events, streamed

CLIENT = "client-abc12345"


def test_list_and_reload_conversations():
    with api(lambda request: streamed(("收到",))) as (client, _):
        client.post("/api/chat", json={"message": "订单 1001 的退款", "client_id": CLIENT})
        listing = client.get("/api/conversations", params={"client_id": CLIENT})
        assert listing.status_code == 200
        item = listing.json()["items"][0]
        assert item["title"] == "订单 1001 的退款"
        assert item["summarized"] is False and item["message_count"] == 2
        detail = client.get(f"/api/conversations/{item['id']}/messages", params={"client_id": CLIENT})
        assert detail.status_code == 200
        rows = detail.json()["items"]
        assert [row["role"] for row in rows] == ["user", "assistant"]
        assert rows[1]["content"] == "收到"
        assert detail.json()["conversation_id"] == item["id"]
        assert all(row["created_at"] for row in rows)


def test_list_only_returns_own_conversations_newest_first():
    with api(lambda request: streamed(("收到",))) as (client, _):
        client.post("/api/chat", json={"message": "第一通", "client_id": CLIENT})
        client.post("/api/chat", json={"message": "第二通", "client_id": CLIENT})
        client.post("/api/chat", json={"message": "别人的会话", "client_id": "client-zzz99999"})
        titles = [item["title"] for item in client.get(
            "/api/conversations", params={"client_id": CLIENT}).json()["items"]]
        assert titles == ["第二通", "第一通"]
        assert client.get("/api/conversations", params={"client_id": "client-nobody1"}).json() == {"items": []}


def test_reload_rejects_other_client():
    with api(lambda request: streamed(("收到",))) as (client, _):
        response = client.post("/api/chat", json={"message": "问题", "client_id": CLIENT})
        conversation_id = events(response)[0][1]["conversation_id"]
        forbidden = client.get(f"/api/conversations/{conversation_id}/messages",
                               params={"client_id": "client-zzz99999"})
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == "forbidden"
        # 不存在的会话同样返回 403，不泄露会话是否存在。
        assert client.get("/api/conversations/999999/messages",
                          params={"client_id": CLIENT}).status_code == 403


def test_conversation_endpoints_reject_bad_parameters():
    with api(lambda request: streamed()) as (client, _):
        assert client.get("/api/conversations").status_code == 422
        assert client.get("/api/conversations", params={"client_id": "短"}).status_code == 422
        assert client.get("/api/conversations/0/messages", params={"client_id": CLIENT}).status_code == 422
        assert client.get("/api/conversations/1/messages").status_code == 422
        # 超出 BIGINT 的 id 要在参数层就拒掉，别让它打到驱动里抛 OverflowError 变 500。
        assert client.get(f"/api/conversations/{2 ** 63}/messages",
                          params={"client_id": CLIENT}).status_code == 422
        assert client.get(f"/api/conversations/{2 ** 62}/messages",
                          params={"client_id": CLIENT}).status_code == 403


def test_file_log_captures_context_lines(tmp_path):
    target = tmp_path / "log" / "app.log"
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    try:
        with api(lambda request: streamed(("好的。",)), log_file=str(target)) as (client, _):
            events(client.post("/api/chat", json={"message": "订单 1001 的退款", "client_id": CLIENT}))
        text = target.read_text(encoding="utf-8")
        assert "history_ctx" in text and "model_ctx" in text
        assert "window=" in text and "summary_tokens=" in text and "total_tokens=" in text
        assert "messages=[{'role': 'system'" in text
        assert "订单 1001 的退款" in text
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(level)


def test_configure_file_logging_is_idempotent_and_skips_memory(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    target = tmp_path / "app.log"
    try:
        configure_file_logging(":memory:")
        configure_file_logging("")
        assert list(root.handlers) == before
        configure_file_logging(str(target))
        configure_file_logging(str(target))
        added = [handler for handler in root.handlers if handler not in before]
        assert len(added) == 1
        logging.getLogger("app.probe").info("探针一行")
        for handler in added:
            handler.flush()
        assert "探针一行" in target.read_text(encoding="utf-8")
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()


def test_startup_selfcheck_reports_insufficient_budget(caplog):
    caplog.set_level(logging.INFO)
    with api(lambda request: streamed(), context_window_tokens=1500):
        pass
    assert any("上下文预算不足" in record.getMessage() for record in caplog.records)
