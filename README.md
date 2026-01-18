# proxy-bench

Bench shadowsocks/shadowsocks2022 protocols performance.

Server side: compio based, written in Rust for best performance.

## Run server

```bash
cargo run --release --locked
```

## Run client

```bash
uv sync --all-extras

uv run shadowsocks/mihomo.py
uv run shadowsocks/sing-box.py
uv run shadowsocks/shadowsocks-rust.py
```
