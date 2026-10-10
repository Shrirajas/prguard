import pytest

from app import payments


@pytest.fixture(autouse=True)
def _clean_ledger():
    payments._BALANCES.clear()


def test_charge_accumulates():
    payments.charge_customer("c1", 500)
    assert payments.charge_customer("c1", 250) == 750


def test_charge_rejects_non_positive_amount():
    with pytest.raises(payments.PaymentError):
        payments.charge_customer("c1", 0)


def test_refund_returns_everything():
    payments.charge_customer("c1", 500)
    assert payments.refund("c1") == 500
    assert payments.refund("c1") == 0


def test_refund_partial_reduces_balance():
    payments.charge_customer("c1", 500)
    assert payments.refund_partial("c1", 200) == 300


def test_refund_partial_rejects_more_than_charged():
    payments.charge_customer("c1", 500)
    with pytest.raises(payments.PaymentError):
        payments.refund_partial("c1", 900)
