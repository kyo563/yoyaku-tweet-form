import unittest
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import app


class CalendarTest(unittest.TestCase):
    def test_reservation_form_shows_later_dates_in_a_collapsed_calendar(self):
        jst = timezone(timedelta(hours=9))
        now = datetime(2026, 12, 26, 0, 1, tzinfo=jst)
        scheduled_dates = {
            "last_primary_day": date(2027, 1, 3),
            "first_additional_day": date(2027, 1, 4),
            "last_additional_day": date(2027, 1, 26),
            "outside_calendar": date(2027, 1, 27),
        }
        videos = [
            app.Video(
                video_id=title,
                title=title,
                description="",
                publish_at_utc=datetime.combine(day, time(0, 5), jst).astimezone(timezone.utc),
            )
            for title, day in scheduled_dates.items()
        ]

        with (
            patch.object(app, "datetime", wraps=datetime) as app_datetime,
            patch.object(app, "handle_oauth_callback"),
            patch.object(app, "fetch_scheduled_videos", return_value=videos),
            patch.object(app, "fetch_japanese_holidays", return_value={}),
        ):
            app_datetime.now.side_effect = now.astimezone
            view = AppTest.from_string("from app import render_reservation_form\nrender_reservation_form()")
            view.session_state["google_creds"] = object()
            view.session_state["templates_loaded"] = True
            view.run()
            refresh = next(button for button in view.button if "予約動画リストを取得" in button.label)
            refresh.click().run()

        self.assertFalse(view.exception)
        expanders = [panel for panel in view.expander if panel.label == "さらに表示"]
        self.assertEqual(len(expanders), 1)
        expander = expanders[0]
        self.assertFalse(expander.proto.expanded)
        self.assertIn("2027/01/04〜2027/01/26", expander.caption[0].value)

        primary, additional = [item.value for item in view.markdown if "<table " in item.value]
        self.assertEqual(additional, expander.markdown[0].value)
        self.assertEqual(primary.count("<td "), 14)
        self.assertIn("last_primary_day", primary)
        self.assertNotIn("last_primary_day", additional)
        for title in ("first_additional_day", "last_additional_day"):
            self.assertNotIn(title, primary)
            self.assertIn(title, additional)
        self.assertNotIn("outside_calendar", primary + additional)
