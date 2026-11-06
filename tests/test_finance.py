from datetime import date
from decimal import Decimal as D

import pytest

from mfetos.finance import cagr, convert, number, post_issue_shares, proceeds_bridge, ratio


@pytest.mark.parametrize(
    "raw,expected", [("1,23,456.70", "123456.70"), ("(123.4)", "-123.4"), ("−12", "-12"), ("0", "0")]
)
def test_numbers(raw, expected):
    assert number(raw) == D(expected)


@pytest.mark.parametrize("raw", ["-", "nil", "N/A", "[●]", "nan", "infinity", ""])
def test_missing_not_zero(raw):
    assert number(raw) is None


def test_units():
    assert convert(D(1), "crore") == convert(D(100), "lakh") == convert(D(10), "million")


def test_ratios_and_cagr():
    assert ratio(None, D(1)) is None
    assert ratio(D(1), D(-1)) is None
    assert ratio(D(1), D(0)) is None
    assert cagr(D(100), D(121), date(2022, 3, 31), date(2024, 3, 31)) == D("0.1")
    assert cagr(D(-1), D(5), date(2022, 3, 31), date(2024, 3, 31)) is None
    assert cagr(D(100), D("146.41"), date(2020, 2, 29), date(2024, 2, 29)) == D("0.1")


def test_dilution_ofs_and_proceeds():
    assert post_issue_shares(D(100), D(10), fresh=D(0)) == 100  # pure OFS
    assert post_issue_shares(D(100), D(10), fresh=D(200)) == 120
    cash, debt = proceeds_bridge(D(20), D(50), D(100), D(10), D(30), D(15))
    assert (cash, debt) == (D(65), D(20))
    # Debt repayment changes neither net debt nor EV again.
    a = proceeds_bridge(D(20), D(50), D(100), D(10), D(0), D(0))
    b = proceeds_bridge(D(20), D(50), D(100), D(10), D(30), D(0))
    assert a[1] - a[0] == b[1] - b[0]
    with pytest.raises(ValueError):
        proceeds_bridge(D(0), D(10), D(1), D(0), D(11), D(0))
    with pytest.raises(ValueError):
        post_issue_shares(D(100), D(10))
