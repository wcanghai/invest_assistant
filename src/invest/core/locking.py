"""跨进程文件锁：崩溃时由操作系统释放，不依赖删除锁文件。"""

import os
from contextlib import contextmanager
from pathlib import Path


class JobBusy(RuntimeError):
    """同一份数据已被另一个更新任务占用。"""


@contextmanager
def file_lock(path):
    # 对固定文件首字节加非阻塞排他锁；锁文件保留以避免删除重建竞态。
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise JobBusy(f"已有任务占用更新锁：{path}") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
