//! Implements deterministic local file reads, writes, and listings.

use std::cmp::Ordering;
use std::fs;
use std::future::Future;
use std::io;
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::AbortSignal;

use super::{
    change_hashes, content_hash, error_result, integer_param, object, resolve_path, short_tag,
    string_param,
};

const DEFAULT_READ_LIMIT: usize = 2_000;
const MAX_READ_CHARS: usize = 50_000;
const BINARY_SNIFF_BYTES: usize = 8_192;
const WEB_READ_TIMEOUT: Duration = Duration::from_secs(30);

/// Carries one HTTP operation without requiring an async runtime.
pub type HttpFuture<'a, T> = Pin<Box<dyn Future<Output = io::Result<T>> + 'a>>;

/// Carries bytes and media type returned by an injected URL reader.
///
/// # Examples
///
/// ```
/// let response = zeta_agent::HttpResponse::new(
///     b"hello".to_vec(),
///     "text/plain".to_owned(),
/// );
/// assert_eq!(response.body, b"hello");
/// ```
pub struct HttpResponse {
    /// Carries the response entity bytes.
    pub body: Vec<u8>,
    /// Carries the response content type.
    pub content_type: String,
}

impl HttpResponse {
    /// Creates one response from captured entity data.
    ///
    /// # Examples
    ///
    /// ```
    /// let response = zeta_agent::HttpResponse::new(Vec::new(), String::new());
    /// assert!(response.body.is_empty());
    /// ```
    pub fn new(body: Vec<u8>, content_type: String) -> Self {
        HttpResponse { body, content_type }
    }
}

/// Supplies DNS resolution and redirect-free HTTP fetches to URL reads.
///
/// The read boundary validates every supplied address before calling
/// [`HttpTransport::fetch`]. Implementations must connect only to one of those
/// addresses and must not follow redirects implicitly.
///
/// # Examples
///
/// Custom transports can resolve and fetch through application-owned clients.
pub trait HttpTransport {
    /// Resolves every address for one URL host.
    fn resolve_host<'a>(
        &'a mut self,
        host: &'a str,
        abort: &'a dyn AbortSignal,
    ) -> HttpFuture<'a, Vec<IpAddr>>;

    /// Fetches one URL using only the already validated addresses.
    fn fetch<'a>(
        &'a mut self,
        url: &'a str,
        addresses: &'a [IpAddr],
        timeout: Duration,
        abort: &'a dyn AbortSignal,
    ) -> HttpFuture<'a, HttpResponse>;
}

/// Rejects URL reads until an application injects an HTTP transport.
///
/// # Examples
///
/// ```
/// let transport = zeta_agent::UnavailableHttpTransport;
/// let _ = transport;
/// ```
pub struct UnavailableHttpTransport;

impl HttpTransport for UnavailableHttpTransport {
    fn resolve_host<'a>(
        &'a mut self,
        _host: &'a str,
        _abort: &'a dyn AbortSignal,
    ) -> HttpFuture<'a, Vec<IpAddr>> {
        Box::pin(async { Err(io::Error::other("no HTTP transport is configured")) })
    }

    fn fetch<'a>(
        &'a mut self,
        _url: &'a str,
        _addresses: &'a [IpAddr],
        _timeout: Duration,
        _abort: &'a dyn AbortSignal,
    ) -> HttpFuture<'a, HttpResponse> {
        Box::pin(async { Err(io::Error::other("no HTTP transport is configured")) })
    }
}

pub(super) async fn read<H: HttpTransport>(
    params: &Map<String, Value>,
    base_directory: &Path,
    http: &mut H,
    abort: &dyn AbortSignal,
) -> Map<String, Value> {
    let path_value = string_param(params, "path");
    if path_value.starts_with("http://") || path_value.starts_with("https://") {
        return read_url(params, &path_value, http, abort).await;
    }
    let offset = integer_param(params, "offset", 0);
    let limit = integer_param(params, "limit", DEFAULT_READ_LIMIT);
    let path = resolve_path(&path_value, base_directory);
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) => return error_result("read-failed", error.to_string()),
    };
    let sniff_end = bytes.len().min(BINARY_SNIFF_BYTES);
    if bytes[..sniff_end].contains(&0) {
        return error_result(
            "binary-file",
            "file looks binary; read supports UTF-8 text only",
        );
    }
    let hash = content_hash(&bytes);
    let tag = short_tag(&hash);
    let text = String::from_utf8_lossy(&bytes);
    let lines = lines_with_endings(&text);
    let start = offset.min(lines.len());
    let end = start.saturating_add(limit).min(lines.len());
    let selected = &lines[start..end];
    let mut content = format!("[{}#{tag}]\n", path.display());
    for (index, line) in selected.iter().enumerate() {
        content.push_str(&format!("{}:{line}", offset + index + 1));
    }
    let (content, truncated) = truncate_chars(&content, MAX_READ_CHARS);
    let line_start = if selected.is_empty() {
        Value::Null
    } else {
        json!(offset + 1)
    };
    let line_end = if selected.is_empty() {
        Value::Null
    } else {
        json!(offset + selected.len())
    };
    object(json!({
        "ok": true,
        "content": [{"type": "text", "text": content}],
        "metadata": {
            "path": path.display().to_string(),
            "offset": offset,
            "limit": limit,
            "truncated": truncated,
            "content_hash": hash,
            "tag": tag,
            "line_start": line_start,
            "line_end": line_end,
        },
    }))
}

async fn read_url<H: HttpTransport>(
    params: &Map<String, Value>,
    url: &str,
    http: &mut H,
    abort: &dyn AbortSignal,
) -> Map<String, Value> {
    let Some(host) = url_host(url) else {
        return error_result("web-read-blocked", "URL has no host");
    };
    let addresses = match http.resolve_host(&host, abort).await {
        Ok(addresses) => addresses,
        Err(error) => {
            return error_result(
                "web-read-blocked",
                format!("could not resolve host: {error}"),
            )
        }
    };
    for address in &addresses {
        if !is_public_address(*address) {
            return error_result(
                "web-read-blocked",
                format!("URL host resolves to non-public address {address}"),
            );
        }
    }
    let response = match http.fetch(url, &addresses, WEB_READ_TIMEOUT, abort).await {
        Ok(response) => response,
        Err(error) => return error_result("web-read-failed", error.to_string()),
    };
    let sniff_end = response.body.len().min(BINARY_SNIFF_BYTES);
    if response.body[..sniff_end].contains(&0) {
        return error_result(
            "binary-url",
            "URL looks binary; read supports UTF-8 text and simple HTML only",
        );
    }
    let hash = content_hash(&response.body);
    let tag = short_tag(&hash);
    let mut text = String::from_utf8_lossy(&response.body).into_owned();
    if response.content_type.to_lowercase().contains("html") || looks_like_html(&text) {
        text = html_to_text(&text);
    }
    let offset = integer_param(params, "offset", 0);
    let limit = integer_param(params, "limit", DEFAULT_READ_LIMIT);
    let lines = lines_with_endings(&text);
    let start = offset.min(lines.len());
    let end = start.saturating_add(limit).min(lines.len());
    let selected = &lines[start..end];
    let mut content = format!("[{url}#{tag}]\n");
    for (index, line) in selected.iter().enumerate() {
        content.push_str(&format!("{}:{line}", offset + index + 1));
    }
    let (content, truncated) = truncate_chars(&content, MAX_READ_CHARS);
    let line_start = if selected.is_empty() {
        Value::Null
    } else {
        json!(offset + 1)
    };
    let line_end = if selected.is_empty() {
        Value::Null
    } else {
        json!(offset + selected.len())
    };
    object(json!({
        "ok": true,
        "content": [{"type": "text", "text": content}],
        "metadata": {
            "path": url,
            "url": url,
            "source": "web",
            "offset": offset,
            "limit": limit,
            "truncated": truncated,
            "content_hash": hash,
            "tag": tag,
            "line_start": line_start,
            "line_end": line_end,
            "content_type": response.content_type,
        },
    }))
}

fn url_host(url: &str) -> Option<String> {
    let (_, authority) = url.split_once("://")?;
    let authority = authority.split('/').next()?;
    let authority = authority.rsplit('@').next()?;
    if let Some(authority) = authority.strip_prefix('[') {
        let (host, _) = authority.split_once(']')?;
        return Some(host.to_owned());
    }
    let host = authority.split(':').next()?;
    if host.is_empty() {
        return None;
    }
    Some(host.to_owned())
}

fn is_public_address(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => is_public_ipv4(address),
        IpAddr::V6(address) => {
            if let Some(address) = address.to_ipv4_mapped() {
                return is_public_ipv4(address);
            }
            let value = u128::from(address);
            value != 0
                && value != 1
                && !ipv6_prefix(value, 0xfc00, 7)
                && !ipv6_prefix(value, 0xfe80, 10)
                && !ipv6_prefix(value, 0xff00, 8)
                && !ipv6_network(value, 0x0064_ff9b_0001_u128 << 80, 48)
                && !ipv6_network(value, 0x0100_u128 << 112, 64)
                && !ipv6_network(value, 0x2001_u128 << 112, 23)
                && !ipv6_network(value, 0x2001_0db8_u128 << 96, 32)
        }
    }
}

fn is_public_ipv4(address: std::net::Ipv4Addr) -> bool {
    let address = u32::from(address);
    !ipv4_network(address, 0x0000_0000, 8)
        && !ipv4_network(address, 0x0a00_0000, 8)
        && !ipv4_network(address, 0x6440_0000, 10)
        && !ipv4_network(address, 0x7f00_0000, 8)
        && !ipv4_network(address, 0xa9fe_0000, 16)
        && !ipv4_network(address, 0xac10_0000, 12)
        && !ipv4_network(address, 0xc000_0000, 24)
        && !ipv4_network(address, 0xc000_0200, 24)
        && !ipv4_network(address, 0xc0a8_0000, 16)
        && !ipv4_network(address, 0xc612_0000, 15)
        && !ipv4_network(address, 0xc633_6400, 24)
        && !ipv4_network(address, 0xcb00_7100, 24)
        && !ipv4_network(address, 0xe000_0000, 3)
}

fn ipv4_network(address: u32, network: u32, prefix: u32) -> bool {
    let mask = u32::MAX << (32 - prefix);
    address & mask == network & mask
}

fn ipv6_prefix(address: u128, prefix: u16, bits: u32) -> bool {
    let network = u128::from(prefix) << 112;
    ipv6_network(address, network, bits)
}

fn ipv6_network(address: u128, network: u128, prefix: u32) -> bool {
    let mask = u128::MAX << (128 - prefix);
    address & mask == network & mask
}

fn looks_like_html(text: &str) -> bool {
    let mut prefix = String::new();
    for character in text.chars().take(512) {
        prefix.push(character);
    }
    let prefix = prefix.to_lowercase();
    prefix.contains("<html") || prefix.contains("<!doctype html") || prefix.contains("<body")
}

fn html_to_text(html: &str) -> String {
    let title = html_element(html, "title");
    let body = html_body(html);
    let mut text = String::new();
    if let Some(title) = title {
        push_html_line(&mut text, &title, false);
    }
    let mut index = 0;
    let bytes = body.as_bytes();
    let mut line = String::new();
    while index < bytes.len() {
        if bytes[index] == b'<' {
            let Some(relative_end) = body[index..].find('>') else {
                break;
            };
            let end = index + relative_end;
            let tag = body[index + 1..end].trim().to_lowercase();
            if tag.starts_with("h1")
                || tag.starts_with("h2")
                || tag.starts_with("h3")
                || tag.starts_with("h4")
                || tag.starts_with("h5")
                || tag.starts_with("h6")
            {
                flush_html_line(&mut text, &mut line);
                line.push_str("# ");
            } else if tag.starts_with("/h")
                || tag.starts_with('p')
                || tag.starts_with("/p")
                || tag.starts_with("div")
                || tag.starts_with("/div")
                || tag.starts_with("section")
                || tag.starts_with("/section")
                || tag.starts_with("article")
                || tag.starts_with("/article")
                || tag.starts_with("br")
                || tag.starts_with("li")
                || tag.starts_with("/li")
            {
                flush_html_line(&mut text, &mut line);
            }
            index = end + 1;
            continue;
        }
        let Some(character) = body[index..].chars().next() else {
            break;
        };
        line.push(character);
        index += character.len_utf8();
    }
    flush_html_line(&mut text, &mut line);
    text
}

fn html_element(html: &str, name: &str) -> Option<String> {
    let lower = html.to_lowercase();
    let start_marker = format!("<{name}");
    let start = lower.find(&start_marker)?;
    let start = start + lower[start..].find('>')? + 1;
    let end_marker = format!("</{name}>");
    let end = lower[start..].find(&end_marker)? + start;
    Some(decode_entities(&html[start..end]))
}

fn html_body(html: &str) -> &str {
    let lower = html.to_lowercase();
    let Some(body) = lower.find("<body") else {
        return html;
    };
    let Some(start) = lower[body..].find('>') else {
        return html;
    };
    let start = body + start + 1;
    let end = lower[start..]
        .find("</body>")
        .map(|end| start + end)
        .unwrap_or(html.len());
    &html[start..end]
}

fn flush_html_line(text: &mut String, line: &mut String) {
    let value = decode_entities(line);
    line.clear();
    push_html_line(text, &value, false);
}

fn push_html_line(text: &mut String, line: &str, heading: bool) {
    let mut words = Vec::new();
    for word in line.split_whitespace() {
        words.push(word);
    }
    if words.is_empty() {
        return;
    }
    if heading {
        text.push_str("# ");
    }
    text.push_str(&words.join(" "));
    text.push('\n');
}

fn decode_entities(text: &str) -> String {
    text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
}

pub(super) fn write(params: &Map<String, Value>, base_directory: &Path) -> Map<String, Value> {
    let destination = string_param(params, "path");
    if destination.is_empty() {
        return error_result("missing-path", "missing path");
    }
    let destination = resolve_path(&destination, base_directory);
    let content = string_param(params, "content");
    let hashes = change_hashes(&destination, &content);
    if let Err(error) = fs::write(&destination, content) {
        return error_result("write-failed", error.to_string());
    }
    let mut metadata = Map::new();
    metadata.insert(
        "path".to_owned(),
        Value::String(destination.display().to_string()),
    );
    for (name, value) in hashes {
        metadata.insert(name, value);
    }
    object(json!({
        "ok": true,
        "content": [{
            "type": "text",
            "text": format!("wrote {}", destination.display()),
        }],
        "metadata": metadata,
    }))
}

pub(super) fn list(params: &Map<String, Value>, base_directory: &Path) -> Map<String, Value> {
    let path_value = params.get("path").and_then(Value::as_str).unwrap_or(".");
    let path = resolve_path(path_value, base_directory);
    let limit = integer_param(params, "limit", 200);
    let recursive = params
        .get("recursive")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let size_floor = params.get("min_size_bytes").and_then(Value::as_u64);
    let exclude = string_array(params.get("exclude"));
    let mut entries = match listed_entries(&path, recursive, &exclude) {
        Ok(entries) => entries,
        Err(error) => return error_result("ls-failed", error.to_string()),
    };
    entries.sort_by(entry_order);
    let mut rows = Vec::new();
    for entry in entries {
        let metadata = match fs::symlink_metadata(&entry) {
            Ok(metadata) => metadata,
            Err(_error) => continue,
        };
        let is_directory = metadata.is_dir();
        if let Some(size_floor) = size_floor {
            if is_directory || metadata.len() < size_floor {
                continue;
            }
        }
        let label = entry_label(&entry, &path, is_directory);
        if is_directory {
            rows.push(format!("-\tdir\t{label}"));
        } else {
            rows.push(format!("{}\tfile\t{label}", metadata.len()));
        }
    }
    let entries = rows.len();
    let shown = rows.len().min(limit);
    let mut lines = rows[..shown].to_vec();
    let omitted = entries.saturating_sub(limit);
    if omitted > 0 {
        lines.push(format!("... {omitted} more"));
    }
    object(json!({
        "ok": true,
        "content": [{"type": "text", "text": lines.join("\n")}],
        "metadata": {
            "path": path.display().to_string(),
            "limit": limit,
            "entries": entries,
            "recursive": recursive,
            "min_size_bytes": size_floor,
            "exclude": exclude,
        },
    }))
}

fn lines_with_endings(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    for line in text.split_inclusive('\n') {
        lines.push(line);
    }
    lines
}

fn truncate_chars(text: &str, limit: usize) -> (String, bool) {
    if text.chars().count() <= limit {
        return (text.to_owned(), false);
    }
    let mut truncated = String::new();
    for character in text.chars().take(limit) {
        truncated.push(character);
    }
    (truncated, true)
}

fn string_array(value: Option<&Value>) -> Vec<String> {
    let Some(Value::Array(values)) = value else {
        return Vec::new();
    };
    let mut strings = Vec::new();
    for value in values {
        if let Some(value) = value.as_str() {
            strings.push(value.to_owned());
        }
    }
    strings
}

fn listed_entries(
    path: &Path,
    recursive: bool,
    exclude: &[String],
) -> Result<Vec<PathBuf>, std::io::Error> {
    if path.is_file() {
        return Ok(vec![path.to_owned()]);
    }
    let mut entries = Vec::new();
    collect_entries(path, path, recursive, exclude, &mut entries)?;
    Ok(entries)
}

fn collect_entries(
    root: &Path,
    directory: &Path,
    recursive: bool,
    exclude: &[String],
    entries: &mut Vec<PathBuf>,
) -> Result<(), std::io::Error> {
    for entry in fs::read_dir(directory)? {
        let entry = entry?.path();
        if excluded(&entry, root, exclude) {
            continue;
        }
        let is_directory = entry.is_dir();
        entries.push(entry.clone());
        if recursive && is_directory {
            collect_entries(root, &entry, recursive, exclude, entries)?;
        }
    }
    Ok(())
}

fn excluded(entry: &Path, root: &Path, patterns: &[String]) -> bool {
    let relative = entry.strip_prefix(root).unwrap_or(entry);
    let relative = relative
        .to_string_lossy()
        .replace(std::path::MAIN_SEPARATOR, "/");
    let name = entry
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("");
    for pattern in patterns {
        let pattern = pattern.trim().trim_matches('/');
        if pattern.is_empty() {
            continue;
        }
        if name == pattern || relative == pattern || relative.starts_with(&format!("{pattern}/")) {
            return true;
        }
    }
    false
}

fn entry_order(left: &PathBuf, right: &PathBuf) -> Ordering {
    let left_directory = left.is_dir();
    let right_directory = right.is_dir();
    match (left_directory, right_directory) {
        (true, false) => Ordering::Less,
        (false, true) => Ordering::Greater,
        (true, true) | (false, false) => left.cmp(right),
    }
}

fn entry_label(entry: &Path, root: &Path, is_directory: bool) -> String {
    let label = if entry == root {
        entry
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("")
            .to_owned()
    } else {
        entry
            .strip_prefix(root)
            .unwrap_or(entry)
            .to_string_lossy()
            .replace(std::path::MAIN_SEPARATOR, "/")
    };
    if is_directory {
        format!("{label}/")
    } else {
        label
    }
}
