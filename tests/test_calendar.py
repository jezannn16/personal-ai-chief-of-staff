import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from chief_of_staff.calendar import (
    CalendarError,
    GoogleCalendarReadOnly,
    configure_calendar_provider,
)


def test_calendar_is_disabled_without_explicit_opt_in_even_if_token_exists():
    assert configure_calendar_provider("false","synthetic-token") is None
    assert configure_calendar_provider("","synthetic-token") is None
    with pytest.raises(ValueError,match="requires"):
        configure_calendar_provider("true","")
    assert isinstance(configure_calendar_provider("yes","synthetic-token"),GoogleCalendarReadOnly)


def test_google_calendar_adapter_uses_bounded_read_only_events_list_and_paginates():
    calls=[]
    def transport(url,headers,timeout):
        calls.append((url,headers,timeout))
        query=parse_qs(urlsplit(url).query)
        assert query["fields"]==["items(id,summary,start,end),nextPageToken"]
        assert query["singleEvents"]==["true"] and query["maxResults"]==["50"]
        if "pageToken" not in query:
            return json.dumps({"items":[{"id":"e1","summary":"Planning","start":{"dateTime":"2026-10-30T09:00:00+03:00"}}],"nextPageToken":"page-two"}).encode()
        return json.dumps({"items":[{"id":"e2","start":{"date":"2026-10-30"}}]}).encode()
    adapter=GoogleCalendarReadOnly("synthetic-token",transport=transport)
    events=adapter.events_between(datetime(2026,10,30,tzinfo=UTC),datetime(2026,10,31,tzinfo=UTC),"Europe/Moscow")
    assert [(event.title,event.starts_at) for event in events]==[("Planning","2026-10-30 09:00 MSK"),("Без названия","2026-10-30 (весь день)")]
    assert len(calls)==2
    assert all(urlsplit(url).path.endswith("/calendars/primary/events") for url,_,_ in calls)
    assert all(headers=={"Authorization":"Bearer synthetic-token","Accept":"application/json"} for _,headers,_ in calls)
    assert all(timeout==5 for _,_,timeout in calls)


def test_calendar_provider_failures_are_safe_and_no_raw_token_in_error():
    provider=GoogleCalendarReadOnly("synthetic-private-token",transport=lambda *_:(_ for _ in ()).throw(TimeoutError("secret data")))
    with pytest.raises(CalendarError,match="without calendar events") as exc:
        provider.events_between(datetime(2026,10,30,tzinfo=UTC),datetime(2026,10,31,tzinfo=UTC),"UTC")
    assert "synthetic-private-token" not in str(exc.value) and "secret data" not in str(exc.value)
    malformed=GoogleCalendarReadOnly("token",transport=lambda *_:b"bad json")
    with pytest.raises(CalendarError,match="invalid JSON"):
        malformed.events_between(datetime(2026,10,30,tzinfo=UTC),datetime(2026,10,31,tzinfo=UTC),"UTC")
