import io
import json
import urllib.error
import urllib.request
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


def test_builtin_transport_disables_redirects_before_calendar_credentials_can_be_forwarded(monkeypatch):
    built=[]
    class RedirectProbeOpener:
        def __init__(self,handlers):self.handlers=handlers
        def open(self,request,timeout):
            redirect_handlers=[h for h in self.handlers if isinstance(h,urllib.request.HTTPRedirectHandler)]
            assert len(redirect_handlers)==1
            handler=redirect_handlers[0]
            assert handler.redirect_request(request,None,302,"Found",{},"https://attacker.example/collect") is None
            raise urllib.error.HTTPError(request.full_url,302,"Found",{},io.BytesIO())
    def build_opener(*handler_classes):
        built.extend(handler_classes)
        return RedirectProbeOpener([klass() for klass in handler_classes])
    monkeypatch.setattr(urllib.request,"build_opener",build_opener)
    provider=GoogleCalendarReadOnly("synthetic-secret",calendar_id="primary")
    with pytest.raises(CalendarError,match="HTTP 302"):
        provider.events_between(datetime(2026,10,30,tzinfo=UTC),datetime(2026,10,31,tzinfo=UTC),"UTC")
    assert built and built[0].__name__=="_NoRedirect"


def test_calendar_rejects_oversized_page_token_and_unbounded_event_page():
    provider=GoogleCalendarReadOnly("token",transport=lambda *_:json.dumps({"items":[],"nextPageToken":"x"*4097}).encode())
    with pytest.raises(CalendarError,match="invalid page token"):
        provider.events_between(datetime(2026,10,30,tzinfo=UTC),datetime(2026,10,31,tzinfo=UTC),"UTC")
    too_many={"items":[{"summary":str(i),"start":{"date":"2026-10-30"}} for i in range(51)]}
    provider=GoogleCalendarReadOnly("token",transport=lambda *_:json.dumps(too_many).encode())
    with pytest.raises(CalendarError,match="more events than"):
        provider.events_between(datetime(2026,10,30,tzinfo=UTC),datetime(2026,10,31,tzinfo=UTC),"UTC")


def test_calendar_sanitizes_control_characters_and_rejects_invalid_transport_data():
    provider=GoogleCalendarReadOnly("token",transport=lambda *_:json.dumps({"items":[{"summary":"Plan\nning","start":{"date":"2026-10-30"}}]}).encode())
    events=provider.events_between(datetime(2026,10,30,tzinfo=UTC),datetime(2026,10,31,tzinfo=UTC),"UTC")
    assert events[0].title=="Planning"
    provider=GoogleCalendarReadOnly("token",transport=lambda *_:None)
    with pytest.raises(CalendarError,match="invalid response data"):
        provider.events_between(datetime(2026,10,30,tzinfo=UTC),datetime(2026,10,31,tzinfo=UTC),"UTC")


def test_calendar_rejects_repeated_page_tokens_and_header_whitespace():
    with pytest.raises(ValueError,match="single value"):
        GoogleCalendarReadOnly("synthetic\ntoken")
    provider=GoogleCalendarReadOnly("token",transport=lambda *_:json.dumps({"items":[],"nextPageToken":"same"}).encode())
    with pytest.raises(CalendarError,match="repeated a page token"):
        provider.events_between(datetime(2026,10,30,tzinfo=UTC),datetime(2026,10,31,tzinfo=UTC),"UTC")
