#define WIN32_LEAN_AND_MEAN
#include <windows.h>

typedef LONG(NTAPI *RtlGetVersionFn)(OSVERSIONINFOW *);

static void show_error(const WCHAR *text) {
    MessageBoxW(NULL, text, L"Agent", MB_OK | MB_ICONERROR);
}

static int win10_or_newer(void) {
    HMODULE ntdll;
    RtlGetVersionFn fn;
    OSVERSIONINFOW info;

    ntdll = GetModuleHandleW(L"ntdll.dll");
    if (!ntdll)
        return 0;
    fn = (RtlGetVersionFn)GetProcAddress(ntdll, "RtlGetVersion");
    if (!fn)
        return 0;
    info.dwOSVersionInfoSize = sizeof(info);
    if (fn(&info) != 0)
        return 0;
    return info.dwMajorVersion >= 10;
}

static int file_exists(const WCHAR *path) {
    DWORD attr = GetFileAttributesW(path);
    return attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY);
}

static void join_root(WCHAR *out, const WCHAR *root, const WCHAR *rel) {
    lstrcpynW(out, root, 900);
    if (out[0] && out[lstrlenW(out) - 1] != L'\\')
        lstrcatW(out, L"\\");
    lstrcatW(out, rel);
}

static void dirname_of(const WCHAR *path, WCHAR *out) {
    int i;
    int last = -1;
    lstrcpynW(out, path, 1000);
    for (i = 0; out[i]; i++) {
        if (out[i] == L'\\' || out[i] == L'/')
            last = i;
    }
    if (last > 0)
        out[last] = 0;
}

static int pick_one(WCHAR *target, WCHAR *dir, const WCHAR *root, const WCHAR *rel) {
    join_root(target, root, rel);
    if (!file_exists(target))
        return 0;
    dirname_of(target, dir);
    return 1;
}

static int pick_target(const WCHAR *root, WCHAR *target, WCHAR *dir) {
    static const WCHAR *win10[] = {
        L"_internal\\win10\\agent.exe",
        L"win10\\agent.exe",
        NULL
    };
    static const WCHAR *win7[] = {
        L"_internal\\win7\\agent.exe",
        L"win7\\agent.exe",
        NULL
    };
    const WCHAR **first;
    const WCHAR **second;
    int i;

    if (win10_or_newer()) {
        first = win10;
        second = win7;
    } else {
        first = win7;
        second = win10;
    }
    for (i = 0; first[i]; i++) {
        if (pick_one(target, dir, root, first[i]))
            return 1;
    }
    for (i = 0; second[i]; i++) {
        if (pick_one(target, dir, root, second[i]))
            return 1;
    }
    return 0;
}

static int start_agent(const WCHAR *target, const WCHAR *dir) {
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    static WCHAR cmd[1100];
    BOOL ok;

    cmd[0] = L'"';
    cmd[1] = 0;
    lstrcatW(cmd, target);
    lstrcatW(cmd, L"\"");

    si.cb = sizeof(si);
    si.lpReserved = NULL;
    si.lpDesktop = NULL;
    si.lpTitle = NULL;
    si.dwX = 0;
    si.dwY = 0;
    si.dwXSize = 0;
    si.dwYSize = 0;
    si.dwXCountChars = 0;
    si.dwYCountChars = 0;
    si.dwFillAttribute = 0;
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    si.cbReserved2 = 0;
    si.lpReserved2 = NULL;
    si.hStdInput = NULL;
    si.hStdOutput = NULL;
    si.hStdError = NULL;

    pi.hProcess = NULL;
    pi.hThread = NULL;
    pi.dwProcessId = 0;
    pi.dwThreadId = 0;

    ok = CreateProcessW(
        target,
        cmd,
        NULL,
        NULL,
        FALSE,
        DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        NULL,
        dir,
        &si,
        &pi);
    if (!ok)
        return 0;
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 1;
}

static int run(void) {
    static WCHAR exe[1000];
    static WCHAR root[1000];
    static WCHAR target[1000];
    static WCHAR dir[1000];
    DWORD n;

    n = GetModuleFileNameW(NULL, exe, 1000);
    if (n == 0 || n >= 1000) {
        show_error(L"无法定位安装目录");
        return 1;
    }
    dirname_of(exe, root);
    if (!pick_target(root, target, dir)) {
        show_error(L"未找到 win10 或 win7 运行时，请重新安装");
        return 1;
    }
    if (!start_agent(target, dir)) {
        show_error(L"启动 Agent 失败");
        return 1;
    }
    return 0;
}

void WINAPI WinMainCRTStartup(void) {
    ExitProcess((UINT)run());
}
