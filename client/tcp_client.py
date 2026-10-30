"""TCP连接数探测工具 V2.3

多线程持续建连并保持，探测目标的最大 TCP 并发连接数。
失败数达到阈值或达到探测上限时自动停止。

停止时立即释放：stop() 当场关掉全部已建连接，不等在途建连
跑完超时、不等工作线程 join；关闭用 SO_LINGER 0 发 RST，
本地端口不进 TIME_WAIT、NAT 会话表里也不再残留，
点停止后立刻看 netstat / 路由器会话数就是干净的。
本工具只建连不发业务数据，RST 关闭不会丢数据。
"""
import queue
import socket
import struct
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

APP_VERSION = "2.3"


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
        root.geometry("620x600")
        root.resizable(False, False)

        self.ui_q = queue.Queue(maxsize=100)
        self.prober = None
        self.probing = False
        self.t_start = 0

        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)

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
        root.protocol("WM_DELETE_WINDOW", self.on_close)

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
