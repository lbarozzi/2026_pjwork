import os
import threading
import time


class LocalPoller:
    def __init__(self, process, path, interval_seconds=10):
        self.path = path or os.environ.get("INCOMING_PATH", "incoming")
        self.backup_path = os.environ.get("BACKUP_PATH", "backup")
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread = None
        self._known_files = set()
        self.process = process

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return

        os.makedirs(self.path, exist_ok=True)
        self._known_files = self._list_regular_files()
        self._stop_event.clear()

        self._thread = threading.Thread(target=self._run, name="local-poller-thread", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self):
        while not self._stop_event.is_set():
            print(".",end="", flush=True)
            current_files = self._list_regular_files()
            new_files = current_files  # - self._known_files

            for filename in sorted(new_files):
                if filename.endswith(".bak"):
                    continue
                # Process the new file
                self.process(os.path.join(self.path, filename))
                self._rename_to_bak(filename)

            self._known_files = self._list_regular_files()
            self._stop_event.wait(self.interval_seconds)

    def _list_regular_files(self):
        try:
            names = os.listdir(self.path)
        except FileNotFoundError:
            #print("FileNOTFOUND")
            return set()

        files = set()
        for name in names:
            full_path = os.path.join(self.path, name)
            if os.path.isfile(full_path):
                files.add(name)
        return files

    def _rename_to_bak(self, filename):
        src = os.path.join(self.path, filename)
        if not os.path.isfile(src):
            return

        dst = filename + ".bak"
        dst = f"{self.backup_path}/{dst}"
        if os.path.exists(dst):
            suffix = int(time.time())
            dst = f"{dst}.{suffix}" 

        os.rename(src, dst)
