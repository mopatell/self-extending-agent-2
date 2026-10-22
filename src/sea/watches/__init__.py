"""Watches: recurring website monitors with Telegram alerts.

A watch is compiled once (the ToolSmith builds a single `check_<id>(path)` tool that parses the
page and returns {"value", "summary", and optionally "matched"}), then polled cheaply on an
interval with no LLM calls on the hot path. See compile.py, poll.py and daemon.py.
"""
