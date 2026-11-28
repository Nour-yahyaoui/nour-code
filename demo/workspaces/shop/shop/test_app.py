from decimal import Decimal

import pytest

from shop.app import Cart


def test_total_adds_up():
    cart = Cart()
    cart.add("a", "10.00")
    cart.add("b", "5.50")
    assert cart.total() == Decimal("15.50")


def test_total_multiplies_quantity():
    cart = Cart()
    cart.add("a", "2.50", quantity=4)
    assert cart.total() == Decimal("10.00")


def test_quantity_must_be_positive():
    cart = Cart()
    with pytest.raises(ValueError):
        cart.add("a", "1.00", quantity=0)
