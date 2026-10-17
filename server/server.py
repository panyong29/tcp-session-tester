"""TCP 会话数测试 - 服务端

asyncio 高并发 TCP echo 服务：
- TCP_PORT（默认 9000）：测试流量端口，收到什么原样返回
- STATS_PORT（默认 9001）：HTTP 状态接口，GET /stats 返回 JSON

docker run -d -p 9000:9000 -p 9001:9001 --ulimit nofile=65535:65535 \
  ghcr.io/panyong29/tcp-session-server:latest
curl http://127.0.0.1:9001/stats
"""
import asyncio
import json
import os
import time

TCP_PORT = int(os.environ.get("TCP_PORT", "9000"))
STATS_PORT = int(os.environ.get("STATS_PORT", "9001"))

stats = {
    "start_time": time.time(),
    "total_accepted": 0,   # 累计接受的连接数
    "active": 0,           # 当前活跃连接数
    "total_rx_bytes": 0,
    "total_tx_bytes": 0,
}


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    stats["total_accepted"] += 1
    stats["active"] += 1
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            stats["total_rx_bytes"] += len(data)
            writer.write(data)  # echo
            await writer.drain()
            stats["total_tx_bytes"] += len(data)
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        stats["active"] -= 1
        try:
            writer.close()
        except Exception:
            pass


async def handle_stats(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    except Exception:
        pass
    body = json.dumps({
        "uptime_sec": round(time.time() - stats["start_time"], 1),
        "total_accepted": stats["total_accepted"],
        "active": stats["active"],
        "total_rx_bytes": stats["total_rx_bytes"],
        "total_tx_bytes": stats["total_tx_bytes"],
    }).encode()
    resp = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() +
            b"\r\nConnection: close\r\n\r\n" + body)
    try:
        writer.write(resp)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def main():
    tcp_srv = await asyncio.start_server(handle_client, "0.0.0.0", TCP_PORT)
    stats_srv = await asyncio.start_server(handle_stats, "0.0.0.0", STATS_PORT)
    print(f"[server] TCP 测试端口 :{TCP_PORT}, 状态接口 :{STATS_PORT}/stats", flush=True)
    async with tcp_srv, stats_srv:
        await asyncio.gather(tcp_srv.serve_forever(), stats_srv.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
