"""Server 内的 mock 数据：按入参做**确定性**伪随机。

需求要「随机生成 mock」，纯随机会让验收②和集成测试无法对答案，
所以种子取工具名＋入参（规格 §18-C1）。不接真实系统，也不建任何表。
"""
import datetime as dt
import hashlib
import random

CARRIERS = ["顺丰速运", "中通快递", "圆通速递", "京东物流", "邮政EMS"]
CARRIER_PREFIX = {"顺丰速运": "SF", "中通快递": "ZTO", "圆通速递": "YT", "京东物流": "JD", "邮政EMS": "EMS"}
COURIERS = ["张伟", "李静", "王强"]
STATIONS = ["上海转运中心", "杭州转运中心", "北京顺义分拨", "广州佛山分拨", "武汉鄂州分拨"]
STEPS = ["包裹已揽收，快递员{courier}", "干线运输中，下一站{station}", "已到达{station}，正在分拣",
         "派送中，预计今日送达", "已签收，感谢使用"]
LOGISTICS_STATUS = ["PICKED_UP", "IN_TRANSIT", "IN_TRANSIT", "IN_TRANSIT", "DELIVERED"]
WARRANTY_BASE_DATE = dt.date(2026, 10, 31)


def seeded(*parts) -> random.Random:
    key = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def logistics_payload(order_id: str) -> dict:
    rnd = seeded("query_logistics", order_id)
    carrier = rnd.choice(CARRIERS)
    courier = rnd.choice(COURIERS)
    station = rnd.choice(STATIONS)
    steps = rnd.randint(1, len(STEPS))
    nodes = [{"time": f"2026-10-{20 + index:02d} {9 + index * 3:02d}:{index * 7 % 60:02d}",
              "text": STEPS[index].format(courier=courier, station=station)}
             for index in range(steps)]
    return {"order_id": order_id, "carrier": carrier,
            "tracking_no": f"{CARRIER_PREFIX[carrier]}{rnd.randint(10**11, 10**12 - 1)}",
            "status": LOGISTICS_STATUS[steps - 1],
            "nodes": nodes}


def warranty_payload(order_id: str) -> dict:
    rnd = seeded("query_warranty", order_id)
    days_left = rnd.randint(-30, 330)
    return {"order_id": order_id,
            "status": "IN_WARRANTY" if days_left >= 0 else "OUT_OF_WARRANTY",
            "days_left": days_left,
            "expires_on": (WARRANTY_BASE_DATE + dt.timedelta(days=days_left)).isoformat()}


def refund_progress_payload(order_id: str) -> dict:
    rnd = seeded("query_refund_progress", order_id)
    status = rnd.choice(["PROCESSING", "APPROVED", "REJECTED", "REFUNDED"])
    remark = {"PROCESSING": "退款审核中，1-3 个工作日内到账", "APPROVED": "审核通过，等待打款",
              "REJECTED": "审核未通过：商品已签收超过 7 天", "REFUNDED": "款项已原路退回"}[status]
    return {"order_id": order_id, "status": status, "remark": remark,
            "updated_at": f"2026-10-{rnd.randint(20, 29):02d} {rnd.randint(8, 20):02d}:00"}
