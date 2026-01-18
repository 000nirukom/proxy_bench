#!/usr/bin/env python3
import json
import subprocess
import time
import tempfile
import os
import sys

import dotenv

dotenv.load_dotenv()

SINGBOX = "sing-box"
OPENSSL = "openssl"
CURL = "curl"

BASE_CLIENT_PORT = 15000
BASE_SERVER_PORT = 20000

HTTP_SERVER_PORT = int(os.environ.get("HTTP_SERVER_PORT", 8089))

WORKDIR = tempfile.mkdtemp(prefix="sb-bench-")

# ─── helpers ─────────────────────────────────────────────────────────


def gen_password(bits=256):
    byte_len = bits // 8
    cmd = [OPENSSL, "rand", "-base64", str(byte_len)]
    out = subprocess.check_output(cmd, text=True).strip()
    return out


def get_pwd_for_method(method):
    if "128" in method:
        return gen_password(128)
    return gen_password(256)


def write_cfg(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def start_singbox(cfg_path, name=""):
    p = subprocess.Popen(
        [SINGBOX, "run", "-c", cfg_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.8)

    # 快速检查是否启动成功
    if p.poll() is not None:
        err = p.stderr.read()
        print(f"{name} sing-box 启动失败:\n{err}")
        return None
    return p


def terminate_process(p):
    if not p:
        return
    try:
        p.terminate()
        p.wait(timeout=4)
    except Exception:
        pass
    finally:
        try:
            p.kill()
        except Exception:
            pass


# ─── config generators ───────────────────────────────────────────────


def gen_ss_config(method, password, server_port, client_port):
    # 2024+ 版本统一使用 shadowsocks 类型
    # ss2022 只是 method 名称不同

    server = {
        "log": {"level": "error", "timestamp": False},
        "inbounds": [
            {
                "type": "shadowsocks",
                "tag": "ss-in",
                "listen": "127.0.0.1",
                "listen_port": server_port,
                "method": method,
                "password": password,
            }
        ],
        "outbounds": [{"type": "direct", "tag": "direct"}],
    }

    client = {
        "log": {"level": "error", "timestamp": False},
        "inbounds": [
            {
                "type": "socks",
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "listen_port": client_port,
            }
        ],
        "outbounds": [
            {
                "type": "shadowsocks",
                "tag": "ss-out",
                "server": "127.0.0.1",
                "server_port": server_port,
                "method": method,
                "password": password,
            },
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"rules": [{"outbound": "ss-out"}]},
    }

    return server, client


# ─── benchmark ───────────────────────────────────────────────────────


def run_curl(client_port: int | None):
    cmd = [
        CURL,
        "--silent",
        "--show-error",
        "-o",
        "/dev/null" if os.name == "posix" else "NUL",
        f"http://127.0.0.1:{HTTP_SERVER_PORT}/bench",
        "-w",
        "%{speed_download}\\n",
    ]
    if client_port is not None:
        cmd.extend(["-x", f"socks5h://127.0.0.1:{client_port}"])

    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()
        speed_bps = float(out)
        return speed_bps / (1024 * 1024)
    except Exception as e:
        print("curl failed:", e)
        if hasattr(e, "output"):
            print(e.output)
        return None


def main():
    tests = [
        ("none",),
        ("aes-128-gcm",),
        ("aes-256-gcm",),
        ("chacha20-ietf-poly1305",),
        ("2022-blake3-aes-128-gcm",),
        ("2022-blake3-aes-256-gcm",),
        ("2022-blake3-chacha20-poly1305",),
    ]

    results = []
    sp = BASE_SERVER_PORT
    cp = BASE_CLIENT_PORT

    print("=== no-proxy ===")
    speed_mib = run_curl(None)
    if speed_mib is not None:
        gbps = speed_mib * 8 / 1000
        print(f"  {speed_mib:6.1f} MiB/s   ≈ {gbps:5.2f} Gbps")
        results.append(("no-proxy", speed_mib))
    else:
        print("  FAILED")
        results.append(("no-proxy", None))

    for (method,) in tests:
        print(f"=== {method} ===")
        password = get_pwd_for_method(method)

        server_cfg, client_cfg = gen_ss_config(method, password, sp, cp)

        s_path = os.path.join(WORKDIR, f"server-{method}.json")
        c_path = os.path.join(WORKDIR, f"client-{method}.json")

        write_cfg(server_cfg, s_path)
        write_cfg(client_cfg, c_path)

        srv = start_singbox(s_path, "server")
        if not srv:
            results.append((method, None))
            sp += 1
            cp += 1
            continue

        cli = start_singbox(c_path, "client")
        if not cli:
            terminate_process(srv)
            results.append((method, None))
            sp += 1
            cp += 1
            continue

        time.sleep(1.2)

        speed_mib = run_curl(cp)
        if speed_mib is not None:
            gbps = speed_mib * 8 / 1000
            print(f"  {speed_mib:6.1f} MiB/s   ≈ {gbps:5.2f} Gbps")
            results.append((method, speed_mib))
        else:
            print("  FAILED")
            results.append((method, None))

        terminate_process(cli)
        terminate_process(srv)

        time.sleep(0.4)  # 给端口释放一点时间
        sp += 2
        cp += 2

    print("\n" + "=" * 40)
    print("           SUMMARY")
    print("-" * 40)
    for method, speed in results:
        if speed is not None:
            gbps = speed * 8 / 1000
            print(f"{method:32} {speed:6.1f} MiB/s  {gbps:5.2f} Gbps")
        else:
            print(f"{method:32} FAILED")

    print("-" * 40)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
    finally:
        try:
            import shutil

            shutil.rmtree(WORKDIR, ignore_errors=True)
        except Exception:
            pass
