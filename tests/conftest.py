"""Tests can run in a clean clone without loading a real bot credential."""
import os

os.environ.setdefault("MAX_BOT_TOKEN", "test-not-a-real-bot-token")
os.environ.setdefault("MAX_WEBHOOK_SECRET", "test-webhook-secret")
