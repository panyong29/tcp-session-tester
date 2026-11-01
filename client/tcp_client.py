"""TCP连接数探测工具 V2.5

多线程持续建连并保持，探测目标的最大 TCP 并发连接数。
失败数达到阈值或达到探测上限时自动停止。

停止时立即释放：stop() 当场关掉全部已建连接，不等在途建连
跑完超时、不等工作线程 join；关闭用 SO_LINGER 0 发 RST，
本地端口不进 TIME_WAIT、NAT 会话表里也不再残留，
点停止后立刻看 netstat / 路由器会话数就是干净的。
本工具只建连不发业务数据，RST 关闭不会丢数据。

V2.4 新增：界面右侧实时显示本进程连接占用（Windows 上用
系统 TCP 表实测，非 Windows 上用程序内计数兜底）。
V2.5 纠正方向：右侧面板不再只看测试程序自己，改成当前
Windows 全部进程的会话数、按活动连接数从高到低排序，前 25 名，
列只有 PID、进程名、活动连接数。注意 Bound 的坑：Windows 会把同一个客户端 socket 报两次，
一次是 Bound（通配地址 0.0.0.0），一次是它真正的 TCP 状态
（本机 IP + 远端），两条的本地端口完全相同。所以每个进程的
合计必须按本地端口去重，绝不能把 Bound 再加一遍，否则总数
会翻倍（例如测试程序 Established 489 + 在途 10 = 去重 499，
不是 998）。
"""
import os
import queue
import socket
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

APP_VERSION = "2.5"

# MIB_TCP_STATE（Windows TCP 表里的状态码）
_TCP_STATE_NAMES = {
    0: "Bound", 1: "Closed", 2: "Listen", 3: "SynSent",
    4: "SynReceived", 5: "Established", 6: "FinWait1", 7: "FinWait2",
    8: "CloseWait", 9: "Closing", 10: "LastAck", 11: "TimeWait",
    12: "DeleteTCB",
}


def _classify_row(state, local_port, remote_addr, remote_port):
    """把一条 TCP 表记录归到一类；返回 None 表示不计入占用。"""
    if state == 2:
        return "listen"
    if remote_addr == 0 and remote_port == 0 and local_port != 0:
        # 远端为空、本地端口已占：就是 PowerShell 里看到的 Bound。
        return "bound"
    if state == 5:
        return "established"
    if state == 3:
        return "synsent"
    if state == 11:
        return "time_wait"
    if state == 8:
        return "close_wait"
    if state in (4, 6, 7, 9, 10):
        return "other"
    return None  # Closed / DeleteTCB / 未知


def _aggregate_tcp_rows(rows):
    """按 PID 聚合 TCP 表记录，并按去重合计从高到低排序。

    rows 是 (pid, state, local_port, remote_addr, remote_port) 元组。
    distinct 按本地端口去重：Bound 条目和 Established/SynSent 条目
    共享同一个本地端口，去重后恰好等于 Bound 数，不会翻倍。
    Listen 和 TIME_WAIT 不计入 distinct（前者不是连接，后者已无
    socket 句柄、且 V2.3 的 RST 关闭本来就不会产生 TIME_WAIT）。
    纯函数，方便在非 Windows 上用模拟数据单测。
    """
    per_pid = {}
    for pid, state, local_port, remote_addr, remote_port in rows:
        kind = _classify_row(state, local_port, remote_addr, remote_port)
        if kind is None:
            continue
        d = per_pid.setdefault(pid, {
            "pid": pid, "established": 0, "synsent": 0, "bound": 0,
            "listen": 0, "time_wait": 0, "close_wait": 0, "other": 0,
            "_ports": set()})
        d[kind] += 1
        if kind not in ("listen", "time_wait"):
            d["_ports"].add(local_port)
    result = []
    for d in per_pid.values():
        d["distinct"] = len(d.pop("_ports"))
        result.append(d)
    result.sort(key=lambda x: x["distinct"], reverse=True)
    return result


def _app_tcp_counts(prober):
    """程序内计数兜底：非 Windows、或系统表读不到时用它。"""
    held = len(prober.sockets) if prober is not None else 0
    inflight = 0
    if prober is not None:
        inflight = max(0, prober.attempted - prober.success - prober.failed)
    return {
        "pid": os.getpid(), "distinct": held + inflight, "established": held,
        "synsent": inflight, "bound": held + inflight, "listen": 0,
        "time_wait": 0, "close_wait": 0, "other": 0, "held": held,
    }


def _win_all_tcp_rows():
    """Windows：用 GetExtendedTcpTable 读全部进程的 IPv4 TCP 表（只读）。

    只读系统表，不改任何进程、不结束任何连接。仅统计 IPv4，
    IPv6 暂不计（预设目标均为 IPv4 地址，当前探测不受影响）。
    """
    import ctypes

    class Row(ctypes.Structure):
        _fields_ = [("dwState", ctypes.c_ulong),
                    ("dwLocalAddr", ctypes.c_ulong),
                    ("dwLocalPort", ctypes.c_ulong),
                    ("dwRemoteAddr", ctypes.c_ulong),
                    ("dwRemotePort", ctypes.c_ulong),
                    ("dwOwningPid", ctypes.c_ulong)]

    iphlpapi = ctypes.windll.iphlpapi
    size = ctypes.c_ulong(0)
    # TCP_TABLE_OWNER_PID_ALL = 5，AF_INET = 2；先问一遍要多大缓冲。
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)
    if size.value == 0:
        raise OSError("TCP 表缓冲大小为 0")
    buf = ctypes.create_string_buffer(size.value)
    if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 5, 0) != 0:
        raise OSError("GetExtendedTcpTable 失败")
    num = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong)).contents.value
    rows = (Row * num).from_address(ctypes.addressof(buf) + 4)
    return [(row.dwOwningPid, row.dwState, row.dwLocalPort,
             row.dwRemoteAddr, row.dwRemotePort) for row in rows]


def _win_process_name(pid, cache):
    """Windows：按 PID 查进程名（只读，查不到就回退）。"""
    if pid in cache:
        return cache[pid]
    if pid == 0:
        name = "Idle"
    elif pid == 4:
        name = "System"
    else:
        name = "未知"
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            # PROCESS_QUERY_INFORMATION(0x0400) | PROCESS_VM_READ(0x0010)
            handle = kernel32.OpenProcess(0x0410, False, pid)
            if handle:
                try:
                    buf = ctypes.create_unicode_buffer(260)
                    if psapi.GetModuleBaseNameW(handle, None, buf, 260):
                        name = buf.value or name
                finally:
                    kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass
    cache[pid] = name
    return name


def get_all_process_tcp_counts(prober, name_cache):
    """全部进程的会话数（已按去重合计排好序）+ 测试程序自己那行。

    返回 (rows, tester_row, source)：rows 是前 25 名之外的全部排序
    结果由调用方截取；tester_row 是本进程那行（表里没有时为 0 行）。
    Windows 优先用系统表，其他平台只返回本进程的程序内计数。
    """
    if sys.platform == "win32":
        try:
            rows = _aggregate_tcp_rows(_win_all_tcp_rows())
            own_pid = os.getpid()
            for d in rows:
                d["name"] = _win_process_name(d["pid"], name_cache)
            tester = next((d for d in rows if d["pid"] == own_pid), None)
            if tester is None:
                tester = {"pid": own_pid, "distinct": 0, "established": 0,
                          "synsent": 0, "bound": 0, "listen": 0,
                          "time_wait": 0, "close_wait": 0, "other": 0,
                          "name": _win_process_name(own_pid, name_cache)}
            return rows, tester, "系统表 · IPv4"
        except Exception:  # noqa: BLE001
            pass
    own = _app_tcp_counts(prober)
    _fallback_name = os.path.basename(sys.argv[0]) if sys.argv else ""
    own["name"] = _fallback_name if _fallback_name not in ("", "-") else "TcpSessionTester"
    return [own], own, "程序内"


# 预设测试目标：(显示名, host, port)
PRESETS = [
    ("阿里DNS", "223.6.6.6", 53),
    ("Google DNS", "8.8.8.8", 53),
    ("Cloudflare DNS", "1.1.1.1", 53),
    ("腾讯DNS", "119.29.29.29", 53),
    ("百度", "www.baidu.com", 80),
    ("我的服务器", "", 0),
]


class Prober:
    """探测器：多线程建连并保持，失败达阈值或达上限时停止。"""

    def __init__(self, host, port, interval_ms, thread_count, max_probe, max_fail, timeout, ui_q):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.interval = interval_ms / 1000.0
        self.thread_count = thread_count
        self.max_probe = max_probe
        self.max_fail = max_fail
        self.ui_q = ui_q
        self.lock = threading.Lock()
        self.sockets = []  # 保持住的连接
        self.attempted = 0
        self.success = 0
        self.failed = 0
        self.consec_failed = 0  # 连续失败计数
        self.fail_timeout = 0   # 超时失败
        self.fail_refused = 0   # 被拒绝
        self.fail_other = 0     # 其他
        self.stop_flag = threading.Event()
        self.threads = []

    @staticmethod
    def _close_socket(s):
        """立即释放一个连接：SO_LINGER 0 发 RST，不进 TIME_WAIT。"""
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                         struct.pack("ii", 1, 0))
        except Exception:  # noqa: BLE001
            pass
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass

    def close_all(self):
        """当场关掉全部已建连接，返回释放数。线程安全，可重复调。"""
        with self.lock:
            sockets, self.sockets = self.sockets, []
        for s in sockets:
            self._close_socket(s)
        return len(sockets)

    def _report(self):
        try:
            self.ui_q.put_nowait({
                "success": self.success, "failed": self.failed,
                "attempted": self.attempted, "active": len(self.sockets),
                "fail_timeout": self.fail_timeout, "fail_refused": self.fail_refused,
                "fail_other": self.fail_other,
            })
        except queue.Full:
            pass

    def _worker(self):
        while not self.stop_flag.is_set():
            auto_stop = False
            with self.lock:
                if self.attempted >= self.max_probe or self.consec_failed >= self.max_fail:
                    self.stop_flag.set()
                    auto_stop = True
                else:
                    self.attempted += 1
            if auto_stop:
                # 自动停止也当场释放，不能只置标志等 join：
                # 否则在途建连跑完超时之前，已建连接还握在手里。
                self.close_all()
                break
            try:
                s = socket.create_connection((self.host, self.port), timeout=self.timeout)
            except (socket.timeout, TimeoutError):
                kind = "timeout"
            except ConnectionRefusedError:
                kind = "refused"
            except Exception:  # noqa: BLE001
                kind = "other"
            else:
                kind = None
            to_close = None
            auto_stop = False
            with self.lock:
                if kind is None:
                    self.success += 1
                    self.consec_failed = 0
                    if self.stop_flag.is_set():
                        # 停止后才建成的连接：不进保持列表，当场关掉，
                        # 否则它会漏到 run() 关完之后、永远没人关。
                        to_close = s
                    else:
                        self.sockets.append(s)
                else:
                    self.failed += 1
                    self.consec_failed += 1
                    if kind == "timeout":
                        self.fail_timeout += 1
                    elif kind == "refused":
                        self.fail_refused += 1
                    else:
                        self.fail_other += 1
                    if self.consec_failed >= self.max_fail:
                        self.stop_flag.set()
                        auto_stop = True
            if to_close is not None:
                self._close_socket(to_close)
            if auto_stop:
                # 连续失败触发自动停止：当场释放全部已建连接。
                self.close_all()
            self._report()
            if self.interval > 0:
                self.stop_flag.wait(self.interval)  # 可中断的间隔

    def run(self):
        self.threads = [threading.Thread(target=self._worker, daemon=True)
                        for _ in range(self.thread_count)]
        for t in self.threads:
            t.start()
        for t in self.threads:
            t.join()
        # 兜底：关掉 join 后还剩的（正常情况下 stop() 已关完，这里是 0）
        self.close_all()
        done = {"done": True, "success": self.success,
                "failed": self.failed, "attempted": self.attempted,
                "fail_timeout": self.fail_timeout, "fail_refused": self.fail_refused,
                "fail_other": self.fail_other}
        # done 消息必须送达，否则界面永远卡在“正在停止…”。
        # 原版用阻塞 put，队列满时会在这里卡住；改成丢弃最旧的
        # 状态上报也要把 done 送出去，绝不阻塞。
        while True:
            try:
                self.ui_q.put_nowait(done)
                break
            except queue.Full:
                try:
                    self.ui_q.get_nowait()
                except queue.Empty:
                    break

    def stop(self):
        """停止探测并当场释放全部已建连接，返回释放数。

        只置标志是不够的：原版要等在途建连跑完超时、工作线程
        join 后才关，停止后连接还握在手里一段时间。
        """
        self.stop_flag.set()
        return self.close_all()


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"TCP连接数探测工具 V{APP_VERSION}")
        root.geometry("1020x600")
        root.resizable(False, False)

        self.ui_q = queue.Queue(maxsize=100)
        self.prober = None
        self.probing = False
        self.t_start = 0
        self._name_cache = {}

        frm = ttk.Frame(root, padding=10)
        frm.pack(side="left", fill="y")

        # 右侧实时占用面板：当前 Windows 全部进程，按去重合计从高到低
        # 排序，只读显示，不改任何探测逻辑、不结束任何进程。
        rfrm = ttk.LabelFrame(root, text="当前进程会话数 前25", padding=10)
        rfrm.pack(side="right", fill="both", expand=True, padx=(0, 10), pady=10)
        cols = ("pid", "name", "active")
        self.tree_usage = ttk.Treeview(rfrm, columns=cols, show="headings", height=24)
        headings = {"pid": "PID", "name": "进程名", "active": "活动连接数"}
        widths = {"pid": 80, "name": 200, "active": 110}
        for c in cols:
            self.tree_usage.heading(c, text=headings[c])
            self.tree_usage.column(c, width=widths[c], anchor="center" if c != "name" else "w")
        self.tree_usage.pack(fill="both", expand=True)
        self.tree_usage.tag_configure("self", background="#fff2cc")

        r = 0
        ttk.Label(frm, text="预设目标:").grid(row=r, column=0, sticky="w", pady=4)
        self.preset_var = tk.StringVar(value=PRESETS[0][0])
        self.cb_preset = ttk.Combobox(frm, textvariable=self.preset_var, width=20,
                                      values=[p[0] for p in PRESETS], state="readonly")
        self.cb_preset.grid(row=r, column=1, sticky="w", pady=4)
        self.cb_preset.bind("<<ComboboxSelected>>", self._on_preset)

        r += 1
        ttk.Label(frm, text="服务器地址:").grid(row=r, column=0, sticky="w", pady=4)
        self.e_host = ttk.Entry(frm, width=22)
        self.e_host.grid(row=r, column=1, sticky="w", pady=4)
        ttk.Label(frm, text="端口:").grid(row=r, column=2, sticky="w", padx=(12, 0), pady=4)
        self.e_port = ttk.Entry(frm, width=10)
        self.e_port.insert(0, "9000")
        self.e_port.grid(row=r, column=3, sticky="w", pady=4)

        r += 1
        ttk.Label(frm, text="时间间隔(ms):").grid(row=r, column=0, sticky="w", pady=4)
        self.e_interval = ttk.Entry(frm, width=22)
        self.e_interval.insert(0, "100")
        self.e_interval.grid(row=r, column=1, sticky="w", pady=4)

        r += 1
        ttk.Label(frm, text="线程数:").grid(row=r, column=0, sticky="w", pady=4)
        self.e_threads = ttk.Entry(frm, width=22)
        self.e_threads.insert(0, "10")
        self.e_threads.grid(row=r, column=1, sticky="w", pady=4)

        r += 1
        ttk.Label(frm, text="探测上限:").grid(row=r, column=0, sticky="w", pady=4)
        self.e_max = ttk.Entry(frm, width=22)
        self.e_max.insert(0, "10000")
        self.e_max.grid(row=r, column=1, sticky="w", pady=4)

        r += 1
        ttk.Label(frm, text="连续失败几次停止:").grid(row=r, column=0, sticky="w", pady=4)
        self.e_maxfail = ttk.Entry(frm, width=22)
        self.e_maxfail.insert(0, "100")
        self.e_maxfail.grid(row=r, column=1, sticky="w", pady=4)

        r += 1
        ttk.Label(frm, text="连接超时(秒):").grid(row=r, column=0, sticky="w", pady=4)
        self.e_timeout = ttk.Entry(frm, width=22)
        self.e_timeout.insert(0, "5")
        self.e_timeout.grid(row=r, column=1, sticky="w", pady=4)

        r += 1
        bfrm = ttk.Frame(frm)
        bfrm.grid(row=r, column=0, columnspan=4, pady=8, sticky="w")
        self.b_start = ttk.Button(bfrm, text="开始探测", command=self.start, width=12)
        self.b_start.pack(side="left", padx=(0, 40))
        self.b_stop = ttk.Button(bfrm, text="停止探测", command=self.stop, width=12,
                                 state="disabled")
        self.b_stop.pack(side="left")
        ttk.Button(bfrm, text="清空日志", command=self._clear_log, width=12).pack(side="left", padx=(40, 0))

        r += 1
        ttk.Label(frm, text="参数解析结果:").grid(row=r, column=0, sticky="w", pady=(4, 2))
        r += 1
        self.t_param = tk.Text(frm, height=4, width=72, state="disabled")
        self.t_param.grid(row=r, column=0, columnspan=4, pady=2)

        r += 1
        ttk.Label(frm, text="探测结果:").grid(row=r, column=0, sticky="w", pady=(6, 2))
        r += 1
        sfrm = ttk.Frame(frm)
        sfrm.grid(row=r, column=0, columnspan=4, sticky="w", pady=2)
        self.l_ok = ttk.Label(sfrm, text="成功连接数: 0", font=("", 10))
        self.l_ok.pack(side="left", padx=(0, 60))
        self.l_fail = ttk.Label(sfrm, text="失败连接数: 0", font=("", 10))
        self.l_fail.pack(side="left")
        r += 1
        self.l_breakdown = ttk.Label(frm, text="", font=("", 9), foreground="gray")
        self.l_breakdown.grid(row=r, column=0, columnspan=4, sticky="w", pady=1)
        r += 1
        self.t_log = tk.Text(frm, height=10, width=72, state="disabled")
        self.t_log.grid(row=r, column=0, columnspan=4, pady=2)

        self._on_preset()  # 启动时按默认预设填充地址端口
        self._poll()
        self._refresh_usage()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _refresh_usage(self):
        """每 1 秒刷新一次右侧全部进程表，失败也不影响探测。"""
        try:
            rows, tester, _source = get_all_process_tcp_counts(
                self.prober if self.probing else None, self._name_cache)
            for item in self.tree_usage.get_children():
                self.tree_usage.delete(item)
            for d in rows[:25]:
                tag = ("self",) if d["pid"] == tester["pid"] else ()
                self.tree_usage.insert("", "end", values=(
                    d["pid"], d.get("name", "未知"), d["distinct"]), tags=tag)
        except Exception:  # noqa: BLE001
            pass
        self.root.after(1000, self._refresh_usage)

    def _on_preset(self, _evt=None):
        name = self.preset_var.get()
        for n, h, p in PRESETS:
            if n == name:
                self.e_host.delete(0, "end")
                self.e_host.insert(0, h)
                self.e_port.delete(0, "end")
                self.e_port.insert(0, str(p) if p else "")
                break

    def _clear_log(self):
        self.t_log.config(state="normal")
        self.t_log.delete("1.0", "end")
        self.t_log.config(state="disabled")

    def _log(self, msg):
        self.t_log.config(state="normal")
        self.t_log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.t_log.see("end")
        self.t_log.config(state="disabled")

    def _set_param_text(self, text):
        self.t_param.config(state="normal")
        self.t_param.delete("1.0", "end")
        self.t_param.insert("end", text)
        self.t_param.config(state="disabled")

    def _int(self, entry, name, lo, hi):
        try:
            v = int(entry.get().strip())
        except ValueError:
            raise ValueError(f"{name}请填整数")
        if not lo <= v <= hi:
            raise ValueError(f"{name}范围 {lo}~{hi}")
        return v

    def start(self):
        if self.probing:
            return
        host = self.e_host.get().strip()
        if not host:
            messagebox.showwarning("提示", "请填写服务器地址")
            return
        try:
            port = self._int(self.e_port, "端口", 1, 65535)
            interval = self._int(self.e_interval, "时间间隔", 0, 60000)
            threads = self._int(self.e_threads, "线程数", 1, 200)
            max_probe = self._int(self.e_max, "探测上限", 1, 1000000)
            max_fail = self._int(self.e_maxfail, "失败次数", 1, 1000000)
            timeout = self._int(self.e_timeout, "连接超时", 1, 60)
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            return
        self._set_param_text(
            f"目标: {host}:{port}\n"
            f"时间间隔: {interval}ms, 线程数: {threads}, "
            f"探测上限: {max_probe}, 连续失败{max_fail}次停止")
        self._log(f"开始探测 {host}:{port}")
        self.probing = True
        self.t_start = time.time()
        self.b_start.config(state="disabled")
        self.b_stop.config(state="normal")
        self.l_ok.config(text="成功连接数: 0")
        self.l_fail.config(text="失败连接数: 0")
        self.l_breakdown.config(text="")
        self._last_host, self._last_port = host, port
        self._last_params = {"interval": interval, "threads": threads,
                             "max_probe": max_probe, "max_fail": max_fail,
                             "timeout": timeout}
        self.prober = Prober(host, port, interval, threads, max_probe, max_fail, timeout, self.ui_q)
        threading.Thread(target=self.prober.run, daemon=True).start()

    def stop(self):
        if self.probing and self.prober:
            released = self.prober.stop()
            self._log(f"正在停止…已释放 {released} 个连接")
            self.b_stop.config(state="disabled")

    def _export_csv(self, m):
        import csv, os
        fname = f"tcp_probe_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        path = os.path.join(desktop, fname) if os.path.isdir(desktop) \
            else os.path.join(os.path.expanduser("~"), fname)
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["目标", f"{self._last_host}:{self._last_port}"])
                w.writerow(["时间间隔(ms)", self._last_params["interval"]])
                w.writerow(["线程数", self._last_params["threads"]])
                w.writerow(["探测上限", self._last_params["max_probe"]])
                w.writerow(["连续失败停止", self._last_params["max_fail"]])
                w.writerow(["连接超时(秒)", self._last_params["timeout"]])
                w.writerow(["成功", m["success"]])
                w.writerow(["失败", m["failed"]])
                w.writerow(["超时失败", m.get("fail_timeout", 0)])
                w.writerow(["拒绝失败", m.get("fail_refused", 0)])
                w.writerow(["其他失败", m.get("fail_other", 0)])
                w.writerow(["共尝试", m["attempted"]])
                w.writerow(["用时(秒)", round(time.time() - self.t_start, 1)])
            return path
        except Exception:  # noqa: BLE001
            return None

    def _poll(self):
        try:
            while True:
                m = self.ui_q.get_nowait()
                if m.get("done"):
                    el = round(time.time() - self.t_start, 1)
                    self._log(f"探测结束，用时{el}s：成功 {m['success']}，"
                              f"失败 {m['failed']}，共尝试 {m['attempted']}")
                    bd = (f"超时 {m.get('fail_timeout',0)} / "
                          f"拒绝 {m.get('fail_refused',0)} / "
                          f"其他 {m.get('fail_other',0)}")
                    self._log(f"失败分类：{bd}")
                    self._log(f"结论：目标最大并发连接数约 {m['success']}")
                    self._log("连接已全部释放")
                    csv_path = self._export_csv(m)
                    if csv_path:
                        self._log(f"结果已导出: {csv_path}")
                    self.probing = False
                    self.prober = None
                    self.b_start.config(state="normal")
                    self.b_stop.config(state="disabled")
                else:
                    self.l_ok.config(text=f"成功连接数: {m['success']}")
                    self.l_fail.config(text=f"失败连接数: {m['failed']}")
                    self.l_breakdown.config(
                        text=f"超时 {m.get('fail_timeout',0)} / "
                             f"拒绝 {m.get('fail_refused',0)} / "
                             f"其他 {m.get('fail_other',0)}")
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def on_close(self):
        if self.prober:
            # stop() 已当场关掉全部连接，关窗口不会再漏连接。
            self.prober.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
