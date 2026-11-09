"""Metadata-only local events and optional best-effort LangSmith exports.

No arbitrary function inputs/outputs or exception messages enter this module.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import queue
import re
import threading
import time
import uuid
from contextlib import contextmanager

from .models import now

CURRENT_SPAN = contextvars.ContextVar("mfetos_span", default=None)
CURRENT_META = contextvars.ContextVar("mfetos_metadata", default={})
SAFE_COUNTS = {
    "documents",
    "pages",
    "facts",
    "evidence",
    "peers",
    "attempt",
    "bytes",
    "requests",
    "completed",
    "total",
}


class EventFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(record.mfetos_event, default=str)


class Observability:
    def __init__(self, settings, store, client=None):
        self.settings, self.store = settings, store
        self.enabled = settings.langsmith_tracing and bool(settings.langsmith_api_key.get_secret_value())
        self.client = client
        self.logger = logging.getLogger("mfetos." + str(store.root))
        self.logger.propagate = False
        self.logger.setLevel(settings.log_level)
        if not self.logger.handlers:
            directory = settings.log_dir or store.root / "logs"
            directory.mkdir(parents=True, exist_ok=True)
            handlers = [
                logging.StreamHandler(),
                logging.handlers.RotatingFileHandler(
                    directory / f"mfetos-{os.getpid()}.jsonl", maxBytes=5 * 1024 * 1024, backupCount=3
                ),
            ]
            for handler in handlers:
                handler.setFormatter(EventFormatter())
                self.logger.addHandler(handler)
        self.pending = queue.Queue(maxsize=512)
        self.export_failed = False
        import atexit

        atexit.register(self.flush)
        if self.enabled:
            if self.client is None:
                from langsmith import Client

                try:
                    self.client = Client(
                        api_key=settings.langsmith_api_key.get_secret_value(),
                        api_url=settings.langsmith_endpoint,
                        timeout_ms=2000,
                        auto_batch_tracing=False,
                        hide_inputs=True,
                        hide_outputs=True,
                    )
                except Exception as exc:
                    self.enabled = False
                    self.event(
                        "observability", "client_unavailable", level="WARNING", error_code=type(exc).__name__
                    )
            if self.enabled:
                self.thread = threading.Thread(target=self._export, daemon=True, name="mfetos-trace-export")
                self.thread.start()
        if settings.langsmith_tracing and not self.enabled:
            self.event(
                "observability", "tracing_unavailable", level="WARNING", error_code="credentials_missing"
            )

    def event(
        self,
        stage,
        event,
        *,
        correlation_id=None,
        run_id=None,
        attempt_id=None,
        level="INFO",
        duration_ms=None,
        error_code=None,
        counts=None,
    ):
        context = CURRENT_META.get()
        correlation_id = correlation_id or context.get("correlation_id")
        run_id = run_id or context.get("run_id")
        # Fail closed: only explicitly enumerated numeric counters and safe exception codes.
        counters = {
            k: v for k, v in (counts or {}).items() if k in SAFE_COUNTS and isinstance(v, (int, float))
        }
        code = error_code if error_code and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", error_code) else None
        payload = {
            "timestamp": now().isoformat(),
            "correlation_id": correlation_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "stage": stage,
            "event": event,
            "level": level,
            "duration_ms": duration_ms,
            "error_code": code,
            "counts": counters,
        }
        self.logger.log(getattr(logging, level), "event", extra={"mfetos_event": payload})
        try:
            self.store.record_event(payload)
        except Exception:
            # Logging availability must not alter an analysis outcome.
            pass

    def _submit(self, method, data):
        if self.enabled and not self.export_failed:
            try:
                self.pending.put_nowait((method, data))
            except queue.Full:
                self.event("observability", "export_queue_full", level="WARNING")

    def _export(self):
        while True:
            item = self.pending.get()
            try:
                if item is None:
                    return
                if self.export_failed:
                    continue
                method, payload = item
                getattr(self.client, method)(**payload)
                if (
                    method == "create_run"
                    and "parent_run_id" not in payload
                    and hasattr(self.client, "get_run_url")
                ):
                    try:
                        url = self.client.get_run_url(
                            run_id=payload["id"], project_name=self.settings.langsmith_project
                        )
                        self.store.set_trace_url(payload["id"], url)
                    except Exception:
                        pass
            except Exception as exc:
                self.export_failed = True
                self.event(
                    "observability", "export_unavailable", level="WARNING", error_code=type(exc).__name__
                )
            finally:
                self.pending.task_done()

    def flush(self, timeout=3):
        deadline = time.monotonic() + timeout
        while self.enabled and self.pending.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    @contextmanager
    def span(self, stage, *, correlation_id=None, run_id=None, counts=None, attempt=False):
        parent = CURRENT_SPAN.get()
        inherited = CURRENT_META.get()
        correlation_id = correlation_id or inherited.get("correlation_id")
        run_id = run_id or inherited.get("run_id")
        meta_token = CURRENT_META.set({"correlation_id": correlation_id, "run_id": run_id})
        sid = str(uuid.uuid4())
        root_id = parent[1] if parent else sid
        token = CURRENT_SPAN.set((sid, root_id))
        start = time.monotonic()
        metadata = {
            "correlation_id": correlation_id,
            "run_id": run_id,
            "stage": stage,
            **{k: v for k, v in (counts or {}).items() if k in SAFE_COUNTS and isinstance(v, (int, float))},
        }
        data = {
            "name": stage,
            "inputs": {},
            "id": sid,
            "run_type": "chain",
            "trace_id": root_id,
            "project_name": self.settings.langsmith_project,
            "start_time": now(),
            "extra": {"metadata": metadata},
        }
        if parent:
            data["parent_run_id"] = parent[0]
        if attempt:
            self.store.start_attempt(sid, correlation_id, run_id, sid if self.enabled else None)
        self._submit("create_run", data)
        self.event(
            stage, "started", correlation_id=correlation_id, run_id=run_id, attempt_id=sid, counts=counts
        )
        error = None
        paused = False
        try:
            yield sid
        except BaseException as exc:
            if type(exc).__name__ in {"GraphInterrupt", "GraphBubbleUp", "CancelledError"}:
                paused = True
            else:
                error = type(exc).__name__
            raise
        finally:
            if attempt and run_id:
                run = self.store.load_run(run_id)
                if run and run["status"] == "failed" and not error:
                    error = "AnalysisFailed"
                if run and run["status"] == "awaiting_decision":
                    paused = True
                if run and run["status"] in {"completed", "insufficient_evidence", "unsupported"}:
                    # Worker shutdown can race the final checkpoint after report persistence.
                    # Cancellation at that point does not turn a completed report into a failure.
                    paused = False
            elapsed = round((time.monotonic() - start) * 1000, 3)
            self.event(
                stage,
                "failed" if error else "interrupted" if paused else "finished",
                correlation_id=correlation_id,
                run_id=run_id,
                attempt_id=sid,
                duration_ms=elapsed,
                error_code=error,
                level="ERROR" if error else "INFO",
            )
            self._submit("update_run", {"run_id": sid, "end_time": now(), "outputs": {}, "error": error})
            if attempt:
                self.store.finish_attempt(sid, "failed" if error else "interrupted" if paused else "finished")
            CURRENT_SPAN.reset(token)
            CURRENT_META.reset(meta_token)
