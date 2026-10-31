import tempfile
import unittest
from pathlib import Path

import requests

from ki.client import KISClient, KISError, KISOutcomeUnknown
from ki.config import Settings


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(
                f"{self.status_code} Server Error",
                response=response,
            )

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, response_body=None) -> None:
        self.calls = []
        self.response_body = response_body

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/oauth2/tokenP"):
            return FakeResponse({"access_token": "token", "expires_in": 3600})
        if url.endswith("/uapi/hashkey"):
            return FakeResponse({"HASH": "hash"})
        raise AssertionError(f"unexpected POST: {url}")

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.response_body is not None:
            return FakeResponse(self.response_body)
        return FakeResponse(
            {
                "rt_cd": "0",
                "output": {"ODNO": "123"},
            }
        )


class RetrySession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.request_count = 0

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        self.request_count += 1
        if self.request_count == 1:
            return FakeResponse({}, status_code=500)
        return FakeResponse({"rt_cd": "0", "output": {"last": "200.00"}})


class KISClientTest(unittest.TestCase):
    def test_apple_quote_uses_overseas_price_api(self) -> None:
        settings = Settings(
            environment="real",
            app_key="key",
            app_secret="secret",
            account_no="",
        )
        session = FakeSession()
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings, session=session, token_dir=Path(temp_dir))
            client.overseas_quote("AAPL", "NAS")

        quote_call = session.calls[-1]
        self.assertTrue(
            quote_call[1].endswith("/uapi/overseas-price/v1/quotations/price")
        )
        self.assertEqual(quote_call[2]["headers"]["tr_id"], "HHDFS00000300")
        self.assertEqual(
            quote_call[2]["params"],
            {"AUTH": "", "EXCD": "NAS", "SYMB": "AAPL"},
        )

    def test_overseas_minute_candles_use_chart_api(self) -> None:
        settings = Settings(
            environment="real",
            app_key="key",
            app_secret="secret",
            account_no="",
        )
        session = FakeSession(
            {
                "rt_cd": "0",
                "output1": {"next": ""},
                "output2": [{"tymd": "20260623", "xhms": "103000", "last": "300"}],
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings, session=session, token_dir=Path(temp_dir))
            result = client.overseas_minute_candles(
                "AAPL",
                "NAS",
                interval=5,
                count=50,
            )

        call = session.calls[-1]
        self.assertTrue(call[1].endswith("/inquire-time-itemchartprice"))
        self.assertEqual(call[2]["headers"]["tr_id"], "HHDFS76950200")
        self.assertEqual(call[2]["params"]["NMIN"], "5")
        self.assertEqual(call[2]["params"]["NREC"], "50")
        self.assertEqual(result["candles"][0]["last"], "300")

    def test_real_overseas_balance_uses_account_api(self) -> None:
        settings = Settings(
            environment="real",
            app_key="key",
            app_secret="secret",
            account_no="12345678",
        )
        session = FakeSession(
            {
                "rt_cd": "0",
                "output1": [{"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "2"}],
                "output2": [{"frcr_pchs_amt1": "200.00"}],
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings, session=session, token_dir=Path(temp_dir))
            result = client.overseas_balance()

        call = session.calls[-1]
        self.assertTrue(call[1].endswith("/inquire-balance"))
        self.assertEqual(call[2]["headers"]["tr_id"], "TTTS3012R")
        self.assertEqual(call[2]["params"]["OVRS_EXCG_CD"], "NASD")
        self.assertEqual(result["positions"][0]["ovrs_pdno"], "AAPL")

    def test_demo_overseas_buying_power_uses_current_tr_id(self) -> None:
        settings = Settings(
            environment="demo",
            app_key="key",
            app_secret="secret",
            account_no="12345678",
        )
        session = FakeSession(
            {
                "rt_cd": "0",
                "output": {
                    "ord_psbl_frcr_amt": "500.00",
                    "max_ord_psbl_qty": "2",
                },
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings, session=session, token_dir=Path(temp_dir))
            result = client.overseas_buying_power("AAPL", "200.50")

        call = session.calls[-1]
        self.assertTrue(call[1].endswith("/inquire-psamount"))
        self.assertEqual(call[2]["headers"]["tr_id"], "VTTS3007R")
        self.assertEqual(call[2]["params"]["OVRS_ORD_UNPR"], "200.50")
        self.assertEqual(result["max_ord_psbl_qty"], "2")

    def test_get_retries_transient_server_error(self) -> None:
        settings = Settings(
            environment="real",
            app_key="key",
            app_secret="secret",
            account_no="",
        )
        session = RetrySession()
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings, session=session, token_dir=Path(temp_dir))
            result = client.overseas_quote("AAPL", "NAS")

        self.assertEqual(result["last"], "200.00")
        self.assertEqual(session.request_count, 2)

    def test_overseas_pending_orders_uses_nccs_api(self) -> None:
        settings = Settings(
            environment="real",
            app_key="key",
            app_secret="secret",
            account_no="12345678",
        )
        session = FakeSession(
            {
                "rt_cd": "0",
                "output": [{"odno": "100", "pdno": "AAPL"}],
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings, session=session, token_dir=Path(temp_dir))
            result = client.overseas_pending_orders()

        call = session.calls[-1]
        self.assertTrue(call[1].endswith("/inquire-nccs"))
        self.assertEqual(call[2]["headers"]["tr_id"], "TTTS3018R")
        self.assertEqual(call[2]["params"]["OVRS_EXCG_CD"], "NASD")
        self.assertEqual(result[0]["odno"], "100")

    def test_overseas_order_history_uses_ccnl_api(self) -> None:
        settings = Settings(
            environment="real",
            app_key="key",
            app_secret="secret",
            account_no="12345678",
        )
        session = FakeSession(
            {
                "rt_cd": "0",
                "output": {"odno": "101", "pdno": "AAPL"},
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings, session=session, token_dir=Path(temp_dir))
            result = client.overseas_order_history("20260601", "20260612")

        call = session.calls[-1]
        self.assertTrue(call[1].endswith("/inquire-ccnl"))
        self.assertEqual(call[2]["headers"]["tr_id"], "TTTS3035R")
        self.assertEqual(call[2]["params"]["PDNO"], "%")
        self.assertEqual(call[2]["params"]["CCLD_NCCS_DVSN"], "00")
        self.assertEqual(result[0]["odno"], "101")

    def test_real_overseas_buy_order_uses_us_tr_id_and_hashkey(self) -> None:
        settings = Settings(
            environment="real",
            app_key="key",
            app_secret="secret",
            account_no="12345678",
        )
        session = FakeSession()
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings, session=session, token_dir=Path(temp_dir))
            client.overseas_order("buy", "AAPL", 1, "250.00")

        order_call = session.calls[-1]
        self.assertTrue(order_call[1].endswith("/overseas-stock/v1/trading/order"))
        self.assertEqual(order_call[2]["headers"]["tr_id"], "TTTT1002U")
        self.assertEqual(order_call[2]["headers"]["hashkey"], "hash")
        self.assertEqual(order_call[2]["json"]["OVRS_EXCG_CD"], "NASD")
        self.assertEqual(order_call[2]["json"]["PDNO"], "AAPL")
        self.assertEqual(order_call[2]["json"]["ORD_QTY"], "1")
        self.assertEqual(order_call[2]["json"]["OVRS_ORD_UNPR"], "250.00")
        self.assertEqual(order_call[2]["json"]["ORD_DVSN"], "00")

    def test_demo_overseas_sell_order_uses_demo_us_tr_id(self) -> None:
        settings = Settings(
            environment="demo",
            app_key="key",
            app_secret="secret",
            account_no="12345678",
        )
        session = FakeSession()
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings, session=session, token_dir=Path(temp_dir))
            client.overseas_order("sell", "AAPL", 1, "250.00")

        order_call = session.calls[-1]
        self.assertEqual(order_call[2]["headers"]["tr_id"], "VTTT1006U")
        self.assertEqual(order_call[2]["json"]["SLL_TYPE"], "00")

    def test_real_overseas_cancel_order_uses_rvsecncl_api(self) -> None:
        settings = Settings(
            environment="real",
            app_key="key",
            app_secret="secret",
            account_no="12345678",
        )
        session = FakeSession({"rt_cd": "0", "output": {"ODNO": "CANCEL123"}})
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings, session=session, token_dir=Path(temp_dir))
            result = client.overseas_cancel_order(
                symbol="AAPL",
                original_order_id="KIS123",
                quantity=1,
            )

        cancel_call = session.calls[-1]
        self.assertEqual(result["ODNO"], "CANCEL123")
        self.assertTrue(cancel_call[1].endswith("/trading/order-rvsecncl"))
        self.assertEqual(cancel_call[2]["headers"]["tr_id"], "TTTT1004U")
        self.assertEqual(cancel_call[2]["headers"]["hashkey"], "hash")
        self.assertEqual(cancel_call[2]["json"]["PDNO"], "AAPL")
        self.assertEqual(cancel_call[2]["json"]["ORGN_ODNO"], "KIS123")
        self.assertEqual(cancel_call[2]["json"]["RVSE_CNCL_DVSN_CD"], "02")
        self.assertEqual(cancel_call[2]["json"]["ORD_QTY"], "1")
        self.assertEqual(cancel_call[2]["json"]["OVRS_ORD_UNPR"], "0")

    def test_demo_overseas_cancel_order_uses_demo_tr_id(self) -> None:
        settings = Settings(
            environment="demo",
            app_key="key",
            app_secret="secret",
            account_no="12345678",
        )
        session = FakeSession()
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings, session=session, token_dir=Path(temp_dir))
            client.overseas_cancel_order(
                symbol="AAPL",
                original_order_id="KIS123",
                quantity=1,
            )

        cancel_call = session.calls[-1]
        self.assertEqual(cancel_call[2]["headers"]["tr_id"], "VTTT1004U")


class OrderPostSafetySession(FakeSession):
    """토큰·해시키는 정상, 주문 요청만 지정한 실패를 낸다."""

    def __init__(self, failure) -> None:
        super().__init__()
        self.failure = failure
        self.order_calls = 0

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        self.order_calls += 1
        if isinstance(self.failure, Exception):
            raise self.failure
        return self.failure


class KISOrderSafetyTest(unittest.TestCase):
    def _order(self, failure):
        settings = Settings(environment="real", app_key="key", app_secret="secret", account_no="12345678")
        session = OrderPostSafetySession(failure)
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings, session=session, token_dir=Path(temp_dir))
            try:
                client.overseas_order("buy", "AAPL", 1, "250.00")
                error = None
            except KISError as exc:
                error = exc
        return error, session

    def test_read_timeout_is_unknown_and_sent_once(self) -> None:
        error, session = self._order(requests.ReadTimeout("late"))
        self.assertIsInstance(error, KISOutcomeUnknown)
        self.assertEqual(session.order_calls, 1)

    def test_server_error_is_unknown_and_sent_once(self) -> None:
        error, session = self._order(FakeResponse({}, status_code=502))
        self.assertIsInstance(error, KISOutcomeUnknown)
        self.assertEqual(session.order_calls, 1)

    def test_business_rejection_is_definite(self) -> None:
        error, _ = self._order(FakeResponse({"rt_cd": "1", "msg_cd": "APBK0013", "msg1": "주문가능금액 부족"}))
        self.assertIsInstance(error, KISError)
        self.assertNotIsInstance(error, KISOutcomeUnknown)

    def test_connect_timeout_is_definite_not_sent(self) -> None:
        error, _ = self._order(requests.ConnectTimeout("no route"))
        self.assertNotIsInstance(error, KISOutcomeUnknown)


if __name__ == "__main__":
    unittest.main()
