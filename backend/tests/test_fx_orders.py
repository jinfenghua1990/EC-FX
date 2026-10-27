"""分销订单列表的过滤测试。

回归：list_orders 曾先 limit() 再 filter()，SQLAlchemy 会抛
InvalidRequestError，导致待审核/已审核订单接口 500。
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxDistributor, FxOrder
from app.services.distribution import list_orders


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture()
def seeded(db):
    d1 = FxDistributor(code="D001", name="分销商一")
    d2 = FxDistributor(code="D002", name="分销商二")
    db.add_all([d1, d2])
    db.flush()
    db.add_all([
        FxOrder(order_no="SO001", distributor_id=d1.id, status="submitted", total_amount=Decimal("10")),
        FxOrder(order_no="SO002", distributor_id=d1.id, status="approved", total_amount=Decimal("20")),
        FxOrder(order_no="SO003", distributor_id=d2.id, status="submitted", total_amount=Decimal("30")),
    ])
    db.commit()
    return db, d1, d2


def test_list_orders_filters_by_status_after_limit(seeded):
    db, _, _ = seeded
    rows = list_orders(db, status="submitted")
    assert {row["order_no"] for row in rows} == {"SO001", "SO003"}


def test_list_orders_filters_by_distributor_and_limit(seeded):
    db, d1, _ = seeded
    rows = list_orders(db, status="submitted", distributor_id=d1.id, limit=1)
    assert len(rows) == 1
    assert rows[0]["order_no"] == "SO001"


def test_list_orders_rejects_unknown_status(seeded):
    db, _, _ = seeded
    with pytest.raises(ValueError):
        list_orders(db, status="not-a-status")
