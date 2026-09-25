#!/usr/bin/env python3
"""Verification fixture for the family-data API. Synthetic roster only."""

import json
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

HOST = "127.0.0.1"
PORT = 8787
PIN = "246801"
TOKEN = "fixture-session"
PACIFIC = ZoneInfo("America/Los_Angeles")


def pacific_today():
    return datetime.now(PACIFIC).date()


def school_days(start, count):
    days = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def mdy(value):
    return f"{value.month}/{value.day}/{value.year}"


def payload():
    today = pacific_today()
    window = school_days(today, 5)
    due = window[1] if len(window) > 1 else window[0]
    past = today - timedelta(days=21)
    scored = today - timedelta(days=14)
    now = datetime.now(PACIFIC).isoformat(timespec="seconds")
    algebra = [
        {
            "description": "Lab Worksheet",
            "due_date": mdy(due),
            "points_earned": None,
            "points_possible": 10,
            "aeries_missing": False,
        },
        {
            "description": "Chapter Quiz",
            "due_date": mdy(scored),
            "points_earned": 18,
            "points_possible": 20,
            "percentage": 90,
            "aeries_missing": False,
        },
    ]
    english = [
        {
            "description": "Reading Log",
            "due_date": mdy(past),
            "points_earned": None,
            "points_possible": 10,
            "aeries_missing": True,
        }
    ]
    science = [
        {
            "description": "Lab Safety",
            "due_date": mdy(scored),
            "points_earned": 10,
            "points_possible": 10,
            "percentage": 100,
            "aeries_missing": False,
        }
    ]

    def student(name, classes, groups):
        return {
            "name": name,
            "classes": classes,
            "assignments_by_class": groups,
        }

    return {
        "latest": {
            "last_updated": now,
            "summer_break": False,
            "school_session": {
                "active": True,
                "first_day": "2026-08-13",
                "last_day": "2027-05-28",
                "quarters": {
                    "q1_end": "2026-10-09",
                    "q2_end": "2026-12-18",
                    "q3_end": "2027-03-12",
                    "q4_end": "2027-05-28",
                },
            },
            "students": [
                student(
                    "Fixture Alpha",
                    [
                        {
                            "period": 1,
                            "course_name": "Algebra Fixture",
                            "teacher": "Teacher Example",
                            "mark": "A",
                            "percent": "94",
                        },
                        {
                            "period": 2,
                            "course_name": "English Fixture",
                            "teacher": "Teacher Example",
                            "mark": "B",
                            "percent": "86",
                        },
                    ],
                    [
                        {"class_name": "1-Algebra Fixture", "period": 1, "assignments": algebra},
                        {"class_name": "2-English Fixture", "period": 2, "assignments": english},
                    ],
                ),
                student(
                    "Fixture Beta",
                    [
                        {
                            "period": 1,
                            "course_name": "Science Fixture",
                            "teacher": "Teacher Example",
                            "mark": "A",
                            "percent": "91",
                        }
                    ],
                    [
                        {"class_name": "1-Science Fixture", "period": 1, "assignments": science},
                    ],
                ),
            ],
        }
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Path and status only. Never print the PIN or the roster.
        print(f"{self.command} {self.path}", flush=True)

    def _cors(self):
        origin = self.headers.get("Origin") or "*"
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, Authorization")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            self._send(200, json.dumps({"ok": True, "fixture": True}))
            return
        if self.path.split("?", 1)[0] == "/v1/grades/latest":
            auth = self.headers.get("Authorization") or ""
            if auth != f"Bearer {TOKEN}":
                self._send(401, json.dumps({"error": "Your session expired. Type your PIN again."}))
                return
            self._send(200, json.dumps(payload()))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/auth/unlock":
            self._send(404, json.dumps({"error": "not found"}))
            return
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            data = {}
        if str(data.get("pin") or "") != PIN:
            self._send(401, json.dumps({"error": "That PIN did not work."}))
            return
        self._send(200, json.dumps({"token": TOKEN}))


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"fixture api {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
