import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class DebouncedHandler(FileSystemEventHandler):
    def __init__(self, callback, debounce_seconds=30):
        self.callback = callback
        self.debounce_seconds = debounce_seconds
        self._timer = None
        self._lock = threading.Lock()

    def _trigger(self):
        with self._lock:
            self._timer = None
        self.callback()

    def on_any_event(self, event):
        if event.is_directory:
            return
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._trigger)
            self._timer.daemon = True
            self._timer.start()


class FileWatcher:
    def __init__(self, paths, on_change, debounce_seconds=30):
        self.paths = paths
        self.on_change = on_change
        self.debounce_seconds = debounce_seconds
        self.observer = None

    def start(self):
        if self.observer is not None:
            return

        self.observer = Observer()
        handler = DebouncedHandler(self.on_change, self.debounce_seconds)

        for path in self.paths:
            self.observer.schedule(handler, path, recursive=True)

        self.observer.daemon = True
        self.observer.start()

    def stop(self):
        if self.observer is None:
            return
        self.observer.stop()
        self.observer.join(timeout=5)
        self.observer = None

