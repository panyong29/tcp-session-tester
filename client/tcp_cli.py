#!/usr/bin/env python3
"""TCP 连接数探测工具 - 命令行版（macOS / Linux）

给没有图形界面的 macOS / Linux 用：clone 下来就能直接跑，
不需要 pip 装任何东西，不需要 tkinter，不需要打包。

用法示例：
    # 直接跑源码（推荐，clone 后就能用）
    python3 client/tcp_cli.py --host 127.0.0.1 --port 9000

    # 用预设目标
    python3 client/tcp_cli.py --preset tencent
    python3 client/tcp_cli.py --preset baidu

    # 改参数：10 线程、间隔 100ms、最多探 5000 个、连续失败 20 次停
    python3 client/tcp_cli.py --host 127.0.0.1 --port 9000 \\
        --threads 10 --interval-ms 100 --max-probe 5000 --max-fail 20

    # 测 60 秒就停，并把结果写进 CSV
    python3 client/tcp_cli.py --preset tencent --duration 60 --csv result.csv

    # 看有哪些预设
    python3 client/tcp_cli.py --list-presets

实时刷新：探测过程中每 200ms（可用 --refresh-ms 改）在终端
原地刷新一行/一组数据：已尝试、成功、失败、当前保持、失败分类、
用时、平均建连速率。按 Ctrl+C 随时停止，停止会立即释放全部
连接（v2.3 起的逻辑：SO_LINGER 0 发 RST，不进 TIME_WAIT），
然后打印最终汇总。非终端输出（例如重定向到文件）时自动改成
每刷新一次打印一行，不用 ANSI 清屏。

本脚本是独立文件，不 import 图形版 tcp_client.py，所以
没有 tkinter 的机器也能跑；图形版 tcp_client.py 本次未改动。
"""

import argparse
import csv
import os
import signal
import socket
import struct
import sys
import threading
import time

VERSION = "2.3"

# 预设目标：(命令行名字, 显示名, host, port)，与图形版保持一致
PRESETS = {
    "ali": ("阿里DNS", "223.6.6.6", 53),
    "google": ("Google DNS", "8.8.8.8", 53),
    "cloudflare": ("Cloudflare DNS", "1.1.1.1", 53),
    "tencent": ("腾讯DNS", "119.29.29.29", 53),
    "baidu": ("百度", "www.baidu.com", 80),
}


class Prober:
    """探测器：多线程建连并保持。逻辑与图形版 v2.3 一致。"""

    def __init__(self, host, port, interval_ms, thread_count,
                 max_probe, max_fail, timeout, duration=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.interval = interval_ms / 1000.0
        self.thread_count = thread_count
        self.max_probe = max_probe
        self.max_fail = max_fail
        self.duration = duration
        self.lock = threading.Lock()
        self.sockets = []  # 保持住的连接
        self.attempted = 0
        self.success = 0
        self.failed = 0
        self.consec_failed = 0
        self.fail_timeout = 0
        self.fail_refused = 0
        self.fail_other = 0
        self.stop_flag = threading.Event()
        self.threads = []
        self.t_start = 0
        self.stop_reason = ""

    @staticmethod
    def _close_socket(s):
        """立即释放一个连接：SO_LINGER 0 发 RST，不进 TIME_WAIT。"""
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                         struct.pack("ii", 1, 0))
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass

    def close_all(self):
        """当场关掉全部已建连接，返回释放数。线程安全，可重复调。"""
        with self.lock:
            sockets, self.sockets = self.sockets, []
        for s in sockets:
            self._close_socket(s)
        return len(sockets)

    def snapshot(self):
        with self.lock:
            return {
                "attempted": self.attempted, "success": self.success,
                "failed": self.failed, "active": len(self.sockets),
                "fail_timeout": self.fail_timeout,
                "fail_refused": self.fail_refused,
                "fail_other": self.fail_other,
            }

    def _worker(self):
        while not self.stop_flag.is_set():
            if self.duration is not None and time.time() - self.t_start >= self.duration:
                self.stop_flag.set()
                self.stop_reason = f"达到测定时长 {self.duration} 秒"
                self.close_all()
                break
            auto_stop = False
            reason = ""
            with self.lock:
                if self.attempted >= self.max_probe:
                    self.stop_flag.set()
                    auto_stop, reason = True, f"达到探测上限 {self.max_probe}"
                elif self.consec_failed >= self.max_fail:
                    self.stop_flag.set()
                    auto_stop, reason = True, f"连续失败 {self.max_fail} 次"
                else:
                    self.attempted += 1
            if auto_stop:
                self.stop_reason = reason
                self.close_all()
                break
            try:
                s = socket.create_connection((self.host, self.port),
                                             timeout=self.timeout)
            except (socket.timeout, TimeoutError):
                kind = "timeout"
            except ConnectionRefusedError:
                kind = "refused"
            except Exception:
                kind = "other"
            else:
                kind = None
            to_close = None
            auto_stop = False
            reason = ""
            with self.lock:
                if kind is None:
                    self.success += 1
                    self.consec_failed = 0
                    if self.stop_flag.is_set():
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
                        auto_stop, reason = True, f"连续失败 {self.max_fail} 次"
            if to_close is not None:
                self._close_socket(to_close)
            if auto_stop:
                self.stop_reason = reason
                self.close_all()
            if self.interval > 0:
                self.stop_flag.wait(self.interval)

    def run(self):
        self.t_start = time.time()
        self.threads = [threading.Thread(target=self._worker, daemon=True)
                        for _ in range(self.thread_count)]
        for t in self.threads:
            t.start()
        for t in self.threads:
            t.join()
        released = self.close_all()
        return released

    def stop(self, reason="手动停止（Ctrl+C）"):
        """停止探测并当场释放全部已建连接，返回释放数。"""
        if not self.stop_reason:
            self.stop_reason = reason
        self.stop_flag.set()
        return self.close_all()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="TCP 连接数探测工具 - 命令行版（macOS / Linux，clone 后直接跑）")
    p.add_argument("--host", default="", help="服务器地址，例如 127.0.0.1")
    p.add_argument("--port", type=int, default=0, help="端口，例如 9000")
    p.add_argument("--preset", choices=sorted(PRESETS),
                   help="预设目标：ali / google / cloudflare / tencent / baidu，"
                        "不填 --host/--port 时用预设的地址端口")
    p.add_argument("--list-presets", action="store_true", help="列出全部预设并退出")
    p.add_argument("--interval-ms", type=int, default=100,
                   help="每次建连尝试之间的间隔（毫秒），默认 100")
    p.add_argument("--threads", type=int, default=10, help="线程数，默认 10")
    p.add_argument("--max-probe", type=int, default=10000,
                   help="探测上限：最多尝试多少个连接，默认 10000")
    p.add_argument("--max-fail", type=int, default=100,
                   help="连续失败几次自动停止，默认 100")
    p.add_argument("--timeout", type=int, default=5, help="单次建连超时（秒），默认 5")
    p.add_argument("--duration", type=int, default=0,
                   help="测定时长（秒），0 表示不限时，默认 0")
    p.add_argument("--refresh-ms", type=int, default=200,
                   help="终端实时刷新间隔（毫秒），默认 200")
    p.add_argument("--csv", default="", help="结果 CSV 路径，不填则不写文件")
    args = p.parse_args(argv)

    if args.list_presets:
        for key in sorted(PRESETS):
            name, host, port = PRESETS[key]
            print(f"{key:12s} {name} {host}:{port}")
        sys.exit(0)

    if args.preset and not args.host:
        _name, args.host, preset_port = PRESETS[args.preset]
        if not args.port:
            args.port = preset_port
    if not args.host:
        p.error("请填 --host，或用 --preset 选一个预设目标")
    if not 1 <= args.port <= 65535:
        p.error("端口范围 1~65535")
    if not 0 <= args.interval_ms <= 60000:
        p.error("时间间隔范围 0~60000ms")
    if not 1 <= args.threads <= 200:
        p.error("线程数范围 1~200")
    if not 1 <= args.max_probe <= 1000000:
        p.error("探测上限范围 1~1000000")
    if not 1 <= args.max_fail <= 1000000:
        p.error("连续失败次数范围 1~1000000")
    if not 1 <= args.timeout <= 60:
        p.error("连接超时范围 1~60 秒")
    if args.duration < 0:
        p.error("测定时长不能为负")
    if args.refresh_ms <= 0:
        p.error("刷新间隔必须大于 0")
    return args


def dashboard_lines(host, port, snap, elapsed):
    rate = snap["attempted"] / elapsed if elapsed > 0 else 0
    return [
        f"目标: {host}:{port}",
        f"已尝试: {snap['attempted']}  成功: {snap['success']}  "
        f"失败: {snap['failed']}  当前保持: {snap['active']}",
        f"失败分类: 超时 {snap['fail_timeout']} / 拒绝 {snap['fail_refused']} / "
        f"其他 {snap['fail_other']}  用时: {elapsed:.1f}s  "
        f"速率: {rate:.0f} 次/秒",
    ]


def main(argv=None):
    args = parse_args(argv)
    duration = args.duration if args.duration > 0 else None
    prober = Prober(args.host, args.port, args.interval_ms, args.threads,
                    args.max_probe, args.max_fail, args.timeout, duration)
    print(f"TCP 连接数探测工具（命令行版）v{VERSION}")
    print(f"目标: {args.host}:{args.port}  线程: {args.threads}  "
          f"间隔: {args.interval_ms}ms  探测上限: {args.max_probe}  "
          f"连续失败 {args.max_fail} 次停  超时: {args.timeout}s"
          + (f"  时长: {args.duration}s" if duration else ""))
    print("按 Ctrl+C 随时停止，停止会立即释放全部连接。")

    # Ctrl+C / kill：主线程收到信号就调 stop()，不在信号处理里做重活。
    def _on_signal(_signum, _frame):
        prober.stop()

    signal.signal(signal.SIGINT, _on_signal)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError):
        pass

    run_thread = threading.Thread(target=prober.run, daemon=True)
    run_thread.start()

    is_tty = sys.stdout.isatty()
    refresh = args.refresh_ms / 1000.0
    printed_lines = 0
    try:
        while run_thread.is_alive():
            snap = prober.snapshot()
            elapsed = time.time() - prober.t_start if prober.t_start else 0
            lines = dashboard_lines(args.host, args.port, snap, elapsed)
            if is_tty:
                if printed_lines:
                    sys.stdout.write(f"\033[{printed_lines}A")
                for line in lines:
                    sys.stdout.write("\033[K" + line + "\n")
                sys.stdout.flush()
                printed_lines = len(lines)
            else:
                print(" | ".join(lines))
            run_thread.join(timeout=refresh)
    except KeyboardInterrupt:
        prober.stop()

    # 兜底：如果 run 线程还没结束（例如在途建连等超时），等它收尾，
    # 但连接在 stop() 那一刻已经放了，不用等收尾才算释放。
    run_thread.join()
    snap = prober.snapshot()
    elapsed = time.time() - prober.t_start if prober.t_start else 0
    if is_tty and printed_lines:
        sys.stdout.write("\n")
    print(f"探测结束，用时 {elapsed:.1f}s：成功 {snap['success']}，"
          f"失败 {snap['failed']}，共尝试 {snap['attempted']}")
    print(f"失败分类：超时 {snap['fail_timeout']} / 拒绝 {snap['fail_refused']} / "
          f"其他 {snap['fail_other']}")
    if prober.stop_reason:
        print(f"停止原因：{prober.stop_reason}")
    print(f"结论：目标最大并发连接数约 {snap['success']}")
    print("连接已全部释放")

    if args.csv:
        try:
            with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["目标", f"{args.host}:{args.port}"])
                w.writerow(["时间间隔(ms)", args.interval_ms])
                w.writerow(["线程数", args.threads])
                w.writerow(["探测上限", args.max_probe])
                w.writerow(["连续失败停止", args.max_fail])
                w.writerow(["连接超时(秒)", args.timeout])
                w.writerow(["成功", snap["success"]])
                w.writerow(["失败", snap["failed"]])
                w.writerow(["超时失败", snap["fail_timeout"]])
                w.writerow(["拒绝失败", snap["fail_refused"]])
                w.writerow(["其他失败", snap["fail_other"]])
                w.writerow(["共尝试", snap["attempted"]])
                w.writerow(["用时(秒)", round(elapsed, 1)])
            print(f"结果已导出: {os.path.abspath(args.csv)}")
        except OSError as e:
            print(f"CSV 导出失败: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
