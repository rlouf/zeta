//! Public behavior tests for authored agent declarations.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};
use zeta_manifest::{
    agent_accepts_event, derive_returns_schema, load_agent, parse_agent, parse_skill,
    render_prompt, scheduled_event_type, validate_prompt, AgentSpec, EgressBinding, EventRegistry,
    ExecutorSpec, IngressBinding, ManifestErrorKind, ModelSpec, RetrySpec, ScheduleEntry,
    SkillResource, SkillSpec, SpecErrorKind,
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
    let spec = parse_agent("slack-qa", COMPLETE_AGENT).unwrap();
    let AgentSpec {
        slug,
        name,
        description,
        instructions,
        source,
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
        locks,
        extensions,
    } = spec;

    assert_eq!(slug, "slack-qa");
    assert_eq!(name, "Slack Q&A");
    assert_eq!(description, "Answers workspace questions.");
    assert_eq!(instructions, "User asked: {{ event.payload.text }}\n");
    assert_eq!(source.as_bytes(), COMPLETE_AGENT);
    assert_eq!(
        content_address.to_string(),
        "b3:3b11502a246625eee17b4262ef24f46d85e0d8cafe0167adc281058b64d131dc"
    );
    assert!(enabled);
    assert_eq!(session, "shared");
    assert_eq!(
        model,
        Some(ModelSpec::Endpoint {
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
    assert!(locks.is_empty());
    assert_eq!(
        extensions,
        object(json!({"writes": {"paths": ["docs/**.md"]}}))
    );
}

#[test]
fn omitted_and_explicit_empty_capabilities_keep_inheritance_distinct() {
    let omitted = parse_agent(
        "worker",
        b"---\nname: Worker\ndescription: Works.\n---\nWork.\n",
    )
    .unwrap();
    let explicit = parse_agent(
        "worker",
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
        "worker",
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
        assert!(parse_agent("worker", source.as_bytes()).is_err());
    }
}

#[test]
fn shared_authoring_vectors_match_parser() {
    let vectors = authoring_vectors();
    assert_eq!(vectors["format"], "zeta-authoring-v1");

    for vector in vectors["agents"]["valid"].as_array().unwrap() {
        let name = vector["name"].as_str().unwrap();
        let path = Path::new(vector["path"].as_str().unwrap());
        let slug = path.file_stem().unwrap().to_str().unwrap();
        let source = vector["source_utf8"].as_str().unwrap().as_bytes();
        let spec = parse_agent(slug, source).unwrap_or_else(|error| panic!("{name}: {error}"));
        let actual = serde_json::to_value(spec).unwrap();
        assert_eq!(actual, vector["expected"], "{name}");
    }

    for vector in vectors["agents"]["invalid"].as_array().unwrap() {
        let name = vector["name"].as_str().unwrap();
        let path = Path::new(vector["path"].as_str().unwrap());
        let slug = path.file_stem().unwrap().to_str().unwrap();
        let source = vector["source_utf8"].as_str().unwrap().as_bytes();
        assert!(parse_agent(slug, source).is_err(), "{name}");
    }

    for vector in vectors["event_schemas"].as_array().unwrap() {
        let mut events = EventRegistry::new();
        let mut failure = None;
        for declaration in vector["declarations"].as_array().unwrap() {
            let event_type = declaration["event_type"].as_str().unwrap();
            let schema = if declaration["schema"].is_null() {
                None
            } else {
                Some(declaration["schema"].as_object().unwrap().clone())
            };
            if let Err(error) = events.register(event_type, schema) {
                failure = Some(error);
                break;
            }
        }
        if let Some(reason) = vector.get("error").and_then(Value::as_str) {
            assert_eq!(
                failure.unwrap().kind().reason(),
                reason,
                "{}",
                vector["name"]
            );
            continue;
        }
        assert!(failure.is_none(), "{}", vector["name"]);
        let mut actual = Vec::new();
        for (event_type, schema) in events.iter() {
            actual.push(Value::Array(vec![
                Value::String(event_type.clone()),
                schema.clone().map(Value::Object).unwrap_or(Value::Null),
            ]));
        }
        let actual = Value::Array(actual);
        assert_eq!(actual, vector["expected"], "{}", vector["name"]);
    }

    for vector in vectors["returned_schemas"].as_array().unwrap() {
        let mut spec = parse_agent(
            "worker",
            b"---\nname: Worker\ndescription: Works.\n---\nWork.\n",
        )
        .unwrap();
        let mut returns = Vec::new();
        for value in vector["returns"].as_array().unwrap() {
            returns.push(value.as_str().unwrap().to_owned());
        }
        spec.returns = returns;
        let mut events = EventRegistry::new();
        for (event_type, schema) in vector["events"].as_object().unwrap() {
            let schema = schema.as_object().cloned();
            events.register(event_type, schema).unwrap();
        }
        let result = derive_returns_schema(&spec, &events);
        if let Some(reason) = vector.get("error").and_then(Value::as_str) {
            assert_eq!(
                result.unwrap_err().kind().reason(),
                reason,
                "{}",
                vector["name"]
            );
            continue;
        }
        assert_eq!(
            result.unwrap().unwrap_or(Value::Null),
            vector["expected"],
            "{}",
            vector["name"]
        );
    }

    for vector in vectors["prompts"].as_array().unwrap() {
        let spec =
            parse_agent("worker", vector["source_utf8"].as_str().unwrap().as_bytes()).unwrap();
        let validation = validate_prompt(&spec);
        if let Some(reason) = vector.get("validation_error").and_then(Value::as_str) {
            assert_eq!(
                validation.unwrap_err().kind().reason(),
                reason,
                "{}",
                vector["name"]
            );
            continue;
        }
        validation.unwrap();
        let rendered = render_prompt(&spec, &vector["event"]).unwrap();
        assert_eq!(
            rendered,
            vector["expected"].as_str().unwrap(),
            "{}",
            vector["name"]
        );
    }

    for vector in vectors["skills"]["flat"].as_array().unwrap() {
        let skill = SkillResource::new(
            vector["name"].as_str().unwrap(),
            vector["body_utf8"].as_str().unwrap().as_bytes(),
        )
        .unwrap();
        assert_eq!(
            skill.object_id.to_string(),
            vector["expected_object_id"].as_str().unwrap()
        );
    }
    for vector in vectors["skills"]["skill_markdown"].as_array().unwrap() {
        let result = parse_skill(
            vector["fallback_name"].as_str().unwrap(),
            vector["source_utf8"].as_str().unwrap().as_bytes(),
        );
        if let Some(reason) = vector.get("error").and_then(Value::as_str) {
            assert_eq!(
                result.unwrap_err().kind().reason(),
                reason,
                "{}",
                vector["fallback_name"]
            );
            continue;
        }
        assert_eq!(
            serde_json::to_value(result.unwrap()).unwrap(),
            vector["expected"]
        );
    }

    for vector in vectors["connectors"].as_array().unwrap() {
        let implementation = zeta_manifest::ImplementationFingerprint::new(
            zeta_substrate::hash_bytes(vector["implementation_utf8"].as_str().unwrap().as_bytes()),
        );
        let connector = zeta_manifest::parse_connector(&vector["describe"], implementation)
            .unwrap_or_else(|error| panic!("{}: {error}", vector["name"]));
        assert_eq!(serde_json::to_value(connector).unwrap(), vector["expected"]);
    }

    for vector in vectors["semantic_diagnostics"].as_array().unwrap() {
        let error = semantic_fixture_error(vector["fixture"].as_str().unwrap());
        assert_eq!(
            error.kind().reason(),
            vector["error"].as_str().unwrap(),
            "{}",
            vector["fixture"]
        );
    }

    let project = zeta_manifest::compile_project(manifest_project_input(false)).unwrap();
    let project_manifest = zeta_manifest::project_manifest(&project).unwrap();
    let execution =
        zeta_manifest::execution_manifest(&project, &project_manifest.id, "worker").unwrap();
    assert_eq!(
        project_manifest.id.to_string(),
        vectors["projects"]["expected_project_id"].as_str().unwrap()
    );
    assert_eq!(
        execution.id.to_string(),
        vectors["projects"]["expected_worker_execution_id"]
            .as_str()
            .unwrap()
    );
    let encoded = serde_json::to_string(&project_manifest).unwrap();
    for field in vectors["projects"]["excluded_fields"].as_array().unwrap() {
        assert!(!encoded.contains(field.as_str().unwrap()));
    }
}

#[test]
fn parser_uses_supplied_bytes_without_reading_the_filesystem() {
    let source = b"---\r\nname: Worker\r\ndescription: Works.\r\n---\r\nKeep CRLF.\r\n";

    let spec = parse_agent("worker", source).unwrap();

    assert_eq!(spec.instructions, "Keep CRLF.\r\n");
    assert_eq!(spec.content_address, zeta_substrate::hash_bytes(source));
}

#[test]
fn loader_reads_exact_bytes_and_derives_the_filename_slug() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("worker.md");
    let source = b"---\r\nname: Worker\r\ndescription: Works.\r\n---\r\nKeep CRLF.\r\n";
    fs::write(&path, source).unwrap();

    let loaded = load_agent(&path).unwrap();
    let parsed = parse_agent("worker", source).unwrap();

    assert_eq!(loaded, parsed);
    assert_eq!(loaded.slug, "worker");
}

#[test]
fn loader_attaches_the_real_path_to_parse_failures() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("worker.md");
    fs::write(&path, b"no frontmatter").unwrap();

    let error = load_agent(&path).unwrap_err();

    assert_eq!(error.kind(), SpecErrorKind::MissingFrontmatterDelimiter);
    assert_eq!(error.path(), Some(path.as_path()));
    assert_eq!(
        error.to_string(),
        format!(
            "missing_frontmatter_delimiter in {}: the first line must be ---",
            path.display()
        )
    );
}

#[test]
fn parser_errors_render_without_a_path() {
    let error = parse_agent("worker", b"no frontmatter").unwrap_err();

    assert_eq!(
        error.to_string(),
        "missing_frontmatter_delimiter: the first line must be ---"
    );
}

#[test]
fn loader_reports_missing_files_with_the_real_path() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("missing.md");

    let error = load_agent(&path).unwrap_err();

    assert_eq!(error.kind(), SpecErrorKind::Io);
    assert_eq!(error.path(), Some(path.as_path()));
}

#[test]
fn loader_validates_the_filename_slug() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("Bad Worker.md");
    fs::write(
        &path,
        b"---\nname: Worker\ndescription: Works.\n---\nWork.\n",
    )
    .unwrap();

    let error = load_agent(&path).unwrap_err();

    assert_eq!(error.kind(), SpecErrorKind::InvalidSlug);
    assert_eq!(error.path(), Some(path.as_path()));
}

#[test]
fn schedules_add_one_synthetic_accept_type() {
    let spec = parse_agent(
        "digest",
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
    assert!(agent_accepts_event(&spec, "repo.changed"));
    assert!(agent_accepts_event(&spec, "agent.digest.scheduled"));
    assert_eq!(scheduled_event_type("digest"), "agent.digest.scheduled");
}

#[test]
fn disabled_agents_never_accept_events() {
    let spec = parse_agent(
        "worker",
        b"---\nname: Worker\ndescription: Works.\nenabled: false\naccepts: [work.requested]\n---\nWork.\n",
    )
    .unwrap();

    assert!(!agent_accepts_event(&spec, "work.requested"));
}

#[test]
fn malformed_source_reports_stable_error_kinds() {
    let cases: &[(&str, &[u8], SpecErrorKind, Option<&str>)] = &[
        (
            "missing",
            b"name: Missing\n",
            SpecErrorKind::MissingFrontmatterDelimiter,
            None,
        ),
        (
            "unclosed",
            b"---\nname: Missing\n",
            SpecErrorKind::MissingClosingFrontmatterDelimiter,
            None,
        ),
        (
            "bad slug",
            b"---\nname: Bad\ndescription: Bad.\n---\n",
            SpecErrorKind::InvalidSlug,
            None,
        ),
        (
            "missing",
            b"---\ndescription: Missing name.\n---\n",
            SpecErrorKind::MissingRequiredField,
            Some("name"),
        ),
        (
            "worker",
            b"---\nname: Worker\ndescription: Works.\nenabled: maybe\n---\n",
            SpecErrorKind::InvalidField,
            Some("enabled"),
        ),
        (
            "worker",
            b"---\nname: Worker\ndescription: Works.\nexecutor:\n  provider: local\n  config:\n    1: value\n---\n",
            SpecErrorKind::InvalidField,
            Some("executor"),
        ),
    ];

    for (slug, source, expected_kind, expected_field) in cases {
        let error = parse_agent(slug, source).unwrap_err();
        assert_eq!(error.kind(), *expected_kind);
        assert_eq!(error.field(), *expected_field);
        assert_eq!(error.path(), None);
    }
}

#[test]
fn malformed_utf8_is_rejected_before_frontmatter_parsing() {
    let error = parse_agent("worker", b"---\n\xff\n---\n").unwrap_err();

    assert_eq!(error.kind(), SpecErrorKind::InvalidUtf8);
    assert_eq!(error.path(), None);
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
    ];

    for (source, expected_field) in cases {
        let error = parse_agent("worker", source).unwrap_err();
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
        let error = parse_agent("worker", source).unwrap_err();
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
        ("base_dir: ''\n", "base_dir"),
        ("base_dir: false\n", "base_dir"),
    ];

    for (declaration, expected_field) in cases {
        let source = format!("---\nname: Worker\ndescription: Works.\n{declaration}---\nWork.\n");
        let error = parse_agent("worker", source.as_bytes()).unwrap_err();
        assert_eq!(error.kind(), SpecErrorKind::InvalidField);
        assert_eq!(error.field(), Some(*expected_field));
    }
}

#[test]
fn session_templates_and_authored_base_directories_are_preserved() {
    let home_relative = parse_agent(
        "support",
        b"---\nname: Support\ndescription: Helps.\nsession: 'chat:{chat_id}'\nbase_dir: ~/vaults/CEO\n---\nHelp.\n",
    )
    .unwrap();
    let project_relative = parse_agent(
        "support",
        b"---\nname: Support\ndescription: Helps.\nbase_dir: worktrees/review\n---\nHelp.\n",
    )
    .unwrap();

    assert_eq!(home_relative.session, "chat:{chat_id}");
    assert_eq!(home_relative.base_dir, Some(PathBuf::from("~/vaults/CEO")));
    assert_eq!(
        project_relative.base_dir,
        Some(PathBuf::from("worktrees/review"))
    );
}

#[test]
fn authored_accepts_do_not_duplicate_the_synthetic_schedule_event() {
    let spec = parse_agent(
        "digest",
        b"---\nname: Digest\ndescription: Summarizes.\naccepts: [agent.digest.scheduled]\nschedules:\n  - cron: '* * * * *'\n---\n",
    )
    .unwrap();

    assert_eq!(spec.accepts, vec!["agent.digest.scheduled"]);
}

#[test]
fn locks_are_typed_core_declarations() {
    let one = parse_agent(
        "worker",
        b"---\nname: Worker\ndescription: Works.\nlocks: context:repo\n---\n",
    )
    .unwrap();
    let several = parse_agent(
        "worker",
        b"---\nname: Worker\ndescription: Works.\nlocks: [context:repo, branch:main, context:repo]\n---\n",
    )
    .unwrap();

    assert_eq!(one.locks, vec!["context:repo"]);
    assert_eq!(several.locks, vec!["context:repo", "branch:main"]);
    assert!(!one.extensions.contains_key("locks"));

    for declaration in ["locks: 1\n", "locks: ['']\n", "locks: [context:repo, 1]\n"] {
        let source = format!("---\nname: Worker\ndescription: Works.\n{declaration}---\n");
        let error = parse_agent("worker", source.as_bytes()).unwrap_err();
        assert_eq!(error.kind(), SpecErrorKind::InvalidField);
        assert_eq!(error.field(), Some("locks"));
    }
}

#[test]
fn event_registry_validates_schemas_and_merges_equal_declarations() {
    let mut events = EventRegistry::new();
    let schema = object(json!({
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
        "additionalProperties": false,
    }));

    events
        .register("work.requested", Some(schema.clone()))
        .unwrap();
    events
        .register("work.requested", Some(schema.clone()))
        .unwrap();

    assert!(events.knows("work.requested"));
    assert_eq!(events.schema("work.requested"), Some(Some(&schema)));
    assert_eq!(events.iter().count(), 1);

    let conflict = events
        .register("work.requested", Some(object(json!({"type": "string"}))))
        .unwrap_err();
    assert_eq!(conflict.kind(), ManifestErrorKind::ConflictingDeclaration);
    assert_eq!(conflict.subject(), Some("work.requested"));

    let malformed = events
        .register(
            "work.invalid",
            Some(object(json!({"type": "definitely-not-a-json-type"}))),
        )
        .unwrap_err();
    assert_eq!(malformed.kind(), ManifestErrorKind::InvalidSchema);
    assert_eq!(malformed.subject(), Some("work.invalid"));
}

#[test]
fn manifest_error_kinds_have_stable_reasons() {
    let cases = [
        (ManifestErrorKind::InvalidSchema, "invalid_schema"),
        (
            ManifestErrorKind::ConflictingDeclaration,
            "conflicting_declaration",
        ),
        (
            ManifestErrorKind::DuplicateDeclaration,
            "duplicate_declaration",
        ),
        (ManifestErrorKind::UnknownEvent, "unknown_event"),
        (
            ManifestErrorKind::InvalidPromptSyntax,
            "invalid_prompt_syntax",
        ),
        (ManifestErrorKind::UnknownPromptRoot, "unknown_prompt_root"),
        (ManifestErrorKind::PromptRender, "prompt_render"),
        (ManifestErrorKind::InvalidSkill, "invalid_skill"),
        (ManifestErrorKind::InvalidConnector, "invalid_connector"),
        (ManifestErrorKind::InvalidCapability, "invalid_capability"),
        (
            ManifestErrorKind::InvalidExecutorProvider,
            "invalid_executor_provider",
        ),
        (ManifestErrorKind::InvalidModel, "invalid_model"),
        (ManifestErrorKind::InvalidAgent, "invalid_agent"),
        (ManifestErrorKind::UnknownTool, "unknown_tool"),
        (ManifestErrorKind::ReservedTool, "reserved_tool"),
        (ManifestErrorKind::UnknownSkill, "unknown_skill"),
        (
            ManifestErrorKind::UnknownExecutorProvider,
            "unknown_executor_provider",
        ),
        (ManifestErrorKind::UnknownExtension, "unknown_extension"),
        (ManifestErrorKind::InvalidBinding, "invalid_binding"),
        (ManifestErrorKind::InvalidManifest, "invalid_manifest"),
        (ManifestErrorKind::InvalidIdentity, "invalid_identity"),
        (ManifestErrorKind::UnknownAgent, "unknown_agent"),
    ];

    for (kind, reason) in cases {
        assert_eq!(kind.reason(), reason);
    }
}

#[test]
fn manifest_errors_expose_a_stable_public_contract() {
    let mut events = EventRegistry::new();
    let error = events.register("", None).unwrap_err();
    let duplicate = events.register("", None).unwrap_err();
    let standard_error: &dyn std::error::Error = &error;

    assert_eq!(error, duplicate);
    assert_eq!(error.kind(), ManifestErrorKind::InvalidSchema);
    assert_eq!(error.subject(), Some(""));
    assert_eq!(error.field(), None);
    assert_eq!(error.detail(), "event type must be non-empty");
    assert_eq!(
        standard_error.to_string(),
        "invalid_schema for \"\": event type must be non-empty"
    );
}

#[test]
fn event_registry_iteration_is_sorted_and_scheduled_events_describe_occurrences() {
    let mut events = EventRegistry::new();
    events.register("z.last", None).unwrap();
    events.register("a.first", None).unwrap();
    events.register_scheduled("digest").unwrap();

    let mut names = Vec::new();
    for (name, _schema) in events.iter() {
        names.push(name.as_str());
    }
    assert_eq!(names, vec!["a.first", "agent.digest.scheduled", "z.last"]);
    assert_eq!(
        events.schema("agent.digest.scheduled"),
        Some(Some(&object(json!({
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "timestamp": {"type": "string"},
            },
            "required": ["date", "timestamp"],
            "additionalProperties": false,
        }))))
    );
}

#[test]
fn returned_schema_hoists_and_rewrites_branch_local_definitions() {
    let spec = parse_agent(
        "worker",
        b"---\nname: Worker\ndescription: Works.\nreturns: [work.completed, audit.recorded]\n---\n",
    )
    .unwrap();
    let mut events = EventRegistry::new();
    events
        .register(
            "work.completed",
            Some(object(json!({
                "type": "object",
                "$defs": {
                    "result": {
                        "type": "object",
                        "properties": {"value": {"$ref": "#/$defs/value"}},
                    },
                    "value": {"type": "string"},
                },
                "properties": {"result": {"$ref": "#/$defs/result"}},
            }))),
        )
        .unwrap();
    events
        .register(
            "audit.recorded",
            Some(object(json!({
                "$defs": {"result": {"type": "integer"}},
                "$ref": "#/$defs/result",
            }))),
        )
        .unwrap();

    let schema = derive_returns_schema(&spec, &events).unwrap().unwrap();

    assert_eq!(schema["type"], "object");
    assert_eq!(
        schema["oneOf"][0]["properties"]["type"]["const"],
        "work.completed"
    );
    assert_eq!(
        schema["oneOf"][0]["properties"]["payload"]["properties"]["result"]["$ref"],
        "#/$defs/event_0_result"
    );
    assert_eq!(
        schema["$defs"]["event_0_result"]["properties"]["value"]["$ref"],
        "#/$defs/event_0_value"
    );
    assert_eq!(
        schema["oneOf"][1]["properties"]["payload"]["$ref"],
        "#/$defs/event_1_result"
    );
    assert_eq!(schema["$defs"]["event_1_result"]["type"], "integer");
}

#[test]
fn returned_schema_handles_none_and_rejects_unknown_events() {
    let without_returns =
        parse_agent("worker", b"---\nname: Worker\ndescription: Works.\n---\n").unwrap();
    assert_eq!(
        derive_returns_schema(&without_returns, &EventRegistry::new()).unwrap(),
        None
    );

    let unknown = parse_agent(
        "worker",
        b"---\nname: Worker\ndescription: Works.\nreturns: [missing.event]\n---\n",
    )
    .unwrap();
    let error = derive_returns_schema(&unknown, &EventRegistry::new()).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::UnknownEvent);
    assert_eq!(error.subject(), Some("missing.event"));
}

#[test]
fn connector_descriptions_become_language_neutral_declarations() {
    let description = json!({
        "id": "slack",
        "protocol_versions": [0],
        "events": {
            "slack.message.received": {
                "type": "object",
                "properties": {"text": {"type": "string"}}
            },
            "slack.message.post": {
                "type": "object",
                "properties": {"text": {"type": "string"}}
            }
        },
        "filters": {
            "slack.message.received": {
                "type": "object",
                "properties": {
                    "channel_ids": {
                        "type": "array",
                        "items": {"type": "string"}
                    }
                }
            }
        },
        "operations": [{
            "name": "slack.message.post",
            "semantics": "idempotent_with_key",
            "options_schema": {
                "type": "object",
                "properties": {"channel_id": {"type": "string"}}
            }
        }],
        "settings": ["SLACK_TOKEN"],
        "command": ["python", "-m", "connector"]
    });
    let fingerprint = zeta_manifest::ImplementationFingerprint::new(zeta_substrate::hash_bytes(
        b"slack implementation",
    ));

    let connector = zeta_manifest::parse_connector(&description, fingerprint).unwrap();

    assert_eq!(connector.id, "slack");
    assert_eq!(connector.protocol_versions, vec![0]);
    assert!(connector.events.knows("slack.message.received"));
    assert!(connector.ingress_event("slack.message.received"));
    assert!(!connector.ingress_event("slack.message.post"));
    assert_eq!(
        connector.operations["slack.message.post"].semantics,
        zeta_manifest::DeliverySemantics::IdempotentWithKey
    );
    let serialized = serde_json::to_value(&connector).unwrap();
    assert!(serialized.get("command").is_none());
}

#[test]
fn connector_descriptions_reject_invalid_protocols_operations_and_schemas() {
    let fingerprint =
        zeta_manifest::ImplementationFingerprint::new(zeta_substrate::hash_bytes(b"connector"));
    let cases = [
        json!({"id": "future", "protocol_versions": [1], "events": {}}),
        json!({
            "id": "missing-event",
            "protocol_versions": [0],
            "events": {},
            "operations": [{"name": "work.run", "semantics": "at_least_once"}]
        }),
        json!({
            "id": "unsafe",
            "protocol_versions": [0],
            "events": {"work.run": null},
            "operations": [{"name": "work.run", "semantics": "whenever"}]
        }),
        json!({
            "id": "bad-schema",
            "protocol_versions": [0],
            "events": {"work.run": {"type": "not-a-type"}}
        }),
        json!({
            "id": "duplicate-setting",
            "protocol_versions": [0],
            "events": {},
            "settings": ["TOKEN", "TOKEN"]
        }),
        json!({
            "id": "unknown-field",
            "protocol_versions": [0],
            "events": {},
            "future": true
        }),
    ];

    for description in cases {
        let error = zeta_manifest::parse_connector(&description, fingerprint.clone()).unwrap_err();
        assert_eq!(error.kind(), ManifestErrorKind::InvalidConnector);
    }
}

fn compiler_fingerprint(label: &str) -> zeta_manifest::ImplementationFingerprint {
    zeta_manifest::ImplementationFingerprint::new(zeta_substrate::hash_bytes(label.as_bytes()))
}

fn compiler_provider(id: &str) -> zeta_manifest::ExecutorProviderSpec {
    zeta_manifest::ExecutorProviderSpec {
        id: id.to_owned(),
        implementation: compiler_fingerprint(id),
    }
}

fn compiler_capability(id: &str, name: &str, owner: Option<&str>) -> zeta_manifest::CapabilitySpec {
    zeta_manifest::CapabilitySpec {
        id: id.parse().unwrap(),
        name: name.to_owned(),
        description: format!("Run {name}."),
        input_schema: serde_json::from_value(serde_json::json!({
            "type": "object",
            "additionalProperties": false
        }))
        .unwrap(),
        delivery_semantics: None,
        owner: owner.map(str::to_owned),
        implementation: compiler_fingerprint(id),
    }
}

fn compiler_model_selection(api: &str, tool_profile: &str) -> zeta_manifest::ModelSelectionSpec {
    zeta_manifest::ModelSelectionSpec {
        profile: "native".to_owned(),
        model: "test-model".to_owned(),
        url: "https://model.invalid/v1".to_owned(),
        thinking: Some("medium".to_owned()),
        api: api.to_owned(),
        tool_profile: tool_profile.to_owned(),
        implementation: compiler_fingerprint("model-adapter"),
    }
}

fn compiler_agent(slug: &str, frontmatter: &str) -> zeta_manifest::AgentSpec {
    let source = format!(
        "---\nname: {slug}\ndescription: Compiles {slug}.\n{frontmatter}---\n{{{{ event.payload }}}}\n"
    );
    zeta_manifest::parse_agent(slug, source.as_bytes()).unwrap()
}

fn compiler_input(agents: Vec<zeta_manifest::AgentSpec>) -> zeta_manifest::AgentProjectInput {
    zeta_manifest::AgentProjectInput {
        agents,
        events: zeta_manifest::EventRegistry::new(),
        skill_resources: Vec::new(),
        skill_specs: Vec::new(),
        connectors: Vec::new(),
        capabilities: Vec::new(),
        executor_providers: vec![compiler_provider("local")],
        model: None,
        runtime_fingerprint: compiler_fingerprint("zeta-runtime"),
    }
}

fn semantic_fixture_error(fixture: &str) -> zeta_manifest::ManifestError {
    let agent = match fixture {
        "unknown_event" => compiler_agent("worker", "accepts: [missing.event]\n"),
        "reserved_tool" => compiler_agent("worker", "tools: [publish_event]\n"),
        "unknown_skill" => compiler_agent("worker", "skills: [missing]\n"),
        "unknown_executor_provider" => compiler_agent("worker", "executor:\n  provider: missing\n"),
        "unknown_extension" => compiler_agent("worker", "future_section: true\n"),
        "invalid_connector_binding" => compiler_agent(
            "worker",
            "accepts:\n  - event: mail.received\n    idempotency_key: 'mail:{id}'\n",
        ),
        unknown => panic!("unknown semantic fixture {unknown:?}"),
    };
    let mut input = compiler_input(vec![agent]);
    if fixture == "invalid_connector_binding" {
        input.events.register("mail.received", None).unwrap();
    }
    zeta_manifest::compile_project(input).unwrap_err()
}

#[test]
fn project_compilation_normalizes_inheritance_and_owner_grants() {
    let inherited = compiler_agent(
        "inherited",
        "enabled: false\nschedules:\n  - cron: '0 * * * *'\n",
    );
    let explicit = compiler_agent("explicit", "tools: []\nskills: []\n");
    let mut input = compiler_input(vec![inherited, explicit]);
    input.capabilities = vec![
        compiler_capability("native.write", "write", None),
        compiler_capability("agent.inherited.echo", "echo", Some("inherited")),
        compiler_capability("native.read", "read", None),
    ];
    input.skill_resources = vec![
        zeta_manifest::SkillResource::new("review", b"Review.\n").unwrap(),
        zeta_manifest::SkillResource::new("build", b"Build.\n").unwrap(),
    ];

    let project = zeta_manifest::compile_project(input).unwrap();

    assert_eq!(
        project
            .agents
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        vec!["explicit", "inherited"]
    );
    let inherited = project.agents.get("inherited").unwrap();
    assert!(!inherited.enabled);
    assert_eq!(
        inherited.tools,
        ["agent.inherited.echo", "native.read", "native.write"]
    );
    assert_eq!(inherited.skills, ["build", "review"]);
    assert!(project.events.knows("agent.inherited.scheduled"));
    let explicit = project.agents.get("explicit").unwrap();
    assert!(explicit.tools.is_empty());
    assert!(explicit.skills.is_empty());
}

#[test]
fn project_compilation_rejects_duplicate_and_conflicting_declarations() {
    let agent = compiler_agent("worker", "");
    let mut duplicate_agent = compiler_input(vec![agent.clone(), agent.clone()]);
    let error = zeta_manifest::compile_project(duplicate_agent.clone()).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::DuplicateDeclaration);
    duplicate_agent.agents.pop();

    duplicate_agent.capabilities = vec![
        compiler_capability("native.read", "read", None),
        compiler_capability("native.read", "read", None),
    ];
    let error = zeta_manifest::compile_project(duplicate_agent).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::DuplicateDeclaration);

    let describe = serde_json::json!({
        "id": "mail",
        "protocol_versions": [0],
        "events": {"mail.received": {"type": "string"}}
    });
    let connector =
        zeta_manifest::parse_connector(&describe, compiler_fingerprint("mail")).unwrap();
    let mut conflict = compiler_input(vec![agent]);
    conflict.connectors.push(connector);
    conflict
        .events
        .register(
            "mail.received",
            Some(serde_json::from_value(serde_json::json!({"type": "object"})).unwrap()),
        )
        .unwrap();
    let error = zeta_manifest::compile_project(conflict).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::ConflictingDeclaration);
}

#[test]
fn project_compilation_rejects_ambiguous_aliases_and_invalid_typed_declarations() {
    let agent = compiler_agent("worker", "tools: [read]\n");
    let mut ambiguous = compiler_input(vec![agent]);
    ambiguous.capabilities = vec![
        compiler_capability("native.read", "read", None),
        compiler_capability("remote.read", "read", None),
    ];
    let error = zeta_manifest::compile_project(ambiguous).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::ConflictingDeclaration);

    let mut invalid_connector = zeta_manifest::parse_connector(
        &serde_json::json!({
            "id": "mail",
            "protocol_versions": [0],
            "events": {"mail.received": null}
        }),
        compiler_fingerprint("mail"),
    )
    .unwrap();
    invalid_connector.protocol_versions.clear();
    let mut input = compiler_input(vec![compiler_agent("worker", "")]);
    input.connectors.push(invalid_connector);
    let error = zeta_manifest::compile_project(input).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::InvalidConnector);

    let mut invalid_agent = compiler_agent("worker", "");
    invalid_agent.locks.push(String::new());
    let error = zeta_manifest::compile_project(compiler_input(vec![invalid_agent])).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::InvalidAgent);

    let mut empty_base_dir = compiler_agent("worker", "");
    empty_base_dir.base_dir = Some(PathBuf::new());
    let error = zeta_manifest::compile_project(compiler_input(vec![empty_base_dir])).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::InvalidAgent);
    assert_eq!(error.field(), Some("base_dir"));

    #[cfg(unix)]
    {
        use std::ffi::OsString;
        use std::os::unix::ffi::OsStringExt;

        let mut non_utf8_base_dir = compiler_agent("worker", "");
        non_utf8_base_dir.base_dir = Some(PathBuf::from(OsString::from_vec(vec![0xff])));
        let error =
            zeta_manifest::compile_project(compiler_input(vec![non_utf8_base_dir])).unwrap_err();
        assert_eq!(error.kind(), ManifestErrorKind::InvalidAgent);
        assert_eq!(error.field(), Some("base_dir"));
    }
}

#[test]
fn project_compilation_preserves_relative_base_directories_in_manifests() {
    let agent = compiler_agent("worker", "base_dir: worktrees/review\n");

    let project = zeta_manifest::compile_project(compiler_input(vec![agent])).unwrap();
    let manifest = zeta_manifest::project_manifest(&project).unwrap();

    assert_eq!(
        manifest.agents["worker"].base_dir,
        Some(PathBuf::from("worktrees/review"))
    );
}

#[test]
fn project_compilation_rejects_unknown_reserved_and_cross_owner_references() {
    let cases = [
        (
            compiler_agent("worker", "accepts: [missing.event]\n"),
            ManifestErrorKind::UnknownEvent,
        ),
        (
            compiler_agent("worker", "tools: [publish_event]\n"),
            ManifestErrorKind::ReservedTool,
        ),
        (
            compiler_agent("worker", "skills: [missing]\n"),
            ManifestErrorKind::UnknownSkill,
        ),
        (
            compiler_agent("worker", "executor:\n  provider: missing\n"),
            ManifestErrorKind::UnknownExecutorProvider,
        ),
        (
            compiler_agent("worker", "future_section: true\n"),
            ManifestErrorKind::UnknownExtension,
        ),
    ];
    for (agent, expected) in cases {
        let error = zeta_manifest::compile_project(compiler_input(vec![agent])).unwrap_err();
        assert_eq!(error.kind(), expected);
    }

    let worker = compiler_agent("worker", "tools: [other-secret]\n");
    let other = compiler_agent("other", "");
    let mut cross_owner = compiler_input(vec![worker, other]);
    cross_owner.capabilities = vec![compiler_capability(
        "agent.other.secret",
        "other-secret",
        Some("other"),
    )];
    let error = zeta_manifest::compile_project(cross_owner).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::UnknownTool);
}

#[test]
fn project_compilation_accepts_exact_model_vocabulary() {
    for api in ["chat-completions", "codex-responses"] {
        for tool_profile in ["native", "codex"] {
            let mut input = compiler_input(Vec::new());
            input.model = Some(compiler_model_selection(api, tool_profile));

            zeta_manifest::compile_project(input).unwrap();
        }
    }
}

#[test]
fn project_compilation_rejects_other_model_api_spellings() {
    for api in [
        "responses",
        "chat_completions",
        "codex_responses",
        "Chat-Completions",
        "codex-responses ",
    ] {
        let mut input = compiler_input(Vec::new());
        input.model = Some(compiler_model_selection(api, "native"));

        let error = zeta_manifest::compile_project(input).unwrap_err();
        assert_eq!(error.kind(), ManifestErrorKind::InvalidModel, "{api}");
        assert_eq!(error.field(), Some("api"), "{api}");
    }
}

#[test]
fn project_compilation_rejects_other_tool_profile_spellings() {
    for tool_profile in ["default", "openai", "Native", "codex-tools", "native "] {
        let mut input = compiler_input(Vec::new());
        input.model = Some(compiler_model_selection("chat-completions", tool_profile));

        let error = zeta_manifest::compile_project(input).unwrap_err();
        assert_eq!(
            error.kind(),
            ManifestErrorKind::InvalidModel,
            "{tool_profile}"
        );
        assert_eq!(error.field(), Some("tool_profile"), "{tool_profile}");
    }
}

#[test]
fn project_compilation_validates_connector_bindings() {
    let describe = serde_json::json!({
        "id": "mail",
        "protocol_versions": [0],
        "events": {
            "mail.received": null,
            "mail.send": null
        },
        "filters": {
            "mail.received": {
                "type": "object",
                "properties": {"folder": {"type": "string"}},
                "required": ["folder"],
                "additionalProperties": false
            }
        },
        "operations": [{
            "name": "mail.send",
            "semantics": "idempotent_with_key",
            "options_schema": {
                "type": "object",
                "properties": {"priority": {"type": "integer"}},
                "required": ["priority"],
                "additionalProperties": false
            }
        }]
    });
    let connector =
        zeta_manifest::parse_connector(&describe, compiler_fingerprint("mail")).unwrap();
    let valid = compiler_agent(
        "worker",
        "accepts:\n  - event: mail.received\n    filter: {folder: inbox}\n    idempotency_key: 'mail:{id}'\npublishes:\n  - event: mail.send\n    with: {priority: 1}\n",
    );
    let mut input = compiler_input(vec![valid]);
    input.connectors.push(connector.clone());
    zeta_manifest::compile_project(input).unwrap();

    let missing_key = compiler_agent(
        "worker",
        "accepts:\n  - event: mail.received\n    filter: {folder: inbox}\n",
    );
    let mut input = compiler_input(vec![missing_key]);
    input.connectors.push(connector.clone());
    let error = zeta_manifest::compile_project(input).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::InvalidBinding);

    let invalid_options = compiler_agent(
        "worker",
        "publishes:\n  - event: mail.send\n    with: {priority: urgent}\n",
    );
    let mut input = compiler_input(vec![invalid_options]);
    input.connectors.push(connector);
    let error = zeta_manifest::compile_project(input).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::InvalidBinding);
}

fn manifest_project_input(reverse: bool) -> zeta_manifest::AgentProjectInput {
    let worker = compiler_agent(
        "worker",
        "accepts: [work.requested]\nreturns: [work.completed]\ntools: [read]\nskills: [review]\n",
    );
    let idle = compiler_agent("idle", "tools: []\nskills: []\n");
    let mut input = compiler_input(if reverse {
        vec![idle, worker]
    } else {
        vec![worker, idle]
    });
    input
        .events
        .register(
            "work.requested",
            Some(serde_json::from_value(serde_json::json!({"type": "object"})).unwrap()),
        )
        .unwrap();
    input
        .events
        .register(
            "work.completed",
            Some(serde_json::from_value(serde_json::json!({"type": "object"})).unwrap()),
        )
        .unwrap();
    input.skill_resources =
        vec![zeta_manifest::SkillResource::new("review", b"Review carefully.\n").unwrap()];
    input.capabilities = vec![compiler_capability("native.read", "read", None)];
    input.model = Some(compiler_model_selection("codex-responses", "native"));
    input
}

#[test]
fn project_manifests_are_order_invariant_strict_and_content_addressed() {
    let project = zeta_manifest::compile_project(manifest_project_input(false)).unwrap();
    let reordered = zeta_manifest::compile_project(manifest_project_input(true)).unwrap();
    let manifest = zeta_manifest::project_manifest(&project).unwrap();
    let reordered_manifest = zeta_manifest::project_manifest(&reordered).unwrap();

    assert_eq!(manifest.id, reordered_manifest.id);
    assert!(manifest.id.to_string().starts_with("project:b3:"));
    let value = serde_json::to_value(&manifest).unwrap();
    let encoded = serde_json::to_string(&value).unwrap();
    assert!(!encoded.contains("command"));
    assert!(!encoded.contains("callable"));
    assert!(!encoded.contains("source_path"));
    let (restored, restored_project) = zeta_manifest::restore_project_manifest(&value).unwrap();
    assert_eq!(restored, manifest);
    assert_eq!(restored_project, project);

    let mut unknown = value.clone();
    unknown
        .as_object_mut()
        .unwrap()
        .insert("future".to_owned(), serde_json::Value::Bool(true));
    let error = zeta_manifest::restore_project_manifest(&unknown).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::InvalidManifest);

    let mut invalid_capability_id = value.clone();
    invalid_capability_id["capabilities"]["native.read"]["id"] = serde_json::json!("read");
    let error = zeta_manifest::restore_project_manifest(&invalid_capability_id).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::InvalidManifest);

    let mut tampered = value;
    tampered["skill_resources"]["review"]["body"] = serde_json::json!("Changed.\n");
    let error = zeta_manifest::restore_project_manifest(&tampered).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::InvalidIdentity);

    let mut tampered_source = serde_json::to_value(&manifest).unwrap();
    tampered_source["agents"]["worker"]["source"] =
        serde_json::json!("---\nname: Worker\ndescription: Changed.\n---\nChanged.\n");
    let error = zeta_manifest::restore_project_manifest(&tampered_source).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::InvalidIdentity);
}

#[test]
fn execution_manifests_select_only_agent_relevant_declarations() {
    let project = zeta_manifest::compile_project(manifest_project_input(false)).unwrap();
    let project_manifest = zeta_manifest::project_manifest(&project).unwrap();

    let manifest =
        zeta_manifest::execution_manifest(&project, &project_manifest.id, "worker").unwrap();

    assert!(manifest
        .id
        .to_string()
        .starts_with("execution_manifest:b3:"));
    assert_eq!(manifest.project_revision, project_manifest.id);
    assert_eq!(
        manifest
            .events
            .iter()
            .map(|(name, _schema)| name.as_str())
            .collect::<Vec<_>>(),
        ["work.completed", "work.requested"]
    );
    assert_eq!(manifest.skill_resources.keys().next().unwrap(), "review");
    assert_eq!(manifest.capabilities.keys().next().unwrap(), "native.read");
    assert!(manifest.connectors.is_empty());
    assert_eq!(manifest.executor_provider.id, "local");

    let value = serde_json::to_value(&manifest).unwrap();
    let restored = zeta_manifest::restore_execution_manifest(&value, &project_manifest).unwrap();
    assert_eq!(restored, manifest);

    let wrong_project =
        zeta_manifest::compile_project(compiler_input(vec![compiler_agent("other", "")])).unwrap();
    let wrong_project = zeta_manifest::project_manifest(&wrong_project).unwrap();
    let error = zeta_manifest::restore_execution_manifest(&value, &wrong_project).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::InvalidIdentity);
}

#[test]
fn prompt_validation_allows_only_event_and_template_locals() {
    let spec = parse_agent(
        "worker",
        b"---\nname: Worker\ndescription: Works.\n---\n{% set label = event.payload.label %}{{ label | upper }}\n",
    )
    .unwrap();

    validate_prompt(&spec).unwrap();

    let unknown = parse_agent(
        "worker",
        b"---\nname: Worker\ndescription: Works.\n---\n{{ payload.text }} {{ system.name }}\n",
    )
    .unwrap();
    let error = validate_prompt(&unknown).unwrap_err();
    assert_eq!(error.kind(), ManifestErrorKind::UnknownPromptRoot);
    assert_eq!(error.subject(), Some("worker"));
    assert_eq!(error.field(), Some("instructions"));
    assert!(error.detail().contains("payload"));
}

#[test]
fn prompt_validation_reports_syntax_errors() {
    let spec = parse_agent(
        "worker",
        b"---\nname: Worker\ndescription: Works.\n---\n{% if event.payload %}\n",
    )
    .unwrap();

    let error = validate_prompt(&spec).unwrap_err();

    assert_eq!(error.kind(), ManifestErrorKind::InvalidPromptSyntax);
    assert_eq!(error.subject(), Some("worker"));
    assert_eq!(error.field(), Some("instructions"));
}

#[test]
fn prompt_rendering_preserves_unicode_and_does_not_autoescape() {
    let spec = parse_agent(
        "worker",
        b"---\nname: Worker\ndescription: Works.\n---\nR\xc3\xa9ponse: {{ event.payload.text }}; missing={{ event.payload.missing }}\n",
    )
    .unwrap();

    let rendered = render_prompt(&spec, &json!({"payload": {"text": "<café & 東京>"}})).unwrap();

    assert_eq!(rendered, "Réponse: <café & 東京>; missing=");
}

#[test]
fn prompt_rendering_reports_runtime_errors() {
    let spec = parse_agent(
        "worker",
        b"---\nname: Worker\ndescription: Works.\n---\n{{ event.missing.deep }}\n",
    )
    .unwrap();

    let error = render_prompt(&spec, &json!({})).unwrap_err();

    assert_eq!(error.kind(), ManifestErrorKind::PromptRender);
    assert_eq!(error.subject(), Some("worker"));
    assert_eq!(error.field(), Some("instructions"));
}

#[test]
fn flat_skill_resources_use_exact_bytes_and_the_skill_object_identity() {
    let source = "Révise <ceci>.\r\n".as_bytes();

    let skill = SkillResource::new("code-review", source).unwrap();

    assert_eq!(skill.name, "code-review");
    assert_eq!(skill.body, "Révise <ceci>.\r\n");
    assert_eq!(
        skill.object_id.to_string(),
        "b3:c7a82e80c103adb406fa92c561ab8cccfea84c11d58a69b2c720ac661c31f7a9"
    );
}

#[test]
fn flat_skill_resources_reject_non_utf8_bytes() {
    let error = SkillResource::new("code-review", b"Review \xff").unwrap_err();

    assert_eq!(error.kind(), ManifestErrorKind::InvalidSkill);
    assert_eq!(error.subject(), Some("code-review"));
    assert_eq!(error.field(), Some("body"));
}

#[test]
fn skill_markdown_parses_metadata_and_preserves_the_body() {
    let source = b"---\r\nname: code-review\r\ndescription: 'Reviews changes: carefully'\r\ndisable-model-invocation: TRUE\r\nignored: metadata\r\n---\r\n# Review\r\nCheck the change.\r\n";

    let skill = parse_skill("fallback", source).unwrap();

    assert_eq!(
        skill,
        SkillSpec {
            name: "code-review".to_owned(),
            description: "Reviews changes: carefully".to_owned(),
            body: "# Review\r\nCheck the change.\r\n".to_owned(),
            disable_model_invocation: true,
        }
    );
}

#[test]
fn skill_markdown_uses_the_caller_supplied_fallback_name() {
    let skill = parse_skill(
        "incident-response",
        b"---\ndescription: Responds to incidents.\n---\nRespond safely.\n",
    )
    .unwrap();

    assert_eq!(skill.name, "incident-response");
    assert_eq!(skill.description, "Responds to incidents.");
    assert_eq!(skill.body, "Respond safely.\n");
    assert!(!skill.disable_model_invocation);
}

#[test]
fn skill_markdown_rejects_invalid_names_and_missing_descriptions() {
    let invalid_name = parse_skill(
        "fallback",
        b"---\nname: Bad_Name\ndescription: Reviews changes.\n---\nReview.\n",
    )
    .unwrap_err();
    assert_eq!(invalid_name.kind(), ManifestErrorKind::InvalidSkill);
    assert_eq!(invalid_name.subject(), Some("Bad_Name"));
    assert_eq!(invalid_name.field(), Some("name"));

    let missing_description = parse_skill("code-review", b"Review changes.\n").unwrap_err();
    assert_eq!(missing_description.kind(), ManifestErrorKind::InvalidSkill);
    assert_eq!(missing_description.subject(), Some("code-review"));
    assert_eq!(missing_description.field(), Some("description"));
}
