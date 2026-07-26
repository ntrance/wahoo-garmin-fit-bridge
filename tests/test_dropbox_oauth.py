from __future__ import annotations

from app.dropbox_oauth import extract_rclone_token


def test_extract_rclone_token():
    output = """
    Paste the following into your remote machine --->
    {"access_token":"abc","token_type":"bearer","expiry":"2026-06-16T12:00:00Z"}
    <---End paste
    """

    assert extract_rclone_token(output) == '{"access_token":"abc","token_type":"bearer","expiry":"2026-06-16T12:00:00Z"}'
