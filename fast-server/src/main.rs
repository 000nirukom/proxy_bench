use std::sync::Arc;
use std::sync::atomic::AtomicUsize;
use std::{env, sync::atomic::Ordering};

use compio::{
    BufResult,
    io::{AsyncRead, AsyncWriteExt as _},
};

static MAX_SEND_BYTES: AtomicUsize = AtomicUsize::new(1024 * 1024 * 1024 * 32); // 32 GiB

#[compio::main]
async fn main() -> anyhow::Result<()> {
    dotenvy::dotenv()?;

    // 从环境变量读取端口，默认 8080
    let port: u16 = env::var("HTTP_SERVER_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8089);

    if let Some(bytes) = env::var("MAX_SEND_BYTES")
        .ok()
        .and_then(|b| b.parse::<usize>().ok())
    {
        MAX_SEND_BYTES.store(bytes, Ordering::Release);
    }

    let listener = compio::net::TcpListener::bind(("127.0.0.1", port)).await?;
    println!("HTTP server running on 127.0.0.1:{}", port);

    let listener = Arc::new(listener);

    loop {
        let (stream, _) = listener.accept().await?;
        compio::runtime::spawn(handle_client(stream)).detach();
    }
}

async fn handle_client(mut stream: compio::net::TcpStream) -> anyhow::Result<()> {
    let buf = vec![0; 4096];
    let result = stream.read(buf).await;
    if result.0? == 0 {
        return Ok(());
    }

    let headers = "HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nContent-Type: application/octet-stream\r\nTransfer-Encoding: chunked\r\n\r\n";
    stream.write_all(headers.as_bytes()).await.0?;
    _ = stream.set_nodelay(true);

    let mut sent_count = 0;

    let max_bytes = MAX_SEND_BYTES.load(Ordering::Acquire);
    while sent_count < max_bytes {
        // 1 MiB chunk
        stream.write_all(b"100000\r\n").await.0?;
        let mut chunk = vec![0u8; 1024 * 1024];
        let BufResult(result, c) = stream.write_all(chunk).await;

        chunk = c;
        _ = stream.write_all(b"\r\n").await.0;

        match result {
            Ok(_) => sent_count += chunk.len(),
            Err(_) => {
                stream.close().await?;
                return Ok(());
            }
        }
    }

    stream.write_all(b"0\r\n\r\n").await.0?;

    Ok(())
}
