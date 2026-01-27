import sys
import pytest
from unittest.mock import MagicMock, AsyncMock

# 1. Mock motor.motor_asyncio BEFORE importing server
mock_motor = MagicMock()
mock_client = MagicMock()
mock_db = MagicMock()
mock_collection = AsyncMock()

# Setup the mock chain
mock_motor.AsyncIOMotorClient.return_value = mock_client
mock_client.__getitem__.return_value = mock_db
mock_db.__getitem__.return_value = mock_collection

# Patch sys.modules to intercept imports
sys.modules["motor"] = MagicMock()
sys.modules["motor.motor_asyncio"] = mock_motor

# 2. Mock other dependencies if needed (e.g., Google Gemini)
# sys.modules["google"] = MagicMock()  # This breaks firebase-admin which needs google.auth
sys.modules["google.generativeai"] = MagicMock()

# 3. Now import the app
from server import app

@pytest.fixture
def test_app():
    return app

@pytest.fixture
def mock_users_collection():
    return mock_collection
