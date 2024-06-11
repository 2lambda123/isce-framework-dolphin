import numpy as np

from dolphin import filtering


def test_filter_long_wavelegnth():
    # Check filtering with ramp phase
    y, x = np.ogrid[-3:3:512j, -3:3:512j]
    unw_ifg = np.pi * (x + y)
    corr = np.ones(unw_ifg.shape, dtype=np.float32)

    # Filtering
    filtered_ifg = filtering.filtering(unw_ifg, corr, dx=300)
    np.testing.assert_allclose(
        filtered_ifg[10:-10, 10:-10],
        np.zeros(filtered_ifg[10:-10, 10:-10].shape),
        atol=1.0,
    )
