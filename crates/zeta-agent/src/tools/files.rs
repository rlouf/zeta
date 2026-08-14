//! Implements deterministic local file reads, writes, and listings.

use std::cmp::Ordering;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use super::{
    change_hashes, content_hash, error_result, integer_param, object, resolve_path, short_tag,
    string_param,
};

const DEFAULT_READ_LIMIT: usize = 2_000;
const MAX_READ_CHARS: usize = 50_000;
const BINARY_SNIFF_BYTES: usize = 8_192;
pub(super) fn read(params: &Map<String, Value>, base_directory: &Path) -> Map<String, Value> {
    let path_value = string_param(params, "path");
    if path_value.starts_with("http://") || path_value.starts_with("https://") {
        return error_result("read-failed", "URL reads are not supported");
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
