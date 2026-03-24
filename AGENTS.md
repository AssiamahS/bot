# Agent Roles

Bot 1 (Frontend): DOES the work on dashboard.html, command_center.html. Never delegates UI tasks.

Bot 2 (Backend): trader.py, risk_controller, spread_scanner only.

Bot 3 (QA): CodeHawk scans only. Never edits code directly.

Bot 4 (Data Bridge): data_bridge.py, port 8084 only.

Bot 5 (PM): Coordinates all bots. Reviews STATUS.md, unblocks work, sets priorities. Never edits production code directly.
