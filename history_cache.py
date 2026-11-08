"""Bounded thread-safe incremental JSONL reader and downsample result cache."""
import json
import os
import threading
from collections import OrderedDict, deque
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024
MAX_SAMPLES = 11000

class HistoryCache:
    def __init__(self):
        self.lock = threading.RLock()
        self.signature = None
        self.offset = 0
        self.pending = b''
        self.anchor = b''
        self.samples = deque(maxlen=MAX_SAMPLES)
        self.results = {}

    def get(self, path, key, build):
        path = Path(path)
        with self.lock:
            try:
                stat = path.stat()
                signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            except FileNotFoundError:
                signature = None
            if signature != self.signature:
                if signature is None:
                    self.samples.clear(); self.pending = b''; self.offset = 0
                else:
                    # Append advances both size and offset. Same-size rewrites,
                    # truncation, and inode rotation force a fresh bounded read.
                    try:
                        stream = path.open('rb')
                    except FileNotFoundError:
                        # Rotation raced stat/open. Keep this immutable answer;
                        # the next request observes the replacement or absence.
                        return self.results[key] if key in self.results else build(list(self.samples))
                    with stream:
                        opened = os.fstat(stream.fileno())
                        signature = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                        stat = opened
                        append = (self.signature is not None and signature[:2] == self.signature[:2]
                                  and signature[2] > self.signature[2] and self.offset == self.signature[2])
                        if append and self.anchor:
                            stream.seek(self.offset-len(self.anchor))
                            append = stream.read(len(self.anchor)) == self.anchor
                        start = self.offset if append else max(0, stat.st_size - MAX_BYTES)
                        if stat.st_size - start > MAX_BYTES:
                            append = False; start = stat.st_size - MAX_BYTES
                        stream.seek(start)
                        if not append:
                            self.samples.clear(); self.pending = b''
                            if start: stream.readline(MAX_BYTES)  # omit a cut partial line
                        data = self.pending + stream.read(max(0,min(MAX_BYTES, stat.st_size - stream.tell())))
                        self.offset = stream.tell()
                        stream.seek(max(0,self.offset-512))
                        self.anchor = stream.read(min(512,self.offset))
                    lines = data.split(b'\n')
                    self.pending = lines.pop()
                    if len(self.pending) > 1024 * 1024:
                        self.pending = b''  # bound malformed unterminated records
                    for line in lines:
                        if len(line) > 65536: continue  # reject pathological records
                        try:
                            sample = json.loads(line)
                            if isinstance(sample, dict): self.samples.append(sample)
                        except (ValueError, UnicodeError):
                            pass
                self.signature = signature
                self.results.clear()
            if key not in self.results:
                self.results[key] = build(list(self.samples))
                if len(self.results) > 16:
                    self.results.pop(next(iter(self.results)))
            return self.results[key]

_lock = threading.Lock()
_caches = OrderedDict()

def cache_for(path):
    key = str(Path(path).resolve())
    with _lock:
        cache = _caches.setdefault(key, HistoryCache())
        _caches.move_to_end(key)
        while len(_caches) > 4: _caches.popitem(last=False)
        return cache
