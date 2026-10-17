"""TCP 会话数测试 - 客户端 v1.0

填服务器地址、端口、并发数等参数，一键测试目标 TCP 服务能扛住多少并发会话。
两种模式：
  建连保持：连上 N 个会话并保持一段时间（测会话表/连接数上限）
  建连+回显：每个连接发一包数据、收回显后关闭（测建连+收发能力）

网络 IO 跑在后台线程的 asyncio 事件循环里，界面不卡。
"""
import asyncio
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

APP_VERSION = "1.0"


# ---------------- 后台测试逻辑（跑在工作线程） ----------------

async def _one_conn(params, shared, release_evt):
    """单个连接：建连 -> 按模式收发/保持 -> 关闭"""
    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(params["host"], params["port"]),
            timeout=params["timeout"])
    except Exception:
        shared["failed"] += 1
        return
    shared["ok"] += 1
    shared["lat_sum"] += time.monotonic() - t0
    shared["active"] += 1
    try:
        if params["mode"] == "echo":
            payload = b"x" * params["payload"]
            writer.write(payload)
            await writer.drain()
            got = 0
            while got < len(payload):
                chunk = await asyncio.wait_for(reader.read(65536), timeout=params["timeout"])
                if not chunk:
                    break
                got += len(chunk)
            if got >= len(payload):
                shared["echo_ok"] += 1
            else:
                shared["echo_fail"] += 1
        else:  # hold：保持到主控释放
            await release_evt.wait()
    except Exception:
        shared["failed"] += 1
    finally:
        shared["active"] -= 1
        try:
            writer.close()
        except Exception:
            pass


def _snapshot(params, shared, phase):
    avg_ms = (shared["lat_sum"] / shared["ok"] * 1000) if shared["ok"] else 0
    return {
        "phase": phase,
        "attempted": shared["attempted"],
        "ok": shared["ok"],
        "failed": shared["failed"],
        "active": shared["active"],
        "avg_ms": round(avg_ms, 1),
        "echo_ok": shared["echo_ok"],
        "echo_fail": shared["echo_fail"],
        "target": params["concurrency"],
    }


async def _run(params, ui_q, stop_flag):
    shared = {"attempted": 0, "ok": 0, "failed": 0, "active": 0,
              "lat_sum": 0.0, "echo_ok": 0, "echo_fail": 0}
    release_evt = asyncio.Event()
    tasks = []
    try:
        # 建连阶段：按速率爬坡
        n = params["concurrency"]
        interval = 1.0 / params["rate"] if params["rate"] > 0 else 0
        for _ in range(n):
            if stop_flag.is_set():
                break
            shared["attempted"] += 1
            tasks.append(asyncio.create_task(_one_conn(params, shared, release_evt)))
            if interval:
                await asyncio.sleep(interval)
        # 等建连结果落定
        while not stop_flag.is_set():
            if shared["ok"] + shared["failed"] >= shared["attempted"] \
                    and shared["attempted"] >= n:
                break
            ui_q.put(_snapshot(params, shared, "connecting"))
            await asyncio.sleep(0.2)
        # 保持阶段（仅 hold 模式）
        if params["mode"] == "hold" and not stop_flag.is_set():
            deadline = time.monotonic() + params["duration"]
            while time.monotonic() < deadline and not stop_flag.is_set():
                ui_q.put(_snapshot(params, shared, "holding"))
                await asyncio.sleep(0.2)
        release_evt.set()  # 释放保持中的连接
        if tasks:
            await asyncio.wait(tasks, timeout=params["timeout"] + 5)
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        ui_q.put(_snapshot(params, shared, "done"))


def worker_main(params, ui_q, stop_flag):
    try:
        asyncio.run(_run(params, ui_q, stop_flag))
    except Exception as e:  # noqa: BLE001
        ui_q.put({"phase": "error", "msg": str(e)})


# ---------------- 界面 ----------------

class App:
    def __init__(self, root):
        self.root = root
        root.title(f"TCP 会话数测试 v{APP_VERSION}")
        root.geometry("560x520")
        root.resizable(False, False)

        self.ui_q = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker = None
        self.testing = False
        self.t_start = 0

        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)

        # 服务器参数
        ttk.Label(frm, text="服务器地址").grid(row=0, column=0, sticky="w", pady=3)
        self.e_host = ttk.Entry(frm, width=24)
        self.e_host.grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(frm, text="端口").grid(row=0, column=2, sticky="w", padx=(10, 0), pady=3)
        self.e_port = ttk.Entry(frm, width=8)
        self.e_port.insert(0, "9000")
        self.e_port.grid(row=0, column=3, sticky="w", pady=3)

        ttk.Label(frm, text="并发连接数").grid(row=1, column=0, sticky="w", pady=3)
        self.e_conn = ttk.Entry(frm, width=12)
        self.e_conn.insert(0, "1000")
        self.e_conn.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(frm, text="建连速率(个/秒,0不限)").grid(row=1, column=2, sticky="w",
                                                          padx=(10, 0), pady=3)
        self.e_rate = ttk.Entry(frm, width=8)
        self.e_rate.insert(0, "200")
        self.e_rate.grid(row=1, column=3, sticky="w", pady=3)

        ttk.Label(frm, text="测试模式").grid(row=2, column=0, sticky="w", pady=3)
        self.mode = tk.StringVar(value="hold")
        mfrm = ttk.Frame(frm)
        mfrm.grid(row=2, column=1, columnspan=3, sticky="w", pady=3)
        ttk.Radiobutton(mfrm, text="建连保持", variable=self.mode, value="hold").pack(side="left")
        ttk.Radiobutton(mfrm, text="建连+回显+关闭", variable=self.mode,
                        value="echo").pack(side="left", padx=(12, 0))

        ttk.Label(frm, text="保持时长(秒)").grid(row=3, column=0, sticky="w", pady=3)
        self.e_dur = ttk.Entry(frm, width=12)
        self.e_dur.insert(0, "30")
        self.e_dur.grid(row=3, column=1, sticky="w", pady=3)
        ttk.Label(frm, text="回显数据(字节)").grid(row=3, column=2, sticky="w",
                                                    padx=(10, 0), pady=3)
        self.e_payload = ttk.Entry(frm, width=8)
        self.e_payload.insert(0, "1024")
        self.e_payload.grid(row=3, column=3, sticky="w", pady=3)

        ttk.Label(frm, text="连接超时(秒)").grid(row=4, column=0, sticky="w", pady=3)
        self.e_timeout = ttk.Entry(frm, width=12)
        self.e_timeout.insert(0, "10")
        self.e_timeout.grid(row=4, column=1, sticky="w", pady=3)

        # 按钮
        bfrm = ttk.Frame(frm)
        bfrm.grid(row=5, column=0, columnspan=4, pady=10)
        self.b_start = ttk.Button(bfrm, text="开始测试", command=self.start, width=14)
        self.b_start.pack(side="left", padx=5)
        self.b_stop = ttk.Button(bfrm, text="停止", command=self.stop, width=14,
                                 state="disabled")
        self.b_stop.pack(side="left", padx=5)

        # 进度 + 统计
        self.prog = ttk.Progressbar(frm, mode="determinate", length=520)
        self.prog.grid(row=6, column=0, columnspan=4, pady=4)
        self.stat_var = tk.StringVar(value="就绪")
        ttk.Label(frm, textvariable=self.stat_var, font=("", 10)).grid(
            row=7, column=0, columnspan=4, sticky="w", pady=2)
        self.stat2_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.stat2_var, font=("", 10)).grid(
            row=8, column=0, columnspan=4, sticky="w", pady=2)

        # 日志
        ttk.Label(frm, text="日志").grid(row=9, column=0, sticky="w", pady=(6, 2))
        self.log = tk.Text(frm, height=10, width=66, state="disabled")
        self.log.grid(row=10, column=0, columnspan=4, pady=2)

        self._poll()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _int(self, entry, name, lo, hi):
        try:
            v = int(entry.get().strip())
        except ValueError:
            raise ValueError(f"{name}请填整数")
        if not lo <= v <= hi:
            raise ValueError(f"{name}范围 {lo}~{hi}")
        return v

    def start(self):
        host = self.e_host.get().strip()
        if not host:
            messagebox.showwarning("提示", "请填写服务器地址")
            return
        try:
            params = {
                "host": host,
                "port": self._int(self.e_port, "端口", 1, 65535),
                "concurrency": self._int(self.e_conn, "并发连接数", 1, 50000),
                "rate": self._int(self.e_rate, "建连速率", 0, 100000),
                "mode": self.mode.get(),
                "duration": self._int(self.e_dur, "保持时长", 1, 86400),
                "payload": self._int(self.e_payload, "回显数据", 1, 1048576),
                "timeout": self._int(self.e_timeout, "连接超时", 1, 120),
            }
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            return
        self.stop_flag.clear()
        self.testing = True
        self.t_start = time.time()
        self.b_start.config(state="disabled")
        self.b_stop.config(state="normal")
        self.prog["value"] = 0
        mode_name = "建连保持" if params["mode"] == "hold" else "建连+回显"
        self._log(f"开始测试 {params['host']}:{params['port']}，模式={mode_name}，"
                  f"并发={params['concurrency']}")
        self.worker = threading.Thread(target=worker_main,
                                       args=(params, self.ui_q, self.stop_flag),
                                       daemon=True)
        self.worker.start()

    def stop(self):
        if self.testing:
            self._log("正在停止…")
            self.stop_flag.set()
            self.b_stop.config(state="disabled")

    def _poll(self):
        try:
            while True:
                s = self.ui_q.get_nowait()
                self._on_stat(s)
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _on_stat(self, s):
        phase = s.get("phase")
        if phase == "error":
            self._log(f"测试出错: {s.get('msg')}")
            self._finish()
            return
        if phase == "done":
            el = round(time.time() - self.t_start, 1)
            self._log(f"测试结束，用时 {el}s：成功 {s['ok']}/{s['target']}，"
                      f"失败 {s['failed']}，平均建连 {s['avg_ms']}ms")
            if s["echo_ok"] or s["echo_fail"]:
                self._log(f"回显：成功 {s['echo_ok']}，失败 {s['echo_fail']}")
            self._finish()
            return
        ph = {"connecting": "建连中", "holding": "保持中"}.get(phase, "")
        self.stat_var.set(
            f"{ph}  已尝试 {s['attempted']}/{s['target']}  "
            f"成功 {s['ok']}  失败 {s['failed']}  当前活跃 {s['active']}")
        self.stat2_var.set(f"平均建连耗时 {s['avg_ms']}ms")
        if s["target"]:
            self.prog["value"] = min(100, s["attempted"] / s["target"] * 100)

    def _finish(self):
        self.testing = False
        self.b_start.config(state="normal")
        self.b_stop.config(state="disabled")
        self.prog["value"] = 100 if self.prog["value"] else 0

    def on_close(self):
        self.stop_flag.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
