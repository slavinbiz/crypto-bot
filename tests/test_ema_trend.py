import numpy as np
import pytest
from ema_trend import calc_ema


def test_calc_ema_basic():
    closes = np.array([10.0, 11.0, 12.0, 13.0, 14.0])
    # seed = mean(10,11,12) = 11.0; alpha = 2/(3+1) = 0.5
    # шаг1: 0.5*13 + 0.5*11.0 = 12.0
    # шаг2: 0.5*14 + 0.5*12.0 = 13.0
    result = calc_ema(closes, period=3)
    assert result == pytest.approx(13.0)


def test_calc_ema_insufficient_data_raises():
    closes = np.array([10.0, 11.0])
    with pytest.raises(ValueError):
        calc_ema(closes, period=3)
