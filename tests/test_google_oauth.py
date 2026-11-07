import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

import app


class GoogleOAuthTest(unittest.TestCase):
    def test_connect_button_requests_a_streamlit_auth_redirect(self):
        auth_url = "https://accounts.google.com/o/oauth2/auth?client_id=test"
        flow = Mock()
        flow.authorization_url.return_value = (auth_url, "unused")
        context = Mock()

        with (
            patch.object(app, "create_flow_with_code_verifier", return_value=flow) as create_flow,
            patch.object(app, "get_script_run_ctx", return_value=context),
        ):
            view = AppTest.from_string("from app import render_reservation_form\nrender_reservation_form()")
            view.run()
            create_flow.assert_not_called()
            context.enqueue.assert_not_called()
            view.button[0].click().run()

        self.assertFalse(view.exception)
        context.enqueue.assert_called_once()
        message = context.enqueue.call_args.args[0]
        self.assertEqual(message.auth_redirect.url, auth_url)
        csrf, code_verifier = app.parse_oauth_state(flow.authorization_url.call_args.kwargs["state"])
        self.assertEqual(csrf, view.session_state["oauth_state"])
        self.assertEqual(code_verifier, view.session_state["oauth_code_verifier"])
        create_flow.assert_called_once_with(code_verifier)
