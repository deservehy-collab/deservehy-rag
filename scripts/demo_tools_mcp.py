"""第 8 章六条验收的演示：每条打印它证明了什么，任一硬条件不满足以非零码退出。

前置条件：
  1) 客服服务已起：.venv\\Scripts\\python.exe -X utf8 -m uvicorn app.main:app --port 8000
  2) 两个 MCP Server 已起：双击 start-mcp.bat（只有 --case logistics 需要；其余自己起子进程）
  3) .env 里的模型与数据库可用（服务与脚本读同一个库，才看得到同一条审计）
"""
import argparse
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from sqlalchemy import create_engine, func, select

from app.config import Settings
from app.database import build_database_url, build_engine as build_sql_engine, build_session_factory
from app.models import Base, Ticket, ToolAuditLog
from app.storage import ConversationStore
from app.tools.mcp import McpDiscovery
from app.tools.wiring import build_engine, build_registry

REPO = Path(__file__).resolve().parents[1]
CLIENT_ID = "demo-mcp-tools"
CASES = ("register", "logistics", "hot-discover", "confirm", "cancel", "timeout")
# 工单号是 T{YYYYMMDD}{seq:03d}，不是 TK 开头：判据只认这个形状。
TICKET_NO = re.compile(r"T\d{11}")
# 「追问」的判据是「在向用户要信息」，不是「句尾有没有问号」：模型会用「请告诉我：1.…2.…」这种清单句式。
ASK_MARKS = ("？", "?", "请告诉我", "请补充", "请说明", "麻烦提供", "还需要")


class DemoFailure(Exception):
    pass


def show(title, lines) -> None:
    print(f"\n=== {title} ===")
    for line in lines:
        print("  " + line)


def wait_port(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise DemoFailure(f"MCP Server 提前退出，返回码 {proc.returncode}")
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise DemoFailure(f"MCP Server 端口 {port} 在 {timeout}s 内没起来")


class McpProcess:
    """自己起、自己关：验收③要的就是「只动 Server 这一侧」。"""

    def __init__(self, port: int, env: dict):
        self.port = port
        self.proc = subprocess.Popen([sys.executable, "-m", "mcp_servers.logistics_server"],
                                     cwd=REPO, env={**os.environ, "LOGISTICS_PORT": str(port), **env})
        wait_port(port, self.proc)
        self.url = f"http://127.0.0.1:{port}/mcp"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@contextmanager
def temp_store():
    """临时库：退出时先 dispose 再删目录——Windows 上 SQLite 文件还被连接占着时删会 PermissionError。"""
    workdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    engine = create_engine(f"sqlite:///{Path(workdir.name) / 'demo.db'}")
    Base.metadata.create_all(engine)
    try:
        yield ConversationStore(build_session_factory(engine))
    finally:
        engine.dispose()
        workdir.cleanup()


def stream(client: httpx.Client, path: str, body: dict) -> dict:
    frames, answer, conversation_id = [], "", None
    event, data = "", []
    with client.stream("POST", path, json=body) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                payload = json.loads("\n".join(data))
                frames.append((event, payload))
                if event == "meta":
                    conversation_id = payload["conversation_id"]
                elif event == "token":
                    answer += payload["content"]
                elif event == "error":
                    raise DemoFailure(payload["message"])
                event, data = "", []
    return {"frames": frames, "answer": answer, "conversation_id": conversation_id}


def card_of(result: dict):
    return next((payload for event, payload in result["frames"] if event == "confirm"), None)


def case_register(settings, args):
    """验收①：新工具只做注册动作就能被用上，核心代码一行没改。"""
    for relative in ("app/workflow/agent_node.py", "app/agent/react.py", "app/tools/wiring.py"):
        if "coupon" in (REPO / relative).read_text(encoding="utf-8"):
            raise DemoFailure(f"{relative} 里出现了 coupon——注册不该改核心代码。")
    with temp_store() as store:
        # 演示不依赖 .env：模块名直接注入 settings，等价于在 .env 里写 TOOL_EXTRA_MODULES
        with_coupon = settings.model_copy(update={"tool_extra_modules": "examples.tools.demo_coupon"})
        conversation_id = store.start_turn(user_id=CLIENT_ID, message="这单有什么券能用？")
        table = build_registry(store, conversation_id, None, settings=with_coupon,
                               mcp_specs=())
        spec = table.get("query_coupon")
        if spec is None:
            raise DemoFailure("注册中心没收到 query_coupon。")
        if not (spec.source == "builtin" and spec.permission == "read" and spec.label and spec.json_schema):
            raise DemoFailure(f"三样不齐：source={spec.source} permission={spec.permission} label={spec.label}")
        engine = build_engine(table, settings=with_coupon, session_factory=store.session_factory)
        outcome = asyncio.run(engine.run(name="query_coupon", args={"order_id": "1001"},
                                         tool_call_id="demo-register", conversation_id=conversation_id))
        if not outcome.ok:
            raise DemoFailure(f"新工具跑不通：{outcome.code}｜{outcome.summary}")
        denied = asyncio.run(engine.run(name="issue_coupon", args={"order_id": "1001"},
                                        tool_call_id="demo-register-write",
                                        conversation_id=conversation_id, confirmed=True))
        if denied.ok or denied.code != "permission_denied":
            raise DemoFailure(f"白名单外的写操作竟然执行了：{denied.code}")
        with store.session_factory() as session:
            rows = session.scalars(select(ToolAuditLog).order_by(ToolAuditLog.id)).all()
            statuses = [row.status for row in rows]
        if "成功" not in statuses or "权限拒绝" not in statuses:
            raise DemoFailure(f"审计没落全：{statuses}")
        show("验收①：注册即可用，不动核心代码", [
            f"query_coupon 已在表里：{spec.label}｜{spec.permission}｜schema {len(spec.json_schema.get('properties', {}))} 个参数",
            f"真跑一次：{outcome.summary}（{outcome.duration_ms}ms，source={outcome.source}）",
            f"注册≠授权：issue_coupon → {denied.code}，审计状态序列 {statuses}",
        ])


def case_logistics(settings, args):
    """验收②：两个 Server 独立进程；问物流这一次走的是 MCP，不是内置。"""
    discovery = McpDiscovery(settings)
    specs = asyncio.run(discovery.specs(force=True))
    by_name = {item.name: item for item in specs}
    if "query_logistics" not in by_name:
        raise DemoFailure("MCP 里没拿到 query_logistics——先双击 start-mcp.bat 起两个 Server。")
    if by_name["query_logistics"].source != "mcp" or by_name["query_logistics"].mcp_server != "logistics":
        raise DemoFailure("query_logistics 的来源不是 logistics MCP Server。")
    if "query_warranty" not in by_name:
        raise DemoFailure("售后 Server 没连上（缺 query_warranty）。")
    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        result = stream(client, "/api/chat",
                        {"message": "订单 1001 的物流到哪了？", "client_id": CLIENT_ID})
    badges = [payload for event, payload in result["frames"] if event == "status"]
    over_mcp = [item for item in badges if item.get("tool") == "query_logistics" and item.get("phase") == "end"]
    if not over_mcp:
        raise DemoFailure(f"这一轮没有物流状态帧。事件：{[e for e, _ in result['frames']]}")
    if over_mcp[-1].get("source") != "mcp":
        raise DemoFailure(f"物流没走 MCP：{over_mcp[-1]}")
    if "1001" not in result["answer"]:
        raise DemoFailure(f"回答没体现订单号。回答：{result['answer']}")
    show("验收②：问物流走 MCP", [
        f"MCP 发现表：{sorted(by_name)}",
        f"status 帧：tool={over_mcp[-1]['tool']} source={over_mcp[-1].get('source')} "
        f"mcp_server={over_mcp[-1].get('mcp_server')} ok={over_mcp[-1]['ok']}",
        f"答：{result['answer'][:120]}",
    ])


def case_hot_discover(settings, args):
    """验收③：Server 侧新加工具只重启 Server——客服这侧进程、代码、缓存配置都不动。"""
    first = McpProcess(8941, {})
    try:
        discovery = McpDiscovery(settings.model_copy(update={"mcp_logistics_url": first.url,
                                                              "mcp_servers": "logistics"}))
        before = {item.name for item in asyncio.run(discovery.specs(force=True))}
        if "query_station_info" in before:
            raise DemoFailure("没开额外工具的 Server 就带了 query_station_info，夹具不成立。")
    finally:
        first.stop()
    second = McpProcess(8941, {"LOGISTICS_EXTRA_TOOLS": "query_station_info"})
    try:
        after = {item.name: item for item in asyncio.run(discovery.specs(force=True))}
        if "query_station_info" not in after:
            raise DemoFailure(f"换了 Server 进程后仍没发现新工具：{sorted(after)}")
        if after["query_station_info"].source != "mcp" or after["query_station_info"].permission != "read":
            raise DemoFailure("新工具来源／权限不对：权限只认我们这侧的规则。")
        show("验收③：热发现（同一个进程、同一个 McpDiscovery 实例）", [
            f"重启前：{sorted(before)}",
            f"重启后：{sorted(after)}（新增 query_station_info｜source={after['query_station_info'].source}｜"
            f"permission={after['query_station_info'].permission}）",
            "客服系统代码没改、进程没重启；force=True 只是跳过 60s 缓存，"
            "不设它则新工具最多 60 秒后出现在表里（TOOL_DISCOVERY_CACHE_SECONDS 可调 0 立刻生效）。",
        ])
    finally:
        second.stop()


# 卡片只长在 agent 这一条路上：退款退货／售后走 policy、投诉走 complaint，压根到不了 create_ticket。
# 所以演示话术要说清「什么问题＋要建工单」，但别踩 退货／退款／投诉 这些词——踩了就换一条路，测不到确认流。
def case_confirm(settings, args):
    """验收④：没说清→先追问；说清了→卡片→确认→落库，回复带工单号。"""
    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        vague = stream(client, "/api/chat", {"message": "帮我建个工单", "client_id": CLIENT_ID})
        if card_of(vague) is not None:
            raise DemoFailure("信息不足却直接推了卡片——追问约束没生效（硬指标见 T16 评估集）。")
        if not any(mark in vague["answer"] for mark in ASK_MARKS):
            raise DemoFailure(f"没有追问，直接答了：{vague['answer'][:80]}")

        full = stream(client, "/api/chat",
                      {"message": "订单 1001 的快递在转运中心停了五天没动，帮我建工单跟进一下",
                       "client_id": CLIENT_ID})
        card = card_of(full)
        if card is None:
            raise DemoFailure(f"说清了却没出卡片：{[e for e, _ in full['frames']]}｜{full['answer'][:80]}")
        resumed = stream(client, "/api/chat/resume",
                         {"conversation_id": full["conversation_id"], "client_id": CLIENT_ID,
                          "decision": "accept", "pending_id": card["pending_id"]})
    found = TICKET_NO.search(resumed["answer"])
    if not found:
        raise DemoFailure(f"确认后回复里没有工单号：{resumed['answer']}")
    factory = build_session_factory(build_sql_engine(build_database_url(settings)))
    with factory() as session:
        # 按主键查回复里那个号，不查「最新一行」：tickets 表是共享的，最新一行未必是这一轮建的。
        ticket = session.get(Ticket, found.group())
    if ticket is None:
        raise DemoFailure(f"回复里的工单号 {found.group()} 在 tickets 表里找不到。")
    show("验收④：追问 → 卡片 → 确认 → 落库", [
        f"「帮我建个工单」→ {vague['answer'][:80]}（无卡片、无写操作）",
        f"卡片字段：{[(item['label'], item['value']) for item in card['fields']]}",
        f"确认后：{resumed['answer'][:80]}",
        f"tickets 表这一行：{ticket.ticket_no}｜{ticket.status}｜{ticket.ticket_type}｜"
        f"会话 #{ticket.conversation_id}｜{ticket.description[:40]}",
    ])


def case_cancel(settings, args):
    """验收⑤：点取消 → 工单没建，审计按「权限拒绝」留痕。"""
    factory = build_session_factory(build_sql_engine(build_database_url(settings)))
    with factory() as session:
        before = session.scalar(select(func.count()).select_from(Ticket)) or 0
    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        full = stream(client, "/api/chat",
                      {"message": "订单 1002 显示已经签收了，可我根本没收到过包裹，帮我建工单让人核查",
                       "client_id": CLIENT_ID})
        card = card_of(full)
        if card is None:
            raise DemoFailure(f"没出卡片，取消流程无从谈起：{full['answer'][:80]}")
        resumed = stream(client, "/api/chat/resume",
                         {"conversation_id": full["conversation_id"], "client_id": CLIENT_ID,
                          "decision": "cancel", "pending_id": card["pending_id"]})
    with factory() as session:
        after = session.scalar(select(func.count()).select_from(Ticket)) or 0
        row = session.scalars(select(ToolAuditLog).where(ToolAuditLog.conversation_id == full["conversation_id"])
                              .order_by(ToolAuditLog.id.desc())).first()
    if after != before:
        raise DemoFailure(f"取消了却多出一张工单：{before} → {after}")
    if row is None or row.status != "权限拒绝":
        raise DemoFailure(f"审计没按「权限拒绝」落：{row.status if row else None}")
    if row.retry_count != 0 or row.tool_name != "create_ticket":
        raise DemoFailure(f"取消的审计字段不对：{row.tool_name}｜retry={row.retry_count}")
    show("验收⑤：取消不建单、审计留痕", [
        f"回复：{resumed['answer'][:80]}",
        f"tickets 行数：{before} → {after}（没动）",
        f"审计：{row.tool_name}｜状态={row.status}｜retry_count={row.retry_count}｜"
        f"arguments={row.arguments}｜error={row.error_message}",
    ])


def case_timeout(settings, args):
    """验收⑥：读操作超时能看到重试与兜底；写操作超时必须一次都不重试。"""
    # 同一个 tool_timeout_seconds 既管握手也管调用（mcp.py:60）：实测握手 0.35～0.46s，
    # 所以超时压到 2 秒（够握手、不够 9 秒的慢响应），不是随便取的小数。
    slow = settings.model_copy(update={"tool_timeout_seconds": 2.0, "tool_max_attempts": 2,
                                       "mcp_servers": "logistics"})
    server = McpProcess(8942, {"LOGISTICS_SLOW_SECONDS": "9"})
    # 这个 Server 必须活到读工具真的被调用之后：发现只拿名字和 schema，执行还要再连一次，
    # 提前关掉只会得到「连不上」，测不到超时与重试。写工具是内置模块，不依赖它。
    try:
        discovery = McpDiscovery(slow.model_copy(update={"mcp_logistics_url": server.url}))
        mcp_specs = asyncio.run(discovery.specs(force=True))
        with temp_store() as store:
            conversation_id = store.start_turn(user_id=CLIENT_ID, message="超时演示")
            # 同一个 settings 同时喂注册与引擎：写白名单与短超时都要生效，否则这条用例测不到「不重试」
            spec_settings = slow.model_copy(update={"tool_extra_modules": "examples.tools.demo_slow_write",
                                                    "tool_write_allowlist": "create_ticket,demo_release_stock"})
            table = build_registry(store, conversation_id, None, settings=spec_settings, mcp_specs=mcp_specs)
            engine = build_engine(table, settings=spec_settings, session_factory=store.session_factory)

            read_outcome = asyncio.run(engine.run(name="query_logistics", args={"order_id": "1001"},
                                                  tool_call_id="demo-timeout-read",
                                                  conversation_id=conversation_id))
            if read_outcome.ok or read_outcome.audit_status != "超时":
                raise DemoFailure(f"读工具超时没按「超时」分诊：{read_outcome.code}｜{read_outcome.audit_status}")
            if read_outcome.retry_count != 1:
                raise DemoFailure(f"读操作该重试 1 次（尝试 2 − 1），实际 {read_outcome.retry_count}")

            import examples.tools.demo_slow_write as slow_write

            write_outcome = asyncio.run(engine.run(name="demo_release_stock", args={"note": "超时看次数"},
                                                   tool_call_id="demo-timeout-write",
                                                   conversation_id=conversation_id, confirmed=True))
            if write_outcome.audit_status != "超时":
                raise DemoFailure(f"写工具没按超时分诊：{write_outcome.code}")
            if slow_write.ATTEMPTS != 1 or write_outcome.retry_count != 0:
                raise DemoFailure(f"写操作被自动重试了：调用 {slow_write.ATTEMPTS} 次，retry_count="
                                  f"{write_outcome.retry_count}")

            with store.session_factory() as session:
                rows = session.scalars(select(ToolAuditLog).order_by(ToolAuditLog.id)).all()
            if len(rows) < 2:
                raise DemoFailure(f"审计没落够两行：{len(rows)}")
            last = rows[-1]
            show("验收⑥：超时、重试与兜底", [
                f"读（MCP query_logistics）：状态={read_outcome.audit_status}｜retry_count={read_outcome.retry_count}"
                f"｜耗时={read_outcome.duration_ms}ms｜兜底给模型的话术={read_outcome.content[:60]}",
                f"写（demo_release_stock）：模块内 ATTEMPTS={slow_write.ATTEMPTS}（应为 1）｜"
                f"retry_count={write_outcome.retry_count}（应为 0）",
                "审计字段：tool_name/source/mcp_server/arguments/result_summary/status/error_message/"
                "retry_count/duration_ms/created_at",
                f"最后一行：{last.tool_name}｜{last.tool_source}｜{last.mcp_server}｜{last.status}｜"
                f"{last.retry_count}｜{last.duration_ms}ms｜{last.result_summary}",
            ])
    finally:
        server.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="第 8 章工具系统六条验收演示。")
    parser.add_argument("--case", default="all", choices=("all", *CASES))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
    except Exception as exc:
        print(f"验收未通过：读不到模型配置——{exc}")
        return 1
    runners = {"register": case_register, "logistics": case_logistics, "hot-discover": case_hot_discover,
               "confirm": case_confirm, "cancel": case_cancel, "timeout": case_timeout}
    selected = CASES if args.case == "all" else (args.case,)
    try:
        for name in selected:
            runners[name](settings, args)
    except (DemoFailure, httpx.HTTPError, ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, DemoFailure) else "服务或数据库不可用，请检查服务、.env 与上游配置。"
        print("\n验收未通过：" + message)
        return 1
    print(f"\n通过：{('、'.join(selected))}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
