//! Implements grounded file edits and atomic patch preparation.

use std::collections::HashSet;
use std::fs;
use std::path::{Component, Path, PathBuf};

use serde_json::{json, Map, Value};

use super::{
    change_hashes, content_hash, error_result, object, resolve_path, short_tag, string_param,
    write_artifact,
};

pub(super) fn edit(params: &Map<String, Value>, base_directory: &Path) -> Map<String, Value> {
    if params.contains_key("input") {
        return hashline_edit(params, base_directory);
    }
    exact_edit(params, base_directory)
}

fn exact_edit(params: &Map<String, Value>, base_directory: &Path) -> Map<String, Value> {
    let location = string_param(params, "location");
    if location.is_empty() {
        return error_result("missing-location", "missing location");
    }
    let location = resolve_path(&location, base_directory);
    let old = string_param(params, "old");
    if old.is_empty() {
        return error_result("missing-old", "missing old");
    }
    let new = string_param(params, "new");
    let before = match read_utf8(&location) {
        Ok(before) => before,
        Err(error) => return error,
    };
    let matches = before.match_indices(&old).count();
    if matches == 0 {
        return error_result("old-text-not-found", "old text was not found");
    }
    if matches > 1 {
        return error_result("old-text-not-unique", "old text matched more than once");
    }
    let after = before.replacen(&old, &new, 1);
    finish_edit(
        &location,
        &before,
        &after,
        object(json!({
            "operation": "exact_replace",
        })),
    )
}

fn hashline_edit(params: &Map<String, Value>, base_directory: &Path) -> Map<String, Value> {
    let input = string_param(params, "input");
    let parsed = match parse_hashline(&input) {
        Ok(parsed) => parsed,
        Err(error) => return error,
    };
    let location = resolve_path(&parsed.location, base_directory);
    let before = match read_utf8(&location) {
        Ok(before) => before,
        Err(error) => return error,
    };
    let hash = content_hash(before.as_bytes());
    if short_tag(&hash) != parsed.tag {
        return error_result(
            "stale-tag",
            "file changed since the tagged read; read it again before editing",
        );
    }
    let after = match apply_line_operations(&before, &parsed.operations) {
        Ok(after) => after,
        Err(error) => return error,
    };
    let mut operations = Vec::new();
    for operation in &parsed.operations {
        let mut metadata = object(json!({
            "kind": operation.kind.as_str(),
            "start": operation.start,
            "end": operation.end,
        }));
        if !operation.body.is_empty() {
            metadata.insert("lines".to_owned(), json!(operation.body.len()));
        }
        operations.push(Value::Object(metadata));
    }
    finish_edit(
        &location,
        &before,
        &after,
        object(json!({
            "mode": "hashline",
            "tag": parsed.tag,
            "operations": operations,
        })),
    )
}

fn finish_edit(
    location: &Path,
    before: &str,
    after: &str,
    edit_metadata: Map<String, Value>,
) -> Map<String, Value> {
    let patch = replacement_patch(location, before, after);
    if patch.is_empty() {
        return error_result("empty-edit", "replacement did not change the file");
    }
    let hashes = change_hashes(location, after);
    if let Err(error) = fs::write(location, after) {
        return error_result("write-failed", error.to_string());
    }
    let artifact = match write_artifact("zeta-edit", &patch) {
        Ok(artifact) => artifact,
        Err(error) => return error_result("artifact-write-failed", error.to_string()),
    };
    let mut metadata = Map::new();
    metadata.insert(
        "location".to_owned(),
        Value::String(location.display().to_string()),
    );
    metadata.insert(
        "artifact".to_owned(),
        Value::String(artifact.display().to_string()),
    );
    for (name, value) in hashes {
        metadata.insert(name, value);
    }
    for (name, value) in edit_metadata {
        metadata.insert(name, value);
    }
    object(json!({
        "ok": true,
        "content": [{
            "type": "text",
            "text": format!("applied exact replacement to {}", location.display()),
        }],
        "metadata": metadata,
    }))
}

struct HashlineEdit {
    location: String,
    tag: String,
    operations: Vec<LineOperation>,
}

struct LineOperation {
    kind: OperationKind,
    start: usize,
    end: usize,
    body: Vec<String>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum OperationKind {
    Swap,
    Delete,
    InsertBefore,
    InsertAfter,
}

impl OperationKind {
    fn as_str(self) -> &'static str {
        match self {
            OperationKind::Swap => "SWAP",
            OperationKind::Delete => "DEL",
            OperationKind::InsertBefore => "INS.PRE",
            OperationKind::InsertAfter => "INS.POST",
        }
    }
}

fn parse_hashline(input: &str) -> Result<HashlineEdit, Map<String, Value>> {
    let lines: Vec<&str> = input.lines().collect();
    let Some(header) = lines.first() else {
        return Err(error_result(
            "missing-section-header",
            "missing [path#tag] header",
        ));
    };
    let Some(header) = header
        .strip_prefix('[')
        .and_then(|value| value.strip_suffix(']'))
    else {
        return Err(error_result(
            "missing-section-header",
            "missing [path#tag] header",
        ));
    };
    let Some((location, tag)) = header.rsplit_once('#') else {
        return Err(error_result(
            "missing-tag",
            "section header must include #tag",
        ));
    };
    if location.is_empty() || tag.is_empty() {
        return Err(error_result(
            "missing-tag",
            "section header must include path and tag",
        ));
    }
    let mut operations = Vec::new();
    let mut index = 1;
    while index < lines.len() {
        if lines[index].is_empty() {
            index += 1;
            continue;
        }
        let (kind, start, end) = parse_operation_header(lines[index])?;
        index += 1;
        let mut body = Vec::new();
        while index < lines.len() && !is_operation_header(lines[index]) {
            let Some(line) = lines[index].strip_prefix('+') else {
                return Err(error_result(
                    "invalid-body-line",
                    "hashline edit body rows must start with +",
                ));
            };
            body.push(format!("{line}\n"));
            index += 1;
        }
        if kind != OperationKind::Delete && body.is_empty() {
            return Err(error_result(
                "missing-body",
                format!("{} requires + body rows", kind.as_str()),
            ));
        }
        if kind == OperationKind::Delete && !body.is_empty() {
            return Err(error_result(
                "invalid-body-line",
                "DEL does not accept body rows",
            ));
        }
        operations.push(LineOperation {
            kind,
            start,
            end,
            body,
        });
    }
    if operations.is_empty() {
        return Err(error_result(
            "missing-operation",
            "hashline edit has no operations",
        ));
    }
    Ok(HashlineEdit {
        location: location.to_owned(),
        tag: tag.to_owned(),
        operations,
    })
}

fn parse_operation_header(line: &str) -> Result<(OperationKind, usize, usize), Map<String, Value>> {
    if let Some(range) = line
        .strip_prefix("SWAP ")
        .and_then(|value| value.strip_suffix(':'))
    {
        return parse_range(OperationKind::Swap, range);
    }
    if let Some(range) = line.strip_prefix("DEL ") {
        return parse_range(OperationKind::Delete, range);
    }
    if let Some(line) = line
        .strip_prefix("INS.PRE ")
        .and_then(|value| value.strip_suffix(':'))
    {
        return parse_position(OperationKind::InsertBefore, line);
    }
    if let Some(line) = line
        .strip_prefix("INS.POST ")
        .and_then(|value| value.strip_suffix(':'))
    {
        return parse_position(OperationKind::InsertAfter, line);
    }
    Err(error_result(
        "unknown-operation",
        format!("unknown hashline operation: {line}"),
    ))
}

fn parse_range(
    kind: OperationKind,
    range: &str,
) -> Result<(OperationKind, usize, usize), Map<String, Value>> {
    let Some((start, end)) = range.split_once("..") else {
        return Err(error_result(
            "unknown-operation",
            format!("unknown hashline operation: {} {range}", kind.as_str()),
        ));
    };
    let (Ok(start), Ok(end)) = (start.parse(), end.parse()) else {
        return Err(error_result(
            "unknown-operation",
            format!("unknown hashline operation: {} {range}", kind.as_str()),
        ));
    };
    if start == 0 || start > end {
        return Err(error_result(
            "invalid-range",
            "operation range is out of order",
        ));
    }
    Ok((kind, start, end))
}

fn parse_position(
    kind: OperationKind,
    line: &str,
) -> Result<(OperationKind, usize, usize), Map<String, Value>> {
    let Ok(line) = line.parse() else {
        return Err(error_result(
            "unknown-operation",
            format!("unknown hashline operation: {} {line}", kind.as_str()),
        ));
    };
    if line == 0 {
        return Err(error_result(
            "line-out-of-range",
            "operation refers to a missing line",
        ));
    }
    Ok((kind, line, line))
}

fn is_operation_header(line: &str) -> bool {
    line.starts_with("SWAP ")
        || line.starts_with("DEL ")
        || line.starts_with("INS.PRE ")
        || line.starts_with("INS.POST ")
}

fn apply_line_operations(
    text: &str,
    operations: &[LineOperation],
) -> Result<String, Map<String, Value>> {
    let mut lines = owned_lines(text);
    let line_count = lines.len();
    for operation in operations {
        let valid = match operation.kind {
            OperationKind::InsertBefore => operation.start <= line_count.max(1),
            OperationKind::InsertAfter => operation.start <= line_count,
            OperationKind::Swap | OperationKind::Delete => operation.end <= line_count,
        };
        if !valid {
            return Err(error_result(
                "line-out-of-range",
                "operation refers to a missing line",
            ));
        }
    }
    let mut ranges = Vec::new();
    for operation in operations {
        if operation.kind == OperationKind::Swap || operation.kind == OperationKind::Delete {
            ranges.push((operation.start, operation.end));
        }
    }
    ranges.sort();
    for adjacent in ranges.windows(2) {
        if adjacent[1].0 <= adjacent[0].1 {
            return Err(error_result(
                "overlapping-operations",
                "operations touch overlapping line ranges",
            ));
        }
    }
    let mut ordered: Vec<&LineOperation> = operations.iter().collect();
    ordered.sort_by_key(|operation| std::cmp::Reverse(operation.start));
    for operation in ordered {
        let start = operation.start - 1;
        let end = operation.end;
        match operation.kind {
            OperationKind::Swap => {
                lines.splice(start..end, operation.body.clone());
            }
            OperationKind::Delete => {
                lines.drain(start..end);
            }
            OperationKind::InsertBefore => {
                lines.splice(start..start, operation.body.clone());
            }
            OperationKind::InsertAfter => {
                lines.splice(end..end, operation.body.clone());
            }
        }
    }
    Ok(lines.concat())
}

pub(super) fn patch(params: &Map<String, Value>, base_directory: &Path) -> Map<String, Value> {
    let source = string_param(params, "patch");
    if source.is_empty() {
        return error_result("missing-patch", "missing patch");
    }
    let sections = match parse_patch_sections(&source) {
        Ok(sections) => sections,
        Err(error) => return error,
    };
    let changes = match prepare_changes(sections, base_directory) {
        Ok(changes) => changes,
        Err(error) => return error,
    };
    if let Err(error) = commit_changes(&changes) {
        return error;
    }
    let artifact = match write_artifact("zeta-patch", &source) {
        Ok(artifact) => artifact,
        Err(error) => return error_result("artifact-write-failed", error.to_string()),
    };
    let mut files = Vec::new();
    let mut metadata = Vec::new();
    for change in &changes {
        files.push(Value::String(
            change
                .move_label
                .as_ref()
                .unwrap_or(&change.label)
                .to_owned(),
        ));
        metadata.push(Value::Object(change.metadata()));
    }
    object(json!({
        "ok": true,
        "content": [{"type": "text", "text": "applied patch"}],
        "metadata": {
            "artifact": artifact.display().to_string(),
            "files": files,
            "changes": metadata,
        },
    }))
}

struct PatchSection {
    kind: ChangeKind,
    label: String,
    lines: Vec<String>,
    move_to: Option<String>,
}

struct PatchChange {
    kind: ChangeKind,
    label: String,
    path: PathBuf,
    before: Option<String>,
    after: Option<String>,
    move_label: Option<String>,
    move_path: Option<PathBuf>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ChangeKind {
    Add,
    Delete,
    Update,
}

impl ChangeKind {
    fn as_str(self) -> &'static str {
        match self {
            ChangeKind::Add => "add",
            ChangeKind::Delete => "delete",
            ChangeKind::Update => "update",
        }
    }
}

impl PatchChange {
    fn metadata(&self) -> Map<String, Value> {
        let mut metadata = object(json!({
            "operation": self.kind.as_str(),
            "path": self.label,
        }));
        if let Some(before) = &self.before {
            metadata.insert(
                "before_hash".to_owned(),
                Value::String(content_hash(before.as_bytes())),
            );
        }
        if let Some(after) = &self.after {
            metadata.insert(
                "after_hash".to_owned(),
                Value::String(content_hash(after.as_bytes())),
            );
        }
        if let Some(move_label) = &self.move_label {
            metadata.insert("move_to".to_owned(), Value::String(move_label.to_owned()));
        }
        metadata
    }
}

fn parse_patch_sections(source: &str) -> Result<Vec<PatchSection>, Map<String, Value>> {
    let lines: Vec<&str> = source.lines().collect();
    if lines.len() < 3 || lines.first() != Some(&"*** Begin Patch") {
        return Err(error_result(
            "invalid-patch",
            "patch must start with *** Begin Patch",
        ));
    }
    if lines.last() != Some(&"*** End Patch") {
        return Err(error_result(
            "invalid-patch",
            "patch must end with *** End Patch",
        ));
    }
    let mut sections = Vec::new();
    let mut index = 1;
    while index < lines.len() - 1 {
        let Some((kind, label)) = parse_section_header(lines[index]) else {
            return Err(error_result(
                "invalid-patch",
                format!("invalid patch section: {}", lines[index]),
            ));
        };
        index += 1;
        let mut move_to = None;
        if index < lines.len() - 1 {
            if let Some(label) = lines[index].strip_prefix("*** Move to: ") {
                if kind != ChangeKind::Update {
                    return Err(error_result(
                        "invalid-patch",
                        "only an update can move a file",
                    ));
                }
                move_to = Some(label.to_owned());
                index += 1;
            }
        }
        let mut body = Vec::new();
        while index < lines.len() - 1 && parse_section_header(lines[index]).is_none() {
            if lines[index].starts_with("*** ") && lines[index] != "*** End of File" {
                return Err(error_result(
                    "invalid-patch",
                    format!("invalid patch line: {}", lines[index]),
                ));
            }
            body.push(lines[index].to_owned());
            index += 1;
        }
        sections.push(PatchSection {
            kind,
            label: label.to_owned(),
            lines: body,
            move_to,
        });
    }
    if sections.is_empty() {
        return Err(error_result("invalid-patch", "patch has no file sections"));
    }
    Ok(sections)
}

fn parse_section_header(line: &str) -> Option<(ChangeKind, &str)> {
    if let Some(label) = line.strip_prefix("*** Add File: ") {
        return Some((ChangeKind::Add, label));
    }
    if let Some(label) = line.strip_prefix("*** Delete File: ") {
        return Some((ChangeKind::Delete, label));
    }
    if let Some(label) = line.strip_prefix("*** Update File: ") {
        return Some((ChangeKind::Update, label));
    }
    None
}

fn prepare_changes(
    sections: Vec<PatchSection>,
    base_directory: &Path,
) -> Result<Vec<PatchChange>, Map<String, Value>> {
    let mut changes = Vec::new();
    let mut used = HashSet::new();
    for section in sections {
        let change = prepare_change(section, base_directory)?;
        if used.contains(&change.path)
            || change
                .move_path
                .as_ref()
                .is_some_and(|path| used.contains(path))
        {
            return Err(error_result(
                "patch-path-conflict",
                format!(
                    "patch uses a file more than once: {}",
                    change.path.display()
                ),
            ));
        }
        used.insert(change.path.clone());
        if let Some(path) = &change.move_path {
            used.insert(path.clone());
        }
        changes.push(change);
    }
    Ok(changes)
}

fn prepare_change(
    section: PatchSection,
    base_directory: &Path,
) -> Result<PatchChange, Map<String, Value>> {
    let path = patch_path(&section.label, base_directory)?;
    match section.kind {
        ChangeKind::Add => prepare_add(section, path),
        ChangeKind::Delete => prepare_delete(section, path),
        ChangeKind::Update => prepare_update(section, path, base_directory),
    }
}

fn prepare_add(section: PatchSection, path: PathBuf) -> Result<PatchChange, Map<String, Value>> {
    if path.exists() {
        return Err(error_result(
            "patch-target-exists",
            format!("file already exists: {}", section.label),
        ));
    }
    if !path.parent().is_some_and(Path::is_dir) {
        return Err(error_result(
            "patch-parent-missing",
            format!(
                "parent directory does not exist: {}",
                path.parent().unwrap_or(Path::new("")).display()
            ),
        ));
    }
    let mut after = String::new();
    for line in section.lines {
        let Some(line) = line.strip_prefix('+') else {
            return Err(error_result(
                "invalid-patch",
                "each added file line must start with +",
            ));
        };
        after.push_str(line);
        after.push('\n');
    }
    Ok(PatchChange {
        kind: ChangeKind::Add,
        label: section.label,
        path,
        before: None,
        after: Some(after),
        move_label: None,
        move_path: None,
    })
}

fn prepare_delete(section: PatchSection, path: PathBuf) -> Result<PatchChange, Map<String, Value>> {
    if !section.lines.is_empty() || section.move_to.is_some() {
        return Err(error_result(
            "invalid-patch",
            "a delete cannot contain patch lines",
        ));
    }
    let before = read_patch_file(&path, &section.label)?;
    Ok(PatchChange {
        kind: ChangeKind::Delete,
        label: section.label,
        path,
        before: Some(before),
        after: None,
        move_label: None,
        move_path: None,
    })
}

fn prepare_update(
    section: PatchSection,
    path: PathBuf,
    base_directory: &Path,
) -> Result<PatchChange, Map<String, Value>> {
    let before = read_patch_file(&path, &section.label)?;
    let hunks = parse_hunks(&section.lines)?;
    let after = apply_hunks(&before, &hunks, &section.label)?;
    let move_path = match &section.move_to {
        Some(label) => Some(patch_path(label, base_directory)?),
        None => None,
    };
    if let Some(move_path) = &move_path {
        if move_path.exists() {
            return Err(error_result(
                "patch-target-exists",
                format!(
                    "file already exists: {}",
                    section.move_to.as_deref().unwrap_or("")
                ),
            ));
        }
        if !move_path.parent().is_some_and(Path::is_dir) {
            return Err(error_result(
                "patch-parent-missing",
                format!(
                    "parent directory does not exist: {}",
                    move_path.parent().unwrap_or(Path::new("")).display()
                ),
            ));
        }
    }
    if after == before && move_path.is_none() {
        return Err(error_result(
            "empty-patch",
            format!("patch does not change: {}", section.label),
        ));
    }
    Ok(PatchChange {
        kind: ChangeKind::Update,
        label: section.label,
        path,
        before: Some(before),
        after: Some(after),
        move_label: section.move_to,
        move_path,
    })
}

struct PatchHunk {
    context: String,
    lines: Vec<String>,
    end_of_file: bool,
}

fn parse_hunks(lines: &[String]) -> Result<Vec<PatchHunk>, Map<String, Value>> {
    let mut hunks = Vec::new();
    let mut index = 0;
    while index < lines.len() {
        let Some(header) = lines[index].strip_prefix("@@") else {
            return Err(error_result(
                "invalid-patch",
                "each update hunk must start with @@",
            ));
        };
        let context = header.trim().to_owned();
        index += 1;
        let mut body = Vec::new();
        let mut end_of_file = false;
        while index < lines.len() && !lines[index].starts_with("@@") {
            if lines[index] == "*** End of File" {
                end_of_file = true;
                index += 1;
                break;
            }
            let line = &lines[index];
            if !line.starts_with(' ') && !line.starts_with('+') && !line.starts_with('-') {
                return Err(error_result(
                    "invalid-patch",
                    "update lines must start with a space, +, or -",
                ));
            }
            body.push(line.to_owned());
            index += 1;
        }
        if body.is_empty() {
            return Err(error_result("invalid-patch", "update hunk has no lines"));
        }
        hunks.push(PatchHunk {
            context,
            lines: body,
            end_of_file,
        });
    }
    if hunks.is_empty() {
        return Err(error_result("invalid-patch", "update has no hunks"));
    }
    Ok(hunks)
}

fn apply_hunks(
    before: &str,
    hunks: &[PatchHunk],
    label: &str,
) -> Result<String, Map<String, Value>> {
    let mut current = owned_lines(before);
    let mut cursor = 0;
    for hunk in hunks {
        if !hunk.context.is_empty() {
            let mut matches = Vec::new();
            for (index, line) in current.iter().enumerate().skip(cursor) {
                if line.trim_end_matches(['\r', '\n']) == hunk.context {
                    matches.push(index);
                }
            }
            cursor = unique_patch_match(matches, label, &hunk.context)? + 1;
        }
        let mut old_lines = Vec::new();
        let mut new_lines = Vec::new();
        for line in &hunk.lines {
            if !line.starts_with('+') {
                old_lines.push(format!("{}\n", &line[1..]));
            }
            if !line.starts_with('-') {
                new_lines.push(format!("{}\n", &line[1..]));
            }
        }
        let matched = if old_lines.is_empty() {
            if hunk.end_of_file {
                current.len()
            } else {
                cursor
            }
        } else {
            let mut matches = Vec::new();
            let last = current.len().saturating_sub(old_lines.len());
            for index in cursor..=last {
                if current[index..index + old_lines.len()] == old_lines
                    && (!hunk.end_of_file || index + old_lines.len() == current.len())
                {
                    matches.push(index);
                }
            }
            unique_patch_match(matches, label, &old_lines.concat())?
        };
        current.splice(matched..matched + old_lines.len(), new_lines.clone());
        cursor = matched + new_lines.len();
    }
    Ok(current.concat())
}

fn unique_patch_match(
    matches: Vec<usize>,
    label: &str,
    context: &str,
) -> Result<usize, Map<String, Value>> {
    if matches.is_empty() {
        return Err(error_result(
            "patch-context-mismatch",
            format!(
                "patch context was not found in {label}: {}",
                context.trim_end_matches('\n')
            ),
        ));
    }
    if matches.len() > 1 {
        return Err(error_result(
            "patch-context-ambiguous",
            format!(
                "patch context matched more than once in {label}: {}",
                context.trim_end_matches('\n')
            ),
        ));
    }
    Ok(matches[0])
}

fn patch_path(label: &str, base_directory: &Path) -> Result<PathBuf, Map<String, Value>> {
    let candidate = Path::new(label);
    if candidate.is_absolute()
        || candidate
            .components()
            .any(|component| component == Component::ParentDir)
    {
        return Err(error_result(
            "invalid-patch-path",
            format!("patch path must stay in the base directory: {label}"),
        ));
    }
    Ok(base_directory.join(candidate))
}

fn commit_changes(changes: &[PatchChange]) -> Result<(), Map<String, Value>> {
    for change in changes {
        let result = match change.kind {
            ChangeKind::Delete => fs::remove_file(&change.path),
            ChangeKind::Add | ChangeKind::Update => {
                let destination = change.move_path.as_ref().unwrap_or(&change.path);
                let result = fs::write(destination, change.after.as_deref().unwrap_or(""));
                if result.is_ok() && change.move_path.is_some() {
                    fs::remove_file(&change.path)
                } else {
                    result
                }
            }
        };
        if let Err(error) = result {
            return Err(error_result(
                "patch-write-failed",
                format!("could not apply patch to {}: {error}", change.label),
            ));
        }
    }
    Ok(())
}

fn read_patch_file(path: &Path, label: &str) -> Result<String, Map<String, Value>> {
    let bytes = fs::read(path).map_err(|error| {
        error_result(
            "patch-read-failed",
            format!("could not read {label}: {error}"),
        )
    })?;
    String::from_utf8(bytes)
        .map_err(|_error| error_result("not-utf8", format!("file is not valid UTF-8: {label}")))
}

fn read_utf8(path: &Path) -> Result<String, Map<String, Value>> {
    let bytes = fs::read(path).map_err(|error| error_result("read-failed", error.to_string()))?;
    String::from_utf8(bytes).map_err(|_error| {
        error_result(
            "not-utf8",
            "file is not valid UTF-8; editing it would corrupt its bytes",
        )
    })
}

fn replacement_patch(location: &Path, before: &str, after: &str) -> String {
    if before == after {
        return String::new();
    }
    let before = owned_lines(before);
    let after = owned_lines(after);
    let mut prefix = 0;
    while prefix < before.len() && prefix < after.len() && before[prefix] == after[prefix] {
        prefix += 1;
    }
    let mut suffix = 0;
    while suffix < before.len().saturating_sub(prefix)
        && suffix < after.len().saturating_sub(prefix)
        && before[before.len() - suffix - 1] == after[after.len() - suffix - 1]
    {
        suffix += 1;
    }
    let context_start = prefix.saturating_sub(3);
    let before_end = (before.len() - suffix + 3).min(before.len());
    let after_end = (after.len() - suffix + 3).min(after.len());
    let before_count = before_end - context_start;
    let after_count = after_end - context_start;
    let mut patch = format!(
        "--- {}\n+++ {}\n@@ -{} +{} @@\n",
        location.display(),
        location.display(),
        diff_range(context_start, before_count),
        diff_range(context_start, after_count),
    );
    for line in &before[context_start..prefix] {
        append_diff_line(&mut patch, ' ', line);
    }
    for line in &before[prefix..before.len() - suffix] {
        append_diff_line(&mut patch, '-', line);
    }
    for line in &after[prefix..after.len() - suffix] {
        append_diff_line(&mut patch, '+', line);
    }
    for line in &before[before.len() - suffix..before_end] {
        append_diff_line(&mut patch, ' ', line);
    }
    patch
}

fn diff_range(start: usize, count: usize) -> String {
    if count == 1 {
        return (start + 1).to_string();
    }
    if count == 0 {
        return format!("{start},0");
    }
    format!("{},{}", start + 1, count)
}

fn append_diff_line(patch: &mut String, prefix: char, line: &str) {
    patch.push(prefix);
    patch.push_str(line);
    if !line.ends_with('\n') {
        patch.push('\n');
        patch.push_str("\\ No newline at end of file\n");
    }
}

fn owned_lines(text: &str) -> Vec<String> {
    let mut lines = Vec::new();
    for line in text.split_inclusive('\n') {
        lines.push(line.to_owned());
    }
    lines
}
