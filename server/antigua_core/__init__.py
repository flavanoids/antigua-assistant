"""Shared Antigua pipeline core.

Both antigua_server.py (on the primary) and antigua_fallback_server.py
(on the backup box) import from this package so skill logic is written once.
Backend differences (STT, LLM, TTS engines) are injected, not duplicated.
"""
