"""Two calls of Discord's HTTP API: create a message in a channel, and edit it later. No gateway connection is needed."""
import json
import re
import secrets
import urllib.error
import urllib.request

API = 'https://discord.com/api/v10'
USER_AGENT = 'DiscordBot (https://github.com/PetzReframed/Reframed-Rehome-Bot, 1.0)'
MESSAGE_LIMIT = 2000
# Discord's error codes for a message no retry can edit: it was deleted, or another account wrote it.
UNKNOWN_MESSAGE, OTHER_AUTHOR = 10008, 50005


class DiscordError(Exception):
    """status: the HTTP status (0: no usable answer). retry_after: seconds to wait first, when Discord said so.
    code: Discord's own error code, 0 when it gave none."""
    def __init__(self, status, message, retry_after=None, code=0):
        self.status, self.retry_after, self.code = status, retry_after, code
        super().__init__(message)

    @property
    def message_lost(self):
        return self.code in (UNKNOWN_MESSAGE, OTHER_AUTHOR)


def multipart(payload: dict, files: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    """The request body and its Content-Type: payload_json, then files[n] in order."""
    boundary = 'rehome-bot-' + secrets.token_hex(16)
    payload = {**payload, 'attachments': [{'id': index, 'filename': name} for index, (name, _) in enumerate(files)]}
    parts = [b'--' + boundary.encode() + b'\r\nContent-Disposition: form-data; name="payload_json"\r\n'
             b'Content-Type: application/json\r\n\r\n' + json.dumps(payload).encode('utf-8') + b'\r\n']
    for index, (name, data) in enumerate(files):
        parts.append(b'--' + boundary.encode() + b'\r\nContent-Disposition: form-data; name="files[%d]"; filename="%s"\r\n'
                     b'Content-Type: application/octet-stream\r\n\r\n' % (index, name.encode('ascii')) + data + b'\r\n')
    parts.append(b'--' + boundary.encode() + b'--\r\n')
    return b''.join(parts), 'multipart/form-data; boundary=' + boundary


def _call(method, path, token, body, content_type, opener, timeout) -> str:
    """Sends one request and answers the ID of the message Discord returns."""
    request = urllib.request.Request(API + path, data=body, method=method, headers={
        'Authorization': 'Bot ' + token, 'Content-Type': content_type, 'User-Agent': USER_AGENT})
    try:
        with opener(request, timeout=timeout) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as error:
        message, code, retry_after = '', 0, None
        try:
            answer = json.loads(error.read(65536))
            message, code = str(answer.get('message', ''))[:200], answer.get('code', 0)
            if error.code == 429:
                retry_after = float(answer.get('retry_after', 5))
        except (ValueError, TypeError, OSError, AttributeError):
            pass
        if error.code == 429 and not (retry_after is not None and retry_after >= 0):
            retry_after = 5.0  # A rate limit without a usable wait, as a proxy in front of Discord answers it.
        raise DiscordError(error.code, f'Discord answered {error.code}' + (': ' + message if message else ''),
                           retry_after, code if isinstance(code, int) else 0) from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise DiscordError(0, 'Discord could not be reached: ' + type(error).__name__) from None
    message_id = str(answer.get('id', '')) if isinstance(answer, dict) else ''
    if not re.fullmatch(r'[0-9]{1,25}', message_id):
        raise DiscordError(0, 'Discord answered without a message ID')
    return message_id


def post_message(token: str, channel_id: str, content: str, files: list[tuple[str, bytes]], nonce: str, *,
                 opener=urllib.request.urlopen, timeout=60) -> str:
    """Posts the message and answers its ID. The text can mention nobody, whatever it contains.

    Discord remembers a nonce for a few minutes: a second post with the same one creates nothing and answers the
    first message. So a post whose answer was lost can be sent again without a duplicate, within that time.
    """
    body, content_type = multipart({'content': content[:MESSAGE_LIMIT], 'allowed_mentions': {'parse': []},
                                    'nonce': nonce, 'enforce_nonce': True}, files)
    return _call('POST', f'/channels/{channel_id}/messages', token, body, content_type, opener, timeout)


def edit_message(token: str, channel_id: str, message_id: str, content: str, *,
                 opener=urllib.request.urlopen, timeout=60) -> None:
    """Replaces the text of a message the bot posted. Its attached files stay: the request names none."""
    body = json.dumps({'content': content[:MESSAGE_LIMIT], 'allowed_mentions': {'parse': []}}).encode('utf-8')
    _call('PATCH', f'/channels/{channel_id}/messages/{message_id}', token, body, 'application/json', opener, timeout)
