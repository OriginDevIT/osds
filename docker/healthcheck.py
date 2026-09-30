"""osds-app healthcheck: is gunicorn answering? Any response below 500 counts.

The app is healthy only once the entrypoint has migrated, granted and minted
the token, because gunicorn is the last thing it starts. The probe's Host
(127.0.0.1) resolves to the wizard before setup and to a 404 after; both mean
the process is up.
"""

import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8000/", timeout=4)
except urllib.error.HTTPError as exc:
    sys.exit(0 if exc.code < 500 else 1)
except Exception:
    sys.exit(1)
