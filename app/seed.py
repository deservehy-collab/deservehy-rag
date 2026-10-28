from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Faq

FAQ_SEED = [
    {"question": "退货政策是什么", "answer": "签收后 7 天内可申请无理由退货，商品需保持完好、吊牌和包装齐全。定制类、贴身衣物和已拆封的食品不支持无理由退货。", "category": "售后"},
    {"question": "怎么申请退货", "answer": "在订单详情页点击「申请售后」，选择退货退款并填写原因。审核通过后会生成退货地址，请在 48 小时内寄回。", "category": "售后"},
    {"question": "退款多久到账", "answer": "仓库签收并验收通过后 1-2 个工作日内发起退款，原路退回。银行卡到账时间以银行处理为准。", "category": "退款"},
    {"question": "换货怎么操作", "answer": "在订单详情页选择「换货」，注明需要的尺码或颜色。有库存时会直接安排发出，无库存会联系你协商。", "category": "售后"},
    {"question": "发货时间", "answer": "现货订单在付款后 48 小时内发出，预售商品以商品页标注的发货时间为准。", "category": "物流"},
    {"question": "可以修改收货地址吗", "answer": "订单未发货前可以在订单详情页修改地址；已发货的订单请联系客服协助联系快递。", "category": "订单"},
    {"question": "发票怎么开", "answer": "订单完成后可在订单详情页申请电子发票，填写抬头和税号，通常 1 个工作日内开出。", "category": "发票"},
    {"question": "商品保修多久", "answer": "电子产品自签收之日起享受 12 个月保修，非人为损坏可申请维修；配件保修 3 个月。", "category": "保修"},
]


def seed_faq(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        if session.scalar(select(func.count()).select_from(Faq)):
            return 0
        session.add_all(Faq(**row) for row in FAQ_SEED)
        session.commit()
    return len(FAQ_SEED)
