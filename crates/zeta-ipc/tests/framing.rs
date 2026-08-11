//! NDJSON framing behavior.

use serde_json::json;
use zeta_ipc::{
    Frame, FrameReader, FrameWriter, Message, Notification, Request, RequestId, PARSE_ERROR,
};

const PING: &str = r#"{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}"#;

fn frames(bytes: &[u8]) -> Vec<Frame> {
    let mut reader = FrameReader::new(bytes);
    let mut frames = Vec::new();
    loop {
        let frame = reader.read_frame().unwrap();
        let Some(frame) = frame else {
            return frames;
        };
        frames.push(frame);
    }
}

fn violation(frame: &Frame) -> &zeta_ipc::Violation {
    let Frame::Violation(violation) = frame else {
        panic!("expected a violation, got {frame:?}");
    };
    violation
}

#[test]
fn junk_lines_surface_between_valid_messages() {
    let stream = format!("not json\n{PING}\n{{\"jsonrpc\":\"2.0\"}}\n");
    let frames = frames(stream.as_bytes());

    assert_eq!(frames.len(), 3);
    assert_eq!(violation(&frames[0]).code, PARSE_ERROR);
    let Frame::Message(Message::Request(request)) = &frames[1] else {
        panic!("the request must survive its neighbors");
    };
    assert_eq!(request.method, "ping");
    assert_eq!(violation(&frames[2]).code, zeta_ipc::INVALID_REQUEST);
}

#[test]
fn an_invalid_request_preserves_its_valid_request_id() {
    let frames = frames(
        br#"{"jsonrpc":"2.0","id":"bad-request","method":"","params":{}}
"#,
    );

    assert_eq!(
        violation(&frames[0]).request_id,
        Some(RequestId::from("bad-request"))
    );
}

#[test]
fn an_empty_line_is_a_parse_violation() {
    let stream = format!("\n{PING}\n");
    let frames = frames(stream.as_bytes());

    assert_eq!(violation(&frames[0]).rule, "empty_line");
    assert_eq!(violation(&frames[0]).code, PARSE_ERROR);
    assert_eq!(frames.len(), 2);
}

#[test]
fn invalid_utf8_is_a_parse_violation_and_the_reader_recovers() {
    let mut stream = br#"{"jsonrpc":"2.0","method":""#.to_vec();
    stream.push(0xff);
    stream.extend_from_slice(b"\"}\n");
    stream.extend_from_slice(PING.as_bytes());
    stream.push(b'\n');

    let frames = frames(&stream);

    assert_eq!(frames.len(), 2);
    assert_eq!(violation(&frames[0]).rule, "parse_error");
    assert_eq!(violation(&frames[0]).code, PARSE_ERROR);
    let Frame::Message(Message::Request(request)) = &frames[1] else {
        panic!("the request after invalid UTF-8 must decode");
    };
    assert_eq!(request.method, "ping");
}

#[test]
fn a_complete_final_object_at_eof_decodes() {
    let frames = frames(PING.as_bytes());

    assert_eq!(frames.len(), 1);
    let Frame::Message(Message::Request(request)) = &frames[0] else {
        panic!("the final object must decode without a newline");
    };
    assert_eq!(request.id, RequestId::from(1_u64));
}

#[test]
fn an_overlong_line_is_discarded_and_the_reader_recovers() {
    let mut stream = vec![b'x'; 4096];
    stream.push(b'\n');
    stream.extend_from_slice(PING.as_bytes());
    stream.push(b'\n');
    let mut reader = FrameReader::with_max_frame_bytes(&stream[..], 1024);

    let first = reader.read_frame().unwrap().unwrap();
    assert_eq!(violation(&first).rule, "frame_too_long");
    let second = reader.read_frame().unwrap().unwrap();
    let Frame::Message(Message::Request(request)) = second else {
        panic!("the request after the discarded line must decode");
    };
    assert_eq!(request.method, "ping");
    assert_eq!(reader.read_frame().unwrap(), None);
}

#[test]
fn the_writer_emits_compact_json_and_a_newline() {
    let message = Message::Notification(Notification::new(
        "event",
        json!({"event": {"type": "test.event"}})
            .as_object()
            .unwrap()
            .clone(),
    ));
    let mut sink = Vec::new();
    let mut writer = FrameWriter::new(&mut sink);

    writer.write_message(&message).unwrap();

    assert_eq!(
        sink,
        b"{\"jsonrpc\":\"2.0\",\"method\":\"event\",\"params\":{\"event\":{\"type\":\"test.event\"}}}\n"
    );
}

#[test]
fn the_writer_preserves_numeric_request_ids() {
    let message = Message::Request(Request::new(
        RequestId::from(-7_i64),
        "ping",
        Default::default(),
    ));
    let mut sink = Vec::new();
    let mut writer = FrameWriter::new(&mut sink);

    writer.write_message(&message).unwrap();

    let parsed = frames(&sink);
    let Frame::Message(Message::Request(request)) = &parsed[0] else {
        panic!("the written request must parse");
    };
    assert_eq!(request.id, RequestId::from(-7_i64));
}
