import os
import time

if os.environ.get("FASTAPI_DIAG_ACTIVE") == "1":
    entry = (time.monotonic_ns(), time.process_time_ns())
    from diag_runtime import activate

    activate(entry)
