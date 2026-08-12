//! Declares the canonical native capability vocabulary.

use serde_json::{json, Map, Value};

use crate::{Capability, DeliverySemantics};

/// Returns native capability declarations in canonical id order.
///
/// # Examples
///
/// ```
/// let capabilities = zeta_agent::native_capabilities();
/// assert_eq!(capabilities.first().unwrap().id.as_str(), "zeta.ast_grep");
/// ```
pub fn native_capabilities() -> Vec<Capability> {
    vec![
        capability(
            "zeta.ast_grep",
            "Search code structurally with ast-grep. Use when looking for syntax patterns rather than plain text. Results include [path#tag] snapshot headers and numbered matched lines for grounded edits.",
            object(json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["pattern", "lang"],
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "ast-grep structural pattern, such as 'subprocess.Popen($$$ARGS)'.",
                    },
                    "lang": {
                        "type": "string",
                        "description": "Language for ast-grep parsing, such as python, rust, typescript, or tsx.",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory to search. Defaults to the current working directory.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Maximum number of structural matches to return.",
                    },
                },
            })),
            None,
        ),
        capability(
            "zeta.bash",
            "Execute a shell command.",
            object(json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["command"],
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {
                        "type": "number",
                        "minimum": 1,
                        "maximum": 600.0,
                        "description": "Seconds before the command is killed (default 120).",
                    },
                },
            })),
            Some(DeliverySemantics::UnsafeToRetry),
        ),
        capability(
            "zeta.edit",
            "Edit a file. Prefer tagged input from read: [path#tag] plus SWAP, DEL, INS.PRE, or INS.POST line operations.",
            object(json!({
                "type": "object",
                "additionalProperties": false,
                "anyOf": [
                    {"required": ["input"]},
                    {"required": ["location", "old", "new"]},
                ],
                "properties": {
                    "input": {"type": "string", "minLength": 1},
                    "location": {"type": "string", "minLength": 1},
                    "old": {"type": "string", "minLength": 1},
                    "new": {"type": "string"},
                },
            })),
            Some(DeliverySemantics::IdempotentWithKey),
        ),
        capability(
            "zeta.grep",
            "Search file contents recursively. Use before read when looking for symbols, errors, strings, or definitions. Successful results include [path#tag] snapshot headers and numbered lines for grounded edits.",
            object(json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["pattern"],
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or regular expression to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory to search. Defaults to the current working directory.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Maximum number of matching lines to return.",
                    },
                },
            })),
            None,
        ),
        capability(
            "zeta.ls",
            "List files with type and byte sizes.",
            object(json!({
                "type": "object",
                "additionalProperties": false,
                "properties": {
                    "path": {"type": "string", "description": "Directory or file to list."},
                    "limit": {"type": "integer", "minimum": 1},
                    "recursive": {
                        "type": "boolean",
                        "description": "List descendants recursively instead of only direct children.",
                    },
                    "min_size_bytes": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Only include files at least this large. Directories are omitted when set.",
                    },
                    "exclude": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Path/name patterns to omit, such as .git or dist.",
                    },
                },
            })),
            None,
        ),
        capability(
            "zeta.patch",
            "Apply a patch to files.",
            object(json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["patch"],
                "properties": {"patch": {"type": "string", "minLength": 1}},
            })),
            Some(DeliverySemantics::IdempotentWithKey),
        ),
        capability(
            "zeta.read",
            "Read a UTF-8 text file or public HTTP(S) URL. Returns a [path#tag] snapshot header and numbered lines.",
            object(json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Local file path or public HTTP(S) URL.",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Number of leading lines to skip (0-based).",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Maximum number of lines to return.",
                    },
                },
            })),
            None,
        ),
        capability(
            "zeta.web_search",
            "Search public web pages using Codex hosted web search. Provide one self-contained query; use read for URLs returned by the search.",
            object(json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Self-contained public web search query.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "Maximum number of source URLs to return.",
                    },
                },
            })),
            None,
        ),
        capability(
            "zeta.write",
            "Write content to a file.",
            object(json!({
                "type": "object",
                "additionalProperties": false,
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
            })),
            Some(DeliverySemantics::IdempotentWithKey),
        ),
    ]
}

fn capability(
    id: &str,
    description: &str,
    input_schema: Map<String, Value>,
    delivery_semantics: Option<DeliverySemantics>,
) -> Capability {
    Capability {
        id: id.parse().expect("native capability ids are valid"),
        description: description.to_owned(),
        input_schema,
        delivery_semantics,
    }
}

fn object(value: Value) -> Map<String, Value> {
    let Value::Object(value) = value else {
        unreachable!("native capability schema must be an object")
    };
    value
}
