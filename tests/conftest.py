import os

os.environ["DOCUFLOW_DSN"] = "postgresql://docuflow:docuflow@localhost:5434/docuflow_test"

import pytest

from docuflow import db


@pytest.fixture(autouse=True)
def clean():
    db.init_schema()
    db.reset()
    yield
