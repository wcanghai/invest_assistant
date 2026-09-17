"""本地文件发布辅助：容忍 Windows 扫描器造成的短暂共享冲突。"""

import time


def replace_with_retry(source, target, attempts=6):
    # 始终保留原子替换，只重试权限占用；持续失败时显式抛出异常。
    for attempt in range(attempts):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.02 * (2 ** attempt))
