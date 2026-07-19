"""Ensure arbitrage_betting_bot/ is on sys.path for all test modules."""
import os
import sys

BOT_DIR = os.path.join(os.path.dirname(__file__), "..", "arbitrage_betting_bot")
sys.path.insert(0, os.path.abspath(BOT_DIR))
