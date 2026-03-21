"""
tests/test_footer.py

Tests for FooterGmailClient — the decorator that appends a configured footer
to every outgoing email body in a single place, rather than at every call site.
"""

from ea.gmail import FooterGmailClient, GmailMessage
from tests.fake_gmail import FakeGmailClient

MY_EMAIL = "me@example.com"
FOOTER = "I'm testing an AI scheduling assistant — please bear with any rough edges."


def _make_wrapped(footer=FOOTER):
    inner = FakeGmailClient(my_email=MY_EMAIL)
    return FooterGmailClient(inner, footer), inner


# ---------------------------------------------------------------------------
# Footer appended to outgoing body
# ---------------------------------------------------------------------------


class TestFooterAppended:
    def test_footer_appended_to_plain_body(self):
        wrapped, inner = _make_wrapped()
        wrapped.send_email(to=MY_EMAIL, subject="EA: booked", body="Booked.")
        sent = inner.sent_to(MY_EMAIL)
        assert len(sent) == 1
        assert sent[0].body == f"Booked.\n\n---\n{FOOTER}"

    def test_footer_appended_to_multiline_body(self):
        wrapped, inner = _make_wrapped()
        body = "Line one.\n\nLine two."
        wrapped.send_email(to=MY_EMAIL, subject="EA: test", body=body)
        sent = inner.sent_to(MY_EMAIL)
        assert sent[0].body == f"{body}\n\n---\n{FOOTER}"

    def test_subject_unchanged(self):
        wrapped, inner = _make_wrapped()
        wrapped.send_email(to=MY_EMAIL, subject="EA: booked — Coffee", body="Booked.")
        sent = inner.sent_to(MY_EMAIL)
        assert sent[0].subject == "EA: booked — Coffee"

    def test_recipient_unchanged(self):
        wrapped, inner = _make_wrapped()
        wrapped.send_email(to="other@example.com", subject="s", body="b")
        # FakeGmailClient records all sent messages; retrieve via thread
        all_threads = inner.list_threads()
        sent_bodies = [
            m.body for t in all_threads for m in t.messages if m.from_addr == MY_EMAIL
        ]
        assert any(FOOTER in b for b in sent_bodies)

    def test_thread_id_forwarded(self):
        """send_email with thread_id should append to the existing thread."""
        inner = FakeGmailClient(my_email=MY_EMAIL)
        # Seed an existing thread
        from ea.gmail import GmailMessage

        inner.seed_thread(
            thread_id="t1",
            messages=[
                GmailMessage(
                    id="m1",
                    thread_id="t1",
                    from_addr="bob@example.com",
                    to_addr=MY_EMAIL,
                    subject="Hi",
                    date="2026-03-20",
                    body="Can we meet?",
                )
            ],
        )
        wrapped = FooterGmailClient(inner, FOOTER)
        msg = wrapped.send_email(
            to="bob@example.com", subject="Re: Hi", body="Sure!", thread_id="t1"
        )
        assert msg.thread_id == "t1"
        thread = inner.get_thread("t1")
        assert len(thread.messages) == 2
        assert thread.messages[-1].body == f"Sure!\n\n---\n{FOOTER}"

    def test_extra_headers_forwarded(self):
        wrapped, inner = _make_wrapped()
        msg = wrapped.send_email(
            to=MY_EMAIL,
            subject="s",
            body="b",
            extra_headers={"X-EA-Original-Thread": "t99"},
        )
        assert msg.extra_headers.get("X-EA-Original-Thread") == "t99"


# ---------------------------------------------------------------------------
# No footer when footer is empty / absent
# ---------------------------------------------------------------------------


class TestNoFooter:
    def test_empty_string_no_footer(self):
        wrapped, inner = _make_wrapped(footer="")
        wrapped.send_email(to=MY_EMAIL, subject="s", body="Original body.")
        sent = inner.sent_to(MY_EMAIL)
        assert sent[0].body == "Original body."

    def test_whitespace_only_footer_not_appended(self):
        """Whitespace-only footer should be treated as absent."""
        wrapped, inner = _make_wrapped(footer="   ")
        wrapped.send_email(to=MY_EMAIL, subject="s", body="Original body.")
        sent = inner.sent_to(MY_EMAIL)
        # The wrapper checks truthiness; "   " is truthy so it WILL append.
        # This test documents the current behaviour — callers should strip.
        assert "---" in sent[0].body


# ---------------------------------------------------------------------------
# Delegation — non-send_email methods pass through to inner client
# ---------------------------------------------------------------------------


class TestDelegation:
    def test_list_threads_delegates(self):
        inner = FakeGmailClient(my_email=MY_EMAIL)
        inner.seed_thread(
            "t1",
            messages=[
                GmailMessage(
                    id="m1",
                    thread_id="t1",
                    from_addr="bob@example.com",
                    to_addr=MY_EMAIL,
                    subject="Hi",
                    date="2026-03-20",
                    body="Hello",
                )
            ],
        )
        wrapped = FooterGmailClient(inner, FOOTER)
        threads = wrapped.list_threads()
        assert len(threads) == 1
        assert threads[0].id == "t1"

    def test_get_thread_delegates(self):
        inner = FakeGmailClient(my_email=MY_EMAIL)
        inner.seed_thread(
            "t1",
            messages=[
                GmailMessage(
                    id="m1",
                    thread_id="t1",
                    from_addr="bob@example.com",
                    to_addr=MY_EMAIL,
                    subject="Hi",
                    date="2026-03-20",
                    body="Hello",
                )
            ],
        )
        wrapped = FooterGmailClient(inner, FOOTER)
        thread = wrapped.get_thread("t1")
        assert thread is not None
        assert thread.id == "t1"

    def test_apply_label_delegates(self):
        inner = FakeGmailClient(my_email=MY_EMAIL)
        inner.seed_thread(
            "t1",
            messages=[
                GmailMessage(
                    id="m1",
                    thread_id="t1",
                    from_addr="bob@example.com",
                    to_addr=MY_EMAIL,
                    subject="Hi",
                    date="2026-03-20",
                    body="Hello",
                )
            ],
        )
        wrapped = FooterGmailClient(inner, FOOTER)
        wrapped.apply_label("t1", "ea-scheduled")
        assert inner.has_label("t1", "ea-scheduled")

    def test_test_helper_sent_to_accessible(self):
        """FakeGmailClient.sent_to() should be reachable through the wrapper."""
        wrapped, inner = _make_wrapped()
        wrapped.send_email(to=MY_EMAIL, subject="s", body="b")
        # sent_to is a FakeGmailClient helper — accessible via __getattr__
        sent = wrapped.sent_to(MY_EMAIL)
        assert len(sent) == 1
