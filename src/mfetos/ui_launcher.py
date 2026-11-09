"""One supervised worker plus Streamlit. Reruns cannot spawn processes."""

import os
import subprocess
import sys
import time
from pathlib import Path


def launch(settings, port=8501):
    from .storage import Store

    Store(settings.data_dir)  # Reject legacy storage before launching either child.
    environment = {
        **os.environ,
        "MFETOS_DATA_DIR": str(settings.data_dir.resolve()),
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
    }
    worker = subprocess.Popen([sys.executable, "-m", "mfetos", "worker"], env=environment)
    ui = None
    try:
        ui = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(Path(__file__).with_name("ui_app.py")),
                "--server.address",
                "127.0.0.1",
                "--server.port",
                str(port),
                "--server.headless",
                "true",
                "--browser.gatherUsageStats",
                "false",
            ],
            env=environment,
        )
        while ui.poll() is None:
            if worker.poll() is not None:
                raise RuntimeError("The research worker exited; UI shutdown preserves queued jobs")
            time.sleep(0.25)
        if ui.returncode:
            raise RuntimeError("Streamlit exited; check the ui optional dependency and port")
    except KeyboardInterrupt:
        pass
    finally:
        for process in (ui, worker):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
