"""Tests for the live order executor safety gates and delegation (mocked SDK)"""

from unittest.mock import MagicMock, patch

import pytest

from src.exchanges.polymarket.live_orders import (
    LIVE_OPT_IN_ENV,
    LIVE_OPT_IN_VALUE,
    LiveOrderExecutor,
    LiveTradingNotPermitted,
    live_trading_allowed,
)


def make_executor(**kwargs):
    defaults = dict(private_key="0x" + "ab" * 32, funder_address="0x1234567890abcdef")
    defaults.update(kwargs)
    return LiveOrderExecutor(**defaults)


def test_live_trading_allowed_requires_exact_opt_in(monkeypatch):
    monkeypatch.delenv(LIVE_OPT_IN_ENV, raising=False)
    assert live_trading_allowed() is False
    monkeypatch.setenv(LIVE_OPT_IN_ENV, "yes")
    assert live_trading_allowed() is False
    monkeypatch.setenv(LIVE_OPT_IN_ENV, LIVE_OPT_IN_VALUE)
    assert live_trading_allowed() is True


def test_place_order_refused_without_opt_in(monkeypatch):
    monkeypatch.delenv(LIVE_OPT_IN_ENV, raising=False)
    ex = make_executor()
    with pytest.raises(LiveTradingNotPermitted):
        ex.place_limit_order("123", "buy", 0.5, 10.0)
    with pytest.raises(LiveTradingNotPermitted):
        ex.cancel_order("oid")
    with pytest.raises(LiveTradingNotPermitted):
        ex.get_open_orders()


def test_place_order_validates_arguments(monkeypatch):
    monkeypatch.setenv(LIVE_OPT_IN_ENV, LIVE_OPT_IN_VALUE)
    ex = make_executor()
    with pytest.raises(ValueError):
        ex.place_limit_order("123", "hold", 0.5, 10.0)
    with pytest.raises(ValueError):
        ex.place_limit_order("123", "buy", 1.5, 10.0)
    with pytest.raises(ValueError):
        ex.place_limit_order("123", "buy", 0.5, 0.0)


def test_place_order_delegates_to_sdk(monkeypatch):
    monkeypatch.setenv(LIVE_OPT_IN_ENV, LIVE_OPT_IN_VALUE)
    ex = make_executor()

    fake_response = MagicMock(
        order_id="0xdeadbeef", status="live", success=True, error_msg=""
    )
    fake_client = MagicMock()
    fake_client.create_and_post_order.return_value = fake_response

    with patch.object(LiveOrderExecutor, "_ensure_client", return_value=fake_client):
        result = ex.place_limit_order("12345", "buy", 0.42, 10.0)

    assert result == {
        "order_id": "0xdeadbeef",
        "status": "live",
        "success": True,
        "error": "",
        "live_trading": True,
    }
    args = fake_client.create_and_post_order.call_args[0][0]
    assert args.token_id == "12345"
    assert args.price == 0.42
    assert args.size == 10.0
    assert args.side == "buy"


def test_cancel_order_delegates_to_sdk(monkeypatch):
    monkeypatch.setenv(LIVE_OPT_IN_ENV, LIVE_OPT_IN_VALUE)
    ex = make_executor()

    fake_client = MagicMock()
    fake_client.cancel_order.return_value = MagicMock(
        canceled=["0xabc"], not_canceled={}
    )
    with patch.object(LiveOrderExecutor, "_ensure_client", return_value=fake_client):
        result = ex.cancel_order("0xabc")

    assert result["canceled"] == ["0xabc"]
    assert result["live_trading"] is True
    fake_client.cancel_order.assert_called_once_with("0xabc")


def test_executor_requires_private_key():
    with pytest.raises(ValueError):
        LiveOrderExecutor(private_key="")
