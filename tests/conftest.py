"""Shared test setup.

Normal use (needs a real MongoDB - Atlas or local):   pytest
Quick Python-only check without MongoDB (CRUD only):  TEST_USE_MOCK=1 pytest

The tests ALWAYS use their own database called "attendance_test_run" and wipe it,
so they can never touch the database you use for the real app.
"""
import importlib
import os

import pytest

USE_MOCK = os.getenv("TEST_USE_MOCK") == "1"
TEST_DB = "attendance_test_run"


def pytest_configure(config):
    config.addinivalue_line("markers", "real_mongo: needs a real MongoDB (aggregation stages, explain)")


def pytest_collection_modifyitems(config, items):
    if USE_MOCK:
        skip = pytest.mark.skip(reason="needs a real MongoDB (run without TEST_USE_MOCK)")
        for item in items:
            if "real_mongo" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def appmod():
    os.environ["MONGO_DB"] = TEST_DB  # set BEFORE the app reads it
    if USE_MOCK:
        import mongomock
        import pymongo

        pymongo.MongoClient = mongomock.MongoClient  # app.main does "from pymongo import MongoClient"
    import app.main as main

    main = importlib.reload(main)  # re-run the module so it builds a client with the settings above
    return main


@pytest.fixture(scope="session")
def client(appmod):
    from fastapi.testclient import TestClient

    with TestClient(appmod.app) as c:  # entering the context runs startup (index creation)
        yield c
    if not USE_MOCK:
        # Some Atlas users may not drop whole databases, so remove the two test collections instead.
        for name in ("employees", "attendance_logs"):
            try:
                appmod.db.drop_collection(name)
            except Exception:
                pass


@pytest.fixture()
def db(appmod, client):
    """A clean database for every test (indexes are kept)."""
    appmod.db.employees.delete_many({})
    appmod.db.attendance_logs.delete_many({})
    return appmod.db