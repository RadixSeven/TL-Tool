import sys

import pytest

from tlt.tlt import some_function


def test_some_function() -> None:
    result = some_function(5)
    assert result == 10


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
