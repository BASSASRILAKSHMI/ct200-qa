import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.parsing.parser import parse_pdf  # noqa: E402

V1 = os.path.join(ROOT, "data", "ct200_manual.pdf")
V2 = os.path.join(ROOT, "data", "ct200_manual_v2.pdf")


@pytest.fixture(scope="session")
def v1():
    return parse_pdf(V1)


@pytest.fixture(scope="session")
def v2():
    return parse_pdf(V2)
