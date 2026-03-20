"""
calendar.py

Google Calendar integration.

Modes:
  fixture_data=dict  — inline dict (unit tests)
  fixture_path=str   — JSON file on disk (integration test fixtures)
  creds=Credentials  — live Google Calendar API

Fixture data shape:
  {
    "calendars": {
      "email@example.com": {"busy": [{"start": "...", "end": "..."}]}
    },
    "events": [                          # optional; used by list_events
      {
        "id": "event-1",
        "summary": "Standup",
        "start": {"dateTime": "2026-03-19T14:00:00+00:00"},
        "end":   {"dateTime": "2026-03-19T15:00:00+00:00"},
        "attendees": [{"email": "me@example.com"}]
      }
    ]
  }
"""

import copy
import json
from datetime import datetime

from ea.network import call_with_retry


class CalendarClient:
    def __init__(
        self,
        fixture_path: str = None,
        fixture_data: dict = None,
        creds=None,
    ):
        self.fixture_path = fixture_path
        self.fixture_data = fixture_data
        self.events_created: list[dict] = []
        self.events_deleted: list[str]  = []   # event IDs
        self.events_updated: list[dict] = []   # {"id": ..., "start": ..., "end": ...}

        # Mutable copy of seeded events so delete/update work in tests
        raw = fixture_data or {}
        if not raw and fixture_path:
            with open(fixture_path) as f:
                raw = json.load(f)
        self._fixture_events: list[dict] = copy.deepcopy(raw.get("events", []))

        self._service = None
        if creds is not None:
            from googleapiclient.discovery import build
            self._service = build("calendar", "v3", credentials=creds)

    # ------------------------------------------------------------------
    # Freebusy
    # ------------------------------------------------------------------

    def get_freebusy(self, time_min: str, time_max: str, attendees: list[str]) -> dict:
        """
        Return free/busy information for a list of attendees over a time window.

        Response shape (matches Google Calendar freebusy API):
          {
            "timeMin": "...", "timeMax": "...",
            "calendars": {
              "email@example.com": {"busy": [{"start": "...", "end": "..."}]}
            }
          }
        """
        if self.fixture_data is not None:
            return self.fixture_data
        if self.fixture_path:
            with open(self.fixture_path) as f:
                return json.load(f)
        if self._service:
            body = {
                "timeMin": time_min,
                "timeMax": time_max,
                "items": [{"id": a} for a in attendees],
            }
            return call_with_retry(
                lambda: self._service.freebusy().query(body=body).execute()
            )
        raise NotImplementedError(
            "No calendar source configured. Pass fixture_data, fixture_path, or creds."
        )

    # ------------------------------------------------------------------
    # Event creation
    # ------------------------------------------------------------------

    def create_event(
        self,
        topic: str,
        start: str,
        end: str,
        attendees: list[str],
        location: str = None,
        self_email: str = None,
    ) -> dict:
        """
        Create a calendar event and send invites to attendees.

        self_email: the organizer's own address. That attendee gets
        responseStatus='accepted' so the event is auto-confirmed on their
        calendar. When self_email is the only attendee, sendUpdates is set
        to 'none' (no invite email for a solo block).

        In fixture mode, records the event in self.events_created for assertions.
        In live mode, calls events.insert with sendUpdates='all'/'none'.
        """
        if self.fixture_data is not None or self.fixture_path:
            event = {
                "id": f"fixture-event-{len(self.events_created) + 1}",
                "status": "confirmed",
                "summary": topic,
                "start": start,
                "end": end,
                "attendees": attendees,
            }
            if location:
                event["location"] = location
            self.events_created.append(event)
            return event

        if self._service:
            def _attendee(email):
                entry = {"email": email}
                if self_email and email.lower() == self_email.lower():
                    entry["responseStatus"] = "accepted"
                return entry

            solo = self_email and [a.lower() for a in attendees] == [self_email.lower()]
            body: dict = {
                "summary": topic,
                "start": {"dateTime": start},
                "end":   {"dateTime": end},
                "attendees": [_attendee(a) for a in attendees],
            }
            if location:
                body["location"] = location
            event = call_with_retry(
                lambda: self._service.events().insert(
                    calendarId="primary",
                    body=body,
                    sendUpdates="none" if solo else "all",
                ).execute()
            )
            self.events_created.append(event)
            return event

        raise NotImplementedError(
            "No calendar source configured. Pass fixture_data, fixture_path, or creds."
        )

    # ------------------------------------------------------------------
    # Event listing
    # ------------------------------------------------------------------

    def list_events(self, time_min: str, time_max: str) -> list[dict]:
        """
        Return all events whose start time falls within [time_min, time_max).

        In fixture mode, filters self._fixture_events by start time.
        In live mode, calls events.list on the primary calendar.

        Each event dict matches the Google Calendar events resource shape:
          {"id": ..., "summary": ...,
           "start": {"dateTime": "..."}, "end": {"dateTime": "..."},
           "attendees": [{"email": "..."}]}
        """
        if self.fixture_data is not None or self.fixture_path:
            t_min = datetime.fromisoformat(time_min.replace("Z", "+00:00"))
            t_max = datetime.fromisoformat(time_max.replace("Z", "+00:00"))
            result = []
            for ev in self._fixture_events:
                raw = (ev.get("start") or {}).get("dateTime", "")
                if not raw:
                    continue
                try:
                    ev_start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if t_min <= ev_start < t_max:
                    result.append(ev)
            return result

        if self._service:
            resp = call_with_retry(
                lambda: self._service.events().list(
                    calendarId="primary",
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                ).execute()
            )
            return resp.get("items", [])

        raise NotImplementedError(
            "No calendar source configured. Pass fixture_data, fixture_path, or creds."
        )

    # ------------------------------------------------------------------
    # Event deletion
    # ------------------------------------------------------------------

    def delete_event(self, event_id: str, send_updates: bool = True) -> None:
        """
        Delete a calendar event.

        send_updates: when True (default), Google sends cancellation emails to
        attendees. Set False for solo blocks where no invite was sent.

        In fixture mode, removes the event from self._fixture_events and
        records the ID in self.events_deleted for test assertions.
        """
        if self.fixture_data is not None or self.fixture_path:
            self._fixture_events = [e for e in self._fixture_events if e["id"] != event_id]
            self.events_deleted.append(event_id)
            return

        if self._service:
            call_with_retry(
                lambda: self._service.events().delete(
                    calendarId="primary",
                    eventId=event_id,
                    sendUpdates="all" if send_updates else "none",
                ).execute()
            )
            self.events_deleted.append(event_id)
            return

        raise NotImplementedError(
            "No calendar source configured. Pass fixture_data, fixture_path, or creds."
        )

    # ------------------------------------------------------------------
    # Event update
    # ------------------------------------------------------------------

    def update_event(self, event_id: str, start: str, end: str) -> dict:
        """
        Update the start and end time of an existing event.

        In fixture mode, mutates the matching event in self._fixture_events
        and records the update in self.events_updated for test assertions.
        In live mode, calls events.patch.
        """
        if self.fixture_data is not None or self.fixture_path:
            for ev in self._fixture_events:
                if ev["id"] == event_id:
                    ev["start"] = {"dateTime": start}
                    ev["end"]   = {"dateTime": end}
                    self.events_updated.append({"id": event_id, "start": start, "end": end})
                    return ev
            raise ValueError(f"Event {event_id!r} not found in fixture")

        if self._service:
            patch = {
                "start": {"dateTime": start},
                "end":   {"dateTime": end},
            }
            event = call_with_retry(
                lambda: self._service.events().patch(
                    calendarId="primary",
                    eventId=event_id,
                    body=patch,
                    sendUpdates="all",
                ).execute()
            )
            self.events_updated.append({"id": event_id, "start": start, "end": end})
            return event

        raise NotImplementedError(
            "No calendar source configured. Pass fixture_data, fixture_path, or creds."
        )
