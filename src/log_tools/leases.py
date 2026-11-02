"""OS-held per-job leases. Released automatically if the owning process exits."""
import os


class Lease:
    def __init__(self, path):
        self.stream = path.open("a+b")
        self.locked = False

    def acquire(self):
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.locked = True
            return True
        except OSError:
            self.stream.close()
            return False

    def close(self):
        if not self.stream.closed:
            try:
                if self.locked:
                    self.stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
