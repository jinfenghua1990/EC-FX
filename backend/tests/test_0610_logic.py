from decimal import Decimal
from app.services.legacy_logic import BASE_PURCHASE_DISCOUNT, REBATE_FALLBACK, REBATE_TAX_FACTOR


def test_store_cost_rebase_matches_0610_formula():
    goods_cost_8_discount = Decimal('80')
    standard_untaxed = goods_cost_8_discount / BASE_PURCHASE_DISCOUNT
    assert standard_untaxed == Decimal('100')
    assert standard_untaxed * Decimal('0.52') == Decimal('52.00')
    assert standard_untaxed * Decimal('0.65') == Decimal('65.00')


def test_rebate_program_total_is_41_percent_times_106():
    basis = Decimal('100')
    for channel, rules in REBATE_FALLBACK.items():
        total = sum(rules.values(), Decimal('0'))
        assert total == Decimal('0.41'), channel
        assert basis * total * REBATE_TAX_FACTOR == Decimal('43.4600')
