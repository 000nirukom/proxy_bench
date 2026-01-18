#!/usr/bin/env python3
import subprocess
import time
import tempfile
import os
import sys
import shutil

import dotenv

dotenv.load_dotenv()

ANYTLS_SERVER = "anytls-server"
ANYTLS_CLIENT = "anytls-client"
OPENSSL = "openssl"
CURL = "curl"

BASE_CLIENT_PORT = 15000
BASE_SERVER_PORT = 20000

HTTP_SERVER_PORT = int(os.environ.get("HTTP_SERVER_PORT", 8089))

WORKDIR = tempfile.mkdtemp(prefix="anytls-cert-bench-")

# ─── helpers ─────────────────────────────────────────────────────────


def run_openssl(cmd, capture_stderr=False):
    try:
        kwargs = {}
        if capture_stderr:
            kwargs["stderr"] = subprocess.STDOUT
        out = subprocess.check_output(cmd, text=True, **kwargs).strip()
        return out
    except subprocess.CalledProcessError as e:
        print("openssl 失败:", e.output if e.output else str(e))
        return None


def generate_selfsigned_cert(key_type: str, domain="bench.local"):
    """
    生成自签名证书
    key_type: "rsa4096" or "ed25519"
    """
    key_path = os.path.join(WORKDIR, f"server-{key_type}.key")
    cert_path = os.path.join(WORKDIR, f"server-{key_type}.crt")

    subj = f"/CN={domain}/O=benchmark/C=HK"

    if key_type == "rsa4096":
        run_openssl([OPENSSL, "genrsa", "-out", key_path, "4096"])
        run_openssl(
            [
                OPENSSL,
                "req",
                "-x509",
                "-new",
                "-nodes",
                "-key",
                key_path,
                "-sha256",
                "-days",
                "3650",
                "-out",
                cert_path,
                "-subj",
                subj,
            ]
        )
    elif key_type == "ed25519":
        run_openssl([OPENSSL, "genpkey", "-algorithm", "ed25519", "-out", key_path])
        run_openssl(
            [
                OPENSSL,
                "req",
                "-x509",
                "-new",
                "-nodes",
                "-key",
                key_path,
                "-days",
                "3650",
                "-out",
                cert_path,
                "-subj",
                subj,
            ]
        )
    else:
        raise ValueError("不支持的 key_type")

    if not (os.path.exists(key_path) and os.path.exists(cert_path)):
        print(f"{key_type} 证书生成失败")
        return None, None

    print(f"已生成 {key_type} 证书：{cert_path}")
    return cert_path, key_path


def start_anytls_server(port, password, cert, key):
    cmd = [
        ANYTLS_SERVER,
        "-l",
        f"127.0.0.1:{port}",
        "-p",
        password,
        "--cert",
        cert,
        "--key",
        key,
        "-L",
        "error",
        "-M",
        "1",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_anytls_client(local_port, server_port, password):
    cmd = [
        ANYTLS_CLIENT,
        "-l",
        f"127.0.0.1:{local_port}",
        "-s",
        f"127.0.0.1:{server_port}",
        "-p",
        password,
        "-L",
        "error",
        "-M",
        "1",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def terminate_process(p):
    if not p:
        return
    try:
        p.terminate()
        p.wait(timeout=3)
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
        print("curl 失败:", e)
        if hasattr(e, "output"):
            print(e.output)
        return None


# ─── main ────────────────────────────────────────────────────────────


def main():
    print("工作目录:", WORKDIR)

    print("正在生成证书...")
    certs = {}

    for kt in ["rsa4096", "ed25519"]:
        cert_path, key_path = generate_selfsigned_cert(kt)
        if cert_path and key_path:
            certs[kt] = (cert_path, key_path)

    if not certs:
        print("证书生成全部失败，退出")
        return

    password = (
        "anytls-"
        + subprocess.check_output([OPENSSL, "rand", "-hex", "12"], text=True).strip()
    )

    tests = [
        ("rsa4096", "anytls-rsa4096"),
        ("ed25519", "anytls-ed25519"),
    ]

    results = []

    sp = BASE_SERVER_PORT
    cp = BASE_CLIENT_PORT

    # 无代理基准
    print("\n=== no-proxy ===")
    speed_mib = run_curl(None)
    if speed_mib is not None:
        gbps = speed_mib * 8 / 1000
        print(f"  {speed_mib:6.1f} MiB/s   ≈ {gbps:5.2f} Gbps")
        results.append(("no-proxy", speed_mib))
    else:
        print("  FAILED")
        results.append(("no-proxy", None))

    for key_type, test_name in tests:
        if key_type not in certs:
            print(f"=== {test_name} 跳过 ===")
            results.append((test_name, None))
            continue

        cert_path, key_path = certs[key_type]

        print(f"\n=== {test_name} ({key_type}) ===")

        srv = start_anytls_server(sp, password, cert_path, key_path)
        time.sleep(0.8)

        cli = start_anytls_client(cp, sp, password)
        time.sleep(1.0)

        speed_mib = run_curl(cp)

        if speed_mib is not None:
            gbps = speed_mib * 8 / 1000
            print(f"  {speed_mib:6.1f} MiB/s   ≈ {gbps:5.2f} Gbps")
            results.append((test_name, speed_mib))
        else:
            print("  FAILED")
            results.append((test_name, None))

        terminate_process(cli)
        terminate_process(srv)

        time.sleep(0.5)
        sp += 2
        cp += 2

    print("\n" + "=" * 50)
    print("                 SUMMARY                  ")
    print("-" * 50)
    for name, speed in results:
        if speed is not None:
            gbps = speed * 8 / 1000
            print(f"{name:36} {speed:6.1f} MiB/s   {gbps:5.2f} Gbps")
        else:
            print(f"{name:36} FAILED")
    print("-" * 50)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(1)
    finally:
        try:
            shutil.rmtree(WORKDIR, ignore_errors=True)
        except Exception:
            pass
