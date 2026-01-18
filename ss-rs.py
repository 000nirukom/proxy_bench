#!/usr/bin/env python3
import subprocess
import time
import tempfile
import os
import sys

import dotenv

dotenv.load_dotenv()

SSSERVER = "ssserver"
SSLOCAL = "sslocal"
OPENSSL = "openssl"
CURL = "curl"

BASE_CLIENT_PORT = 15000
BASE_SERVER_PORT = 20000

HTTP_SERVER_PORT = int(os.environ.get("HTTP_SERVER_PORT", 8089))

WORKDIR = tempfile.mkdtemp(prefix="ssrust-bench-")

# ─── helpers ─────────────────────────────────────────────────────────


def gen_password(bits=256):
    byte_len = bits // 8
    cmd = [OPENSSL, "rand", "-base64", str(byte_len)]
    return subprocess.check_output(cmd, text=True).strip()


def get_pwd_for_method(method):
    if "128" in method:
        return gen_password(128)
    return gen_password(256)


def start_proc(cmd, name=""):
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.8)

    if p.poll() is not None:
        err = p.stderr.read()
        print(f"{name} 启动失败:\n{err}")
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
        "%{speed_download}\n",
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

        # ---- start ssserver ----
        server_cmd = [
            SSSERVER,
            "-s",
            "127.0.0.1:{}".format(sp),
            "-m",
            method,
            "-k",
            password,
        ]

        srv = start_proc(server_cmd, "ssserver")
        if not srv:
            results.append((method, None))
            sp += 1
            cp += 1
            continue

        # ---- start sslocal ----
        client_cmd = [
            SSLOCAL,
            "-b",
            "127.0.0.1:{}".format(cp),
            "-s",
            "127.0.0.1:{}".format(sp),
            "-m",
            method,
            "-k",
            password,
        ]

        cli = start_proc(client_cmd, "sslocal")
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

        time.sleep(0.4)
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
