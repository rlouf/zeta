//! Framing behavior: junk tolerance, partial lines, bounded frames.

use zeta_ipc::{Envelope, Frame, FrameReader, FrameWriter};

const HEARTBEAT: &str = r#"{"id":"m-1","kind":"heartbeat","ts":"2026-08-10T12:00:00Z","v":0}"#;

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

fn rule_of(frame: &Frame) -> String {
    match frame {
        Frame::Envelope(envelope) => panic!("expected a violation, got {envelope:?}"),
        Frame::Violation(violation) => violation.rule.clone(),
    }
}

#[test]
fn junk_lines_surface_as_violations_between_valid_envelopes() {
    let stream = format!("not json\n{HEARTBEAT}\n{{\"v\":0}}\n");
    let frames = frames(stream.as_bytes());
    assert_eq!(frames.len(), 3);
    assert_eq!(rule_of(&frames[0]), "bad_json");
    let Frame::Envelope(_) = &frames[1] else {
        panic!("the valid envelope must survive its neighbors");
    };
    assert_eq!(rule_of(&frames[2]), "missing_field:kind");
}

#[test]
fn an_empty_line_is_a_violation() {
    let stream = format!("\n{HEARTBEAT}\n");
    let frames = frames(stream.as_bytes());
    assert_eq!(rule_of(&frames[0]), "empty_line");
    assert_eq!(frames.len(), 2);
}

#[test]
fn a_partial_line_at_eof_still_decodes() {
    let frames = frames(HEARTBEAT.as_bytes());
    assert_eq!(frames.len(), 1);
    let Frame::Envelope(envelope) = &frames[0] else {
        panic!("the unterminated final line must decode");
    };
    assert_eq!(envelope.id(), "m-1");
}

#[test]
fn an_overlong_line_is_discarded_without_growing_the_buffer() {
    let mut stream = Vec::new();
    stream.extend_from_slice(&vec![b'x'; 4096]);
    stream.push(b'\n');
    stream.extend_from_slice(HEARTBEAT.as_bytes());
    stream.push(b'\n');
    let mut reader = FrameReader::with_max_frame_bytes(&stream[..], 1024);
    let first = reader.read_frame().unwrap().unwrap();
    assert_eq!(rule_of(&first), "frame_too_long");
    let second = reader.read_frame().unwrap().unwrap();
    let Frame::Envelope(envelope) = second else {
        panic!("the stream must recover after the discard");
    };
    assert_eq!(envelope.id(), "m-1");
    assert_eq!(reader.read_frame().unwrap(), None);
}

#[test]
fn the_writer_frames_canonical_lines() {
    let envelope = Envelope::parse_str(HEARTBEAT).unwrap();
    let mut sink = Vec::new();
    let mut writer = FrameWriter::new(&mut sink);
    writer.write_envelope(&envelope).unwrap();
    assert_eq!(sink, format!("{HEARTBEAT}\n").into_bytes());
}
