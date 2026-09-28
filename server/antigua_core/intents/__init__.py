"""Intent parsers, one module per skill.

Each turns a transcript into a request for its skill (or None). Routing
order lives in antigua_core/classify.py, which also re-exports these names.
"""
