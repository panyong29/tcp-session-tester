"""TCP连接数探测工具 V1.0（NBR 风格）

多线程持续建连并保持，探测目标的最大 TCP 并发连接数。
失败数达到阈值或达到探测上限时自动停止。
"""
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

APP_VERSION = "1.0"


class Prober:
    """探测器：多线程建连并保持，失败达阈值或达上限时停止。"""

    def __init__(self, host, port, interval_ms, thread_count, max_probe, max_fail, ui_q):
        self.host = host
        self.port = port
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
        self.stop_flag = threading.Event()
        self.threads = []

    def _report(self):
        try:
            self.ui_q.put_nowait({
                "success": self.success, "failed": self.failed,
                "attempted": self.attempted, "active": len(self.sockets),
            })
        except queue.Full:
            pass

    def _worker(self):
        while not self.stop_flag.is_set():
            with self.lock:
                if self.attempted >= self.max_probe or self.failed >= self.max_fail:
                    self.stop_flag.set()
                    break
                self.attempted += 1
            try:
                s = socket.create_connection((self.host, self.port), timeout=10)
            except Exception:  # noqa: BLE001
                with self.lock:
                    self.failed += 1
                self._report()
            else:
                with self.lock:
                    self.sockets.append(s)
                    self.success += 1
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
        for s in self.sockets:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        self.sockets.clear()
        self.ui_q.put({"done": True, "success": self.success,
                       "failed": self.failed, "attempted": self.attempted})

    def stop(self):
        self.stop_flag.set()


class App:
    def __init__(self, root):
        self.root = root
        root.title("TCP连接数探测工具 V1.0")
        root.geometry("620x600")
        root.resizable(False, False)

        self.ui_q = queue.Queue(maxsize=100)
        self.prober = None
        self.probing = False
        self.t_start = 0

        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)

        r = 0
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
        ttk.Label(frm, text="失败多少次停止探测:").grid(row=r, column=0, sticky="w", pady=4)
        self.e_maxfail = ttk.Entry(frm, width=22)
        self.e_maxfail.insert(0, "100")
        self.e_maxfail.grid(row=r, column=1, sticky="w", pady=4)

        r += 1
        bfrm = ttk.Frame(frm)
        bfrm.grid(row=r, column=0, columnspan=4, pady=8, sticky="w")
        self.b_start = ttk.Button(bfrm, text="开始探测", command=self.start, width=12)
        self.b_start.pack(side="left", padx=(0, 40))
        self.b_stop = ttk.Button(bfrm, text="停止探测", command=self.stop, width=12,
                                 state="disabled")
        self.b_stop.pack(side="left")

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
        self.t_log = tk.Text(frm, height=10, width=72, state="disabled")
        self.t_log.grid(row=r, column=0, columnspan=4, pady=2)

        self._poll()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

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
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            return
        self._set_param_text(
            f"目标: {host}:{port}\n"
            f"时间间隔: {interval}ms, 线程数: {threads}, "
            f"探测上限: {max_probe}, 失败{max_fail}次停止")
        self._log(f"开始探测 {host}:{port}")
        self.probing = True
        self.t_start = time.time()
        self.b_start.config(state="disabled")
        self.b_stop.config(state="normal")
        self.l_ok.config(text="成功连接数: 0")
        self.l_fail.config(text="失败连接数: 0")
        self.prober = Prober(host, port, interval, threads, max_probe, max_fail, self.ui_q)
        threading.Thread(target=self.prober.run, daemon=True).start()

    def stop(self):
        if self.probing and self.prober:
            self._log("正在停止…")
            self.prober.stop()
            self.b_stop.config(state="disabled")

    def _poll(self):
        try:
            while True:
                m = self.ui_q.get_nowait()
                if m.get("done"):
                    el = round(time.time() - self.t_start, 1)
                    self._log(f"探测结束，用时{el}s：成功 {m['success']}，"
                              f"失败 {m['failed']}，共尝试 {m['attempted']}")
                    self._log(f"结论：目标最大并发连接数约 {m['success']}")
                    self.probing = False
                    self.prober = None
                    self.b_start.config(state="normal")
                    self.b_stop.config(state="disabled")
                else:
                    self.l_ok.config(text=f"成功连接数: {m['success']}")
                    self.l_fail.config(text=f"失败连接数: {m['failed']}")
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def on_close(self):
        if self.prober:
            self.prober.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
