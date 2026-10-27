from app.services.importers import read_tabular


def test_csv_read():
    rows = read_tabular("x.csv", "订单号,结算金额\nA1,12.34\n".encode("utf-8"))
    assert rows[0]["订单号"] == "A1"
