# -*- coding: utf-8 -*-
"""Win7 可用的纯 Python NTFS $MFT 扫描（ctypes，无外部 exe）。

先用 FSCTL_GET_NTFS_VOLUME_DATA；失败则读引导扇区定位 $MFT。
再按 $MFT 的 data run 分片读取并解析 FILE 记录，拼出完整路径。
"""
from __future__ import print_function

import ctypes
import struct
from ctypes import wintypes

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_BEGIN = 0
FSCTL_GET_NTFS_VOLUME_DATA = 0x00090064
FSCTL_GET_NTFS_FILE_RECORD = 0x00090068
ATTR_STANDARD_INFO = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_END = 0xFFFFFFFF
FILE_NAME_POSIX = 0
FILE_NAME_WIN32 = 1
FILE_NAME_DOS = 2
FILE_NAME_WIN32_DOS = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
CHUNK = 4 * 1024 * 1024

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.SetFilePointerEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int64,
    ctypes.POINTER(ctypes.c_int64),
    wintypes.DWORD,
]
kernel32.SetFilePointerEx.restype = wintypes.BOOL
kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
kernel32.DeviceIoControl.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class ScanCancelled(Exception):
    pass


def normalize_drive(s):
    s = (s or "").strip().upper().rstrip("\\/")
    if len(s) == 1 and s.isalpha():
        return s + ":"
    if len(s) == 2 and s[1] == ":" and s[0].isalpha():
        return s
    return ""


def _u16(buf, off):
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def _u64(buf, off):
    return struct.unpack_from("<Q", buf, off)[0]


def _i64(buf, off):
    return struct.unpack_from("<q", buf, off)[0]


def read_le_uint(b):
    n = 0
    for i, x in enumerate(bytearray(b)):
        n |= x << (8 * i)
    return n


def read_le_signed(b):
    if not b:
        return 0
    bits = len(b) * 8
    n = read_le_uint(b)
    if n & (1 << (bits - 1)):
        n -= 1 << bits
    return n


def file_ref_rec(ref):
    return ref & 0x0000FFFFFFFFFFFF


def utf16z(b):
    if len(b) < 2:
        return ""
    try:
        return b.decode("utf-16-le").rstrip("\x00")
    except Exception:
        return ""


def prefer_ns(ns, old):
    score = {FILE_NAME_WIN32: 3, FILE_NAME_WIN32_DOS: 3, FILE_NAME_POSIX: 2, FILE_NAME_DOS: 1}
    return score.get(ns, 0) > score.get(old, 0)


def apply_usa(rec, sector):
    if len(rec) < 8 or sector <= 0:
        return
    usa_off = _u16(rec, 4)
    usa_count = _u16(rec, 6)
    if usa_off < 0 or usa_count < 2:
        return
    need = usa_off + usa_count * 2
    if need > len(rec):
        return
    for i in range(1, usa_count):
        pos = i * sector - 2
        if pos < 0 or pos + 2 > len(rec):
            return
        src = usa_off + i * 2
        rec[pos:pos + 2] = rec[src:src + 2]


def parse_runs(data, start_vcn=0):
    runs = []
    lcn = 0
    vcn = start_vcn
    i = 0
    data = bytearray(data)
    while i < len(data):
        h = data[i]
        if h == 0:
            break
        i += 1
        len_size = h & 0x0F
        off_size = h >> 4
        if len_size <= 0 or i + len_size + off_size > len(data):
            break
        length = read_le_uint(data[i:i + len_size])
        i += len_size
        if off_size > 0:
            delta = read_le_signed(data[i:i + off_size])
            i += off_size
            lcn += delta
            runs.append({"start_vcn": vcn, "length": length, "lcn": lcn})
        else:
            runs.append({"start_vcn": vcn, "length": length, "lcn": -1})
        vcn += length
    return runs


def parse_boot_info(buf):
    if len(buf) < 0x48:
        raise RuntimeError("引导扇区过短")
    oem = bytes(buf[3:11])
    if oem not in (b"NTFS    ", b"NTFS"):
        if oem[:4] != b"NTFS":
            raise RuntimeError("不是 NTFS 卷（OEM=%r）" % oem)
    bytes_per_sector = _u16(buf, 0x0B)
    sectors_per_cluster = buf[0x0D]
    if bytes_per_sector == 0 or sectors_per_cluster == 0:
        raise RuntimeError("引导扇区参数无效")
    cluster_size = bytes_per_sector * sectors_per_cluster
    mft_lcn = _u64(buf, 0x30)
    cpr = struct.unpack_from("<b", buf, 0x40)[0]
    if cpr < 0:
        bytes_record = 1 << (-cpr)
    elif cpr > 0:
        bytes_record = cpr * cluster_size
    else:
        bytes_record = 1024
    return {
        "bytes_sector": bytes_per_sector,
        "bytes_cluster": cluster_size,
        "bytes_record": bytes_record,
        "mft_lcn": mft_lcn,
        "mft_valid": 0,
    }


def parse_record(raw):
    meta = {"parent": 0, "name": "", "size": 0, "is_dir": False, "in_use": False}
    if len(raw) < 0x30 or bytes(raw[0:4]) != b"FILE":
        return meta
    flags = _u16(raw, 0x16)
    meta["in_use"] = bool(flags & 0x01)
    meta["is_dir"] = bool(flags & 0x02)
    if file_ref_rec(_u64(raw, 0x20)) != 0:
        return meta
    if not meta["in_use"]:
        return meta
    off = _u16(raw, 0x14)
    best_ns = -1
    while off + 16 <= len(raw):
        atype = _u32(raw, off)
        if atype == ATTR_END:
            break
        alen = _u32(raw, off + 4)
        if alen < 16 or off + alen > len(raw):
            break
        nonres = raw[off + 8]
        name_len = raw[off + 9]
        if atype == ATTR_FILE_NAME and nonres == 0:
            csize = _u32(raw, off + 0x10)
            coff = _u16(raw, off + 0x14)
            body = off + coff
            if coff >= 0x18 and body + 0x42 <= off + alen and csize >= 0x42:
                ns = raw[body + 0x41]
                nlen = raw[body + 0x40]
                name_bytes = body + 0x42
                if nlen > 0 and name_bytes + nlen * 2 <= off + alen and (best_ns < 0 or prefer_ns(ns, best_ns)):
                    meta["parent"] = file_ref_rec(_u64(raw, body))
                    meta["name"] = utf16z(bytes(raw[name_bytes:name_bytes + nlen * 2]))
                    if meta["size"] == 0:
                        meta["size"] = _i64(raw, body + 0x30)
                    best_ns = ns
        if atype == ATTR_DATA and name_len == 0:
            if nonres == 0:
                meta["size"] = _u32(raw, off + 0x10)
            elif off + 0x38 <= off + alen:
                meta["size"] = _i64(raw, off + 0x30)
        off += alen
    return meta


def parse_unnamed_data_runs(raw):
    if len(raw) < 0x30 or bytes(raw[0:4]) != b"FILE":
        return []
    off = _u16(raw, 0x14)
    all_runs = []
    while off + 16 <= len(raw):
        atype = _u32(raw, off)
        if atype == ATTR_END:
            break
        alen = _u32(raw, off + 4)
        if alen < 16 or off + alen > len(raw):
            break
        nonres = raw[off + 8]
        name_len = raw[off + 9]
        if atype == ATTR_DATA and name_len == 0 and nonres != 0 and off + 0x40 <= off + alen:
            start_vcn = _u64(raw, off + 0x10)
            run_off = _u16(raw, off + 0x20)
            if run_off > 0 and off + run_off < off + alen:
                all_runs.extend(parse_runs(raw[off + run_off:off + alen], start_vcn))
        off += alen
    return all_runs


def parse_data_real_size(raw):
    if len(raw) < 0x30 or bytes(raw[0:4]) != b"FILE":
        return 0
    off = _u16(raw, 0x14)
    while off + 16 <= len(raw):
        atype = _u32(raw, off)
        if atype == ATTR_END:
            break
        alen = _u32(raw, off + 4)
        if alen < 16 or off + alen > len(raw):
            break
        nonres = raw[off + 8]
        name_len = raw[off + 9]
        if atype == ATTR_DATA and name_len == 0:
            if nonres == 0:
                return _u32(raw, off + 0x10)
            if off + 0x38 <= off + alen:
                return _u64(raw, off + 0x30)
        off += alen
    return 0


def parse_attr_list_extra_records(raw):
    if len(raw) < 0x30 or bytes(raw[0:4]) != b"FILE":
        return []
    off = _u16(raw, 0x14)
    recs = []
    while off + 16 <= len(raw):
        atype = _u32(raw, off)
        if atype == ATTR_END:
            break
        alen = _u32(raw, off + 4)
        if alen < 16 or off + alen > len(raw):
            break
        nonres = raw[off + 8]
        if atype == ATTR_ATTRIBUTE_LIST and nonres == 0:
            csize = _u32(raw, off + 0x10)
            coff = _u16(raw, off + 0x14)
            body = off + coff
            end = min(body + csize, off + alen)
            p = body
            while p + 0x1A <= end:
                entry_len = _u16(raw, p + 4)
                if entry_len < 0x1A or p + entry_len > end:
                    break
                if _u32(raw, p) == ATTR_DATA:
                    recs.append(file_ref_rec(_u64(raw, p + 0x10)))
                p += entry_len
        off += alen
    return recs


def merge_runs(dst, extra):
    seen = {}
    for r in list(dst) + list(extra):
        if r.get("length"):
            seen[r["start_vcn"]] = r
    return sorted(seen.values(), key=lambda x: x["start_vcn"])


def sane_size(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return 0
    if n < 0 or n > (1 << 48):
        return 0
    return n


def build_path(drive, rec, metas):
    if rec == 5:
        return drive + "\\"
    parts = []
    seen = set()
    cur = rec
    for _ in range(256):
        if cur in (5, 0):
            break
        if cur in seen:
            break
        seen.add(cur)
        m = metas.get(cur)
        if not m or not m.get("name") or m.get("name") == ".":
            break
        parts.append(m["name"])
        if m.get("parent") == cur:
            break
        cur = m.get("parent") or 0
    if not parts:
        return drive + "\\"
    parts.reverse()
    return drive + "\\" + "\\".join(parts)


class Volume(object):
    def __init__(self, handle):
        self.handle = handle
        self.bytes_cluster = 0
        self.bytes_record = 0
        self.bytes_sector = 512
        self.mft_valid = 0
        self.runs = []

    def close(self):
        if self.handle and self.handle != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def seek(self, off):
        new_ptr = ctypes.c_int64(0)
        if not kernel32.SetFilePointerEx(self.handle, ctypes.c_int64(off), ctypes.byref(new_ptr), FILE_BEGIN):
            raise OSError("Seek 0x%x 失败: %s" % (off, ctypes.get_last_error()))

    def read_at(self, off, n):
        self.seek(off)
        buf = (ctypes.c_char * n)()
        got = wintypes.DWORD(0)
        if not kernel32.ReadFile(self.handle, buf, n, ctypes.byref(got), None):
            raise OSError("读盘 0x%x 失败: %s" % (off, ctypes.get_last_error()))
        return buf.raw[:got.value]

    def ioctl_volume_data(self):
        out = (ctypes.c_char * 256)()
        ret = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            self.handle,
            FSCTL_GET_NTFS_VOLUME_DATA,
            None,
            0,
            out,
            256,
            ctypes.byref(ret),
            None,
        )
        if not ok or ret.value < 88:
            return None
        raw = out.raw
        return {
            "bytes_sector": struct.unpack_from("<I", raw, 40)[0],
            "bytes_cluster": struct.unpack_from("<I", raw, 44)[0],
            "bytes_record": struct.unpack_from("<I", raw, 48)[0],
            "mft_valid": struct.unpack_from("<Q", raw, 56)[0],
            "mft_lcn": struct.unpack_from("<Q", raw, 64)[0],
        }

    def read_boot_info(self):
        raw = self.read_at(0, 512)
        if len(raw) < 512:
            raise RuntimeError("读取卷引导扇区失败。")
        return parse_boot_info(raw)

    def read_record_ioctl(self, idx):
        inn = ctypes.c_uint64(idx)
        cap = 16 + int(self.bytes_record) + int(self.bytes_sector)
        out = (ctypes.c_char * cap)()
        ret = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            self.handle,
            FSCTL_GET_NTFS_FILE_RECORD,
            ctypes.byref(inn),
            8,
            out,
            cap,
            ctypes.byref(ret),
            None,
        )
        if not ok or ret.value < 12:
            raise OSError("读记录 %s 失败" % idx)
        raw_out = bytearray(out.raw[:ret.value])
        rec_len = _u32(raw_out, 8)
        end = 12 + int(rec_len)
        if rec_len < 48 or end > len(raw_out):
            rec_len = int(self.bytes_record)
            end = 12 + rec_len
        raw = bytearray(raw_out[12:end])
        if len(raw) < self.bytes_record and 12 + int(self.bytes_record) <= len(raw_out):
            raw = bytearray(raw_out[12:12 + int(self.bytes_record)])
        apply_usa(raw, int(self.bytes_sector))
        return raw

    def mft_disk_offset(self, mft_off):
        if not self.bytes_cluster:
            return None
        vcn = mft_off // self.bytes_cluster
        intra = mft_off % self.bytes_cluster
        for r in self.runs:
            if r["start_vcn"] <= vcn < r["start_vcn"] + r["length"]:
                if r["lcn"] < 0:
                    return None
                return r["lcn"] * self.bytes_cluster + (vcn - r["start_vcn"]) * self.bytes_cluster + intra
        return None

    def read_record(self, idx):
        mft_off = idx * self.bytes_record
        disk_off = self.mft_disk_offset(mft_off)
        if disk_off is not None:
            try:
                raw = bytearray(self.read_at(disk_off, int(self.bytes_record)))
                if len(raw) >= self.bytes_record:
                    apply_usa(raw, int(self.bytes_sector))
                    return raw
            except OSError:
                pass
        return self.read_record_ioctl(idx)

    def ingest_records(self, buf, first_idx, metas):
        rs = int(self.bytes_record)
        if rs <= 0:
            return
        n = len(buf) // rs
        for i in range(n):
            raw = bytearray(buf[i * rs:(i + 1) * rs])
            apply_usa(raw, int(self.bytes_sector))
            m = parse_record(raw)
            if not m["in_use"] or not m["name"]:
                continue
            if m["is_dir"]:
                m["size"] = 0
            else:
                m["size"] = sane_size(m["size"])
            metas[first_idx + i] = m

    def load_mft_runs(self, info):
        first_off = info["mft_lcn"] * self.bytes_cluster
        first = bytearray(self.read_at(first_off, int(self.bytes_record)))
        if len(first) < self.bytes_record:
            raise RuntimeError("读 $MFT 首记录失败")
        apply_usa(first, int(self.bytes_sector))
        self.runs = parse_unnamed_data_runs(first)
        extras = parse_attr_list_extra_records(first)
        real_size = parse_data_real_size(first)
        if real_size and not self.mft_valid:
            self.mft_valid = real_size
        if not self.runs:
            nclus = (self.mft_valid + self.bytes_cluster - 1) // self.bytes_cluster if self.bytes_cluster else 16
            if nclus == 0:
                nclus = 16
            self.runs = [{"start_vcn": 0, "length": nclus, "lcn": int(info["mft_lcn"])}]
        for rec in extras:
            if not rec:
                continue
            try:
                raw = self.read_record_ioctl(rec)
            except OSError:
                try:
                    raw = self.read_record(rec)
                except OSError:
                    continue
            self.runs = merge_runs(self.runs, parse_unnamed_data_runs(raw))
        if not self.runs:
            raise RuntimeError("$MFT runlist 为空")


def open_volume(drive):
    path = "\\\\.\\" + drive
    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or int(handle) == -1:
        err = ctypes.get_last_error()
        if err == 5:
            raise PermissionError("需要管理员权限才能读取 MFT")
        raise OSError("打开 %s 失败: %s" % (drive, err))
    return handle


def scan_volume(drive, cancel_check=None):
    drive = normalize_drive(drive)
    if not drive:
        raise ValueError("盘符格式无效，应为 X:")
    handle = open_volume(drive)
    vol = Volume(handle)
    try:
        info = vol.ioctl_volume_data()
        if not info:
            info = vol.read_boot_info()
        if not info.get("bytes_cluster") or not info.get("bytes_record"):
            raise RuntimeError("不是 NTFS 或卷信息无效")
        vol.bytes_cluster = int(info["bytes_cluster"])
        vol.bytes_record = int(info["bytes_record"])
        vol.bytes_sector = int(info.get("bytes_sector") or 512)
        if vol.bytes_sector == 0:
            vol.bytes_sector = 512
        vol.mft_valid = int(info.get("mft_valid") or 0)
        vol.load_mft_runs(info)
        metas = {}
        for r in vol.runs:
            if r["lcn"] < 0 or not r["length"]:
                continue
            disk_off = r["lcn"] * vol.bytes_cluster
            nbyte = r["length"] * vol.bytes_cluster
            got = 0
            while got < nbyte:
                if cancel_check and cancel_check():
                    raise ScanCancelled()
                want = CHUNK if nbyte - got > CHUNK else int(nbyte - got)
                try:
                    buf = vol.read_at(disk_off + got, want)
                except OSError:
                    break
                if not buf:
                    break
                first_idx = (r["start_vcn"] * vol.bytes_cluster + got) // vol.bytes_record
                vol.ingest_records(buf, first_idx, metas)
                got += len(buf)
        out = []
        for rec, m in metas.items():
            if rec == 5:
                continue
            out.append({
                "path": build_path(drive, rec, metas),
                "name": m["name"],
                "size": m["size"],
                "is_dir": m["is_dir"],
            })
        return out
    finally:
        vol.close()


if __name__ == "__main__":
    import sys
    import time

    d = normalize_drive(sys.argv[1] if len(sys.argv) > 1 else "C:")
    t0 = time.time()
    rows = scan_volume(d)
    print("drive=%s entries=%d elapsed=%.2fs" % (d, len(rows), time.time() - t0))
    for row in rows:
        if row["name"].lower() == "notepad.exe" and "windows" in (row["path"] or "").lower():
            print(row)
            break
