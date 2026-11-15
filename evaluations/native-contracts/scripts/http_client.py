"""Small HTTP boundary shared by the host controller and isolated worker."""

import json
import time
import urllib.error
import urllib.request


def http(url, body=None, method=None, headers=None, timeout=60):
    req = urllib.request.Request(
        url,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
        data=None if body is None else json.dumps(body).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait(fn, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = fn()
        if result:
            return result
        time.sleep(0.1)
    raise TimeoutError("contract observation deadline")
