import pytest

from mock_occ.app import reset_state


@pytest.fixture(autouse=True)
def fresh_mock_state():
    """Carts live in the mock's memory; give every test an empty backend."""
    reset_state()
