"""
tests/test_subject_trigger.py

Unit tests for _find_ea_trigger_in_messages covering:
  - EA: command in subject line (primary use case)
  - Reply/forward prefixes suppressing the subject trigger
  - Quoted body lines not re-firing a prior EA: command
  - Body takes priority over subject when both are present
  - Only messages from my_email are checked
"""

import pytest
from ea.gmail import GmailMessage
from ea.poll import _find_ea_trigger_in_messages

MY_EMAIL = "me@example.com"
OTHER   = "bob@example.com"


def _msg(subject, body, from_addr=MY_EMAIL, *, i=0):
    return GmailMessage(
        id=f"msg-{i}",
        thread_id="t1",
        from_addr=from_addr,
        to_addr=MY_EMAIL if from_addr != MY_EMAIL else OTHER,
        subject=subject,
        date="2026-03-20T14:00:00Z",
        body=body,
    )


# ---------------------------------------------------------------------------
# Subject-line trigger (happy path)
# ---------------------------------------------------------------------------

class TestSubjectTrigger:

    def test_command_in_subject_no_body(self):
        msgs = [_msg("EA: block Friday 3-4pm", "")]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) == "block Friday 3-4pm"

    def test_command_in_subject_with_unrelated_body(self):
        msgs = [_msg("EA: schedule coffee with bob@example.com Thursday 2pm",
                     "Just a note to myself.")]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) == \
            "schedule coffee with bob@example.com Thursday 2pm"

    def test_subject_case_insensitive(self):
        msgs = [_msg("ea: block Friday 3-4pm", "")]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) == "block Friday 3-4pm"

    def test_subject_with_leading_whitespace(self):
        # Subject header values can have leading spaces after unfolding
        msgs = [_msg("  EA: block Friday 3-4pm", "")]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) == "block Friday 3-4pm"


# ---------------------------------------------------------------------------
# Reply / forward prefixes must suppress the subject trigger
# ---------------------------------------------------------------------------

class TestReplyPrefixIgnored:

    @pytest.mark.parametrize("prefix", [
        "Re:",
        "re:",
        "RE:",
        "Fwd:",
        "FWD:",
        "Fw:",
        "FW:",
        "AW:",    # German reply
        "WG:",    # German forward
    ])
    def test_reply_prefix_not_triggered(self, prefix):
        subject = f"{prefix} EA: schedule coffee with bob@example.com Thursday 2pm"
        msgs = [_msg(subject, "")]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) is None

    def test_reply_prefix_with_body_command_still_triggers(self):
        """A reply whose body contains a new EA: command should still fire."""
        msgs = [_msg("Re: EA: old command", "EA: reschedule to Friday 2pm")]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) == "reschedule to Friday 2pm"


# ---------------------------------------------------------------------------
# Quoted body lines must not re-fire a prior command
# ---------------------------------------------------------------------------

class TestQuotedBodyIgnored:

    def test_only_quoted_ea_line_does_not_trigger(self):
        body = (
            "> EA: schedule coffee with bob@example.com Thursday 2pm\n"
            "\n"
            "Sounds good, see you then."
        )
        msgs = [_msg("Re: EA: schedule coffee", body)]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) is None

    def test_quoted_ea_ignored_fresh_command_fires(self):
        body = (
            "> EA: schedule coffee Thursday 2pm\n"
            "\n"
            "EA: reschedule to Friday 3pm"
        )
        msgs = [_msg("Re: EA: schedule coffee", body)]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) == "reschedule to Friday 3pm"

    def test_multiple_quoted_lines(self):
        body = (
            "> On Thu, Mar 19 at 2pm, me@example.com wrote:\n"
            "> EA: schedule coffee with bob@example.com Thursday 2pm\n"
            "> \n"
            "\n"
            "Never mind, cancel this."
        )
        msgs = [_msg("Re: EA: old", body)]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) is None


# ---------------------------------------------------------------------------
# Body takes priority over subject
# ---------------------------------------------------------------------------

class TestBodyPriority:

    def test_body_command_beats_subject_command(self):
        msgs = [_msg("EA: block Friday 3-4pm", "EA: reschedule standup to Monday 10am")]
        # Body wins
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) == \
            "reschedule standup to Monday 10am"


# ---------------------------------------------------------------------------
# Only my_email is checked
# ---------------------------------------------------------------------------

class TestOnlyMyEmail:

    def test_subject_command_from_other_ignored(self):
        msgs = [_msg("EA: schedule something", "", from_addr=OTHER)]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) is None

    def test_body_command_from_other_ignored(self):
        msgs = [_msg("Meeting request", "EA: schedule something", from_addr=OTHER)]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) is None

    def test_mixed_thread_only_mine_fires(self):
        msgs = [
            _msg("EA: hijack attempt", "", from_addr=OTHER, i=0),
            _msg("EA: block Friday 3-4pm", "", from_addr=MY_EMAIL, i=1),
        ]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) == "block Friday 3-4pm"


# ---------------------------------------------------------------------------
# No trigger cases
# ---------------------------------------------------------------------------

class TestNoTrigger:

    def test_empty_thread(self):
        assert _find_ea_trigger_in_messages([], MY_EMAIL) is None

    def test_no_ea_anywhere(self):
        msgs = [_msg("Can we meet?", "How about Thursday?", from_addr=OTHER)]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) is None

    def test_ea_in_middle_of_subject_not_matched(self):
        # EA: must be at the start of the subject
        msgs = [_msg("Please send EA: command", "")]
        assert _find_ea_trigger_in_messages(msgs, MY_EMAIL) is None
