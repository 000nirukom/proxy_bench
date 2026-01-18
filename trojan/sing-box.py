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

WORKDIR = tempfile.mkdtemp(prefix="sb-trojan-cert-bench-")

# ─── helpers ─────────────────────────────────────────────────────────


def run_openssl(cmd, capture_stderr=False):
    """執行 openssl 命令"""
    try:
        kwargs = {}
        if capture_stderr:
            kwargs["stderr"] = subprocess.STDOUT
        out = subprocess.check_output(cmd, text=True, **kwargs).strip()
        return out
    except subprocess.CalledProcessError as e:
        print("openssl 執行失敗:", e.output if e.output else str(e))
        return None


def generate_selfsigned_cert(key_type: str, domain="bench.local"):
    """
    生成自簽名證書
    key_type: "rsa4096" 或 "ed25519"
    """
    key_path = os.path.join(WORKDIR, f"server-{key_type}.key")
    cert_path = os.path.join(WORKDIR, f"server-{key_type}.crt")

    subj = f"/CN={domain}/O=benchmark/C=HK"

    if key_type == "rsa4096":
        # RSA 4096
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
        # Ed25519
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
        print(f"{key_type} 證書或私钥生成失敗")
        return None, None

    print(f"已生成 {key_type} 證書：{cert_path}")
    return cert_path, key_path


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

    if p.poll() is not None:
        err = p.stderr.read()
        print(f"{name} sing-box 啟動失敗:\n{err}")
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


def gen_trojan_config(password, server_port, client_port, cert_path, key_path):
    server = {
        "log": {"level": "error", "timestamp": False},
        "inbounds": [
            {
                "type": "trojan",
                "tag": "trojan-in",
                "listen": "127.0.0.1",
                "listen_port": server_port,
                "users": [{"password": password}],
                "tls": {
                    "enabled": True,
                    "server_name": "bench.local",
                    "certificate_path": cert_path,
                    "key_path": key_path,
                    "alpn": ["h2", "http/1.1"],
                },
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
                "type": "trojan",
                "tag": "trojan-out",
                "server": "127.0.0.1",
                "server_port": server_port,
                "password": password,
                "tls": {
                    "enabled": True,
                    "server_name": "bench.local",
                    "insecure": True,
                    "alpn": ["h2", "http/1.1"],
                },
            },
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"rules": [{"outbound": "trojan-out"}]},
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
        print("curl 失敗:", e)
        if hasattr(e, "output"):
            print(e.output)
        return None


def main():
    # 先生成兩組證書
    print("正在生成證書...")
    certs = {}

    for kt in ["rsa4096", "ed25519"]:
        cert_path, key_path = generate_selfsigned_cert(kt)
        if cert_path and key_path:
            certs[kt] = (cert_path, key_path)
        else:
            print(f"跳過 {kt} 測試（證書生成失敗）")

    if not certs:
        print("所有證書生成失敗，無法繼續測試")
        return

    # 隨機密碼（所有測試共用）
    password = (
        "trojan-"
        + subprocess.check_output([OPENSSL, "rand", "-hex", "12"], text=True).strip()
    )

    tests = [
        ("rsa4096", "trojan-tls-rsa4096"),
        ("ed25519", "trojan-tls-ed25519"),
    ]

    results = []
    sp = BASE_SERVER_PORT
    cp = BASE_CLIENT_PORT

    # 無代理基準
    print("\n=== no-proxy ===")
    speed_mib = run_curl(None)
    if speed_mib is not None:
        gbps = speed_mib * 8 / 1000
        print(f"  {speed_mib:6.1f} MiB/s   ≈ {gbps:5.2f} Gbps")
        results.append(("no-proxy", speed_mib))
    else:
        print("  FAILED")
        results.append(("no-proxy", None))

    # 逐一測試不同簽名算法
    for key_type, test_name in tests:
        if key_type not in certs:
            print(f"=== {test_name} ===  (跳過 - 證書不可用)")
            results.append((test_name, None))
            sp += 2
            cp += 2
            continue

        cert_path, key_path = certs[key_type]
        print(f"\n=== {test_name}  (使用 {key_type} 簽名) ===")

        server_cfg, client_cfg = gen_trojan_config(
            password, sp, cp, cert_path, key_path
        )

        s_path = os.path.join(WORKDIR, f"server-{test_name}.json")
        c_path = os.path.join(WORKDIR, f"client-{test_name}.json")

        write_cfg(server_cfg, s_path)
        write_cfg(client_cfg, c_path)

        srv = start_singbox(s_path, "server")
        if not srv:
            results.append((test_name, None))
            sp += 2
            cp += 2
            continue

        cli = start_singbox(c_path, "client")
        if not cli:
            terminate_process(srv)
            results.append((test_name, None))
            sp += 2
            cp += 2
            continue

        time.sleep(1.3)

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

        time.sleep(0.5)  # 端口釋放緩衝
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
        print("\n已中斷。")
        sys.exit(1)
    finally:
        try:
            import shutil

            shutil.rmtree(WORKDIR, ignore_errors=True)
        except Exception:
            pass
