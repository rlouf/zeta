use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use serde_json::{Map, Value};

use super::{
    chat_completions_request, decode_chat_completions_stream, decode_responses_stream,
    http_error_detail, model_stream_timeout, parse_sse_lines, responses_request, CodexCredentials,
    ModelInput, ModelStreamTimeout, Observation,
};

fn vectors() -> Value {
    let workspace = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let path = workspace.join("spec/vectors/agent/models.json");
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn object(value: &Value) -> &Map<String, Value> {
    value.as_object().unwrap()
}

fn strings(value: &Value) -> Vec<String> {
    let mut output = Vec::new();
    for value in value.as_array().unwrap() {
        output.push(value.as_str().unwrap().to_owned());
    }
    output
}

fn model_input(value: &Value) -> ModelInput {
    let value = object(value);
    let mut messages = Vec::new();
    for message in value["messages"].as_array().unwrap() {
        messages.push(object(message).clone());
    }
    let mut tools = Vec::new();
    if let Some(values) = value.get("tools").and_then(Value::as_array) {
        for value in values {
            tools.push(value.clone());
        }
    }
    let selected_model = value
        .get("selected_model")
        .or_else(|| value.get("model"))
        .and_then(Value::as_str)
        .map(str::to_owned);
    ModelInput {
        messages,
        tools,
        tool_choice: value["tool_choice"].clone(),
        max_tokens: value["max_tokens"].as_u64().unwrap(),
        selected_model,
        session_id: value
            .get("session_id")
            .and_then(Value::as_str)
            .map(str::to_owned),
        thinking: value
            .get("thinking")
            .and_then(Value::as_str)
            .map(str::to_owned),
    }
}

fn observed_deltas(observations: &[Observation]) -> (Vec<String>, Vec<String>) {
    let mut content = Vec::new();
    let mut reasoning = Vec::new();
    for observation in observations {
        match observation {
            Observation::TextDelta { text } => content.push(text.clone()),
            Observation::ReasoningDelta { text } => reasoning.push(text.clone()),
            Observation::Status { status: _, text: _ } => {}
        }
    }
    (content, reasoning)
}

#[test]
fn chat_completion_requests_match_python_vectors() {
    let vectors = vectors();
    for case in vectors["chat_completions"]["requests"].as_array().unwrap() {
        let input = model_input(&case["input"]);
        let request = chat_completions_request(&input).unwrap();
        assert_eq!(Value::Object(request), case["expected"], "{}", case["name"]);
    }
}

#[test]
fn chat_completion_streams_match_python_vectors() {
    let vectors = vectors();
    for case in vectors["chat_completions"]["streams"].as_array().unwrap() {
        let decoded = decode_chat_completions_stream(&strings(&case["sse"])).unwrap();
        assert_eq!(
            Value::Object(decoded.response),
            case["expected"],
            "{}",
            case["name"]
        );
        let (content, reasoning) = observed_deltas(&decoded.observations);
        assert_eq!(content, strings(&case["expected_observations"]["content"]));
        assert_eq!(
            reasoning,
            strings(&case["expected_observations"]["reasoning"])
        );
    }
}

#[test]
fn chat_completion_failures_match_python_vectors() {
    let vectors = vectors();
    for case in vectors["chat_completions"]["failures"].as_array().unwrap() {
        let error = decode_chat_completions_stream(&strings(&case["sse"]))
            .unwrap_err()
            .to_string();
        assert_eq!(error, case["expected_error"].as_str().unwrap());
    }
}

#[test]
fn sse_line_parser_matches_python_vectors() {
    let vectors = vectors();
    let parser = &vectors["chat_completions"]["sse_parser"];
    let frames = parse_sse_lines(&strings(&parser["lines"]));
    assert_eq!(frames, strings(&parser["expected_frames"]));
}

#[test]
fn http_failure_details_match_python_vectors() {
    let vectors = vectors();
    for case in vectors["chat_completions"]["http_failures"]
        .as_array()
        .unwrap()
    {
        let detail = http_error_detail(
            case["status"].as_u64().unwrap() as u16,
            case["url"].as_str().unwrap(),
            &case["body"],
        );
        assert_eq!(detail, case["expected"].as_str().unwrap());
    }
}

#[test]
fn model_timeouts_match_python_vectors() {
    let vectors = vectors();
    for case in vectors["chat_completions"]["timeouts"].as_array().unwrap() {
        let timeout: ModelStreamTimeout = model_stream_timeout(
            case["first_output_seconds"].as_f64().unwrap(),
            case["idle_seconds"].as_f64().unwrap(),
        );
        assert_eq!(serde_json::to_value(timeout).unwrap(), case["expected"]);
    }
}

#[test]
fn responses_requests_match_python_vectors() {
    let vectors = vectors();
    for case in vectors["responses"]["requests"].as_array().unwrap() {
        let input = model_input(&case["input"]);
        let request = responses_request(&input).unwrap();
        assert_eq!(Value::Object(request), case["expected"], "{}", case["name"]);
    }
}

#[test]
fn codex_headers_match_python_vectors() {
    let vectors = vectors();
    let case = &vectors["responses"]["codex_headers"];
    let credentials = CodexCredentials::new(
        case["credentials"]["access_token"]
            .as_str()
            .unwrap()
            .to_owned(),
        case["credentials"]["account_id"]
            .as_str()
            .unwrap()
            .to_owned(),
    );
    let headers = super::codex_request_headers(&credentials, case["session"].as_str().unwrap());
    let expected: BTreeMap<String, String> =
        serde_json::from_value(case["expected"].clone()).unwrap();
    assert_eq!(headers, expected);
}

#[test]
fn responses_streams_match_python_vectors() {
    let vectors = vectors();
    for case in vectors["responses"]["streams"].as_array().unwrap() {
        let decoded = decode_responses_stream(&strings(&case["sse"])).unwrap();
        assert_eq!(
            Value::Object(decoded.response),
            case["expected"],
            "{}",
            case["name"]
        );
        let expected = case.get("expected_observations");
        if let Some(expected) = expected {
            let (content, reasoning) = observed_deltas(&decoded.observations);
            assert_eq!(content, strings(&expected["content"]));
            assert_eq!(reasoning, strings(&expected["reasoning"]));
        }
    }
}

#[test]
fn responses_failures_match_python_vectors() {
    let vectors = vectors();
    for case in vectors["responses"]["failures"].as_array().unwrap() {
        let error = decode_responses_stream(&strings(&case["sse"]))
            .unwrap_err()
            .to_string();
        assert_eq!(error, case["expected_error"].as_str().unwrap());
    }
}
