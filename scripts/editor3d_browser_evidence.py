"""Redact ephemeral loopback launch credentials from published browser logs."""
import ipaddress
import re
import sys
from urllib.parse import unquote_plus, urlsplit, urlunsplit

_BEARER = re.compile(r'''(authorization["']?\s*:\s*["']?bearer\s+)[^\s"'<>]+''', re.I)
_URL = re.compile(r'''https?://[^\s<>"')]+''', re.I)
_CREDENTIAL_KEYS = {'token', 'access_token', 'auth_token', 'authorization', 'bearer', 'api_key', 'apikey'}


def _redact_loopback_url(match):
    value = match.group(0)
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if host != 'localhost':
            if not host or not ipaddress.ip_address(host).is_loopback:
                return value
    except ValueError:
        return value
    query_parts = re.split('([&;])', parts.query)
    for index in range(0, len(query_parts), 2):
        item = query_parts[index]
        if not item:
            continue
        key, separator, _ = item.partition('=')
        if not separator:
            query_parts[index] = '[REDACTED]'
        elif unquote_plus(key).casefold() in _CREDENTIAL_KEYS:
            query_parts[index] = key + '=[REDACTED]'
    return urlunsplit(parts._replace(query=''.join(query_parts),
                                   fragment='[REDACTED]' if parts.fragment else ''))


def redact_browser_log(text):
    return _URL.sub(_redact_loopback_url, _BEARER.sub(lambda match: match.group(1)+'[REDACTED]', text))


def main():
    # Stream generated test logs instead of retaining an unbounded log in memory.
    for line in sys.stdin:
        sys.stdout.write(redact_browser_log(line))


if __name__ == '__main__':
    main()
