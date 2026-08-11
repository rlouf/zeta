//! Public behavior tests for authored agent declarations.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};
use zeta_authoring::{
    matches, parse_agent, scheduled_event_type, AgentSpec, EgressBinding, ExecutorSpec,
    IngressBinding, ModelSpec, RetrySpec, ScheduleEntry, SpecErrorKind,
};

const COMPLETE_AGENT: &[u8] = br#"---
name: Slack Q&A
description: Answers workspace questions.
enabled: true
session: shared
model:
  name: qwen3.6
  url: http://127.0.0.1:8080/v1/chat/completions
executor:
  provider: modal
  config:
    retries: 3
    timeout: 1.5
    enabled: true
    regions: [eu-west, us-east]
    fallback: null
accepts:
  - event: slack.message.received
    filter:
      channel_ids: [C123]
    idempotency_key: slack:{team_id}:{message_ts}
publishes:
  - event: slack.message.post
    with:
      channel_ids: [C123]
returns:
  - support.completed
skills:
  - code-review
tools:
  - read
schedules:
  - cron: "0 18 * * 0"
    timezone: Europe/Paris
    catchup: latest
retry:
  max_attempts: 3
  backoff_seconds: 1.5
base_dir: /srv/zeta
writes:
  paths: [docs/**.md]
---
User asked: {{ event.payload.text }}
"#;

fn object(value: Value) -> Map<String, Value> {
    value.as_object().unwrap().clone()
}

fn authoring_vectors() -> Value {
    let workspace = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let path = workspace.join("spec/vectors/authoring/agents.json");
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

#[test]
fn complete_agent_matches_python_declaration_behavior() {
    let spec = parse_agent(Path::new("agents/slack-qa.md"), COMPLETE_AGENT).unwrap();
    let AgentSpec {
        slug,
        name,
        description,
        instructions,
        path,
        content_address,
        enabled,
        session,
        model,
        executor,
        accepts,
        publishes,
        returns,
        skills,
        skills_inherit,
        tools,
        tools_inherit,
        schedules,
        retry,
        base_dir,
        ingress,
        egress,
        extensions,
    } = spec;

    assert_eq!(slug, "slack-qa");
    assert_eq!(name, "Slack Q&A");
    assert_eq!(description, "Answers workspace questions.");
    assert_eq!(instructions, "User asked: {{ event.payload.text }}\n");
    assert_eq!(path, PathBuf::from("agents/slack-qa.md"));
    assert_eq!(
        content_address.to_string(),
        "b3:3b11502a246625eee17b4262ef24f46d85e0d8cafe0167adc281058b64d131dc"
    );
    assert!(enabled);
    assert_eq!(session, "shared");
    assert_eq!(
        model,
        Some(ModelSpec {
            name: "qwen3.6".to_owned(),
            url: "http://127.0.0.1:8080/v1/chat/completions".to_owned(),
        })
    );
    assert_eq!(
        executor,
        ExecutorSpec {
            provider: "modal".to_owned(),
            config: object(json!({
                "enabled": true,
                "fallback": null,
                "regions": ["eu-west", "us-east"],
                "retries": 3,
                "timeout": 1.5,
            })),
        }
    );
    assert_eq!(
        accepts,
        vec![
            "slack.message.received".to_owned(),
            "agent.slack-qa.scheduled".to_owned(),
        ]
    );
    assert_eq!(publishes, vec!["slack.message.post"]);
    assert_eq!(returns, vec!["support.completed"]);
    assert_eq!(skills, vec!["code-review"]);
    assert!(!skills_inherit);
    assert_eq!(tools, vec!["read"]);
    assert!(!tools_inherit);
    assert_eq!(
        schedules,
        vec![ScheduleEntry {
            cron: "0 18 * * 0".to_owned(),
            timezone: Some("Europe/Paris".to_owned()),
            catchup: Some("latest".to_owned()),
        }]
    );
    assert_eq!(
        retry,
        Some(RetrySpec {
            max_attempts: Some(3),
            backoff_seconds: Some(1.5),
        })
    );
    assert_eq!(base_dir, Some(PathBuf::from("/srv/zeta")));
    assert_eq!(
        ingress,
        vec![IngressBinding {
            event: "slack.message.received".to_owned(),
            filter: object(json!({"channel_ids": ["C123"]})),
            idempotency_key: Some("slack:{team_id}:{message_ts}".to_owned()),
        }]
    );
    assert_eq!(
        egress,
        vec![EgressBinding {
            event: "slack.message.post".to_owned(),
            options: object(json!({"channel_ids": ["C123"]})),
            idempotency_key: None,
        }]
    );
    assert_eq!(
        extensions,
        object(json!({"writes": {"paths": ["docs/**.md"]}}))
    );
}

#[test]
fn omitted_and_explicit_empty_capabilities_keep_inheritance_distinct() {
    let omitted = parse_agent(
        Path::new("worker.md"),
        b"---\nname: Worker\ndescription: Works.\n---\nWork.\n",
    )
    .unwrap();
    let explicit = parse_agent(
        Path::new("worker.md"),
        b"---\nname: Worker\ndescription: Works.\nskills: null\ntools: []\n---\nWork.\n",
    )
    .unwrap();

    assert!(omitted.skills_inherit);
    assert!(omitted.tools_inherit);
    assert_eq!(omitted.session, "per-event");
    assert_eq!(omitted.executor, ExecutorSpec::default());
    assert!(!explicit.skills_inherit);
    assert!(!explicit.tools_inherit);
    assert!(explicit.skills.is_empty());
    assert!(explicit.tools.is_empty());
}

#[test]
fn portable_yaml_profile_matches_json_scalars() {
    let spec = parse_agent(
        Path::new("worker.md"),
        b"---\nname: yes\ndescription: 2026-08-11\nenabled: true\nretry:\n  max_attempts: 0o12\n  backoff_seconds: 1e3\nmetadata:\n  affirmative: yes\n  disabled: off\n  leading_zero: 012\n  sexagesimal: 1:20\n  separated: 1_000\n  date: 2026-08-11\n  overflow: 1e400\n---\nWork.\n",
    )
    .unwrap();

    assert_eq!(spec.name, "yes");
    assert_eq!(spec.description, "2026-08-11");
    assert_eq!(
        spec.retry,
        Some(RetrySpec {
            max_attempts: Some(10),
            backoff_seconds: Some(1000.0),
        })
    );
    assert_eq!(
        spec.extensions,
        object(json!({
            "metadata": {
                "affirmative": "yes",
                "disabled": "off",
                "leading_zero": "012",
                "sexagesimal": "1:20",
                "separated": "1_000",
                "date": "2026-08-11",
                "overflow": "1e400",
            }
        }))
    );
}

#[test]
fn non_json_yaml_constructs_are_rejected() {
    let declarations = [
        "metadata:\n  key: first\n  key: second\n",
        "defaults: &defaults\n  value: one\nmetadata:\n  <<: *defaults\n",
        "metadata:\n  1: value\n",
        "metadata:\n  score: .nan\n",
        "metadata:\n  number: 18446744073709551616\n",
        "metadata: &metadata\n  self: *metadata\n",
    ];

    for declaration in declarations {
        let source = format!("---\nname: Worker\ndescription: Works.\n{declaration}---\nWork.\n");
        assert!(parse_agent(Path::new("worker.md"), source.as_bytes()).is_err());
    }
}

#[test]
fn shared_authoring_vectors_match_parser() {
    let vectors = authoring_vectors();
    assert_eq!(vectors["format"], "zeta-authoring-agent-v0");

    for vector in vectors["valid"].as_array().unwrap() {
        let name = vector["name"].as_str().unwrap();
        let path = Path::new(vector["path"].as_str().unwrap());
        let source = vector["source_utf8"].as_str().unwrap().as_bytes();
        let spec = parse_agent(path, source).unwrap_or_else(|error| panic!("{name}: {error}"));
        let mut actual = serde_json::to_value(spec).unwrap();
        actual.as_object_mut().unwrap().remove("path");
        assert_eq!(actual, vector["expected"], "{name}");
    }

    for vector in vectors["invalid"].as_array().unwrap() {
        let name = vector["name"].as_str().unwrap();
        let path = Path::new(vector["path"].as_str().unwrap());
        let source = vector["source_utf8"].as_str().unwrap().as_bytes();
        assert!(parse_agent(path, source).is_err(), "{name}");
    }
}

#[test]
fn parser_uses_supplied_bytes_and_logical_path_without_reading_the_filesystem() {
    let path = Path::new("/this/path/does/not/exist/worker.md");
    let source = b"---\r\nname: Worker\r\ndescription: Works.\r\n---\r\nKeep CRLF.\r\n";

    let spec = parse_agent(path, source).unwrap();

    assert_eq!(spec.path, path);
    assert_eq!(spec.instructions, "Keep CRLF.\r\n");
    assert_eq!(spec.content_address, zeta::substrate::hash_bytes(source));
}

#[test]
fn schedules_add_one_synthetic_accept_type() {
    let spec = parse_agent(
        Path::new("digest.md"),
        b"---\nname: Digest\ndescription: Summarizes.\naccepts: [repo.changed]\nschedules:\n  - cron: '* * * * *'\n  - cron: '0 18 * * 0'\n---\nSummarize.\n",
    )
    .unwrap();

    assert_eq!(
        spec.accepts,
        vec![
            "repo.changed".to_owned(),
            "agent.digest.scheduled".to_owned()
        ]
    );
    assert!(matches(&spec, "repo.changed"));
    assert!(matches(&spec, "agent.digest.scheduled"));
    assert_eq!(scheduled_event_type("digest"), "agent.digest.scheduled");
}

#[test]
fn disabled_agents_never_match_events() {
    let spec = parse_agent(
        Path::new("worker.md"),
        b"---\nname: Worker\ndescription: Works.\nenabled: false\naccepts: [work.requested]\n---\nWork.\n",
    )
    .unwrap();

    assert!(!matches(&spec, "work.requested"));
}

#[test]
fn malformed_source_reports_stable_error_kinds() {
    let cases: &[(&str, &[u8], SpecErrorKind, Option<&str>)] = &[
        (
            "missing.md",
            b"name: Missing\n",
            SpecErrorKind::MissingFrontmatterDelimiter,
            None,
        ),
        (
            "unclosed.md",
            b"---\nname: Missing\n",
            SpecErrorKind::MissingClosingFrontmatterDelimiter,
            None,
        ),
        (
            "bad slug.md",
            b"---\nname: Bad\ndescription: Bad.\n---\n",
            SpecErrorKind::InvalidSlug,
            None,
        ),
        (
            "missing.md",
            b"---\ndescription: Missing name.\n---\n",
            SpecErrorKind::MissingRequiredField,
            Some("name"),
        ),
        (
            "worker.md",
            b"---\nname: Worker\ndescription: Works.\nenabled: maybe\n---\n",
            SpecErrorKind::InvalidField,
            Some("enabled"),
        ),
        (
            "worker.md",
            b"---\nname: Worker\ndescription: Works.\nexecutor:\n  provider: local\n  config:\n    1: value\n---\n",
            SpecErrorKind::InvalidField,
            Some("executor"),
        ),
    ];

    for (path, source, expected_kind, expected_field) in cases {
        let error = parse_agent(Path::new(path), source).unwrap_err();
        assert_eq!(error.kind(), *expected_kind);
        assert_eq!(error.field(), *expected_field);
    }
}

#[test]
fn malformed_utf8_is_rejected_before_frontmatter_parsing() {
    let error = parse_agent(Path::new("worker.md"), b"---\n\xff\n---\n").unwrap_err();

    assert_eq!(error.kind(), SpecErrorKind::InvalidUtf8);
}

#[test]
fn nested_declarations_reject_unsupported_fields_and_values() {
    let cases: &[(&[u8], &str)] = &[
        (
            b"---\nname: Worker\ndescription: Works.\nmodel:\n  name: qwen\n  url: local\n  timeout: 3\n---\n",
            "model",
        ),
        (
            b"---\nname: Worker\ndescription: Works.\nretry:\n  max_attempts: 0\n---\n",
            "retry",
        ),
        (
            b"---\nname: Worker\ndescription: Works.\nschedules:\n  - cron: '* * * * *'\n    payload: {}\n---\n",
            "schedules",
        ),
        (
            b"---\nname: Worker\ndescription: Works.\npublishes:\n  - event: work.completed\n    filter: {}\n---\n",
            "publishes",
        ),
        (
            b"---\nname: Worker\ndescription: Works.\nbase_dir: relative/path\n---\n",
            "base_dir",
        ),
    ];

    for (source, expected_field) in cases {
        let error = parse_agent(Path::new("worker.md"), source).unwrap_err();
        assert_eq!(error.kind(), SpecErrorKind::InvalidField);
        assert_eq!(error.field(), Some(*expected_field));
    }
}

#[test]
fn frontmatter_shape_failures_keep_distinct_diagnostics() {
    let cases: &[(&[u8], SpecErrorKind, Option<&str>)] = &[
        (b"---\nname: [\n---\n", SpecErrorKind::InvalidYaml, None),
        (
            b"---\n- name\n- description\n---\n",
            SpecErrorKind::ExpectedFrontmatterObject,
            None,
        ),
        (
            b"---\nname: Worker\n---\n",
            SpecErrorKind::MissingRequiredField,
            Some("description"),
        ),
        (
            b"---\nname: Worker\ndescription: Works.\n1: value\n---\n",
            SpecErrorKind::ExpectedFrontmatterObject,
            None,
        ),
        (
            b"---\nname: Worker\ndescription: Works.\nmode: .nan\n---\n",
            SpecErrorKind::InvalidField,
            Some("mode"),
        ),
    ];

    for (source, expected_kind, expected_field) in cases {
        let error = parse_agent(Path::new("worker.md"), source).unwrap_err();
        assert_eq!(error.kind(), *expected_kind);
        assert_eq!(error.field(), *expected_field);
    }
}

#[test]
fn every_core_declaration_rejects_the_wrong_shape() {
    let cases: &[(&str, &str)] = &[
        ("session: later\n", "session"),
        ("enabled: yes\n", "enabled"),
        ("accepts: work.requested\n", "accepts"),
        ("publishes: [1]\n", "publishes"),
        ("returns: [work.completed, 1]\n", "returns"),
        ("skills: code-review\n", "skills"),
        ("tools: [read, '']\n", "tools"),
        ("model:\n  name: qwen\n", "model"),
        ("executor: {}\n", "executor"),
        (
            "executor:\n  provider: local\n  config:\n    threshold: .inf\n",
            "executor",
        ),
        ("accepts:\n  - filter: {}\n", "accepts"),
        (
            "accepts:\n  - event: work.requested\n    filter: []\n",
            "accepts",
        ),
        (
            "publishes:\n  - event: work.completed\n    with: []\n",
            "publishes",
        ),
        (
            "publishes:\n  - event: work.completed\n    idempotency_key: 1\n",
            "publishes",
        ),
        ("schedules: hourly\n", "schedules"),
        ("schedules: [hourly]\n", "schedules"),
        ("schedules:\n  - timezone: UTC\n", "schedules"),
        (
            "schedules:\n  - cron: '* * * * *'\n    timezone: 1\n",
            "schedules",
        ),
        (
            "schedules:\n  - cron: '* * * * *'\n    catchup: all\n",
            "schedules",
        ),
        ("retry: always\n", "retry"),
        ("retry:\n  max_attempts: true\n", "retry"),
        ("retry:\n  backoff_seconds: -1\n", "retry"),
        ("retry:\n  jitter: true\n", "retry"),
        ("base_dir: false\n", "base_dir"),
    ];

    for (declaration, expected_field) in cases {
        let source = format!("---\nname: Worker\ndescription: Works.\n{declaration}---\nWork.\n");
        let error = parse_agent(Path::new("worker.md"), source.as_bytes()).unwrap_err();
        assert_eq!(error.kind(), SpecErrorKind::InvalidField);
        assert_eq!(error.field(), Some(*expected_field));
    }
}

#[test]
fn session_templates_and_home_relative_base_directories_are_preserved() {
    let spec = parse_agent(
        Path::new("support.md"),
        b"---\nname: Support\ndescription: Helps.\nsession: 'chat:{chat_id}'\nbase_dir: ~/vaults/CEO\n---\nHelp.\n",
    )
    .unwrap();

    assert_eq!(spec.session, "chat:{chat_id}");
    assert_eq!(spec.base_dir, Some(PathBuf::from("~/vaults/CEO")));
}

#[test]
fn authored_accepts_do_not_duplicate_the_synthetic_schedule_event() {
    let spec = parse_agent(
        Path::new("digest.md"),
        b"---\nname: Digest\ndescription: Summarizes.\naccepts: [agent.digest.scheduled]\nschedules:\n  - cron: '* * * * *'\n---\n",
    )
    .unwrap();

    assert_eq!(spec.accepts, vec!["agent.digest.scheduled"]);
}
