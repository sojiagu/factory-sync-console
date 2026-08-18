# PyInstaller 常把残缺的 zstandard 打进包，urllib3 一读 __version__ 就崩。
# 启动时禁止导入，让 urllib3 走无 zstd 的普通 HTTP（局域网完全够用）。
import sys


class _BlockZstandard:
    def find_spec(self, fullname, path, target=None):
        if fullname == "zstandard" or fullname.startswith("zstandard."):
            raise ImportError("zstandard disabled in agent build")
        return None


sys.meta_path.insert(0, _BlockZstandard())
sys.modules.pop("zstandard", None)
