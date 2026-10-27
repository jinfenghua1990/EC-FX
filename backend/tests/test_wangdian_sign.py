from app.services.wangdian import sign_params


def test_official_sign_example():
    params = {
        "appkey": "test2-xx",
        "page_no": 0,
        "end_time": "2016-08-01 13:00:00",
        "start_time": "2016-08-01 12:00:00",
        "page_size": 40,
        "sid": "test2",
        "timestamp": 1470042310,
    }
    assert sign_params(params, "12345") == "ad4e6fe037ea6e3ba4768317be9d1309"
