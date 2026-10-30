"""Optional read-only Google Calendar events adapter."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CalendarError(RuntimeError):
    """Safe calendar provider error, without response body or credentials."""


@dataclass(frozen=True)
class CalendarEvent:
    title: str
    starts_at: str


class CalendarProvider(Protocol):
    def events_between(self, start: datetime, end: datetime, timezone_name: str) -> list[CalendarEvent]: ...


def validate_events(payload: object, timezone_name: str) -> tuple[list[CalendarEvent], str | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
        raise CalendarError("Calendar returned an invalid response")
    tz=ZoneInfo(timezone_name);events=[]
    for item in payload.get("items", []):
        if not isinstance(item, dict):
            continue
        start=item.get("start")
        if not isinstance(start, dict):
            continue
        raw=start.get("dateTime")
        if isinstance(raw,str):
            try:
                parsed=datetime.fromisoformat(raw)
                if parsed.tzinfo is None:continue
                starts_at=parsed.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
            except ValueError:
                continue
        else:
            raw=start.get("date")
            if not isinstance(raw,str):continue
            try:starts_at=f"{datetime.fromisoformat(raw).date().isoformat()} (весь день)"
            except ValueError:continue
        title=item.get("summary")
        title=title.strip()[:200] if isinstance(title,str) else "Без названия"
        events.append(CalendarEvent(title or "Без названия",starts_at))
    next_page=payload.get("nextPageToken")
    if next_page is not None and not isinstance(next_page,str):
        raise CalendarError("Calendar returned an invalid page token")
    return events,next_page


class GoogleCalendarReadOnly:
    """Uses only Google Calendar events.list GET; no create/update/delete methods exist."""
    BASE="https://www.googleapis.com/calendar/v3/calendars"

    def __init__(self,access_token:str,calendar_id:str="primary",timeout:float=5.0,
                 transport:Callable[[str,dict[str,str],float],bytes]|None=None):
        if not access_token.strip():raise ValueError("Calendar access token is required")
        if not calendar_id.strip() or len(calendar_id)>256:raise ValueError("Calendar ID is invalid")
        if not 0<timeout<=30:raise ValueError("Calendar timeout must be between 0 and 30 seconds")
        self._token=access_token.strip();self.calendar_id=calendar_id.strip();self.timeout=timeout
        self.transport=transport or self._get

    @staticmethod
    def _get(url:str,headers:dict[str,str],timeout:float)->bytes:
        request=urllib.request.Request(url,headers=headers,method="GET")
        with urllib.request.urlopen(request,timeout=timeout) as response:
            return response.read(1_000_001)

    def events_between(self,start:datetime,end:datetime,timezone_name:str)->list[CalendarEvent]:
        if start.tzinfo is None or end.tzinfo is None or end<=start:
            raise ValueError("Calendar range must be increasing and timezone-aware")
        try:ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Calendar timezone must be an IANA name") from exc
        calendar_id=urllib.parse.quote(self.calendar_id,safe="")
        base=f"{self.BASE}/{calendar_id}/events"
        params={"timeMin":start.isoformat(),"timeMax":end.isoformat(),"timeZone":timezone_name,
                "singleEvents":"true","orderBy":"startTime","maxResults":"50",
                "fields":"items(id,summary,start,end),nextPageToken"}
        headers={"Authorization":f"Bearer {self._token}","Accept":"application/json"}
        events=[];page=0;token=None
        while True:
            page+=1
            query={**params}
            if token:query["pageToken"]=token
            url=base+"?"+urllib.parse.urlencode(query)
            try:raw=self.transport(url,headers,self.timeout)
            except (TimeoutError,urllib.error.URLError,OSError) as exc:
                status=getattr(exc,"code",None)
                suffix=f"HTTP {status}" if isinstance(status,int) else "network error"
                raise CalendarError(f"Google Calendar read failed ({suffix}); briefing will continue without calendar events") from None
            if len(raw)>1_000_000:raise CalendarError("Google Calendar response exceeded the size limit")
            try:payload=json.loads(raw)
            except (json.JSONDecodeError,UnicodeDecodeError):raise CalendarError("Google Calendar returned invalid JSON") from None
            batch,token=validate_events(payload,timezone_name);events.extend(batch)
            if not token:return events
            if page>=3:raise CalendarError("Google Calendar has more than 150 events in this range; narrow the time window")


def configure_calendar_provider(enabled:str,access_token:str,calendar_id:str="primary") -> CalendarProvider|None:
    """An explicit opt-in plus a manually supplied OAuth access token is required."""
    if enabled.strip().lower() not in {"1","true","yes"}:return None
    if not access_token.strip():raise ValueError("CALENDAR_ENABLED requires GOOGLE_CALENDAR_ACCESS_TOKEN")
    return GoogleCalendarReadOnly(access_token,calendar_id)
