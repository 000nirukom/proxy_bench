#!/usr/bin/env python3
import subprocess
import time
import tempfile
import os
import sys
import yaml  # 需要 pip install pyyaml

import dotenv

dotenv.load_dotenv()

MIHOMO = "/usr/bin/mihomo"
OPENSSL = "openssl"
CURL = "curl"

BASE_CLIENT_PORT = 15000
BASE_SERVER_PORT = 20000

HTTP_SERVER_PORT = int(os.environ.get("HTTP_SERVER_PORT", 8089))

WORKDIR = tempfile.mkdtemp(prefix="mihomo-bench-")


def gen_password(byte_len):
    """生成指定字节数的 base64 密码"""
    cmd = [OPENSSL, "rand", "-base64", str(byte_len)]
    out = subprocess.check_output(cmd, text=True).strip()
    return out


def get_pwd_for_method(method):
    if method.startswith("2022-"):
        if "128" in method:
            return gen_password(16)
        else:
            return gen_password(32)
    else:
        return gen_password(32)


def write_cfg(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(obj, f, allow_unicode=True, sort_keys=False)


def start_mihomo(cfg_path, name=""):
    p = subprocess.Popen(
        [MIHOMO, "-f", cfg_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(2.0)

    if p.poll() is not None:
        time.sleep(0.5)
        stdout = p.stdout.read()
        stderr = p.stderr.read()
        print(f"{name} mihomo 启动失败 (return code {p.returncode}):")
        if stdout.strip():
            print("--- stdout ---")
            print(stdout)
        if stderr.strip():
            print("--- stderr ---")
            print(stderr)
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


def gen_ss_config(method, password, server_port, client_port):
    server = {
        "log-level": "error",
        "allow-lan": True,
        "mode": "direct",
        "listeners": [
            {
                "name": "ss-in",
                "type": "shadowsocks",
                "listen": "127.0.0.1",
                "port": server_port,
                "cipher": method,
                "password": password,
                "udp": True,
            }
        ],
    }

    client = {
        "mode": "rule",
        "mixed-port": 0,
        "port": 0,
        "socks-port": client_port,
        "allow-lan": False,
        "log-level": "error",
        "proxies": [
            {
                "name": "ss-out",
                "type": "ss",
                "server": "127.0.0.1",
                "port": server_port,
                "cipher": method,
                "password": password,
                "udp": True,
            }
        ],
        "proxy-groups": [
            {
                "name": "auto",
                "type": "select",
                "proxies": ["ss-out"],
            }
        ],
        "rules": [
            "MATCH,auto",
        ],
    }

    return server, client


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
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            text=True,
            env={},
        ).strip()
        speed_bps = float(out)
        return speed_bps / (1024 * 1024)
    except Exception as e:
        print("curl 失败:", e)
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
        print(f"\n=== {method} ===")
        password = get_pwd_for_method(method)

        server_cfg, client_cfg = gen_ss_config(method, password, sp, cp)

        s_path = os.path.join(WORKDIR, f"server-{method}.yaml")
        c_path = os.path.join(WORKDIR, f"client-{method}.yaml")

        write_cfg(server_cfg, s_path)
        write_cfg(client_cfg, c_path)

        srv = start_mihomo(s_path, "server")
        if not srv:
            results.append((method, None))
            sp += 2
            cp += 2
            continue

        cli = start_mihomo(c_path, "client")
        if not cli:
            terminate_process(srv)
            results.append((method, None))
            sp += 2
            cp += 2
            continue

        time.sleep(1.5)

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

        time.sleep(0.5)
        sp += 2
        cp += 2

    print("\n" + "=" * 50)
    print("                  测速总结")
    print("-" * 50)
    for method, speed in results:
        if speed is not None:
            gbps = speed * 8 / 1000
            print(f"{method:32} {speed:6.1f} MiB/s   {gbps:5.2f} Gbps")
        else:
            print(f"{method:32} FAILED")
    print("-" * 50)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(1)
    finally:
        try:
            import shutil

            shutil.rmtree(WORKDIR, ignore_errors=True)
        except Exception:
            pass
